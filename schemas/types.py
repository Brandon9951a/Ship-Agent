"""Shared business records. Numeric values are supplied by tools, never filled here."""

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Literal, TypeVar


class Status(str, Enum):
    OK = "ok"
    NEED_CLARIFICATION = "need_clarification"
    INVALID_INPUT = "invalid_input"
    INFEASIBLE = "infeasible"
    FAILED = "failed"
    AWAITING_CHOICE = "awaiting_choice"


class InfeasibleType(str, Enum):
    TIME = "time"
    SOC = "soc"
    POWER = "power"
    ROUTE = "route"
    COMBINED = "combined"


EnergyScope = Literal["propulsion", "total"]
ModelT = TypeVar("ModelT", bound="SchemaModel")


class SchemaModel:
    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible record, including unknown values as null."""
        def plain(value: Any) -> Any:
            if isinstance(value, Enum):
                return value.value
            if isinstance(value, dict):
                return {key: plain(item) for key, item in value.items()}
            if isinstance(value, list):
                return [plain(item) for item in value]
            return value

        return plain(asdict(self))

    @classmethod
    def from_dict(cls: type[ModelT], data: dict[str, Any]) -> ModelT:
        """Decode types only; call validate() to check business readiness."""
        from .validate import decode_model
        return decode_model(cls, data)


@dataclass(frozen=True)
class SourceRef(SchemaModel):
    source_id: str
    kind: Literal["document", "user", "measurement", "assumption", "tool"]
    locator: str | None = None
    confirmed: bool = False
    note: str | None = None


@dataclass(frozen=True)
class ParameterValue(SchemaModel):
    value: float | str | None
    unit: str | None
    source: SourceRef


@dataclass(frozen=True)
class DataConflict(SchemaModel):
    field_name: str
    candidates: list[ParameterValue]
    resolution: str | None = None


@dataclass(frozen=True)
class VoyageRequest(SchemaModel):
    origin: str | None = None
    destination: str | None = None
    departure_at: str | None = None
    arrival_deadline: str | None = None
    max_duration_h: float | None = None
    soc_initial: float | None = None
    load_state: str | None = None
    draft_m: float | None = None
    environment: dict[str, ParameterValue] = field(default_factory=dict)


@dataclass(frozen=True)
class VesselState(SchemaModel):
    vessel_id: str
    capacity_kwh: float | None = None
    soc_initial: float | None = None
    soc_min: float | None = None
    soc_alarm: float | None = None
    max_power_kw: float | None = None
    auxiliary_power_kw: float | None = None
    draft_m: float | None = None
    sources: dict[str, SourceRef] = field(default_factory=dict)
    assumptions: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DataContext(SchemaModel):
    vessel: VesselState
    route_id: str | None = None
    parameters: dict[str, ParameterValue] = field(default_factory=dict)
    conflicts: list[DataConflict] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Segment(SchemaModel):
    segment_id: str
    origin: str
    destination: str
    distance_km: float
    max_speed_kmh: float | None = None
    min_speed_kmh: float | None = None
    waiting_h: float | None = None
    source: SourceRef | None = None
    assumptions: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EnergyResult(SchemaModel):
    segment_id: str
    speed_kmh: float
    power_kw: float
    duration_h: float
    energy_kwh: float
    energy_scope: EnergyScope
    model_id: str
    peak_power_kw: float | None = None
    assumptions: list[str] = field(default_factory=list)
    propulsion_energy_kwh: float | None = None
    auxiliary_energy_kwh: float | None = None
    source: SourceRef | None = None
    model_approval_ref: str | None = None


@dataclass(frozen=True)
class ConstraintCheck(SchemaModel):
    name: str
    passed: bool | None
    actual: float | None = None
    limit: float | None = None
    unit: str | None = None
    reason: str | None = None
    source: SourceRef | None = None


@dataclass(frozen=True)
class OptimizationResult(SchemaModel):
    feasible: bool
    energy_scope: EnergyScope
    energy_results: list[EnergyResult] = field(default_factory=list)
    total_energy_kwh: float | None = None
    total_duration_h: float | None = None
    eta: str | None = None
    infeasible_type: InfeasibleType | None = None
    reason: str | None = None
    checks: list[ConstraintCheck] = field(default_factory=list)
    search_description: str | None = None
    assumptions: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SocPoint(SchemaModel):
    segment_id: str
    soc: float


@dataclass(frozen=True)
class ManagementPlan(SchemaModel):
    safe: bool | None = None
    soc_initial: float | None = None
    soc_final: float | None = None
    soc_min: float | None = None
    capacity_kwh: float | None = None
    available_energy_kwh: float | None = None
    required_energy_kwh: float | None = None
    auxiliary_energy_kwh: float | None = None
    charge_required_kwh: float | None = None
    soc_trajectory: list[SocPoint] = field(default_factory=list)
    checks: list[ConstraintCheck] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TraceEvent(SchemaModel):
    tool: str
    status: Status
    message: str
    input_id: str | None = None


@dataclass(frozen=True)
class VoyagePlan(SchemaModel):
    request: VoyageRequest
    status: Status
    data: DataContext | None = None
    segments: list[Segment] = field(default_factory=list)
    optimization: OptimizationResult | None = None
    management: ManagementPlan | None = None
    clarification_questions: list[str] = field(default_factory=list)
    reason: str | None = None
    assumptions: list[str] = field(default_factory=list)
    trace: list[TraceEvent] = field(default_factory=list)


@dataclass(frozen=True)
class ToolResponse(SchemaModel):
    tool: str
    status: Status
    payload: dict[str, Any] | None = None
    missing_fields: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    reason: str | None = None
    infeasible_type: InfeasibleType | None = None
