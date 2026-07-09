#!/usr/bin/env python3
"""P1 consumer: reads P1 data from RabbitMQ and writes it to InfluxDB."""
import json
import logging
import os
import time

import pika
import pika.exceptions
from influxdb import InfluxDBClient

# ---------------------------------------------------------------------------
# Configuration (via environment variables with sensible defaults)
# ---------------------------------------------------------------------------
RABBITMQ_HOST = os.getenv('RABBITMQ_HOST', 'localhost')
RABBITMQ_PORT = int(os.getenv('RABBITMQ_PORT', '5672'))
RABBITMQ_USER = os.getenv('RABBITMQ_USER', 'guest')
RABBITMQ_PASS = os.getenv('RABBITMQ_PASS', 'guest')
RABBITMQ_QUEUE = os.getenv('RABBITMQ_QUEUE', 'p1data')

INFLUXDB_HOST = os.getenv('INFLUXDB_HOST', 'localhost')
INFLUXDB_PORT = int(os.getenv('INFLUXDB_PORT', '8086'))
INFLUXDB_USER = os.getenv('INFLUXDB_USER', 'admin')
INFLUXDB_PASS = os.getenv('INFLUXDB_PASS', 'admin')
INFLUXDB_DB = os.getenv('INFLUXDB_DB', 'p1data')

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S')


class P1Consumer:
    def __init__(self):
        self._influx = InfluxDBClient(
            host=INFLUXDB_HOST,
            port=INFLUXDB_PORT,
            username=INFLUXDB_USER,
            password=INFLUXDB_PASS,
            database=INFLUXDB_DB,
        )
        self._influx.create_database(INFLUXDB_DB)
        self._connection = None
        self._channel = None

    # ------------------------------------------------------------------
    # RabbitMQ helpers
    # ------------------------------------------------------------------
    def _connect_rabbitmq(self):
        credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASS)
        params = pika.ConnectionParameters(
            host=RABBITMQ_HOST,
            port=RABBITMQ_PORT,
            credentials=credentials,
            heartbeat=60,
            blocked_connection_timeout=30,
        )
        while True:
            try:
                self._connection = pika.BlockingConnection(params)
                self._channel = self._connection.channel()
                self._channel.queue_declare(queue=RABBITMQ_QUEUE, durable=True)
                self._channel.basic_qos(prefetch_count=1)
                self._channel.basic_consume(
                    queue=RABBITMQ_QUEUE,
                    on_message_callback=self._on_message,
                )
                logging.info(
                    'Connected to RabbitMQ at %s:%d, consuming queue "%s"',
                    RABBITMQ_HOST, RABBITMQ_PORT, RABBITMQ_QUEUE,
                )
                return
            except pika.exceptions.AMQPConnectionError as exc:
                logging.warning('RabbitMQ not ready (%s), retrying in 5 s ...', exc)
                time.sleep(5)

    # ------------------------------------------------------------------
    # Message handler
    # ------------------------------------------------------------------
    def _on_message(self, channel, method, _properties, body):
        try:
            payload = json.loads(body)
            point = {
                'measurement': payload['measurement'],
                'time': payload['time'],
                'tags': payload.get('tags', {}),
                'fields': payload['fields'],
            }
            self._influx.write_points([point], time_precision='s')
            logging.info('Ingested: %s', point)
            channel.basic_ack(delivery_tag=method.delivery_tag)
        except Exception as exc:
            logging.exception('Failed to ingest message: %s', exc)
            # Reject without requeue to avoid poison-pill loop
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def run(self):
        self._connect_rabbitmq()
        while True:
            try:
                self._channel.start_consuming()
            except pika.exceptions.AMQPConnectionError as exc:
                logging.warning('RabbitMQ connection lost (%s), reconnecting ...', exc)
                time.sleep(5)
                self._connect_rabbitmq()
            except KeyboardInterrupt:
                logging.info('Shutting down ...')
                self._channel.stop_consuming()
                self._connection.close()
                break


if __name__ == '__main__':
    P1Consumer().run()
