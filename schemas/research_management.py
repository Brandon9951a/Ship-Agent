"""Draft v2 request records for the research battery-management tool.

These records are deliberately separate from ``schemas.types``: that module is
the legacy demonstration contract and contains a single merged SOC value.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from schemas.types import SourceRef


SCHEMA_VERSION = "research-tools/2.0-draft"


def _plain(value: Any) -> Any:
    if isinstance(value, SourceRef):
        return value.to_dict()
    if hasattr(value, "__dataclass_fields__"):
        return {key: _plain(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _source(raw: Any) -> SourceRef:
    if isinstance(raw, SourceRef):
        return raw
    if not isinstance(raw, dict):
        raise TypeError("source reference must be an object")
    return SourceRef.from_dict(raw)


@dataclass(frozen=True)
class PowerDemandPoint:
    """One contiguous bus-side interval from the shared Tspeed adapter."""

    segment_id: str
    phase: Literal["sailing", "waiting", "docking"]
    start_offset_s: float
    duration_s: float
    demand_bus_kw: float
    demand_source: SourceRef | None
    conversion_model_ref: str
    conversion_source: SourceRef | None
    motor_power_kw: float | None = None
    propulsion_bus_kw: float | None = None
    auxiliary_bus_kw: float | None = None
    component_source: SourceRef | None = None

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PowerDemandPoint":
        return cls(
            segment_id=raw.get("segment_id"),
            phase=raw.get("phase"),
            start_offset_s=raw.get("start_offset_s"),
            duration_s=raw.get("duration_s"),
            demand_bus_kw=raw.get("demand_bus_kw"),
            demand_source=(_source(raw["demand_source"])
                           if raw.get("demand_source") is not None else None),
            conversion_model_ref=raw.get("conversion_model_ref"),
            conversion_source=(_source(raw["conversion_source"])
                               if raw.get("conversion_source") is not None else None),
            motor_power_kw=raw.get("motor_power_kw"),
            propulsion_bus_kw=raw.get("propulsion_bus_kw"),
            auxiliary_bus_kw=raw.get("auxiliary_bus_kw"),
            component_source=(
                _source(raw["component_source"])
                if raw.get("component_source") is not None else None
            ),
        )


@dataclass(frozen=True)
class BatteryParameters:
    battery_id: Literal["battery_1", "battery_2"]
    capacity_kwh: float
    initial_soc: float
    soc_min: float
    min_power_kw: float
    max_power_kw: float
    discharge_efficiency: float
    sources: dict[str, SourceRef]

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "BatteryParameters":
        sources = raw.get("sources", {})
        if not isinstance(sources, dict):
            raise TypeError("battery sources must be an object")
        return cls(
            battery_id=raw.get("battery_id"),
            capacity_kwh=raw.get("capacity_kwh"),
            initial_soc=raw.get("initial_soc"),
            soc_min=raw.get("soc_min"),
            min_power_kw=raw.get("min_power_kw"),
            max_power_kw=raw.get("max_power_kw"),
            discharge_efficiency=raw.get("discharge_efficiency"),
            sources={key: _source(value) for key, value in sources.items()
                     if value is not None},
        )


@dataclass(frozen=True)
class CostWeights:
    """Equation (5-27)--(5-30) reference profile from thesis printed p. 88."""

    k_p: float = 1e-4
    k_s: float = 1e3
    k_soc: float = 0.3
    k_low: float = 1e5
    low_load_threshold: float = 0.65
    profile_id: str = "thesis-5.2.2-equations-5-27-to-5-30"
    source: SourceRef = field(default_factory=lambda: SourceRef(
        "liu-hanyou-thesis-2024", "document", "printed page 88, equations 5-27 to 5-30",
        True, "Reference experiment weights; not vessel safety limits.",
    ))

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "CostWeights":
        if raw is None:
            return cls()
        defaults = cls()
        return cls(
            k_p=raw.get("k_p", defaults.k_p),
            k_s=raw.get("k_s", defaults.k_s),
            k_soc=raw.get("k_soc", defaults.k_soc),
            k_low=raw.get("k_low", defaults.k_low),
            low_load_threshold=raw.get("low_load_threshold", defaults.low_load_threshold),
            profile_id=raw.get("profile_id", defaults.profile_id),
            source=_source(raw.get("source", defaults.source.to_dict())),
        )


@dataclass(frozen=True)
class FDPGridConfig:
    soc_step: float = 0.05
    max_states_per_layer: int = 20_000
    max_transitions: int = 5_000_000
    algorithm_version: str = "fdp-three-mode-conservative-grid/1.0.0"

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "FDPGridConfig":
        if raw is None:
            return cls()
        defaults = cls()
        return cls(
            soc_step=raw.get("soc_step", defaults.soc_step),
            max_states_per_layer=raw.get("max_states_per_layer", defaults.max_states_per_layer),
            max_transitions=raw.get("max_transitions", defaults.max_transitions),
            algorithm_version=raw.get("algorithm_version", defaults.algorithm_version),
        )


@dataclass(frozen=True)
class BatteryManagementRequest:
    schema_version: str
    task_id: str
    request_revision: int
    snapshot_id: str
    scenario_id: str
    call_id: str
    selected_candidate_id: str
    selected_demand_ref: str
    demand_series: list[PowerDemandPoint]
    battery_1: BatteryParameters
    battery_2: BatteryParameters
    load_rate_reference_kw: float
    load_rate_reference_source: SourceRef | None
    allowed_mode_transitions: tuple[tuple[int, int], ...]
    allowed_mode_transitions_source: SourceRef | None
    previous_mode: int | None = None
    planning_origin_at: str | None = None
    parent_call_id: str | None = None
    grid: FDPGridConfig = field(default_factory=FDPGridConfig)
    weights: CostWeights = field(default_factory=CostWeights)
    assumptions: list[dict[str, str]] = field(default_factory=list)
    source_mode: Literal["simulation", "replay", "measured"] = "simulation"

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "BatteryManagementRequest":
        if not isinstance(raw, dict):
            raise TypeError("request must be an object")
        series = raw.get("demand_series", [])
        transitions = raw.get("allowed_mode_transitions", [])
        assumptions = raw.get("assumptions", [])
        if not isinstance(series, list) or not isinstance(transitions, (list, tuple)):
            raise TypeError("demand_series and allowed_mode_transitions must be arrays")
        if not isinstance(assumptions, list):
            raise TypeError("assumptions must be an array")
        return cls(
            schema_version=raw.get("schema_version", SCHEMA_VERSION),
            task_id=raw.get("task_id"),
            request_revision=raw.get("request_revision"),
            snapshot_id=raw.get("snapshot_id"),
            scenario_id=raw.get("scenario_id"),
            call_id=raw.get("call_id"),
            parent_call_id=raw.get("parent_call_id"),
            selected_candidate_id=raw.get("selected_candidate_id"),
            selected_demand_ref=raw.get("selected_demand_ref"),
            planning_origin_at=raw.get("planning_origin_at"),
            demand_series=[
                item if isinstance(item, PowerDemandPoint) else PowerDemandPoint.from_dict(item)
                for item in series
            ],
            battery_1=(raw.get("battery_1") if isinstance(raw.get("battery_1"), BatteryParameters)
                       else BatteryParameters.from_dict(raw.get("battery_1", {}))),
            battery_2=(raw.get("battery_2") if isinstance(raw.get("battery_2"), BatteryParameters)
                       else BatteryParameters.from_dict(raw.get("battery_2", {}))),
            load_rate_reference_kw=raw.get("load_rate_reference_kw"),
            load_rate_reference_source=(
                _source(raw["load_rate_reference_source"])
                if raw.get("load_rate_reference_source") is not None else None
            ),
            allowed_mode_transitions=tuple(tuple(pair) for pair in transitions),
            allowed_mode_transitions_source=(
                _source(raw["allowed_mode_transitions_source"])
                if raw.get("allowed_mode_transitions_source") is not None else None
            ),
            previous_mode=raw.get("previous_mode"),
            grid=(raw.get("grid") if isinstance(raw.get("grid"), FDPGridConfig)
                  else FDPGridConfig.from_dict(raw.get("grid"))),
            weights=(raw.get("weights") if isinstance(raw.get("weights"), CostWeights)
                     else CostWeights.from_dict(raw.get("weights"))),
            assumptions=assumptions,
            source_mode=raw.get("source_mode", "simulation"),
        )


def source_from_dict(raw: dict[str, Any]) -> SourceRef:
    """Public helper used by JSON fixture builders and tests."""
    return _source(raw)

