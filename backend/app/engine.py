from __future__ import annotations

import asyncio
import math
import random
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Optional

from .configuration import ConfigurationStore
from .drivers.adam6050 import Adam6050Client, ModbusError
from .drivers.tpg366 import GaugeReading, TPG366Client, TPG366Error
from .models import AppConfig, SampleChannel, SystemStatus
from .storage import HistoryStore


STATUS_LABELS = {
    0: "正常",
    1: "低于量程",
    2: "高于量程",
    3: "传感器故障",
    4: "测量通道已关闭",
    5: "未连接真空规",
    6: "真空规识别错误",
}
SIM_TRIP_PA = (1.0e-2, 1.0e-2, 1.0e-2)
SIM_RESET_PA = (5.0e-3, 5.0e-3, 5.0e-3)


class InterlockError(RuntimeError):
    pass


class InterlockEngine:
    """Serializes state transitions and keeps protective logic off the browser."""

    def __init__(self, config_store: ConfigurationStore, history: HistoryStore) -> None:
        self.config_store = config_store
        self.history = history
        self.config = config_store.get_config()
        self.mode = self.config.mode
        self.interlock = "disarmed"
        self.trip_reason: Optional[str] = None
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._subscribers: set[asyncio.Queue] = set()
        self.tpg: Optional[TPG366Client] = None
        self.adam: Optional[Adam6050Client] = None
        self._last_tpg_read: Optional[float] = None
        self._last_history_write = 0.0
        self._last_retention_check = 0.0
        self._recovery_since: Optional[float] = None
        self._valve_command: Optional[str] = None
        self._valve_command_at = 0.0
        self._open_output = False
        self._heartbeat_state = False
        self._last_heartbeat_at = 0.0
        self._last_io_read = 0.0
        self._next_unit_check = 0.0
        self._io_inputs = [False] * 12
        self._channels: list[dict] = []
        self._permit_ok = False
        self._tpg_connected = self.mode == "simulation"
        self._adam_connected = self.mode == "simulation"
        self._tpg_error: Optional[str] = None
        self._io_error: Optional[str] = None
        self._setpoints_synced = self.mode == "simulation"
        self._sim_pressure = [2.0e-3, 3.1e-3, 1.8e-3, 2.2e-3, 4.0e-3, 2.5e-3]
        self._sim_state = "closed"
        self._sim_open_after = 0.0
        self._sim_close_after = 0.0
        self._sim_sensor_conflict = False
        self._sim_permit = True
        self._sim_io_fault = False
        self._sim_gauge_status = [0] * 6
        self.status = SystemStatus(mode=self.mode)
        self._refresh_status()

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="vacuum-interlock-loop")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._close_devices()

    async def _close_devices(self) -> None:
        if self.tpg:
            await self.tpg.close()
        if self.adam:
            await self.adam.close()

    def _stamp(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _refresh_status(self) -> SystemStatus:
        now = time.monotonic()
        age = None if self._last_tpg_read is None else max(0.0, now - self._last_tpg_read)
        inputs = self._io_inputs
        open_di = self.config.valve_open_di
        closed_di = self.config.valve_closed_di
        open_sensor = self._input_active(open_di, self.config.valve_open_active_high) if open_di is not None and self._adam_connected else False
        closed_sensor = self._input_active(closed_di, self.config.valve_closed_active_high) if self._adam_connected else False
        if self.mode == "simulation":
            self._tpg_connected = True
            self._adam_connected = not self._sim_io_fault
            closed_sensor = self._sim_state == "closed" and self._adam_connected
            open_sensor = self._sim_state == "open" and self._adam_connected
            if self._sim_sensor_conflict:
                closed_sensor = open_sensor = self._adam_connected
            self._permit_ok = self._sim_permit
        if not self._adam_connected:
            valve = "unknown"
        elif open_sensor and closed_sensor:
            valve = "fault"
        elif closed_sensor:
            valve = "closed"
        elif open_di is not None and open_sensor:
            valve = "open"
        elif self._valve_command == "open" and now - self._valve_command_at < self.config.valve_timeout_s:
            valve = "opening"
        elif self._valve_command == "close" and now - self._valve_command_at < self.config.valve_timeout_s:
            valve = "closing"
        else:
            valve = "unknown"

        labels = {
            "disarmed": "未启用",
            "ready": "待命",
            "armed": "联锁已启用",
            "tripped": "联锁已触发",
            "fault": "故障停机",
        }
        channel_models = []
        for sample in self._channels:
            index = sample["channel"] - 1
            channel_config = self.config.channels[index]
            channel_models.append(
                SampleChannel(
                    **sample,
                    label=channel_config.label or f"通道 {index + 1}",
                    trip_pa=(channel_config.trip_pa if channel_config.enabled else
                             SIM_TRIP_PA[index] if self.mode == "simulation" and index < len(SIM_TRIP_PA) else None),
                    reset_pa=(channel_config.reset_pa if channel_config.enabled else
                              SIM_RESET_PA[index] if self.mode == "simulation" and index < len(SIM_RESET_PA) else None),
                )
            )
        self.status = SystemStatus(
            timestamp=self._stamp(),
            mode=self.mode,
            interlock=self.interlock,
            interlock_label=labels.get(self.interlock, self.interlock),
            trip_reason=self.trip_reason,
            valve=valve,
            valve_open_sensor=open_sensor,
            valve_closed_sensor=closed_sensor,
            tpg_connected=self._tpg_connected,
            adam_connected=self._adam_connected,
            sample_age_s=age,
            unit="Pa",
            permit_ok=self._permit_ok and (self.mode == "simulation" or self._setpoints_synced),
            fail_closed_confirmed=self.config.fail_closed_confirmed,
            history_recording=True,
            channels=channel_models,
        )
        return self.status

    def snapshot(self) -> dict:
        self._refresh_status()
        return self.status.model_dump(mode="json")

    async def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self._subscribers.add(queue)
        queue.put_nowait(self.snapshot())
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def _publish(self) -> None:
        payload = self.snapshot()
        for queue in tuple(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    def _event(self, kind: str, severity: str, message: str, actor: str = "system", details: Optional[dict] = None) -> dict:
        return self.history.write_event(kind, severity, message, actor, details)

    def _validate_arm(self) -> None:
        if self.interlock == "tripped":
            raise InterlockError("联锁已触发，须在压力恢复并保持后人工复位")
        if self.mode == "real":
            if not self.config.fail_closed_confirmed:
                raise InterlockError("尚未确认阀门断电关闭和硬件许可回路，真实联锁不能启用")
            if not self.config.tpg_host or not self.config.adam_host:
                raise InterlockError("请先设置 TPG 366 与 ADAM-6050 的 IP 地址")
            selected = [ch for ch in self.config.channels if ch.enabled]
            if not selected:
                raise InterlockError("至少选择一个真空保护通道")
            if not self._tpg_connected or not self._adam_connected:
                raise InterlockError("TPG 366 和 ADAM-6050 都必须在线")
            if not self._setpoints_synced:
                raise InterlockError("TPG 366 继电器阈值尚未读回验证")
            if self._tpg_error or self._io_error:
                raise InterlockError("设备报告故障，联锁不能启用")
            if self.status.valve != "closed":
                raise InterlockError("先关闭阀门并确认关闭位置传感器动作")
        if self._last_tpg_read is None or time.monotonic() - self._last_tpg_read > self.config.stale_timeout_s:
            raise InterlockError("真空数据尚未更新或已超时")
        bad = self._pressure_fault()
        if bad:
            raise InterlockError(bad)
        if not self._permit_ok:
            raise InterlockError("硬件压力许可输入未就绪")

    def _pressure_fault(self) -> Optional[str]:
        if self.mode == "simulation":
            selected = [(index, SIM_TRIP_PA[index]) for index in range(len(SIM_TRIP_PA))]
        else:
            selected = [(index, cfg.trip_pa) for index, cfg in enumerate(self.config.channels) if cfg.enabled]
        if not selected:
            return "尚未配置受保护的真空通道"
        by_channel = {s["channel"]: s for s in self._channels}
        for index, trip_pa in selected:
            sample = by_channel.get(index + 1)
            if sample is None or not sample.get("valid"):
                status = "无数据" if sample is None else sample.get("status", "测量错误")
                return f"通道 {index + 1} 测量异常：{status}"
            pressure = sample["pressure_pa"]
            if pressure is None or not math.isfinite(pressure):
                return f"通道 {index + 1} 压力读数无效"
            if pressure >= trip_pa:
                return f"通道 {index + 1} 压力达到关阀阈值"
        return None

    def _reset_conditions(self) -> Optional[str]:
        if self._last_tpg_read is None or time.monotonic() - self._last_tpg_read > self.config.stale_timeout_s:
            return "真空读数已超时，不能复位联锁"
        if self.mode == "real" and (not self._tpg_connected or not self._adam_connected):
            return "TPG 366 与 ADAM-6050 必须在线才能复位"
        fault = self._pressure_fault()
        if fault:
            return fault
        by_channel = {s["channel"]: s for s in self._channels}
        if self.mode == "simulation":
            selected = [(index, SIM_RESET_PA[index]) for index in range(len(SIM_RESET_PA))]
        else:
            selected = [(index, cfg.reset_pa) for index, cfg in enumerate(self.config.channels) if cfg.enabled]
        for index, reset_pa in selected:
            sample = by_channel.get(index + 1)
            if sample is None or sample.get("pressure_pa") is None:
                return f"通道 {index + 1} 没有有效读数"
            if sample["pressure_pa"] >= reset_pa:
                return f"通道 {index + 1} 压力尚未降到恢复阈值以下"
        if not self._permit_ok:
            return "硬件压力许可或心跳监控仍未就绪"
        return None

    async def _write_output(self, channel: int, value: bool) -> None:
        if self.mode == "simulation":
            if channel == self.config.open_output:
                self._open_output = value
                now = time.monotonic()
                if value:
                    self._sim_close_after = 0.0
                    self._sim_open_after = now + 0.45
                else:
                    self._sim_open_after = 0.0
                    self._sim_close_after = now + 0.45
            return
        if not self.adam:
            raise ModbusError("ADAM-6050 驱动未初始化")
        await self.adam.write_output(channel, value)

    async def _safe_open_output(self) -> None:
        if self.mode == "simulation":
            self._open_output = False
            self._sim_open_after = 0.0
            self._sim_close_after = time.monotonic() + 0.45
            return
        if self.adam and self.adam.connected and self.config.fail_closed_confirmed:
            try:
                await self.adam.write_output(self.config.open_output, False)
                self._open_output = False
            except ModbusError as exc:
                self._io_error = str(exc)
                self._event("io_error", "critical", "撤销开阀输出失败；硬件看门狗须独立关闭阀门", details={"error": str(exc)})

    async def trip(self, reason: str, kind: str = "pressure_trip", fault: bool = False) -> None:
        async with self._lock:
            already = self.interlock in ("tripped", "fault")
            self.interlock = "fault" if fault else "tripped"
            self.trip_reason = reason
            self._recovery_since = None
            if not already or self._valve_command != "close":
                self._valve_command = "close"
                self._valve_command_at = time.monotonic()
            await self._safe_open_output()
            if not already:
                snapshot = [
                    {"channel": s["channel"], "pressure_pa": s.get("pressure_pa"), "status": s.get("status")}
                    for s in self._channels
                ]
                self._event(kind, "critical", reason, details={"channels": snapshot})
            self._refresh_status()
            self._publish()

    async def disarm(self, actor: str = "operator") -> None:
        if self.interlock == "armed" or self.interlock == "ready":
            self.interlock = "disarmed"
            self.trip_reason = None
            self._recovery_since = None
            self._valve_command = "close"
            self._valve_command_at = time.monotonic()
            self._event("interlock_disarm", "warning", "操作员关闭联锁；阀门保持关闭", actor)
        await self._safe_open_output()
        self._refresh_status()
        self._publish()

    async def arm(self, actor: str = "operator") -> None:
        self._refresh_status()
        self._validate_arm()
        if self.interlock not in ("disarmed", "ready"):
            raise InterlockError("联锁当前状态不允许启用")
        try:
            await self._write_output(self.config.open_output, False)
            if self.mode == "real":
                await self._exercise_watchdog()
                await self._write_output(self.config.reset_output, True)
                await asyncio.sleep(0.25)
                await self._write_output(self.config.reset_output, False)
                await self._poll_io()
                if not self._latch_ready():
                    raise InterlockError("硬件联锁复位后未检测到许可锁存反馈")
        except (ModbusError, InterlockError) as exc:
            await self._safe_open_output()
            self.interlock = "fault"
            self.trip_reason = str(exc)
            self._event("arm_failed", "critical", f"联锁启用失败：{exc}", actor)
            raise InterlockError(str(exc)) from exc
        self.interlock = "armed"
        self.trip_reason = None
        self._event("interlock_armed", "info", "分子泵真空保护联锁已启用", actor)
        self._refresh_status()
        self._publish()

    async def reset(self, actor: str = "operator") -> None:
        if self.interlock not in ("tripped", "fault"):
            raise InterlockError("当前没有待复位的联锁故障")
        recovery_fault = self._reset_conditions()
        if recovery_fault:
            raise InterlockError(recovery_fault)
        self._refresh_status()
        if self.status.valve != "closed":
            raise InterlockError("复位前必须确认阀门已关闭")
        if self._recovery_since is None:
            raise InterlockError("等待真空恢复状态稳定")
        remaining = self.config.recovery_hold_s - (time.monotonic() - self._recovery_since)
        if remaining > 0:
            raise InterlockError(f"真空恢复稳定时间不足，还需 {remaining:.1f} 秒")
        if self.mode == "real":
            if not self.adam or not self.config.fail_closed_confirmed:
                raise InterlockError("真实硬件没有就绪")
            await self._write_output(self.config.open_output, False)
            await self._exercise_watchdog()
            await self._write_output(self.config.reset_output, True)
            await asyncio.sleep(0.25)
            await self._write_output(self.config.reset_output, False)
            await self._poll_io()
            if not self._latch_ready():
                raise InterlockError("硬件联锁没有确认复位")
        self.interlock = "armed"
        self.trip_reason = None
        self._valve_command = "close"
        self._valve_command_at = time.monotonic()
        self._event("interlock_reset", "warning", "操作员复位联锁；阀门仍保持关闭", actor)
        self._refresh_status()
        self._publish()

    def _latch_ready(self) -> bool:
        if self.mode == "simulation":
            return self._sim_permit and not self._sim_io_fault
        return bool(self._di(self.config.latch_ready_di))

    async def open_valve(self, actor: str = "operator") -> None:
        self._refresh_status()
        if self.interlock != "armed":
            raise InterlockError("必须先启用真空联锁")
        fault = self._pressure_fault()
        if fault:
            await self.trip(fault)
            raise InterlockError(fault)
        if not self._permit_ok or not self._latch_ready():
            raise InterlockError("硬件许可或锁存状态未就绪")
        if self.status.valve != "closed":
            raise InterlockError("只有确认阀门关闭后才能发送开阀命令")
        await self._write_output(self.config.open_output, True)
        self._open_output = True
        self._valve_command = "open"
        self._valve_command_at = time.monotonic()
        self._event("valve_open_command", "warning", "操作员请求开阀", actor)
        self._refresh_status()
        self._publish()

    async def close_valve(self, actor: str = "operator", reason: str = "操作员请求关阀") -> None:
        await self._safe_open_output()
        self._valve_command = "close"
        self._valve_command_at = time.monotonic()
        self._event("valve_close_command", "info", reason, actor)
        self._refresh_status()
        self._publish()

    async def apply_config(self, config: AppConfig, actor: str = "operator") -> None:
        async with self._lock:
            if self.interlock == "armed":
                self.interlock = "tripped"
                self.trip_reason = "设备配置已修改，联锁保持锁存关闭"
                await self._safe_open_output()
                self._event("config_change_trip", "critical", self.trip_reason, actor)
            else:
                await self._safe_open_output()
            old = self.config
            self.config = config
            self.mode = config.mode
            self.config_store.update(config)
            self._setpoints_synced = False
            if old.mode != config.mode or old.tpg_host != config.tpg_host or old.tpg_port != config.tpg_port:
                if self.tpg:
                    await self.tpg.close()
                self.tpg = None
                self._tpg_connected = False
                self._last_tpg_read = None
            if old.adam_host != config.adam_host or old.adam_port != config.adam_port:
                if self.adam:
                    await self.adam.close()
                self.adam = None
                self._adam_connected = False
            if config.mode == "simulation":
                self._tpg_connected = True
                self._adam_connected = not self._sim_io_fault
                self._permit_ok = self._sim_permit
            else:
                self._tpg_connected = False
                self._adam_connected = False
                self._permit_ok = False
            self._event(
                "config_changed", "warning", "设备或联锁配置已保存",
                actor,
                {"previous_mode": old.mode, "mode": config.mode, "version": self.config_store.version},
            )
        if config.mode == "real":
            await self.connect_devices()
        self._refresh_status()
        self._publish()

    async def connect_devices(self) -> dict:
        if self.mode != "real":
            return {"ok": True, "mode": self.mode}
        results = {}
        if self.config.tpg_host:
            if self.tpg is None or self.tpg.host != self.config.tpg_host or self.tpg.port != self.config.tpg_port:
                if self.tpg:
                    await self.tpg.close()
                self.tpg = TPG366Client(self.config.tpg_host, self.config.tpg_port)
            try:
                await self.tpg.connect()
                await self.tpg.read_unit()
                self._tpg_connected = True
                self._tpg_error = None
                results["tpg"] = {"ok": True}
            except TPG366Error as exc:
                self._tpg_connected = False
                self._tpg_error = str(exc)
                results["tpg"] = {"ok": False, "error": str(exc)}
        else:
            results["tpg"] = {"ok": False, "error": "未配置 IP"}
        if self.config.adam_host:
            if self.adam is None or self.adam.host != self.config.adam_host or self.adam.port != self.config.adam_port:
                if self.adam:
                    await self.adam.close()
                self.adam = Adam6050Client(
                    self.config.adam_host, self.config.adam_port, self.config.adam_unit_id
                )
            try:
                await self._poll_io()
                self._adam_connected = True
                self._io_error = None
                results["adam"] = {"ok": True}
            except ModbusError as exc:
                self._adam_connected = False
                self._io_error = str(exc)
                results["adam"] = {"ok": False, "error": str(exc)}
        else:
            results["adam"] = {"ok": False, "error": "未配置 IP"}
        self._refresh_status()
        self._publish()
        return {"ok": all(item.get("ok") for item in results.values()), **results}

    async def sync_setpoints(self, actor: str = "operator") -> list[dict]:
        if self.mode != "real" or not self.tpg or not self._tpg_connected:
            raise InterlockError("TPG 366 尚未连接")
        if self.interlock == "armed":
            await self.trip("正在修改 TPG 继电器设置")
        settings = []
        for relay, channel in enumerate(self.config.channels, start=1):
            if channel.enabled:
                if channel.trip_pa is None or channel.reset_pa is None:
                    raise InterlockError(f"通道 {relay} 阈值未配置")
                settings.append(
                    await self.tpg.configure_setpoint(relay, relay, channel.reset_pa, channel.trip_pa)
                )
            else:
                # Inactive physical relay slots remain permissive; active channels
                # retain a real low/high hysteresis in the dedicated TPG hardware.
                settings.append(await self.tpg.configure_setpoint(relay, None, 0.0, 1.0e12))
        self._setpoints_synced = True
        self._event("tpg_setpoints_synced", "warning", "TPG 366 六路继电器阈值写入并读回验证", actor, {"settings": settings})
        self._publish()
        return settings

    async def inject_pressure(self, channel: int, pressure_pa: float) -> None:
        if self.mode != "simulation":
            raise InterlockError("只能在模拟模式注入压力")
        self._sim_pressure[channel - 1] = pressure_pa

    async def inject_io_fault(self, fault: bool) -> None:
        if self.mode != "simulation":
            raise InterlockError("只能在模拟模式注入 I/O 故障")
        self._sim_io_fault = fault
        self._adam_connected = not fault

    async def _exercise_watchdog(self) -> None:
        # This software heartbeat is observed by a separate retriggerable
        # hardware watchdog; ADAM's own communications FSV is also configured
        # at commissioning. Neither behavior is inferred from this API write.
        if self.mode == "simulation":
            return
        assert self.adam is not None
        for state in (True, False, True):
            await self.adam.write_output(self.config.heartbeat_output, state)
            self._heartbeat_state = state
            self._last_heartbeat_at = time.monotonic()
            await asyncio.sleep(0.25)
        await self._poll_io()
        if not self._input_active(self.config.watchdog_ok_di, self.config.watchdog_ok_active_high):
            raise InterlockError("独立硬件看门狗反馈尚未就绪")
        self._refresh_status()
        self._publish()

    def _di(self, channel: Optional[int]) -> bool:
        if channel is None or channel < 0 or channel >= len(self._io_inputs):
            return False
        return bool(self._io_inputs[channel])

    def _input_active(self, channel: Optional[int], active_high: bool) -> bool:
        raw = self._di(channel)
        return raw if active_high else not raw

    async def _poll_io(self) -> None:
        if self.mode == "simulation":
            self._adam_connected = not self._sim_io_fault
            self._permit_ok = self._sim_permit and not self._sim_io_fault
            self._last_io_read = time.monotonic()
            return
        if not self.adam:
            return
        inputs = await self.adam.read_inputs()
        outputs = await self.adam.read_outputs()
        self._io_inputs = inputs
        self._adam_connected = True
        self._io_error = None
        self._permit_ok = (
            self._input_active(self.config.permit_ok_di, self.config.permit_ok_active_high)
            and self._input_active(self.config.watchdog_ok_di, self.config.watchdog_ok_active_high)
        )
        if self.config.air_ok_di is not None:
            self._permit_ok = self._permit_ok and self._input_active(
                self.config.air_ok_di, self.config.air_ok_active_high
            )
        self._last_io_read = time.monotonic()
        if self.interlock == "armed" and not outputs[self.config.open_output] and self._open_output:
            self._open_output = False

    async def _poll_tpg(self) -> None:
        if self.mode == "simulation":
            now = time.monotonic()
            if self._sim_open_after and now >= self._sim_open_after:
                self._sim_state = "open"
                self._sim_open_after = 0.0
            if self._sim_close_after and now >= self._sim_close_after:
                self._sim_state = "closed"
                self._sim_close_after = 0.0
            # Gentle drift keeps the trace alive but stays under the example limits.
            for index, pressure in enumerate(self._sim_pressure):
                if self._sim_gauge_status[index] == 0:
                    self._sim_pressure[index] = max(1e-12, pressure * (1 + random.uniform(-0.006, 0.006)))
            self._channels = [
                {
                    "channel": i + 1,
                    "pressure_pa": self._sim_pressure[i],
                    "raw_value": self._sim_pressure[i],
                    "raw_unit": "Pa",
                    "status_code": self._sim_gauge_status[i],
                    "status": STATUS_LABELS.get(self._sim_gauge_status[i], "未知状态"),
                    "valid": self._sim_gauge_status[i] == 0,
                }
                for i in range(6)
            ]
            self._tpg_connected = True
            self._last_tpg_read = now
            return
        if not self.tpg:
            return
        readings = await self.tpg.read_channels()
        self._channels = [self._reading_dict(item) for item in readings]
        self._last_tpg_read = time.monotonic()
        self._tpg_connected = True
        self._tpg_error = None

    @staticmethod
    def _reading_dict(reading: GaugeReading) -> dict:
        return {
            "channel": reading.channel,
            "pressure_pa": reading.pressure_pa,
            "raw_value": reading.raw_value,
            "raw_unit": reading.raw_unit,
            "status_code": reading.status_code,
            "status": STATUS_LABELS.get(reading.status_code, f"未知状态 {reading.status_code}"),
            "valid": reading.valid,
        }

    async def _record_sample(self) -> None:
        timestamp = self._stamp()
        try:
            self.history.write_sample(timestamp, self._channels, self.mode, self.config_store.version)
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            self._event("history_write_error", "error", "真空数据无法写入历史数据库", details={"error": str(exc)})

    async def _run(self) -> None:
        if self.mode == "real":
            await self.connect_devices()
        while True:
            tick = time.monotonic()
            try:
                await self._poll_tpg()
                if self.mode == "real" and tick - self._last_io_read >= 0.2:
                    await self._poll_io()
                if self.mode == "real" and self.interlock == "armed" and tick - self._last_heartbeat_at >= 0.5:
                    self._heartbeat_state = not self._heartbeat_state
                    await self._write_output(self.config.heartbeat_output, self._heartbeat_state)
                    self._last_heartbeat_at = time.monotonic()
                if self.mode == "real" and tick >= self._next_unit_check and self.tpg:
                    await self.tpg.read_unit()
                    self._next_unit_check = tick + 1.0
            except (TPG366Error, ModbusError, OSError, ValueError) as exc:
                if self.mode == "real":
                    if isinstance(exc, TPG366Error):
                        self._tpg_connected = False
                        self._tpg_error = str(exc)
                        if self.tpg:
                            await self.tpg.close()
                    else:
                        self._adam_connected = False
                        self._io_error = str(exc)
                        self._permit_ok = False
                        if self.adam:
                            await self.adam.close()
                    if self.interlock == "armed":
                        await self.trip(f"设备通信故障：{exc}", kind="device_communication_trip", fault=True)
                else:
                    self._event("simulation_error", "error", f"模拟器内部故障：{exc}")

            if self.mode == "simulation":
                self._tpg_connected = True
                self._adam_connected = not self._sim_io_fault
                self._permit_ok = self._sim_permit and not self._sim_io_fault

            pressure_fault = self._pressure_fault() if self._channels else None
            if self.interlock == "armed" and pressure_fault:
                await self.trip(pressure_fault)

            if self.interlock == "armed":
                age = None if self._last_tpg_read is None else time.monotonic() - self._last_tpg_read
                if age is None or age > self.config.stale_timeout_s:
                    await self.trip("真空数据更新超时，已撤销开阀许可", kind="stale_data_trip", fault=True)
                elif not self._permit_ok:
                    await self.trip("硬件压力许可或心跳监控失效", kind="hardware_permit_trip", fault=True)

            if self.mode == "real" and self._valve_command is not None:
                self._refresh_status()
                command_age = time.monotonic() - self._valve_command_at
                if self.status.valve == "fault":
                    await self.trip("插板阀打开/关闭位置反馈同时有效", kind="valve_sensor_conflict", fault=True)
                elif command_age > self.config.valve_timeout_s:
                    expected = "open" if self._valve_command == "open" else "closed"
                    if self.status.valve != expected:
                        action = "打开" if expected == "open" else "关闭"
                        await self.trip(f"插板阀{action}位置反馈超时", kind="valve_motion_timeout", fault=True)

            recovery_fault = self._reset_conditions() if self.interlock in ("tripped", "fault") else "inactive"
            if self.interlock in ("tripped", "fault") and recovery_fault is None:
                if self._recovery_since is None:
                    self._recovery_since = time.monotonic()
            elif self.interlock in ("tripped", "fault"):
                self._recovery_since = None

            now = time.monotonic()
            if self._channels and now - self._last_history_write >= self.config.sample_interval_s:
                await self._record_sample()
                self._last_history_write = now
            if self.config.history_retention_days is not None and now - self._last_retention_check >= 3600:
                cutoff = datetime.now(timezone.utc).timestamp() - self.config.history_retention_days * 86400
                cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
                try:
                    deleted = self.history.purge_before(cutoff_iso)
                    if deleted:
                        self._event("history_retention", "info", "按保留期限清理历史记录", details={"deleted_samples": deleted})
                except (OSError, sqlite3.Error) as exc:
                    self._event("history_retention_error", "error", "历史保留期限清理失败", details={"error": str(exc)})
                self._last_retention_check = now
            self._refresh_status()
            self._publish()
            await asyncio.sleep(max(0.02, 0.1 - (time.monotonic() - tick)))
