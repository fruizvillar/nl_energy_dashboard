# build

```bash
docker build -t p1serial/p1serial ./p1serial_docker
```

# compose

Run the full stack from the repository root:

```bash
cd .
docker compose up -d --build
```

# services

- `p1-reader` publishes raw telegram payloads into RabbitMQ
- `p1-ingestor` consumes the queue and writes all parsed DSMR fields into InfluxDB

# check

```bash
docker compose logs -f p1-reader p1-ingestor
```
