from __future__ import annotations

import asyncio
import struct
from typing import Optional


class ModbusError(RuntimeError):
    pass


class Adam6050Client:
    """ADAM-6050 Modbus/TCP adapter (inputs 00001..00012, outputs 00017..00022)."""

    DI_START = 0
    DI_COUNT = 12
    DO_START = 16
    DO_COUNT = 6

    def __init__(self, host: str, port: int = 502, unit_id: int = 1, timeout_s: float = 0.4) -> None:
        self.host, self.port, self.unit_id, self.timeout_s = host, port, unit_id, timeout_s
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._transaction = 0
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        if self.connected:
            return
        await self.close()
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=self.timeout_s
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise ModbusError(f"无法连接 ADAM-6050 {self.host}:{self.port}: {exc}") from exc

    async def close(self) -> None:
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=0.3)
            except (OSError, asyncio.TimeoutError):
                pass

    async def _request(self, function: int, payload: bytes) -> bytes:
        if not self.connected:
            await self.connect()
        assert self._writer is not None and self._reader is not None
        self._transaction = (self._transaction + 1) & 0xFFFF
        tid = self._transaction
        pdu = bytes((function,)) + payload
        frame = struct.pack(">HHHB", tid, 0, len(pdu) + 1, self.unit_id) + pdu
        try:
            self._writer.write(frame)
            await asyncio.wait_for(self._writer.drain(), timeout=self.timeout_s)
            head = await asyncio.wait_for(self._reader.readexactly(7), timeout=self.timeout_s)
            response_tid, protocol, length, unit = struct.unpack(">HHHB", head)
            if response_tid != tid or protocol != 0 or unit != self.unit_id or length < 2 or length > 253:
                raise ModbusError("ADAM-6050 Modbus 报文头不匹配")
            body = await asyncio.wait_for(self._reader.readexactly(length - 1), timeout=self.timeout_s)
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
            await self.close()
            raise ModbusError(f"ADAM-6050 通信中断: {exc}") from exc
        if not body:
            raise ModbusError("ADAM-6050 返回空报文")
        if body[0] & 0x80:
            raise ModbusError(f"ADAM-6050 Modbus 异常码 {body[1] if len(body) > 1 else '?'}")
        if body[0] != function:
            raise ModbusError("ADAM-6050 Modbus 功能码不匹配")
        return body[1:]

    async def read_inputs(self) -> list[bool]:
        async with self._lock:
            data = await self._request(2, struct.pack(">HH", self.DI_START, self.DI_COUNT))
            # Byte count plus two data octets are needed to cover DI0..DI11.
            if len(data) != 3 or data[0] != 2:
                raise ModbusError(f"ADAM-6050 DI 响应长度错误: {data!r}")
            bits = int.from_bytes(data[1:], "little")
            return [bool(bits & (1 << index)) for index in range(self.DI_COUNT)]

    async def read_outputs(self) -> list[bool]:
        async with self._lock:
            data = await self._request(1, struct.pack(">HH", self.DO_START, self.DO_COUNT))
            if len(data) != 2 or data[0] != 1:
                raise ModbusError(f"ADAM-6050 DO 响应长度错误: {data!r}")
            bits = int.from_bytes(data[1:], "little")
            return [bool(bits & (1 << index)) for index in range(self.DO_COUNT)]

    async def write_output(self, channel: int, state: bool) -> None:
        if not 0 <= channel < self.DO_COUNT:
            raise ValueError("ADAM-6050 输出编号必须为 0 至 5")
        address = self.DO_START + channel
        value = 0xFF00 if state else 0x0000
        async with self._lock:
            data = await self._request(5, struct.pack(">HH", address, value))
            if data != struct.pack(">HH", address, value):
                raise ModbusError("ADAM-6050 输出读回确认不匹配")
