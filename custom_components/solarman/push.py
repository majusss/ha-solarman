from __future__ import annotations

import time
import struct
import asyncio

from logging import getLogger
from dataclasses import dataclass
from collections.abc import Callable

from homeassistant.core import HomeAssistant

from .const import *
from .common import *

_LOGGER = getLogger(__name__)

FRAME_START = 0xA5
FRAME_END = 0x15
FRAME_HEADER_SIZE = 11
FRAME_TRAILER_SIZE = 2
FRAME_MAX_PAYLOAD = 2048

# Control codes sent by the logger which expect an acknowledgement (HANDSHAKE, DATA, INFO, HEARTBEAT, REPORT)
ACKNOWLEDGED_CODES = (0x41, 0x42, 0x43, 0x47, 0x48)

def checksum(data: bytes):
    return sum(data) & 0xFF

def split_frames(buffer: bytearray):
    while True:
        if (start := buffer.find(FRAME_START)) < 0:
            buffer.clear()
            return
        if start:
            del buffer[:start]
        if len(buffer) < FRAME_HEADER_SIZE:
            return
        if (length := int.from_bytes(buffer[1:3], "little")) > FRAME_MAX_PAYLOAD:
            del buffer[0]
            continue
        if len(buffer) < (size := FRAME_HEADER_SIZE + length + FRAME_TRAILER_SIZE):
            return
        frame = bytes(buffer[:size])
        if frame[-1] != FRAME_END or frame[-2] != checksum(frame[1:-2]):
            del buffer[0]
            continue
        del buffer[:size]
        yield frame

def frame_serial(frame: bytes):
    return int.from_bytes(frame[7:11], "little")

def build_ack(frame: bytes, sequence: int):
    payload = bytes((frame[11] if len(frame) > FRAME_HEADER_SIZE + FRAME_TRAILER_SIZE else 0, 0x01)) + struct.pack("<II", int(time.time()), PUSH_ACK_INTERVAL)
    body = bytes((FRAME_START,)) + struct.pack("<H", len(payload)) + bytes((frame[3], (frame[4] - 0x30) & 0xFF, sequence & 0xFF, frame[6])) + frame[7:11] + payload
    return body + bytes((checksum(body[1:]), FRAME_END))

def decode_registers(frame: bytes, big_endian: list[list[int]] | None = None):
    data = frame[:-FRAME_TRAILER_SIZE]
    if len(data) % 2:
        data += b"\x00"
    ranges = big_endian or []
    return [int.from_bytes(data[o:o + 2], "big" if any(s <= o <= e for s, e in ranges) else "little") for o in range(0, len(data), 2)]

def parse_upstream(value: str):
    if not value:
        return None
    host, _, port = value.strip().rpartition(":")
    return (host, int(port)) if host and port.isdigit() else (value.strip(), DEFAULT_[CONF_PUSH_PORT])

@dataclass
class PushHandler:
    name: str
    host: str
    mode: str
    upstream: tuple[str, int] | None
    serial: Callable[[], int]
    callback: Callable[[bytes], None]

    def matches_serial(self, serial: int):
        return (s := self.serial()) > 0 and s == serial

    def matches_host(self, host: str):
        try:
            return str(getipaddress(self.host)) == host
        except Exception:
            return False

class PushConnection:
    def __init__(self, server: PushServer, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.server = server
        self.reader = reader
        self.writer = writer
        self.peer = (writer.get_extra_info("peername") or ("?", 0))[0]
        self.handler: PushHandler | None = None
        self.upstream: asyncio.StreamWriter | None = None
        self.upstream_task: asyncio.Task | None = None
        self.sequence = 0

    async def _open_upstream(self):
        try:
            reader, self.upstream = await asyncio.wait_for(asyncio.open_connection(*self.handler.upstream), PUSH_UPSTREAM_TIMEOUT)
            self.upstream_task = create_task(self._upstream_loop(reader))
            _LOGGER.debug(f"[{self.peer}] Proxying to {self.handler.upstream[0]}:{self.handler.upstream[1]}")
        except Exception as e:
            self.upstream = None
            _LOGGER.warning(f"[{self.peer}] Cannot connect to upstream {self.handler.upstream[0]}:{self.handler.upstream[1]}, acknowledging locally: {strepr(e)}")

    async def _upstream_loop(self, reader: asyncio.StreamReader):
        try:
            while data := await reader.read(1024):
                self.writer.write(data)
                await self.writer.drain()
        except Exception as e:
            _LOGGER.debug(f"[{self.peer}] Upstream error: {strepr(e)}")
        finally:
            _LOGGER.debug(f"[{self.peer}] Upstream connection closed")
            await self._close_upstream()

    async def _close_upstream(self):
        if (upstream := self.upstream) is not None:
            self.upstream = None
            try:
                upstream.close()
                await upstream.wait_closed()
            except Exception:
                pass

    async def _forward(self, data: bytes):
        try:
            self.upstream.write(data)
            await self.upstream.drain()
            return True
        except Exception as e:
            _LOGGER.debug(f"[{self.peer}] Forwarding failed: {strepr(e)}")
            await self._close_upstream()
            return False

    async def _acknowledge(self, frame: bytes):
        if frame[4] in ACKNOWLEDGED_CODES:
            self.sequence = (self.sequence + 1) & 0xFF
            self.writer.write(build_ack(frame, self.sequence))
            await self.writer.drain()

    async def run(self):
        _LOGGER.debug(f"[{self.peer}] Logger connected")
        buffer = bytearray()
        pending = bytearray()
        try:
            while data := await self.reader.read(1024):
                buffer += data
                pending += data
                for frame in split_frames(buffer):
                    _LOGGER.debug(f"[{self.peer}] PUSH RECV: {frame.hex(' ')}")
                    if self.handler is None:
                        if (handler := self.server.resolve(frame_serial(frame), self.peer)) is None:
                            _LOGGER.warning(f"[{self.peer}] Received data from unknown logger {frame_serial(frame)}, closing connection")
                            return
                        self.handler = handler
                        if handler.mode == PUSH_MODE_PROXY and handler.upstream:
                            await self._open_upstream()
                    try:
                        self.handler.callback(frame)
                    except Exception:
                        _LOGGER.exception(f"[{self.peer}] Error processing pushed frame")
                    if self.upstream is None:
                        await self._acknowledge(frame)
                if self.upstream is not None and pending:
                    if not await self._forward(bytes(pending)):
                        for frame in split_frames(bytearray(pending)):
                            await self._acknowledge(frame)
                pending.clear()
        except (ConnectionError, OSError) as e:
            _LOGGER.debug(f"[{self.peer}] Connection error: {strepr(e)}")
        finally:
            _LOGGER.debug(f"[{self.peer}] Logger disconnected")
            if self.upstream_task:
                self.upstream_task.cancel()
            await self._close_upstream()
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass

class PushServer:
    def __init__(self, port: int):
        self.port = port
        self.handlers: dict[str, PushHandler] = {}
        self._server: asyncio.Server | None = None
        self._connections: set[asyncio.Task] = set()

    def resolve(self, serial: int, host: str):
        handlers = list(self.handlers.values())
        return next((h for h in handlers if h.matches_serial(serial)), None) or next((h for h in handlers if h.matches_host(host)), None) or (handlers[0] if len(handlers) == 1 else None)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        task = asyncio.current_task()
        self._connections.add(task)
        try:
            await PushConnection(self, reader, writer).run()
        finally:
            self._connections.discard(task)

    async def start(self):
        self._server = await asyncio.start_server(self._client, IP_ANY, self.port)
        _LOGGER.info(f"Listening for logger data on port {self.port}")

    async def stop(self):
        if self._server:
            self._server.close()
            for task in list(self._connections):
                task.cancel()
            await self._server.wait_closed()
            self._server = None
            _LOGGER.info(f"Stopped listening for logger data on port {self.port}")

def _servers(hass: HomeAssistant) -> dict[int, PushServer]:
    return hass.data.setdefault(DOMAIN, {}).setdefault(CONF_PUSH, {})

async def async_register(hass: HomeAssistant, port: int, key: str, handler: PushHandler):
    servers = _servers(hass)
    if (server := servers.get(port)) is None:
        server = PushServer(port)
        await server.start()
        servers[port] = server
    server.handlers[key] = handler

async def async_unregister(hass: HomeAssistant, port: int, key: str):
    servers = _servers(hass)
    if (server := servers.get(port)) is not None:
        server.handlers.pop(key, None)
        if not server.handlers:
            del servers[port]
            await server.stop()
