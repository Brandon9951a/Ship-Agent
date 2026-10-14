"""接口 v2 第 5 节：单一转换位置的母线需求工件 ``PowerDemandSeries``。

Tenergy 只预测主推进电机侧功率；电机侧 → 母线侧的换算与辅助负载合并
由本工件（由 Tspeed 负责人交付的共同需求构建器）一次完成。
Tspeed 的电池约束与 Tmanagement 必须读取同一工件，不得各自暗算。
"""

from __future__ import annotations

from dataclasses import dataclass

from .diagnostics import Diagnostic
from .envelope import (
    ArtifactRef,
    ContractModel,
    DataQualityFlag,
    SourceRef,
    _require_finite,
    stable_hash,
)

POWER_DEMAND_SCHEMA_VERSION = "research-tools/2.0-draft/power-demand-series"


@dataclass(frozen=True)
class PowerDemandPoint(ContractModel):
    """一个区间的功率需求；母线侧无依据时 ``demand_bus_kw`` 为 null。"""

    phase: str
    start_offset_s: float
    duration_s: float
    motor_power_kw: float | None
    propulsion_bus_kw: float | None
    auxiliary_bus_kw: float | None
    demand_bus_kw: float | None
    conversion_model_ref: str | None = None
    segment_id: str | None = None
    planned_speed_kmh: float | None = None
    quality_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "quality_flags", tuple(self.quality_flags))
        _require_finite(self.start_offset_s, "start_offset_s", minimum=0)
        duration = _require_finite(self.duration_s, "duration_s")
        if duration <= 0:
            raise ValueError("PowerDemandPoint.duration_s 必须大于 0")
        for name in ("motor_power_kw", "propulsion_bus_kw", "auxiliary_bus_kw",
                     "demand_bus_kw", "planned_speed_kmh"):
            _require_finite(getattr(self, name), name, minimum=0, allow_none=True)

    @property
    def end_offset_s(self) -> float:
        return self.start_offset_s + self.duration_s

    def demand_energy_kwh(self) -> float | None:
        """按 ``Σ P·Δt/3600`` 复算母线侧电量；null 传播为 null。"""
        if self.demand_bus_kw is None:
            return None
        return self.demand_bus_kw * self.duration_s / 3600.0

    def motor_energy_kwh(self) -> float | None:
        if self.motor_power_kw is None:
            return None
        return self.motor_power_kw * self.duration_s / 3600.0


@dataclass(frozen=True)
class PowerDemandSeries(ContractModel):
    """逐时母线需求工件：带自身配置哈希、输入工件 ID 与来源。

    研究系统只有一个采用版本的转换；转换逻辑不藏在 DE 目标函数或
    FDP 状态转移中。
    """

    series_id: str
    points: tuple[PowerDemandPoint, ...]
    conversion_model_ref: str
    conversion_config_hash: str
    input_energy_ref: str | None = None
    source_refs: tuple[SourceRef, ...] = ()
    bus_side_available: bool = False
    schema_version: str = POWER_DEMAND_SCHEMA_VERSION
    auxiliary_model_ref: str | None = None
    diagnostics: tuple[Diagnostic, ...] = ()
    quality_flags: tuple[str, ...] = ()
    artifact_ref: ArtifactRef | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "points", tuple(self.points))
        object.__setattr__(self, "source_refs", tuple(self.source_refs))
        object.__setattr__(self, "quality_flags", tuple(self.quality_flags))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
        if not self.series_id.strip():
            raise ValueError("series_id 必须非空")
        if not self.conversion_model_ref.strip():
            raise ValueError("conversion_model_ref 必须非空（缺依据时用显式标注的假设）")
        if not self.conversion_config_hash.strip():
            raise ValueError("conversion_config_hash 必须非空，用于版本核对")
        if self.bus_side_available:
            missing = [index for index, point in enumerate(self.points)
                       if point.demand_bus_kw is None]
            if missing:
                raise ValueError(
                    f"bus_side_available=true 但区间 {missing[:3]} 的 demand_bus_kw 为 null")

    @property
    def total_duration_s(self) -> float:
        return sum(point.duration_s for point in self.points)

    def demand_energy_kwh(self) -> float | None:
        """整段母线需求电量；任取值为 null 时整体为 null，不用 0 补。"""
        if not self.bus_side_available:
            return None
        total = 0.0
        for point in self.points:
            value = point.demand_energy_kwh()
            if value is None:
                return None
            total += value
        return total

    def peak_demand_kw(self) -> float | None:
        values = [point.demand_bus_kw for point in self.points
                  if point.demand_bus_kw is not None]
        return max(values) if values else None

    def energy_balance(self) -> dict[str, float | None]:
        """独立单步能量守恒检查所需的汇总（转换前后保存对应关系）。

        任一分项出现 null 时该项返回 null，不用 0 补齐缺失区间。
        """
        return {
            "motor_energy_kwh": _integrate(
                [(point.motor_power_kw, point.duration_s) for point in self.points]),
            "propulsion_bus_energy_kwh": _integrate(
                [(point.propulsion_bus_kw, point.duration_s) for point in self.points]),
            "auxiliary_bus_energy_kwh": _integrate(
                [(point.auxiliary_bus_kw, point.duration_s) for point in self.points]),
            "demand_bus_energy_kwh": self.demand_energy_kwh(),
        }

    def config_hash(self) -> str:
        return stable_hash({
            "conversion_model_ref": self.conversion_model_ref,
            "conversion_config_hash": self.conversion_config_hash,
            "auxiliary_model_ref": self.auxiliary_model_ref,
            "input_energy_ref": self.input_energy_ref,
            "bus_side_available": self.bus_side_available,
        })


def _integrate(pairs) -> float | None:
    """按 ``Σ P·Δt/3600`` 积分；任一功率未知则整体 null。"""
    total = 0.0
    for power, duration in pairs:
        if power is None:
            return None
        total += power * duration / 3600.0
    return total


@dataclass(frozen=True)
class DemandAcknowledgement(ContractModel):
    """Tmanagement 收到母线需求工件后的确认，用于版本核对。"""

    series_id: str
    conversion_config_hash: str
    accepted: bool
    detail: str | None = None
