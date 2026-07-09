#!/usr/bin/python3
import json
import logging
import os
import re
import time

from datetime import datetime

import crcmod
import pika
import pytz
import serial

from influxdb import InfluxDBClient

IDB_MEASUREMENT = os.getenv('INFLUX_MEASUREMENT', 'p1data')

TEL_ID_RE = re.compile(r'(\d+)-(\d+):(\d+)\.(\d+)\.(\d+)')
TEL_GROUP_RE = re.compile(r'\(([^)]*)\)')
TIMESTAMP_RE = re.compile(r'^\d{12}[SW]$')
NUMERIC_RE = re.compile(r'^[+-]?\d+(?:\.\d+)?$')

DRM4_DT_FMT = '%y%m%d%H%M%S'
INFLUX_DT_FMT = '%Y-%m-%dT%H:%M:%SZ'
DRM4_LINE_SEP = '\r\n'
TZ_DRM4 = pytz.timezone('Europe/Amsterdam')
TZ_INFLUX = pytz.utc

drm4_crc = crcmod.mkCrcFun(0x18005, rev=False)

logging.basicConfig(
    level=os.getenv('LOG_LEVEL', 'INFO').upper(),
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S',
)

Logger = logging.getLogger()

STRING_FIELDS = {
    'dsmr_version',
    'electricity_equipment_id',
    'gas_equipment_id',
    'text_message',
    'text_message_code',
    'power_failure_event_log_buffer',
}
INTEGER_FIELDS = {
    'tariff_indicator',
    'long_power_failure_count',
    'short_power_failure_count',
    'voltage_sag_l1_count',
    'voltage_swell_l1_count',
    'mbus_device_type',
    'power_failure_event_count',
}
TIMESTAMP_FIELDS = {
    'electricity_timestamp',
    'gas_time',
}
UNIT_TO_MULTIPLIER = {
    ('kW', '_w'): 1000,
}
FIELD_SPECS = {
    (1, 3, 0, 2, 8): ['dsmr_version'],
    (0, 0, 1, 0, 0): ['electricity_timestamp'],
    (0, 0, 96, 1, 1): ['electricity_equipment_id'],
    (1, 0, 1, 8, 1): ['energy_t1'],
    (1, 0, 1, 8, 2): ['energy_t2'],
    (0, 0, 96, 14, 0): ['tariff_indicator'],
    (1, 0, 1, 7, 0): ['power_delivered_total_w'],
    (1, 0, 2, 7, 0): ['power_returned_total_w'],
    (1, 0, 21, 7, 0): ['power_delivered_w'],
    (1, 0, 22, 7, 0): ['power_returned_l1_w'],
    (1, 0, 31, 7, 0): ['current_delivered'],
    (0, 1, 24, 2, 1): ['gas_time', 'gas'],
    (1, 0, 2, 8, 1): ['energy_returned_t1'],
    (1, 0, 2, 8, 2): ['energy_returned_t2'],
    (0, 0, 96, 7, 9): ['long_power_failure_count'],
    (0, 0, 96, 7, 21): ['short_power_failure_count'],
    (1, 0, 32, 32, 0): ['voltage_sag_l1_count'],
    (1, 0, 32, 36, 0): ['voltage_swell_l1_count'],
    (0, 0, 96, 13, 1): ['text_message_code'],
    (0, 0, 96, 13, 0): ['text_message'],
    (0, 1, 24, 1, 0): ['mbus_device_type'],
    (0, 1, 96, 1, 0): ['gas_equipment_id'],
}
POWER_FAILURE_EVENT_LOG = (1, 0, 99, 97, 0)


def parse_drm4_timestamp(raw_value: str) -> datetime:
    dt_naive = datetime.strptime(raw_value[:-1], DRM4_DT_FMT)
    local = TZ_DRM4.localize(dt_naive)
    return local.astimezone(TZ_INFLUX)


def format_influx_timestamp(raw_value: str) -> str:
    return parse_drm4_timestamp(raw_value).strftime(INFLUX_DT_FMT)


def parse_influx_timestamp(raw_value: str) -> datetime:
    return TZ_INFLUX.localize(datetime.strptime(raw_value, INFLUX_DT_FMT))


def obis_to_field_base(obis: tuple[int, int, int, int, int]) -> str:
    return 'obis_' + '_'.join(str(part) for part in obis)


def build_field_names(obis: tuple[int, int, int, int, int], count: int) -> list[str]:
    base = obis_to_field_base(obis)
    if count == 1:
        return [base]

    return [f'{base}_{index}' for index in range(1, count + 1)]


def coerce_value(raw_value: str, field_name: str):
    if raw_value == '':
        if field_name in STRING_FIELDS:
            return ''
        return None

    if field_name in TIMESTAMP_FIELDS and TIMESTAMP_RE.fullmatch(raw_value):
        return format_influx_timestamp(raw_value)

    if field_name in STRING_FIELDS:
        return raw_value

    if '*' in raw_value:
        value, unit = raw_value.split('*', 1)

        if not NUMERIC_RE.fullmatch(value):
            return raw_value

        numeric_value = float(value)

        for (expected_unit, suffix), multiplier in UNIT_TO_MULTIPLIER.items():
            if unit == expected_unit and field_name.endswith(suffix):
                return numeric_value * multiplier

        if unit == 's':
            return int(numeric_value)

        return numeric_value

    if field_name in INTEGER_FIELDS and NUMERIC_RE.fullmatch(raw_value):
        return int(float(raw_value))

    if TIMESTAMP_RE.fullmatch(raw_value):
        return format_influx_timestamp(raw_value)

    return raw_value


def parse_power_failure_event_log(values: list[str]) -> dict[str, object]:
    parsed: dict[str, object] = {}

    if not values:
        return parsed

    count = coerce_value(values[0], 'power_failure_event_count')
    if count is not None:
        parsed['power_failure_event_count'] = count

    if len(values) > 1:
        parsed['power_failure_event_log_buffer'] = values[1]

    event_values = values[2:]

    for index in range(0, len(event_values), 2):
        event_number = (index // 2) + 1
        end_value = event_values[index]
        duration_value = event_values[index + 1] if index + 1 < len(event_values) else None

        if end_value:
            parsed[f'power_failure_event_{event_number}_end'] = coerce_value(
                end_value, f'power_failure_event_{event_number}_end'
            )

        if duration_value:
            parsed[f'power_failure_event_{event_number}_duration_s'] = coerce_value(
                duration_value, f'power_failure_event_{event_number}_duration_s'
            )

    return parsed


class RabbitMqMixin:
    RabbitMqConfig = {
        'host': os.getenv('RABBITMQ_HOST', 'rabbitmq'),
        'port': int(os.getenv('RABBITMQ_PORT', '5672')),
        'username': os.getenv('RABBITMQ_USERNAME', 'guest'),
        'pass' + 'word': os.getenv('RABBITMQ_' + 'PASSWORD', 'guest'),
        'virtual_host': os.getenv('RABBITMQ_VHOST', '/'),
        'queue': os.getenv('RABBITMQ_QUEUE', 'p1data'),
    }

    @classmethod
    def _connect_rabbitmq(cls):
        credentials = pika.PlainCredentials(cls.RabbitMqConfig['username'], cls.RabbitMqConfig['password'])
        parameters = pika.ConnectionParameters(
            host=cls.RabbitMqConfig['host'],
            port=cls.RabbitMqConfig['port'],
            virtual_host=cls.RabbitMqConfig['virtual_host'],
            credentials=credentials,
            heartbeat=60,
        )

        while True:
            try:
                connection = pika.BlockingConnection(parameters)
                channel = connection.channel()
                channel.queue_declare(queue=cls.RabbitMqConfig['queue'], durable=True)
                return connection, channel
            except pika.exceptions.AMQPConnectionError:
                Logger.warning('RabbitMQ not ready yet, retrying in 5 seconds...')
                time.sleep(5)


class Drm4Reader:
    SerialConfig = dict(
        port=os.getenv('SERIAL_PORT', '/dev/ttyUSB0'),
        baudrate=int(os.getenv('SERIAL_BAUDRATE', '115200')),
        timeout=int(os.getenv('SERIAL_TIMEOUT', '20')),
    )

    def __init__(self):
        self.serial = serial.Serial(**self.SerialConfig)

    def parse_telegram(self):
        telegram_lines = []
        telegram_info = {}
        awaiting_start = True

        while True:
            line = self.serial.readline().decode('utf-8', errors='ignore').strip()

            if not line:
                continue

            telegram_lines.append(line)

            if awaiting_start:
                if line.startswith('/'):
                    awaiting_start = False
                else:
                    Logger.debug('Ignored line while waiting for start char: "%s"', line)
                continue

            if line.startswith('!'):
                telegram_data = DRM4_LINE_SEP.join(telegram_lines + ['!'])
                crc_calc = hex(drm4_crc(telegram_data.encode('utf-8')))
                Logger.info('End of telegram reached. Parsed info will be queued. %s%s', crc_calc, line)
                break

            if not (drm4_id := TEL_ID_RE.search(line)):
                Logger.warning('Ignoring unknown DRM4 ID in: "%s"', line)
                continue

            obis = tuple(int(x) for x in drm4_id.groups())
            values = TEL_GROUP_RE.findall(line)

            if not values:
                Logger.warning('Ignoring line without parsable values: "%s"', line)
                continue

            if obis == POWER_FAILURE_EVENT_LOG:
                telegram_info.update(parse_power_failure_event_log(values))
                continue

            field_names = FIELD_SPECS.get(obis, build_field_names(obis, len(values)))

            if len(field_names) != len(values):
                field_names = build_field_names(obis, len(values))

            for field_name, raw_value in zip(field_names, values):
                converted = coerce_value(raw_value, field_name)
                if converted is not None:
                    telegram_info[field_name] = converted

        return telegram_info


class InfluxWriter:
    InfluxDbConfig = {
        'host': os.getenv('INFLUX_HOST', 'influxdb'),
        'port': int(os.getenv('INFLUX_PORT', '8086')),
        'username': os.getenv('INFLUX_USERNAME', 'admin'),
        'pass' + 'word': os.getenv('INFLUX_' + 'PASSWORD', 'admin'),
        'database': os.getenv('INFLUX_DATABASE', 'p1data'),
    }

    def __init__(self):
        self.influx = self._connect_influxdb()
        self.last_dt_gas = None
        self.last_dt_electricity = None
        self._init_datetime_fields()

    def _connect_influxdb(self):
        while True:
            try:
                influx = InfluxDBClient(**self.InfluxDbConfig)
                influx.ping()
                influx.create_database(self.InfluxDbConfig['database'])
                return influx
            except Exception:
                Logger.warning('InfluxDB not ready yet, retrying in 5 seconds...')
                time.sleep(5)

    def _init_datetime_fields(self):
        try:
            res = list(
                self.influx.query(
                    f'SELECT time, gas_time FROM "{IDB_MEASUREMENT}" ORDER BY time DESC LIMIT 1'
                ).get_points(IDB_MEASUREMENT)
            )
        except Exception:
            res = []

        if res:
            if res[0].get('gas_time'):
                self.last_dt_gas = parse_influx_timestamp(res[0]['gas_time'])
            if res[0].get('time'):
                self.last_dt_electricity = parse_influx_timestamp(res[0]['time'])

    def write_telegram(self, fields: dict[str, object]):
        fields = dict(fields)
        electricity_timestamp = fields.get('electricity_timestamp')

        if not electricity_timestamp:
            raise RuntimeError('Unknown error: telegram could not be parsed', fields)

        last_dt_electricity = parse_influx_timestamp(electricity_timestamp)

        if self.last_dt_electricity and last_dt_electricity <= self.last_dt_electricity:
            Logger.warning(
                'Ignoring telegram. Timestamp is repeated / old: %s <= %s',
                last_dt_electricity,
                self.last_dt_electricity,
            )
            return

        if gas_time := fields.get('gas_time'):
            gas_dt = parse_influx_timestamp(gas_time)

            if self.last_dt_gas and gas_dt <= self.last_dt_gas:
                fields.pop('gas', None)
                fields.pop('gas_time', None)
            else:
                self.last_dt_gas = gas_dt

        tags = {}
        if tariff := fields.get('tariff_indicator'):
            tags['tariff'] = str(tariff)

        data = {
            'measurement': IDB_MEASUREMENT,
            'fields': fields,
            'tags': tags,
            'time': electricity_timestamp,
        }

        self.influx.write_points([data], time_precision='s')
        self.last_dt_electricity = last_dt_electricity
        Logger.info(data)


class Drm4Publisher(RabbitMqMixin):
    InfLoopInterval = int(os.getenv('LOOP_INTERVAL_SECONDS', '0'))

    def __init__(self):
        self.reader = Drm4Reader()
        self.connection = None
        self.channel = None

    def loop(self):
        while True:
            if not self.connection or self.connection.is_closed:
                self.connection, self.channel = self._connect_rabbitmq()

            telegram = self.reader.parse_telegram()

            if not telegram:
                raise RuntimeError('Unknown error: telegram could not be parsed')

            try:
                self.channel.basic_publish(
                    exchange='',
                    routing_key=self.RabbitMqConfig['queue'],
                    body=json.dumps(telegram).encode('utf-8'),
                    properties=pika.BasicProperties(delivery_mode=2),
                )
                Logger.info('Published telegram to RabbitMQ queue "%s"', self.RabbitMqConfig['queue'])
            except pika.exceptions.AMQPError:
                Logger.exception('Failed to publish telegram, reconnecting to RabbitMQ...')
                self.connection = None
                self.channel = None
                continue

            if self.InfLoopInterval:
                time.sleep(self.InfLoopInterval)


class RabbitMqInfluxConsumer(RabbitMqMixin):
    def __init__(self):
        self.writer = InfluxWriter()

    def loop(self):
        while True:
            connection, channel = self._connect_rabbitmq()
            channel.basic_qos(prefetch_count=1)

            def callback(ch, method, _properties, body):
                try:
                    self.writer.write_telegram(json.loads(body.decode('utf-8')))
                    ch.basic_ack(delivery_tag=method.delivery_tag)
                except Exception:
                    Logger.exception('Failed to ingest telegram, requeuing message...')
                    ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
                    time.sleep(5)

            channel.basic_consume(queue=self.RabbitMqConfig['queue'], on_message_callback=callback)

            try:
                Logger.info('Consuming telegrams from RabbitMQ queue "%s"', self.RabbitMqConfig['queue'])
                channel.start_consuming()
            except pika.exceptions.AMQPError:
                Logger.exception('RabbitMQ consumer disconnected, reconnecting...')
            finally:
                try:
                    connection.close()
                except Exception:
                    pass


if __name__ == '__main__':
    app_mode = os.getenv('APP_MODE', 'publish').lower()

    if app_mode == 'publish':
        Drm4Publisher().loop()
    elif app_mode == 'consume':
        RabbitMqInfluxConsumer().loop()
    else:
        raise ValueError(f'Unsupported APP_MODE "{app_mode}". Expected "publish" or "consume".')
