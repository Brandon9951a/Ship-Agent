"""Tspeed 负责人交付的共同需求构建器：电机侧 → 母线侧的唯一转换位置。

设计约束（接口 v2 第 5 节）：
* Tenergy 只预测主推进电机侧功率；
* 电机到母线的换算与辅助负载合并**只在此处执行一次**，Tspeed 的电池约束与
  Tmanagement 读取同一 ``PowerDemandSeries`` 工件；
* 转换逻辑不藏在 DE 目标函数或 FDP 状态转移中；
* 缺转换依据时 ``demand_bus_kw = null``，仍可做显式标注的电机侧对照实验，
  但不得宣称 SOC 安全或完整实船规划；
* 辅助负载不为 0 也不能默认，必须给出模型与来源。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Protocol

from research.contracts import (
    ArtifactRef,
    Diagnostic,
    EnergyResultEnvelope,
    OutcomeCode,
    Phase,
    PowerDemandPoint,
    PowerDemandSeries,
    SourceRef,
    stable_hash,
)
from research.contracts.envelope import _require_finite


class DemandBuildError(Exception):
    """构建失败，携带 v2 诊断项，供工具层转换为 outcome_code。"""

    def __init__(self, code: str, message: str, *, diagnostic: Diagnostic | None = None):
        super().__init__(message)
        self.code = code
        self.diagnostic = diagnostic


@dataclass(frozen=True)
class ConversionModel:
    """电机侧 → 母线侧的转换模型。

    二选一：
    * ``constant_efficiency``：``propulsion_bus_kw = motor_power_kw / efficiency``；
    * ``affine_loss``：``propulsion_bus_kw = motor_power_kw / efficiency + offset_kw``。
    ``efficiency`` 是"母线 → 电机"方向的效率，必须 < 1 且 > 0；不得默认为 1。
    """

    model_id: str
    kind: str
    efficiency: float
    offset_kw: float = 0.0
    source_ref: SourceRef | None = None
    note: str | None = None

    KINDS = ("constant_efficiency", "affine_loss")

    def __post_init__(self) -> None:
        if not self.model_id.strip():
            raise ValueError("ConversionModel.model_id 必须非空")
        if self.kind not in self.KINDS:
            raise ValueError(f"kind 只允许 {self.KINDS}")
        efficiency = _require_finite(self.efficiency, "efficiency")
        if not 0.0 < efficiency < 1.0:
            raise ValueError(
                "efficiency 必须在 (0, 1) 内；未知效率不得默认为 1")
        _require_finite(self.offset_kw, "offset_kw", minimum=0)

    def config_hash(self) -> str:
        return stable_hash({
            "model_id": self.model_id,
            "kind": self.kind,
            "efficiency": self.efficiency,
            "offset_kw": self.offset_kw,
            "source_id": self.source_ref.source_id if self.source_ref else None,
        })

    def to_bus_kw(self, motor_power_kw: float | None) -> float | None:
        """把电机侧功率换算为推进母线侧功率；输入 null 传播为 null。"""
        if motor_power_kw is None:
            return None
        power = _require_finite(motor_power_kw, "motor_power_kw", minimum=0)
        if self.kind == "constant_efficiency":
            return power / self.efficiency
        return power / self.efficiency + self.offset_kw


@dataclass(frozen=True)
class AuxiliaryModel:
    """辅助负载模型：逐阶段（航行/等待/停靠）常数功率，必须带来源。"""

    model_id: str
    sailing_kw: float
    waiting_kw: float
    berthing_kw: float
    source_ref: SourceRef | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        if not self.model_id.strip():
            raise ValueError("AuxiliaryModel.model_id 必须非空")
        for name in ("sailing_kw", "waiting_kw", "berthing_kw"):
            value = _require_finite(getattr(self, name), name)
            if value < 0:
                raise ValueError(f"{name} 不得为负；未知辅助负载不得默认为 0")
        if self.source_ref is None:
            raise ValueError(
                "AuxiliaryModel 必须给出 source_ref：未知辅助功率不得默认为 0")

    def power_kw(self, phase: str) -> float:
        if phase == Phase.SAILING.value:
            return self.sailing_kw
        if phase == Phase.WAITING.value:
            return self.waiting_kw
        if phase == Phase.BERTHING.value:
            return self.berthing_kw
        raise DemandBuildError(
            "unsupported_phase",
            f"辅助负载模型未定义阶段 {phase!r} 的取值；不得默认为 0")

    def config_hash(self) -> str:
        return stable_hash({
            "model_id": self.model_id,
            "sailing_kw": self.sailing_kw,
            "waiting_kw": self.waiting_kw,
            "berthing_kw": self.berthing_kw,
            "source_id": self.source_ref.source_id if self.source_ref else None,
        })


@dataclass(frozen=True)
class DemandBuilderSpec:
    """一次采用的转换版本；三个数值工具共用同一份配置哈希。

    ``conversion`` 为 ``None`` 表示当前没有取得转换依据：此时只输出电机侧
    序列（``demand_bus_kw = null``），供明确标注的电机侧航速对照实验。
    """

    builder_id: str
    auxiliary: AuxiliaryModel
    conversion: ConversionModel | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.builder_id.strip():
            raise ValueError("DemandBuilderSpec.builder_id 必须非空")
        if self.conversion is None and not (self.reason or "").strip():
            raise ValueError("未取得转换依据时必须写明 reason，以便如实标注研究情景")

    @property
    def bus_side_available(self) -> bool:
        return self.conversion is not None

    def config_hash(self) -> str:
        return stable_hash({
            "builder_id": self.builder_id,
            "conversion": self.conversion.config_hash() if self.conversion else None,
            "auxiliary": self.auxiliary.config_hash(),
        })

    def source_refs(self) -> tuple[SourceRef, ...]:
        refs: list[SourceRef] = []
        if self.conversion is not None and self.conversion.source_ref is not None:
            refs.append(self.conversion.source_ref)
        if self.auxiliary.source_ref is not None:
            refs.append(self.auxiliary.source_ref)
        return tuple(refs)

    def evidence_note(self) -> str:
        """区分"已核实转换"与"具名研究假设"，供结果如实标注。"""
        if self.conversion is None:
            return "无转换依据：仅电机侧对照实验，不得宣称 SOC 安全"
        kind = self.conversion.source_ref.kind if self.conversion.source_ref else "unknown"
        if kind == "assumption":
            return "母线侧为具名研究假设情景，不代表实船效率标定"
        return "母线侧采用已核实设备参数"


def build_power_demand(energy: EnergyResultEnvelope, *, spec: DemandBuilderSpec,
                       series_id: str, input_energy_ref: str | None = None,
                       artifact_ref: ArtifactRef | None = None,
                       ) -> PowerDemandSeries:
    """由 Tenergy 的逐时电机侧结果构建唯一母线需求工件。

    逐区间执行同一转换，航行/等待/补能阶段分别记录；负需求不在此处静默处理。
    """
    if not energy.ok:
        raise DemandBuildError(
            "energy_not_ok",
            f"候选 {energy.candidate_id} 的 Tenergy 结果未成功，不能构建母线需求")
    points: list[PowerDemandPoint] = []
    quality: list[str] = []
    conversion = spec.conversion
    for point in energy.power_points:
        phase = point.phase
        auxiliary_kw = spec.auxiliary.power_kw(phase)
        motor_power = point.predicted_motor_power_kw
        propulsion_bus = conversion.to_bus_kw(motor_power) if conversion else None
        demand_bus = (propulsion_bus + auxiliary_kw) if propulsion_bus is not None else None
        flags = tuple(point.quality_flags)
        if conversion is None and "missing" not in flags:
            # 母线侧无依据：该区间的 demand_bus_kw 明确不可用，不用 0 冒充。
            flags = flags + ("missing",)
        points.append(PowerDemandPoint(
            phase=phase,
            start_offset_s=point.start_offset_s,
            duration_s=point.duration_s,
            motor_power_kw=motor_power,
            propulsion_bus_kw=propulsion_bus,
            auxiliary_bus_kw=auxiliary_kw,
            demand_bus_kw=demand_bus,
            conversion_model_ref=(conversion.model_id if conversion else None),
            segment_id=point.segment_id,
            planned_speed_kmh=point.planned_speed_kmh,
            quality_flags=flags,
        ))
    diagnostics: list[Diagnostic] = []
    if conversion is None:
        diagnostics.append(Diagnostic(
            stage="demand_build",
            code="conversion_basis_missing",
            detail=f"{spec.reason}；demand_bus_kw 保持 null，只能做电机侧对照实验",
            field_ref="conversion_model_ref",
            source_refs=spec.source_refs(),
            retryable=False,
        ))
    else:
        if conversion.source_ref is not None and conversion.source_ref.kind == "assumption":
            diagnostics.append(Diagnostic(
                stage="demand_build",
                code="conversion_is_named_assumption",
                detail="母线侧取自具名研究假设，不代表实船效率标定",
                field_ref="conversion_model_ref",
                source_refs=(conversion.source_ref,),
                retryable=False,
            ))
    series = PowerDemandSeries(
        series_id=series_id,
        points=tuple(points),
        conversion_model_ref=(conversion.model_id if conversion else
                              f"none.{spec.builder_id}"),
        conversion_config_hash=spec.config_hash(),
        input_energy_ref=input_energy_ref,
        source_refs=spec.source_refs(),
        bus_side_available=spec.bus_side_available,
        auxiliary_model_ref=spec.auxiliary.model_id,
        diagnostics=tuple(diagnostics),
        quality_flags=tuple(dict.fromkeys(quality)),
        artifact_ref=artifact_ref,
        note=spec.evidence_note(),
    )
    return series


def energy_balance_check(series: PowerDemandSeries, *,
                         tolerance_kwh: float = 1e-9) -> dict[str, object]:
    """独立单步能量守恒检查。

    对常数效率模型：``demand_bus_kwh == motor_kwh / η + auxiliary_kwh``。
    母线侧不可用时返回 ``applicable=False``，而不是给出通过结论。
    """
    tolerance = _require_finite(tolerance_kwh, "tolerance_kwh", minimum=0)
    if not series.bus_side_available:
        return {"applicable": False,
                "reason": "母线侧无依据（demand_bus_kw 为 null），不做守恒结论"}
    balance = series.energy_balance()
    motor = balance["motor_energy_kwh"]
    demand = balance["demand_bus_energy_kwh"]
    auxiliary = balance["auxiliary_bus_energy_kwh"]
    propulsion = balance["propulsion_bus_energy_kwh"]
    if None in (motor, demand, auxiliary, propulsion):
        return {"applicable": False, "reason": "存在未预测区间，能量汇总不完整"}
    residual = demand - (propulsion + auxiliary)
    return {
        "applicable": True,
        "motor_energy_kwh": motor,
        "propulsion_bus_energy_kwh": propulsion,
        "auxiliary_bus_energy_kwh": auxiliary,
        "demand_bus_energy_kwh": demand,
        "residual_kwh": residual,
        "tolerance_kwh": tolerance,
        "passed": abs(residual) <= tolerance,
    }


def bus_power_limit_check(series: PowerDemandSeries, limit_kw: float, *,
                          tolerance_kw: float = 1e-6,
                          source_ref: SourceRef | None = None) -> dict[str, object]:
    """同边界功率上限检查；只在母线侧可用时给出结论。"""
    limit = _require_finite(limit_kw, "limit_kw", minimum=0)
    if not series.bus_side_available:
        return {"applicable": False,
                "reason": "母线侧无依据，功率上限检查不适用（不得用电机侧数值代替）"}
    peak = series.peak_demand_kw()
    if peak is None:
        return {"applicable": False, "reason": "存在未预测区间，峰值不确定"}
    return {"applicable": True, "peak_demand_kw": peak, "limit_kw": limit,
            "passed": peak <= limit + tolerance_kw, "source_ref": source_ref}
