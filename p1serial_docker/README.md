# build & run

All services (RabbitMQ, InfluxDB, Grafana, publisher and consumer) are managed via Docker Compose.

## Prerequisites

* Docker Engine 24+ with the Compose plugin
* A P1 smart meter connected via USB serial adapter at `/dev/ttyUSB0`
  (override with `SERIAL_PORT` env var if different)

## Start the stack

```bash
docker compose up -d --build
```

Service URLs after startup:

| Service | URL |
|---|---|
| Grafana dashboard | http://localhost:3000 (admin / admin) |
| RabbitMQ management | http://localhost:15672 (guest / guest) |
| InfluxDB API | http://localhost:8086 |

## Check logs

```bash
# P1 serial publisher
docker compose logs -f p1publisher

# P1 InfluxDB consumer
docker compose logs -f p1consumer
```

## Architecture

```
Smart meter
    │ serial (USB)
    ▼
p1publisher  ──► RabbitMQ (queue: p1data)  ──► p1consumer ──► InfluxDB ──► Grafana
```

* **p1publisher** – reads DSMR P1 telegrams from the serial port and publishes
  them as JSON messages to RabbitMQ.
* **p1consumer** – consumes messages from RabbitMQ and writes all fields to
  InfluxDB (measurement `p1data`).

## Environment variables

All services can be configured via environment variables in `docker-compose.yml`.

| Variable | Default | Description |
|---|---|---|
| `SERIAL_PORT` | `/dev/ttyUSB0` | Serial device path |
| `SERIAL_BAUD` | `115200` | Baud rate |
| `RABBITMQ_HOST` | `rabbitmq` | RabbitMQ hostname |
| `RABBITMQ_USER` | `guest` | RabbitMQ username |
| `RABBITMQ_PASS` | `guest` | RabbitMQ password |
| `RABBITMQ_QUEUE` | `p1data` | Queue name |
| `INFLUXDB_HOST` | `influxdb` | InfluxDB hostname |
| `INFLUXDB_DB` | `p1data` | InfluxDB database |
| `INFLUXDB_USER` | `admin` | InfluxDB username |
| `INFLUXDB_PASS` | `admin` | InfluxDB password |

