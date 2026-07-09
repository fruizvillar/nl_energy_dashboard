#!/usr/bin/env python3
"""P1 serial publisher: reads DSMR P1 telegrams and publishes them to RabbitMQ."""
import json
import logging
import os
import re
import time
from datetime import datetime
from enum import Enum

import crcmod
import pika
import pytz
import serial

# ---------------------------------------------------------------------------
# Configuration (via environment variables with sensible defaults)
# ---------------------------------------------------------------------------
SERIAL_PORT = os.getenv('SERIAL_PORT', '/dev/ttyUSB0')
SERIAL_BAUD = int(os.getenv('SERIAL_BAUD', '115200'))
SERIAL_TIMEOUT = int(os.getenv('SERIAL_TIMEOUT', '20'))

RABBITMQ_HOST = os.getenv('RABBITMQ_HOST', 'localhost')
RABBITMQ_PORT = int(os.getenv('RABBITMQ_PORT', '5672'))
RABBITMQ_USER = os.getenv('RABBITMQ_USER', 'guest')
RABBITMQ_PASS = os.getenv('RABBITMQ_PASS', 'guest')
RABBITMQ_QUEUE = os.getenv('RABBITMQ_QUEUE', 'p1data')

IDB_MEASUREMENT = 'p1data'

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DRM4_DT_FMT = '%y%m%d%H%M%S'
INFLUX_DT_FMT = '%Y-%m-%dT%H:%M:%SZ'
DRM4_LINE_SEP = '\r\n'
TZ_DRM4 = pytz.timezone('Europe/Amsterdam')
TZ_INFLUX = pytz.utc

tel_id_re = re.compile(r'(\d+)-(\d+):(\d+)\.(\d+)\.(\d+)')
tel_values_re = re.compile(r'\(([^)]*)\)')
numeric_re = re.compile(r'^[\d.]+')

drm4_crc = crcmod.mkCrcFun(0x18005, rev=False)

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S')
Logger = logging.getLogger()


# ---------------------------------------------------------------------------
# DSMR P1 field definitions
# https://www.netbeheernederland.nl/_upload/Files/Slimme_meter_15_32ffe3cc38.pdf
# ---------------------------------------------------------------------------
class Drm4(Enum):
    VERSION = (1, 3, 0, 2, 8)
    TIMESTAMP_ELECTRICITY = (0, 0, 1, 0, 0)
    EQ_ID = (0, 0, 96, 1, 1)
    READ_DEL_T1_KWH = (1, 0, 1, 8, 1)
    READ_DEL_T2_KWH = (1, 0, 1, 8, 2)
    READ_RET_T1_KWH = (1, 0, 2, 8, 1)
    READ_RET_T2_KWH = (1, 0, 2, 8, 2)
    TARIFF_INDICATOR = (0, 0, 96, 14, 0)
    POWER_DEL_TOTAL_KW = (1, 0, 1, 7, 0)
    POWER_RET_TOTAL_KW = (1, 0, 2, 7, 0)
    POWER_FAIL_COUNT = (0, 0, 96, 7, 9)
    POWER_FAIL_LONG_COUNT = (0, 0, 96, 7, 21)
    POWER_FAIL_LOG = (1, 0, 99, 97, 0)
    VOLTAGE_SAGS_L1 = (1, 0, 32, 32, 0)
    VOLTAGE_SWELLS_L1 = (1, 0, 32, 36, 0)
    TEXT_MESSAGE_CODES = (0, 0, 96, 13, 1)
    TEXT_MESSAGE = (0, 0, 96, 13, 0)
    POWER_DEL_L1_KW = (1, 0, 21, 7, 0)
    POWER_RET_L1_KW = (1, 0, 22, 7, 0)
    CURRENT_L1_A = (1, 0, 31, 7, 0)
    GAS_DEVICE_TYPE = (0, 1, 24, 1, 0)
    GAS_EQ_ID = (0, 1, 96, 1, 0)
    GAS_T_VOLUME_M3 = (0, 1, 24, 2, 1)


def _first_numeric(values: list[str]) -> float | None:
    """Return the first numeric value extracted from a parenthesised group list."""
    for v in values:
        if m := numeric_re.match(v):
            return float(m.group())
    return None


def _parse_dt_to_utc(raw: str) -> datetime:
    dt_naive = datetime.strptime(str(int(float(raw))), DRM4_DT_FMT)
    return TZ_DRM4.localize(dt_naive).astimezone(TZ_INFLUX)


# ---------------------------------------------------------------------------
# Publisher
# ---------------------------------------------------------------------------
class P1Publisher:
    def __init__(self):
        self._serial = serial.Serial(
            port=SERIAL_PORT, baudrate=SERIAL_BAUD, timeout=SERIAL_TIMEOUT)
        self._channel = None
        self._connection = None
        self._connect_rabbitmq()

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
                logging.info('Connected to RabbitMQ at %s:%d', RABBITMQ_HOST, RABBITMQ_PORT)
                return
            except pika.exceptions.AMQPConnectionError as exc:
                logging.warning('RabbitMQ not ready (%s), retrying in 5 s …', exc)
                time.sleep(5)

    def _publish(self, payload: dict):
        body = json.dumps(payload, default=str)
        properties = pika.BasicProperties(
            delivery_mode=pika.DeliveryMode.Persistent)
        try:
            self._channel.basic_publish(
                exchange='',
                routing_key=RABBITMQ_QUEUE,
                body=body,
                properties=properties,
            )
        except pika.exceptions.AMQPError:
            logging.warning('Lost RabbitMQ connection, reconnecting …')
            self._connect_rabbitmq()
            self._channel.basic_publish(
                exchange='',
                routing_key=RABBITMQ_QUEUE,
                body=body,
                properties=properties,
            )

    # ------------------------------------------------------------------
    # Telegram parsing
    # ------------------------------------------------------------------
    def parse_telegram(self) -> dict | None:
        """Read one complete P1 telegram from the serial port and return its data."""
        t_content: list[str] = []
        telegram_info: dict = {}
        awaiting_start = True

        while True:
            line = self._serial.readline().decode('utf-8').strip()
            t_content.append(line)

            if not line:
                continue

            if awaiting_start:
                if line.startswith('/'):
                    awaiting_start = False
                else:
                    logging.debug('Ignored line while waiting for start char: "%s"', line)
                continue

            if line.startswith('!'):
                t_content.append('!')
                data = DRM4_LINE_SEP.join(t_content)
                crc_calc = hex(drm4_crc(data.encode('utf-8')))
                logging.info('End of telegram. CRC: %s%s', crc_calc, line)
                break

            if not (drm4_id := tel_id_re.search(line)):
                logging.debug('No OBIS id in line: "%s"', line)
                continue

            try:
                field = Drm4(tuple(int(x) for x in drm4_id.groups()))
            except ValueError:
                logging.debug('Unknown OBIS id %s – skipping.', drm4_id[0])
                continue

            values = tel_values_re.findall(line)  # all (...) groups on the line

            self._handle_field(field, values, telegram_info)

        return telegram_info or None

    def _handle_field(self, field: Drm4, values: list[str], info: dict):
        """Parse one field and store result in *info*."""
        match field:

            case Drm4.TIMESTAMP_ELECTRICITY:
                if values:
                    info['dt_electricity'] = _parse_dt_to_utc(values[0])

            # Meter readings – kWh
            case Drm4.READ_DEL_T1_KWH:
                if (v := _first_numeric(values)) is not None:
                    info['energy_t1'] = v
            case Drm4.READ_DEL_T2_KWH:
                if (v := _first_numeric(values)) is not None:
                    info['energy_t2'] = v
            case Drm4.READ_RET_T1_KWH:
                if (v := _first_numeric(values)) is not None:
                    info['energy_ret_t1'] = v
            case Drm4.READ_RET_T2_KWH:
                if (v := _first_numeric(values)) is not None:
                    info['energy_ret_t2'] = v

            # Tariff
            case Drm4.TARIFF_INDICATOR:
                if (v := _first_numeric(values)) is not None:
                    info['tariff_indicator'] = int(v)

            # Instantaneous power – converted to W
            case Drm4.POWER_DEL_TOTAL_KW:
                if (v := _first_numeric(values)) is not None:
                    info['power_del_total_w'] = round(1000 * v, 3)
            case Drm4.POWER_RET_TOTAL_KW:
                if (v := _first_numeric(values)) is not None:
                    info['power_ret_total_w'] = round(1000 * v, 3)
            case Drm4.POWER_DEL_L1_KW:
                if (v := _first_numeric(values)) is not None:
                    info['power_delivered_w'] = round(1000 * v, 3)
            case Drm4.POWER_RET_L1_KW:
                if (v := _first_numeric(values)) is not None:
                    info['power_ret_l1_w'] = round(1000 * v, 3)

            # Current
            case Drm4.CURRENT_L1_A:
                if (v := _first_numeric(values)) is not None:
                    info['current_delivered'] = v

            # Failure counters
            case Drm4.POWER_FAIL_COUNT:
                if (v := _first_numeric(values)) is not None:
                    info['power_fail_count'] = int(v)
            case Drm4.POWER_FAIL_LONG_COUNT:
                if (v := _first_numeric(values)) is not None:
                    info['power_fail_long_count'] = int(v)
            case Drm4.POWER_FAIL_LOG:
                # First value is the number of log entries
                if (v := _first_numeric(values)) is not None:
                    info['power_fail_log_count'] = int(v)

            # Voltage quality
            case Drm4.VOLTAGE_SAGS_L1:
                if (v := _first_numeric(values)) is not None:
                    info['voltage_sags_l1'] = int(v)
            case Drm4.VOLTAGE_SWELLS_L1:
                if (v := _first_numeric(values)) is not None:
                    info['voltage_swells_l1'] = int(v)

            # Gas: values = [timestamp_str, volume_str]
            case Drm4.GAS_T_VOLUME_M3:
                if len(values) >= 2:
                    gas_dt = _parse_dt_to_utc(values[0])
                    if (v := _first_numeric([values[1]])) is not None:
                        info['gas'] = v
                        info['gas_time'] = gas_dt.strftime(INFLUX_DT_FMT)

            # String / identifier fields (skip if empty)
            case Drm4.EQ_ID:
                if values and values[0]:
                    info['eq_id'] = values[0]
            case Drm4.GAS_EQ_ID:
                if values and values[0]:
                    info['gas_eq_id'] = values[0]
            case Drm4.GAS_DEVICE_TYPE:
                if (v := _first_numeric(values)) is not None:
                    info['gas_device_type'] = int(v)
            case Drm4.TEXT_MESSAGE_CODES:
                if values and values[0]:
                    info['text_msg_codes'] = values[0]
            case Drm4.TEXT_MESSAGE:
                if values and values[0]:
                    info['text_msg'] = values[0]

            # VERSION – informational only, not stored
            case Drm4.VERSION:
                pass

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def run(self):
        fields = self.parse_telegram()
        if not fields:
            logging.warning('Empty telegram, skipping.')
            return

        dt_electricity = fields.pop('dt_electricity', None)
        if not dt_electricity:
            logging.warning('Telegram has no electricity timestamp, skipping.')
            return

        tags = {'tariff': str(fields.pop('tariff_indicator', ''))}

        payload = {
            'measurement': IDB_MEASUREMENT,
            'time': dt_electricity.strftime(INFLUX_DT_FMT),
            'tags': tags,
            'fields': fields,
        }

        self._publish(payload)
        logging.info('Published: %s', payload)

    def loop(self):
        while True:
            try:
                self.run()
            except serial.SerialException as exc:
                logging.error('Serial error: %s – retrying in 5 s', exc)
                time.sleep(5)
            except Exception as exc:
                logging.exception('Unexpected error: %s', exc)
                time.sleep(1)


if __name__ == '__main__':
    P1Publisher().loop()
