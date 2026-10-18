from __future__ import annotations

from logging import getLogger
from itertools import count
from datetime import datetime, timedelta

from homeassistant.util import dt as dt_util
from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import *
from .common import *
from .device import Device
from .provider import ConfigurationProvider
from .push import PushHandler, async_register, async_unregister, decode_registers, frame_serial, parse_upstream

_LOGGER = getLogger(__name__)

class Coordinator(DataUpdateCoordinator[dict[str, tuple[int | float | str | list, int | float | None]]]):
    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry[Coordinator]):
        self.device = Device(ConfigurationProvider(hass, config_entry))
        self.push_updated: datetime | None = None
        self.push_frames: dict[str, str] = {}
        self._push_registered = False
        super().__init__(hass, _LOGGER, config_entry = config_entry, name = "", update_interval = TIMINGS_UPDATE_INTERVAL, always_update = False)

    @property
    def push_enabled(self):
        return self.device.config.push_mode != PUSH_MODE_DISABLED

    @property
    def push_available(self):
        return self.push_updated is not None and dt_util.utcnow() - self.push_updated < PUSH_TIMEOUT

    def _push_serial(self):
        return self.device.endpoint.serial or (self.device.modbus.serial if self.device.modbus else 0)

    @callback
    def _handle_push(self, frame: bytes):
        self.push_frames[f"0x{frame[4]:02X}"] = frame.hex()
        if frame[4] not in (self.device.profile.parser.push_codes if self.device.profile.parser else ()):
            return
        self.push_updated = dt_util.utcnow()
        if self.device.endpoint.serial == 0 and (serial := frame_serial(frame)):
            self.device.endpoint.serial = serial
        result = self.device.profile.parser.process_push(frame, decode_registers)
        _LOGGER.debug(f"[{self.device.endpoint.host}] Pushed data processed: {result}")
        self.data = (self.data or {}) | result
        self.async_update_listeners()

    async def _async_push_setup(self):
        if not self.push_enabled:
            return
        config = self.device.config
        await async_register(self.hass, config.push_port, self.config_entry.entry_id, PushHandler(config.name, config.host, config.push_mode, parse_upstream(config.push_upstream), self._push_serial, self._handle_push))
        self._push_registered = True

    @DataUpdateCoordinator.update_interval.setter
    def update_interval(self, value: timedelta | None):
        DataUpdateCoordinator.update_interval.fset(self, value)
        self.counter = self._update_interval_seconds

    @property
    def name(self):
        return self.device.config.name

    @name.setter
    def name(self, _: str):
        pass

    @property
    def counter(self):
        return self._counter_value

    @counter.setter
    def counter(self, value: int | float):
        self._counter = count(0, int(value))
        self._counter_value = next(self._counter)

    async def _async_setup(self):
        await super()._async_setup()
        try:
            await self.device.setup()
        except TimeoutError:
            raise
        except Exception as e:
            raise UpdateFailed(strepr(e)) from e

    async def _async_update_data(self):
        try:
            return await self.device.get(self.counter)
        except TimeoutError:
            raise
        except Exception as e:
            raise UpdateFailed(strepr(e)) from e

    def _async_refresh_finished(self):
        super()._async_refresh_finished()
        if self.data:
            self._counter_value = next(self._counter)
        elif not self.last_update_success:
            self.counter = self._update_interval_seconds

    async def init(self):
        await super().async_config_entry_first_refresh()
        serial_number, _ = self.data.get(slugify("device", "serial", "number", "sensor"), (str(self.device.modbus.serial) if self.device.modbus.serial > 0 else None, None))
        device_info = build_device_info(self.config_entry.entry_id, serial_number, self.device.endpoint.mac, self.device.endpoint.host, self.device.profile.info, self.device.config.name)
        self.device.info[self.config_entry.entry_id] = device_info
        if not self.push_enabled and self.device.profile.parser:
            self.device.profile.parser.disable_push()
        postprocess_descriptions(self)
        _LOGGER.debug(device_info)
        await self._async_push_setup()
        return self

    async def async_shutdown(self):
        await super().async_shutdown()
        if self._push_registered:
            self._push_registered = False
            await async_unregister(self.hass, self.device.config.push_port, self.config_entry.entry_id)
        try:
            await self.device.shutdown()
        except Exception as e:
            _LOGGER.exception(f"Unexpected error shutting down {self.name}")
