from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator


class ChannelConfig(BaseModel):
    enabled: bool = False
    label: str = ""
    trip_pa: Optional[float] = Field(default=None, gt=0)
    reset_pa: Optional[float] = Field(default=None, ge=0)

    @model_validator(mode="after")
    def thresholds_are_ordered(self) -> "ChannelConfig":
        if self.enabled and (self.trip_pa is None or self.reset_pa is None):
            raise ValueError("启用保护的通道必须设置关阀与恢复阈值")
        if self.enabled and self.reset_pa is not None and self.trip_pa is not None:
            if self.reset_pa >= self.trip_pa:
                raise ValueError("恢复阈值必须低于关阀阈值")
        return self


class AppConfig(BaseModel):
    mode: Literal["simulation", "real"] = "simulation"
    tpg_host: str = ""
    tpg_port: int = Field(default=8000, ge=1, le=65535)
    adam_host: str = ""
    adam_port: int = Field(default=502, ge=1, le=65535)
    adam_unit_id: int = Field(default=1, ge=0, le=255)
    sample_interval_s: float = Field(default=1.0, ge=0.2, le=30.0)
    stale_timeout_s: float = Field(default=1.0, ge=0.5, le=10.0)
    valve_timeout_s: float = Field(default=2.0, ge=0.5, le=20.0)
    recovery_hold_s: float = Field(default=5.0, ge=0.0, le=120.0)
    history_retention_days: Optional[int] = Field(default=None, ge=1, le=3650)
    allow_lan: bool = False
    # The hardware mapping is intentionally explicit.  DI0/DI1 must report
    # mutually exclusive open/closed limit sensors; output 0 requests OPEN.
    # HTC supplies a position Sensor as an option; the exact switch count and
    # polarity depend on the delivered sensor/solenoid assembly.
    valve_closed_di: int = Field(default=0, ge=0, le=11)
    valve_open_di: Optional[int] = Field(default=None, ge=0, le=11)
    # Unverified permissive signals default to inactive until wiring polarity is checked.
    valve_closed_active_high: bool = True
    valve_open_active_high: bool = True
    permit_ok_di: int = Field(default=1, ge=0, le=11)
    permit_ok_active_high: bool = True
    watchdog_ok_di: int = Field(default=2, ge=0, le=11)
    watchdog_ok_active_high: bool = True
    latch_ready_di: int = Field(default=3, ge=0, le=11)
    latch_ready_active_high: bool = True
    air_ok_di: Optional[int] = Field(default=None, ge=0, le=11)
    air_ok_active_high: bool = True
    open_output: int = Field(default=0, ge=0, le=5)
    heartbeat_output: int = Field(default=1, ge=0, le=5)
    reset_output: int = Field(default=2, ge=0, le=5)
    # Set only after the exact installed solenoid, valve return behavior and
    # independent permissive circuit have been verified on the bench.
    fail_closed_confirmed: bool = False
    channels: list[ChannelConfig] = Field(
        default_factory=lambda: [ChannelConfig() for _ in range(6)]
    )

    @model_validator(mode="after")
    def verify_channel_count_and_mapping(self) -> "AppConfig":
        if len(self.channels) != 6:
            raise ValueError("TPG 366 配置必须包含六个通道")
        inputs = [self.valve_closed_di, self.valve_open_di, self.permit_ok_di,
                  self.watchdog_ok_di, self.latch_ready_di, self.air_ok_di]
        used = [item for item in inputs if item is not None]
        if len(used) != len(set(used)):
            raise ValueError("阀位、压力许可、看门狗与复位反馈必须使用不同输入")
        outputs = [self.open_output, self.heartbeat_output, self.reset_output]
        if len(set(outputs)) != len(outputs):
            raise ValueError("开阀、心跳和复位必须使用不同输出")
        return self


class ConfigPatch(BaseModel):
    config: AppConfig


class ActionRequest(BaseModel):
    reason: str = Field(default="操作员请求", max_length=240)


class LoginRequest(BaseModel):
    password: str = Field(min_length=12, max_length=256)


class SampleChannel(BaseModel):
    channel: int
    label: str
    pressure_pa: Optional[float] = None
    raw_value: Optional[float] = None
    raw_unit: str = "Pa"
    status_code: Optional[int] = None
    status: str = "未连接"
    valid: bool = False
    trip_pa: Optional[float] = None
    reset_pa: Optional[float] = None


class SystemStatus(BaseModel):
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    mode: Literal["simulation", "real"] = "simulation"
    interlock: Literal["disarmed", "ready", "armed", "tripped", "fault"] = "disarmed"
    interlock_label: str = "未启用"
    trip_reason: Optional[str] = None
    valve: Literal["unknown", "open", "closed", "opening", "closing", "fault"] = "unknown"
    valve_open_sensor: bool = False
    valve_closed_sensor: bool = False
    tpg_connected: bool = False
    adam_connected: bool = False
    sample_age_s: Optional[float] = None
    unit: str = "Pa"
    permit_ok: bool = False
    fail_closed_confirmed: bool = False
    history_recording: bool = True
    channels: list[SampleChannel] = Field(default_factory=list)


class InjectPressure(BaseModel):
    channel: int = Field(ge=1, le=6)
    pressure_pa: float = Field(gt=0)


class EventRecord(BaseModel):
    id: int
    timestamp: str
    type: str
    severity: str
    message: str
    actor: str
    details: dict = Field(default_factory=dict)
