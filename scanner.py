#!/usr/bin/env python3
"""Starlink WiFi device tracker that publishes presence to Home Assistant via MQTT."""

import json
import logging
import signal
import sys
import time
from pathlib import Path

import grpc
import paho.mqtt.client as mqtt
import yaml
from yagrc.reflector import GrpcReflectionClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("starlink-tracker")

MQTT_DISCOVERY_PREFIX = "homeassistant"
MQTT_STATE_PREFIX = "starlink-tracker"
AVAILABILITY_TOPIC = f"{MQTT_STATE_PREFIX}/status"


def load_config(path: str = "/config/config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def build_discovery_payload(device_id: str, device_cfg: dict) -> dict:
    return {
        "name": None,
        "unique_id": f"starlink_{device_id}",
        "object_id": f"starlink_{device_id}",
        "state_topic": f"{MQTT_STATE_PREFIX}/{device_id}/state",
        "payload_home": "home",
        "payload_not_home": "not_home",
        "source_type": "router",
        "availability": [
            {"topic": AVAILABILITY_TOPIC, "payload_available": "online", "payload_not_available": "offline"}
        ],
        "device": {
            "identifiers": [f"starlink_tracker_{device_id}"],
            "name": device_cfg["name"],
            "manufacturer": "Starlink Device Tracker",
        },
    }


class StarlinkScanner:
    def __init__(self, router_address: str):
        self.router_address = router_address
        self._channel = None
        self._stub = None
        self._request_type = None
        self._response_type = None

    def _connect(self):
        self._channel = grpc.insecure_channel(self.router_address)
        ref_client = GrpcReflectionClient()
        ref_client.load_protocols(self._channel, symbols=["SpaceX.API.Device.Device"])
        self._request_type = ref_client.message_class("SpaceX.API.Device.Request")
        self._response_type = ref_client.message_class("SpaceX.API.Device.Response")
        self._stub = self._channel.unary_unary(
            "/SpaceX.API.Device.Device/Handle",
            request_serializer=self._request_type.SerializeToString,
            response_deserializer=self._response_type.FromString,
        )
        log.info("Connected to Starlink router at %s", self.router_address)

    def get_clients(self) -> list[dict] | None:
        """Query Starlink router for WiFi clients. Returns list of dicts or None on failure."""
        try:
            if self._stub is None:
                self._connect()

            req = self._request_type()
            req.wifi_get_clients.SetInParent()
            resp = self._stub(req, timeout=10)

            clients = []
            for client in resp.wifi_get_clients.clients:
                clients.append({
                    "name": client.name,
                    "mac": client.mac_address,
                    "ip": client.ip_address,
                    "client_id": client.client_id,
                    "role": int(client.role),
                })
            return clients

        except Exception:
            log.exception("Failed to query Starlink router")
            self._channel = None
            self._stub = None
            return None


class DeviceTracker:
    def __init__(self, config: dict):
        self.config = config
        self.mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="starlink-device-tracker", protocol=mqtt.MQTTv311)
        self.scanner = StarlinkScanner(config["starlink"]["router_address"])
        self.running = True

        self.devices = config.get("devices", {})
        # Map starlink_name → device_id for fast lookup
        self._name_map = {cfg["starlink_name"]: did for did, cfg in self.devices.items()}
        # Track consecutive misses per device
        self._miss_counts: dict[str, int] = {did: 0 for did in self.devices}
        # Current published state per device
        self._states: dict[str, str] = {did: "not_home" for did in self.devices}

        self._misses_for_not_home = config.get("scanner", {}).get("misses_for_not_home", 3)
        self._interval = config.get("scanner", {}).get("interval", 30)
        self._log_all = config.get("scanner", {}).get("log_all_clients", False)
        self._online = False

    def _on_mqtt_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            log.info("Connected to MQTT broker")
            self._publish_discovery()
            self._publish_availability("online")
            # Republish current states on reconnect
            for device_id, state in self._states.items():
                self._publish_state(device_id, state)
        else:
            log.error("MQTT connection failed with code %d", rc)

    def _on_mqtt_disconnect(self, client, userdata, flags, rc, properties=None):
        self._online = False
        if rc != 0:
            log.warning("Unexpected MQTT disconnect (rc=%d), will reconnect", rc)

    def _publish_discovery(self):
        for device_id, device_cfg in self.devices.items():
            topic = f"{MQTT_DISCOVERY_PREFIX}/device_tracker/starlink_{device_id}/config"
            payload = build_discovery_payload(device_id, device_cfg)
            self.mqtt_client.publish(topic, json.dumps(payload), retain=True)
            log.info("Published discovery for %s", device_id)

    def _publish_availability(self, state: str):
        self.mqtt_client.publish(AVAILABILITY_TOPIC, state, retain=True)
        self._online = state == "online"

    def _publish_state(self, device_id: str, state: str):
        topic = f"{MQTT_STATE_PREFIX}/{device_id}/state"
        self.mqtt_client.publish(topic, state, retain=True)

    def connect_mqtt(self):
        mqtt_cfg = self.config["mqtt"]
        self.mqtt_client.will_set(AVAILABILITY_TOPIC, "offline", retain=True)
        self.mqtt_client.on_connect = self._on_mqtt_connect
        self.mqtt_client.on_disconnect = self._on_mqtt_disconnect

        if mqtt_cfg.get("username"):
            self.mqtt_client.username_pw_set(mqtt_cfg["username"], mqtt_cfg.get("password", ""))

        log.info("Connecting to MQTT broker at %s:%s", mqtt_cfg["host"], mqtt_cfg["port"])
        self.mqtt_client.connect(mqtt_cfg["host"], int(mqtt_cfg["port"]), keepalive=60)
        self.mqtt_client.loop_start()

    def scan_once(self):
        clients = self.scanner.get_clients()

        if clients is None:
            # Scan failed — don't change device states, mark unavailable after repeated failures
            log.warning("Scan failed, not updating device states")
            self._publish_availability("offline")
            return

        if not self._online:
            self._publish_availability("online")

        if self._log_all:
            # role 1=CLIENT, 2=REPEATER, 3=CONTROLLER
            client_names = [c["name"] or f"<unnamed:{c['mac']}>" for c in clients if c["role"] == 1]
            log.info("Clients on network: %s", ", ".join(client_names) if client_names else "(none)")

        # Determine which tracked devices are present
        seen_names = {c["name"] for c in clients}
        for device_id, device_cfg in self.devices.items():
            starlink_name = device_cfg["starlink_name"]
            if starlink_name in seen_names:
                self._miss_counts[device_id] = 0
                if self._states[device_id] != "home":
                    log.info("%s (%s) is home", device_id, starlink_name)
                    self._states[device_id] = "home"
                    self._publish_state(device_id, "home")
            else:
                self._miss_counts[device_id] += 1
                if self._states[device_id] == "home" and self._miss_counts[device_id] >= self._misses_for_not_home:
                    log.info("%s (%s) is not_home (missed %d scans)", device_id, starlink_name, self._miss_counts[device_id])
                    self._states[device_id] = "not_home"
                    self._publish_state(device_id, "not_home")

    def run(self):
        self.connect_mqtt()
        log.info("Starting scan loop (interval=%ds, misses_for_not_home=%d)", self._interval, self._misses_for_not_home)
        while self.running:
            self.scan_once()
            time.sleep(self._interval)

    def stop(self):
        log.info("Shutting down")
        self.running = False
        self._publish_availability("offline")
        self.mqtt_client.disconnect()
        self.mqtt_client.loop_stop()


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "/config/config.yaml"
    config = load_config(config_path)

    tracker = DeviceTracker(config)

    def handle_signal(signum, frame):
        tracker.stop()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    tracker.run()


if __name__ == "__main__":
    main()
