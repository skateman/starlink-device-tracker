# Starlink Device Tracker

Tracks WiFi devices connected to a Starlink router and publishes their presence to Home Assistant via MQTT autodiscovery.

## How it works

1. Queries the Starlink router's gRPC API for connected WiFi clients
2. Matches client names against configured devices
3. Publishes `home`/`not_home` states to MQTT
4. HA auto-discovers the device tracker entities via MQTT

## Setup

1. Copy the example config:
   ```sh
   cp config.yaml.example config.yaml
   ```

2. Edit `config.yaml` with your MQTT credentials and device mappings

3. Run:
   ```sh
   docker run -d \
     --name starlink-device-tracker \
     --restart unless-stopped \
     -v $(pwd)/config.yaml:/config/config.yaml:ro \
     ghcr.io/skateman/starlink-device-tracker
   ```

## Configuration

| Key | Description |
|-----|-------------|
| `mqtt.host` | MQTT broker address |
| `mqtt.port` | MQTT broker port (default: 1883) |
| `mqtt.username` | MQTT username (optional) |
| `mqtt.password` | MQTT password (optional) |
| `starlink.router_address` | Starlink router gRPC endpoint (default: `192.168.1.1:9000`) |
| `log_level` | Log verbosity: `debug`, `info`, `warning`, `error` (default: `info`) |
| `scanner.interval` | Scan interval in seconds (default: 30) |
| `scanner.misses_for_not_home` | Consecutive missed scans before marking `not_home` (default: 30) |
| `devices.<id>.name` | Friendly name shown in HA |
| `devices.<id>.starlink_name` | Device name as reported by Starlink router |

## Adding devices

Set `log_level: debug` in the config — the scanner logs all connected client names each cycle at debug level. Use those names in `starlink_name` to track new devices.
