FROM ghcr.io/sparky8512/starlink-grpc-tools:latest

RUN pip install --no-cache-dir paho-mqtt PyYAML

COPY scanner.py /app/scanner.py

ENTRYPOINT ["python3", "/app/scanner.py"]
CMD ["/config/config.yaml"]
