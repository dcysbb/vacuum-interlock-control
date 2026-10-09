from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import Optional


UNIT_TO_PA = {
    0: ("mbar", 100.0),
    1: ("Torr", 133.32236842105263),
    2: ("Pa", 1.0),
    3: ("micron", 0.13332236842105263),
    4: ("hPa", 100.0),
    5: ("V", None),
}


@dataclass(frozen=True)
class GaugeReading:
    channel: int
    status_code: int
    raw_value: float
    raw_unit: str
    pressure_pa: Optional[float]

    @property
    def valid(self) -> bool:
        return self.status_code == 0 and self.pressure_pa is not None and math.isfinite(self.pressure_pa)


class TPG366Error(RuntimeError):
    pass


class TPG366Client:
    """TPG 366 Mnemonics protocol over the Ethernet/Winsock port (normally 8000).

    A command receives ACK/NAK first; a following ENQ requests the value line.
    The TPG also emits readings while idle, so ACK parsing skips those lines.
    """

    def __init__(self, host: str, port: int = 8000, timeout_s: float = 0.35) -> None:
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._lock = asyncio.Lock()
        self._unit_code: Optional[int] = None

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
            raise TPG366Error(f"无法连接 TPG 366 {self.host}:{self.port}: {exc}") from exc

    async def close(self) -> None:
        writer, self._writer = self._writer, None
        self._reader = None
        self._unit_code = None
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=0.3)
            except (OSError, asyncio.TimeoutError):
                pass

    async def _read_line(self) -> str:
        if self._reader is None:
            raise TPG366Error("TPG 366 未连接")
        try:
            raw = await asyncio.wait_for(self._reader.readuntil(b"\n"), timeout=self.timeout_s)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError) as exc:
            await self.close()
            raise TPG366Error(f"TPG 366 响应超时或连接断开: {exc}") from exc
        return raw.decode("ascii", errors="replace").strip("\r\n \x00")

    async def _send(self, payload: bytes) -> None:
        if self._writer is None or self._writer.is_closing():
            await self.connect()
        assert self._writer is not None
        self._writer.write(payload)
        try:
            await asyncio.wait_for(self._writer.drain(), timeout=self.timeout_s)
        except (OSError, asyncio.TimeoutError) as exc:
            await self.close()
            raise TPG366Error(f"TPG 366 写入失败: {exc}") from exc

    async def _command_locked(self, mnemonic: str, value: Optional[str] = None) -> str:
        command = mnemonic if value is None else f"{mnemonic},{value}"
        await self._send(command.encode("ascii") + b"\r\n")
        # At power-on, an unsolicited value can be present before the ACK.
        for _ in range(12):
            reply = await self._read_line()
            if reply == "\x06":
                break
            if reply == "\x15":
                raise TPG366Error(f"TPG 366 拒绝命令 {mnemonic}")
        else:
            raise TPG366Error(f"TPG 366 未确认命令 {mnemonic}")
        await self._send(b"\x05")
        response = await self._read_line()
        if response in ("\x15", ""):
            raise TPG366Error(f"TPG 366 无法读取 {mnemonic}")
        return response

    async def read_unit(self) -> tuple[str, Optional[float]]:
        async with self._lock:
            response = await self._command_locked("UNI")
            try:
                code = int(response.split(",")[0].strip())
            except ValueError as exc:
                raise TPG366Error(f"无法解析 TPG 366 压力单位: {response}") from exc
            if code not in UNIT_TO_PA:
                raise TPG366Error(f"未知 TPG 366 压力单位代码: {code}")
            self._unit_code = code
            return UNIT_TO_PA[code]

    async def read_channels(self) -> list[GaugeReading]:
        async with self._lock:
            if self._unit_code is None:
                response = await self._command_locked("UNI")
                try:
                    self._unit_code = int(response.split(",")[0].strip())
                except ValueError as exc:
                    raise TPG366Error(f"无法解析 TPG 366 压力单位: {response}") from exc
                if self._unit_code not in UNIT_TO_PA:
                    raise TPG366Error(f"未知 TPG 366 压力单位代码: {self._unit_code}")
            unit, factor = UNIT_TO_PA[self._unit_code]
            if factor is None:
                raise TPG366Error("TPG 366 正在输出电压而非压力，禁止联锁")
            response = await self._command_locked("PRX")
            fields = [field.strip() for field in response.split(",")]
            if len(fields) != 12:
                raise TPG366Error(f"TPG 366 PRX 应有 12 个字段，实际 {len(fields)}: {response}")
            readings = []
            for index in range(6):
                try:
                    status = int(fields[index * 2])
                    raw = float(fields[index * 2 + 1])
                except ValueError as exc:
                    raise TPG366Error(f"无法解析 TPG 366 通道 {index + 1}: {response}") from exc
                pa = raw * factor if status in (0, 1) and math.isfinite(raw) else None
                readings.append(GaugeReading(index + 1, status, raw, unit, pa))
            return readings

    async def configure_setpoint(self, relay: int, channel: Optional[int], reset_pa: float, trip_pa: float) -> dict:
        if not 1 <= relay <= 6:
            raise ValueError("TPG 366 继电器编号必须为 1 至 6")
        if reset_pa < 0 or (channel is not None and trip_pa <= reset_pa):
            raise ValueError("关阀阈值必须高于恢复阈值")
        async with self._lock:
            if self._unit_code is None:
                unit_response = await self._command_locked("UNI")
                self._unit_code = int(unit_response.split(",")[0].strip())
            unit, factor = UNIT_TO_PA[self._unit_code]
            if factor is None:
                raise TPG366Error("TPG 366 压力单位必须是压力单位")
            assignment = 1 if channel is None else channel
            low_value, high_value = (0.0, 0.0) if channel is None else (reset_pa / factor, trip_pa / factor)
            await self._command_locked(
                f"SP{relay}",
                f"{assignment},{low_value:.7E},{high_value:.7E}",
            )
            fields = [part.strip() for part in (await self._command_locked(f"SP{relay}")).split(",")]
            if len(fields) != 3:
                raise TPG366Error(f"无法读回 TPG 366 继电器 {relay}: {fields}")
            actual_assignment, low, high = int(fields[0]), float(fields[1]), float(fields[2])
            expected_low, expected_high = low_value, high_value
            tolerance = max(abs(high_value) * 1e-5, 1e-12)
            if actual_assignment != assignment or abs(low - expected_low) > tolerance or abs(high - expected_high) > tolerance:
                raise TPG366Error(f"TPG 366 继电器 {relay} 读回不匹配: {fields}")
            return {
                "relay": relay,
                "channel": channel,
                "reset_pa": low * factor if channel is not None else None,
                "trip_pa": high * factor if channel is not None else None,
                "always_on": channel is None,
            }
