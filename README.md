# Customised version of smart-counter Dashboard

**Only tested so far with Dutch smart meters.**

The repository now ships a Docker Compose based ingestion pipeline:

- `p1-reader` reads DSMR/P1 telegrams from the serial device
- `rabbitmq` buffers the telegrams
- `p1-ingestor` parses and writes all available telegram fields into InfluxDB
- `influxdb` stores the measurements for Grafana

## Start the stack

```bash
cd .
docker compose up -d --build
```

If your smart meter is exposed on a different device path, override it before starting:

```bash
export P1_SERIAL_DEVICE=/dev/ttyUSB0
docker compose up -d --build
```

## Services

- RabbitMQ AMQP: `localhost:5672`
- RabbitMQ management UI: `http://localhost:15672`
- InfluxDB HTTP API: `http://localhost:8086`

Default credentials can be overridden with environment variables:

- `RABBITMQ_USERNAME`
- `RABBITMQ_PASSWORD`
- `INFLUX_USERNAME`
- `INFLUX_PASSWORD`
- `INFLUX_DATABASE`
- `INFLUX_MEASUREMENT`

## Notes

- The parser keeps the existing Grafana-facing field names such as `energy_t1`, `energy_t2`, `power_delivered_w`, `current_delivered`, and `gas`.
- Additional DSMR fields are now also ingested, including returned energy, total import/export power, power-failure counters, voltage quality counters, text-message fields, and gas meter metadata.
