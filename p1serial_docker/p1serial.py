#!/usr/bin/python3
import logging
import re
import time

from datetime import datetime
from enum import Enum
from logging.handlers import TimedRotatingFileHandler

import crcmod
import pytz
import serial

from influxdb import InfluxDBClient

# Create the InfluxDB client object
IDB_MEASUREMENT = "p1data"

tel_id_re = re.compile(r'(\d+)-(\d+):(\d+)\.(\d+)\.(\d+)')

tel_content_re_extra = re.compile(r'\(([\d.]*)[^)]*\)\(?([\d.]+)?')

DRM4_DT_FMT = '%y%m%d%H%M%S'
INFLUX_DT_FMT = '%Y-%m-%dT%H:%M:%SZ'
DRM4_LINE_SEP = '\r\n'
TZ_DRM4 = pytz.timezone("Europe/Amsterdam")
TZ_INFLUX = pytz.utc


drm4_crc = crcmod.mkCrcFun(0x18005, rev=False)  # FIXME: Find the way in which

logging.basicConfig(
    handlers=[TimedRotatingFileHandler('/home/pi/p1/log/main.log', when='D', backupCount=7)],
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt='%Y-%m-%dT%H:%M:%S')

Logger = logging.getLogger()


class Drm4(Enum):
    """ https://www.netbeheernederland.nl/_upload/Files/Slimme_meter_15_32ffe3cc38.pdf """
    VERSION = (1, 3, 0, 2, 8)  # unused

    TIMESTAMP_ELECTRICITY = (0, 0, 1, 0, 0)

    EQ_ID = (0, 0, 96, 1, 1)  # unused

    READ_DEL_T1_KWH = (1, 0, 1, 8, 1)
    READ_DEL_T2_KWH = (1, 0, 1, 8, 2)
    TARIFF_INDICATOR = (0, 0, 96, 14, 0)
    POWER_DEL_KW = (1, 0, 21, 7, 0)
    CURRENT_A = (1, 0, 31, 7, 0)
    GAS_T_VOLUME_M3 = (0, 1, 24, 2, 1)

    # These we receive but we don't actually use!
    UNUSED_01 = (1, 0, 2, 8, 1)
    UNUSED_02 = (1, 0, 2, 8, 2)
    UNUSED_03 = (1, 0, 1, 7, 0)
    UNUSED_04 = (1, 0, 2, 7, 0)
    UNUSED_05 = (0, 0, 96, 7, 9)
    UNUSED_06 = (0, 0, 96, 7, 21)
    UNUSED_07 = (1, 0, 99, 97, 0)
    UNUSED_08 = (1, 0, 32, 32, 0)
    UNUSED_09 = (1, 0, 32, 36, 0)
    UNUSED_10 = (0, 0, 96, 13, 1)
    UNUSED_11 = (0, 0, 96, 13, 0)
    UNUSED_12 = (1, 0, 22, 7, 0)
    UNUSED_13 = (0, 1, 24, 1, 0)
    UNUSED_14 = (0, 1, 96, 1, 0)


class Drm4ReaderUploader:
    SerialConfig = dict(port='/dev/ttyUSB0', baudrate=115200, timeout=20)
    InfluxDbConfig = dict(username='admin', password='admin', database='p1data')

    InfLoopInterval = 0

    def __init__(self):
        self.serial = serial.Serial(**self.SerialConfig)
        self.influx = InfluxDBClient(**self.InfluxDbConfig)

        self.last_dt_gas = None
        self.last_dt_electricity = None

        self._init_datetime_fields()

    def _init_datetime_fields(self):
        if res := list(self.influx.query('SELECT time, gas_time FROM p1 ORDER BY time DESC LIMIT 1').get_points('p1')):
            self.last_dt_gas = TZ_INFLUX.localize(datetime.strptime(res[0]['gas_time'], INFLUX_DT_FMT))

        if res := list(
                self.influx.query('SELECT time, power_delivered_w FROM p1 ORDER BY time DESC LIMIT 1').get_points(
                    'p1')):
            self.last_dt_electricity = TZ_INFLUX.localize(datetime.strptime(res[0]['time'], INFLUX_DT_FMT))

    def parse_telegram(self):
        t_content = []
        telegram_info = {}

        awaiting_start = True

        while True:
            line = self.serial.readline().decode('utf-8').strip()

            # Preparing CRC
            t_content.append(line)

            if not line:
                continue

            if awaiting_start:
                if line.startswith('/'):
                    awaiting_start = False
                else:
                    logging.debug(f'Ignored line while waiting for start char: "{line}"')
                continue

            if line.startswith('!'):
                t_content.append('!')
                data = DRM4_LINE_SEP.join(t_content)
                crc_calc = hex(drm4_crc(data.encode('utf-8')))
                logging.info(f'End of telegram reached. Sending info ... {crc_calc}{line}')
                break

            if not (drm4_id := tel_id_re.search(line)):
                logging.warning(f'Ignoring unknown DRM4 ID in: "{line}"')
                continue

            try:
                field = Drm4(tuple(int(x) for x in drm4_id.groups()))

            except ValueError:
                logging.warning(f'Ignoring non-implemented field {drm4_id[0]}. Line: "{line}".')
                continue

            if g_content := tel_content_re_extra.search(line):
                converted = [float(x) for x in g_content.groups('nan') if x]
                if len(converted) < 2:
                    converted = [float('nan'), converted[0]]  # The 1st match failed, we make it NaN

                value, extra = converted

            else:
                logging.warning(f'Read info field {field}. {line}.')
                continue

            match field:

                case Drm4.TIMESTAMP_ELECTRICITY:
                    dt = self._parse_dt_to_utc(value)
                    if self.last_dt_electricity and dt <= self.last_dt_electricity:
                        logging.warning(
                            f'Ignoring telegram. Timestamp is repeated /old: {dt} <= {self.last_dt_electricity}')
                        telegram_info = None
                        break

                    telegram_info['dt_electricity'] = dt

                case Drm4.READ_DEL_T1_KWH:
                    telegram_info['energy_t1'] = float(value)
                case Drm4.READ_DEL_T2_KWH:
                    telegram_info['energy_t2'] = float(value)

                case Drm4.TARIFF_INDICATOR:
                    telegram_info['tariff_indicator'] = int(value)

                case Drm4.POWER_DEL_KW:
                    telegram_info['power_delivered_w'] = 1000 * float(value)

                case Drm4.CURRENT_A:
                    telegram_info['current_delivered'] = float(value)

                case Drm4.GAS_T_VOLUME_M3:
                    dt = self._parse_dt_to_utc(value)

                    if self.last_dt_gas and dt <= self.last_dt_gas:
                        # Ignoring Gas info. Timestamp is repeated
                        continue

                    telegram_info['gas'] = float(extra)
                    telegram_info['gas_time'] = dt.strftime(INFLUX_DT_FMT)

        return telegram_info

    def loop(self):
        """ Runs `run` in a loop"""
        while True:
            self.run()
            if self.InfLoopInterval:
                time.sleep(self.InfLoopInterval)

    def run(self):
        fields = self.parse_telegram()

        if not fields:
            raise RuntimeError('Unknown error: datagram could not be parsed')

        if not (last_dt_electricity := fields.pop('dt_electricity', self.last_dt_electricity)):
            raise RuntimeError('Unknown error: datagram could not be parsed', fields)

        tags = dict(tariff=fields.pop('tariff_indicator', None))

        # Create the JSON data structure for InfluxDB
        data = {
            "measurement": IDB_MEASUREMENT,
            "fields": fields,
            "tags": tags,
            "time": last_dt_electricity
        }

        # Send the JSON data to InfluxDB
        self.influx.write_points([data], time_precision='s')
        logging.info(data)

    @staticmethod
    def _parse_dt_to_utc(dt_naive_f: str):
        dt_naive = datetime.strptime(str(int(dt_naive_f)), DRM4_DT_FMT)
        local = TZ_DRM4.localize(dt_naive)
        return local.astimezone(pytz.utc)


if __name__ == '__main__':
    Drm4ReaderUploader().loop()
