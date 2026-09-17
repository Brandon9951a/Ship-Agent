"""Strict decoding and minimum business checks for the shared contract."""

from dataclasses import MISSING, dataclass, fields, is_dataclass
from datetime import datetime
from enum import Enum
from math import isclose, isfinite
from types import UnionType
from typing import Any, Literal, TypeVar, Union, get_args, get_origin, get_type_hints

from .types import (
    DataContext, EnergyResult, ManagementPlan, OptimizationResult,
    SchemaModel, Segment, SocPoint, SourceRef, Status, ToolResponse, VesselState,
    VoyagePlan, VoyageRequest,
)


@dataclass(frozen=True)
class ValidationIssue:
    field: str
    code: str
    message: str


@dataclass(frozen=True)
class ValidationResult:
    status: Status
    issues: tuple[ValidationIssue, ...] = ()

    @property
    def valid(self) -> bool:
        return self.status == Status.OK

    @property
    def missing_fields(self) -> list[str]:
        return [issue.field for issue in self.issues if issue.code == "missing"]

    @property
    def questions(self) -> list[str]:
        labels = {
            "origin": "起点", "destination": "终点", "soc_initial": "初始 SOC",
            "departure_at": "出发时间",
            "load_state_or_draft_m": "载况或吃水",
            "arrival_deadline_or_max_duration_h": "到达截止时间或最长耗时",
        }
        return [f"请补充{labels.get(i.field, i.field)}。{i.message}"
                for i in self.issues if i.code == "missing"]


class SchemaValidationError(ValueError):
    def __init__(self, result: ValidationResult):
        self.result = result
        super().__init__("; ".join(f"{i.field}: {i.message}" for i in result.issues))


T = TypeVar("T", bound=SchemaModel)


def _result(issues: list[ValidationIssue]) -> ValidationResult:
    if not issues:
        return ValidationResult(Status.OK)
    status = (Status.NEED_CLARIFICATION if all(i.code == "missing" for i in issues)
              else Status.INVALID_INPUT)
    return ValidationResult(status, tuple(issues))


def _path(parent: str, name: str) -> str:
    return f"{parent}.{name}" if parent else name


def _decode(expected: Any, value: Any, path: str) -> Any:
    def fail(code: str, message: str) -> None:
        raise SchemaValidationError(_result([ValidationIssue(path, code, message)]))

    if expected is Any:
        # Tool payloads must remain JSON-compatible, including finite numbers.
        if value is None or isinstance(value, (str, bool)):
            return value
        if isinstance(value, (int, float)) and isfinite(value):
            return value
        if isinstance(value, list):
            return [_decode(Any, item, f"{path}[{i}]") for i, item in enumerate(value)]
        if isinstance(value, dict) and all(isinstance(k, str) for k in value):
            return {k: _decode(Any, v, _path(path, k)) for k, v in value.items()}
        fail("invalid_type", "需要可序列化的 JSON 值，数值必须有限。")
    origin, args = get_origin(expected), get_args(expected)
    if origin in (Union, UnionType):
        for option in args:
            try:
                return _decode(option, value, path)
            except SchemaValidationError:
                pass
        fail("invalid_type", "值的类型不符合字段定义。")
    if origin is Literal:
        if value not in args or not any(type(value) is type(item) for item in args):
            fail("invalid_value", f"只允许以下值：{args}。")
        return value
    if origin is list:
        if not isinstance(value, list):
            fail("invalid_type", "需要列表。")
        return [_decode(args[0], item, f"{path}[{i}]") for i, item in enumerate(value)]
    if origin is dict:
        if not isinstance(value, dict):
            fail("invalid_type", "需要字典。")
        return {_decode(args[0], k, path): _decode(args[1], v, _path(path, str(k)))
                for k, v in value.items()}
    if expected is type(None):
        if value is not None:
            fail("invalid_type", "需要 null。")
        return None
    if isinstance(expected, type) and issubclass(expected, Enum):
        try:
            return expected(value)
        except (ValueError, TypeError):
            fail("invalid_value", "未知状态或枚举值。")
    if isinstance(expected, type) and is_dataclass(expected):
        if not isinstance(value, dict):
            fail("invalid_type", "需要结构化对象。")
        definitions = {f.name: f for f in fields(expected)}
        hints = get_type_hints(expected)
        unknown = set(value) - set(definitions)
        if unknown:
            fail("unknown_field", "未知字段：" + ", ".join(sorted(map(str, unknown))))
        decoded = {}
        for name, definition in definitions.items():
            child = _path(path, name)
            if name in value:
                decoded[name] = _decode(hints[name], value[name], child)
            elif definition.default is MISSING and definition.default_factory is MISSING:
                raise SchemaValidationError(_result([
                    ValidationIssue(child, "invalid_type", "结构必需字段缺失。")
                ]))
        return expected(**decoded)
    if expected is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
            fail("invalid_type", "需要有限数值，不能使用布尔值或数值字符串。")
        return float(value)
    if expected is str:
        if not isinstance(value, str):
            fail("invalid_type", "需要字符串。")
        return value
    if expected is bool:
        if type(value) is not bool:
            fail("invalid_type", "需要布尔值。")
        return value
    fail("invalid_type", "不支持的字段类型。")


def decode_model(model_type: type[T], data: dict[str, Any]) -> T:
    return _decode(model_type, data, "")


def _timestamp(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.utcoffset() is None:
        raise ValueError("timezone required")
    return timestamp


def validate(model: SchemaModel) -> ValidationResult:
    """Check types first, then business rules; never modify or fill the input."""
    if not isinstance(model, SchemaModel) or not is_dataclass(model):
        return _result([ValidationIssue("", "invalid_type", "需要业务数据结构。")])
    try:
        decode_model(type(model), model.to_dict())
    except SchemaValidationError as exc:
        return exc.result
    issues: list[ValidationIssue] = []

    def issue(path: str, code: str, message: str) -> None:
        issues.append(ValidationIssue(path, code, message))

    def required(value: Any, path: str) -> None:
        if value is None or (isinstance(value, str) and not value.strip()):
            issue(path, "missing", "需要补充此字段。")

    def positive(value: float | None, path: str, zero: bool = False) -> None:
        if value is not None and (value < 0 if zero else value <= 0):
            issue(path, "invalid_value", "不能为负数。" if zero else "必须大于 0。")

    def soc(value: float | None, path: str) -> None:
        if value is not None and not 0 <= value <= 1:
            issue(path, "invalid_value", "SOC 内部必须为 0–1，85% 应传 0.85。")

    def timestamp(value: str | None, path: str) -> datetime | None:
        if value is None:
            return None
        try:
            return _timestamp(value)
        except ValueError:
            issue(path, "invalid_value", "需要带时区的 ISO 8601 时间。")
            return None

    def checks_ok(checks: list, path: str, names: set[str]) -> None:
        present = {check.name for check in checks}
        for name in sorted(names - present):
            issue(_path(path, name), "missing", "缺少硬约束校验。")
        for check in checks:
            if check.passed is None:
                issue(_path(path, check.name), "missing", "约束尚未确认。")
            elif not check.passed:
                issue(_path(path, check.name), "inconsistent_value", "成功结果含未通过约束。")
        if len(present) != len(checks):
            issue(path, "inconsistent_value", "约束名称重复。")

    def walk(value: Any, path: str = "") -> None:
        p = lambda name: _path(path, name)
        if isinstance(value, SourceRef):
            required(value.source_id, p("source_id"))
        elif isinstance(value, VoyageRequest):
            for name in ("origin", "destination", "soc_initial"):
                required(getattr(value, name), p(name))
            if value.origin is not None and value.origin == value.destination:
                issue(p("destination"), "invalid_value", "起点与终点不能相同。")
            soc(value.soc_initial, p("soc_initial"))
            positive(value.draft_m, p("draft_m"))
            positive(value.max_duration_h, p("max_duration_h"))
            if (not value.load_state or not value.load_state.strip()) and value.draft_m is None:
                issue(p("load_state_or_draft_m"), "missing", "请补充载况或吃水。")
            if value.arrival_deadline is None and value.max_duration_h is None:
                issue(p("arrival_deadline_or_max_duration_h"), "missing", "请补充到达约束。")
            if value.arrival_deadline is not None:
                required(value.departure_at, p("departure_at"))
            departure = timestamp(value.departure_at, p("departure_at"))
            deadline = timestamp(value.arrival_deadline, p("arrival_deadline"))
            if departure and deadline and deadline <= departure:
                issue(p("arrival_deadline"), "invalid_value", "到达时间必须晚于出发时间。")
        elif isinstance(value, VesselState):
            required(value.vessel_id, p("vessel_id"))
            for name in ("capacity_kwh", "soc_initial", "soc_min", "max_power_kw",
                         "auxiliary_power_kw"):
                required(getattr(value, name), p(name))
                if name not in value.sources:
                    issue(p("sources." + name), "missing", "采用参数需记录来源。")
            for name in ("capacity_kwh", "max_power_kw", "draft_m"):
                positive(getattr(value, name), p(name))
            positive(value.auxiliary_power_kw, p("auxiliary_power_kw"), zero=True)
            for name in ("soc_initial", "soc_min", "soc_alarm"):
                soc(getattr(value, name), p(name))
            if value.soc_alarm is not None and value.soc_min is not None:
                if value.soc_alarm < value.soc_min:
                    issue(p("soc_alarm"), "invalid_value", "告警阈值不能低于规划安全下限。")
        elif isinstance(value, DataContext):
            required(value.route_id, p("route_id"))
            for name in value.missing_fields:
                issue(p(name), "missing", "取数仍有缺项。")
            for conflict in value.conflicts:
                if not conflict.resolution:
                    issue(p("conflicts." + conflict.field_name), "missing", "参数冲突尚未处理。")
            for name, parameter in value.parameters.items():
                if parameter.value is None:
                    issue(p("parameters." + name + ".value"), "missing", "采用参数尚无值。")
        elif isinstance(value, Segment):
            for name in ("segment_id", "origin", "destination", "max_speed_kmh",
                         "waiting_h", "source"):
                required(getattr(value, name), p(name))
            if value.origin == value.destination:
                issue(p("destination"), "invalid_value", "航段起终点不能相同。")
            for name in ("distance_km", "max_speed_kmh", "min_speed_kmh"):
                positive(getattr(value, name), p(name))
            positive(value.waiting_h, p("waiting_h"), zero=True)
            if value.min_speed_kmh is not None and value.max_speed_kmh is not None:
                if value.min_speed_kmh > value.max_speed_kmh:
                    issue(p("min_speed_kmh"), "invalid_value", "最低速度不能超过限速。")
        elif isinstance(value, EnergyResult):
            for name in ("segment_id", "model_id"):
                required(getattr(value, name), p(name))
            for name in ("speed_kmh", "duration_h"):
                positive(getattr(value, name), p(name))
            for name in ("power_kw", "energy_kwh", "peak_power_kw"):
                positive(getattr(value, name), p(name), zero=True)
            if not isclose(value.energy_kwh, value.power_kw * value.duration_h,
                           rel_tol=1e-6, abs_tol=1e-6):
                issue(p("energy_kwh"), "inconsistent_value", "能耗与平均功率 × 总耗时不一致。")
            if value.peak_power_kw is not None and value.peak_power_kw < value.power_kw:
                issue(p("peak_power_kw"), "invalid_value", "峰值功率不能低于平均功率。")
        elif isinstance(value, OptimizationResult):
            timestamp(value.eta, p("eta"))
            for name in ("total_energy_kwh", "total_duration_h"):
                positive(getattr(value, name), p(name), zero=True)
            if value.feasible:
                for name in ("total_energy_kwh", "total_duration_h"):
                    required(getattr(value, name), p(name))
                if not value.energy_results:
                    issue(p("energy_results"), "missing", "可行结果必须有分段计算。")
                checks_ok(value.checks, p("checks"), {"time", "speed", "power"})
                if value.infeasible_type is not None:
                    issue(p("infeasible_type"), "inconsistent_value", "可行结果不能同时标为不可行。")
                ids = [item.segment_id for item in value.energy_results]
                if len(set(ids)) != len(ids):
                    issue(p("energy_results"), "inconsistent_value", "选定方案的航段重复。")
                if any(item.energy_scope != value.energy_scope for item in value.energy_results):
                    issue(p("energy_scope"), "inconsistent_value", "分段能耗口径不一致。")
                for name, total, attr in (
                    ("total_energy_kwh", value.total_energy_kwh, "energy_kwh"),
                    ("total_duration_h", value.total_duration_h, "duration_h"),
                ):
                    if total is not None and not isclose(
                        total, sum(getattr(item, attr) for item in value.energy_results),
                        rel_tol=1e-6, abs_tol=1e-6,
                    ):
                        issue(p(name), "inconsistent_value", "汇总与分段结果不一致。")
            else:
                required(value.reason, p("reason"))
                required(value.infeasible_type, p("infeasible_type"))
                if value.energy_results or any(v is not None for v in (
                    value.total_energy_kwh, value.total_duration_h, value.eta
                )):
                    issue(path, "inconsistent_value", "不可行结果不能附带已选定的成功方案。")
        elif isinstance(value, SocPoint):
            required(value.segment_id, p("segment_id"))
            soc(value.soc, p("soc"))
        elif isinstance(value, ManagementPlan):
            for name in ("soc_initial", "soc_final", "soc_min"):
                soc(getattr(value, name), p(name))
            for name in ("capacity_kwh", "available_energy_kwh", "required_energy_kwh",
                         "auxiliary_energy_kwh", "charge_required_kwh"):
                positive(getattr(value, name), p(name), zero=name != "capacity_kwh")
            if value.safe is True:
                for name in ("soc_initial", "soc_final", "soc_min", "capacity_kwh",
                             "available_energy_kwh", "required_energy_kwh"):
                    required(getattr(value, name), p(name))
                checks_ok(value.checks, p("checks"), {"soc"})
                if not value.soc_trajectory:
                    issue(p("soc_trajectory"), "missing", "安全方案需给出分段 SOC。")
                if value.soc_min is not None:
                    for name in ("soc_initial", "soc_final"):
                        current = getattr(value, name)
                        if current is not None and current < value.soc_min:
                            issue(p(name), "inconsistent_value", "安全方案越过 SOC 下限。")
                    if any(point.soc < value.soc_min for point in value.soc_trajectory):
                        issue(p("soc_trajectory"), "inconsistent_value", "分段 SOC 越过下限。")
                if value.soc_final is not None and value.soc_trajectory:
                    if not isclose(value.soc_final, value.soc_trajectory[-1].soc,
                                   abs_tol=1e-6, rel_tol=1e-6):
                        issue(p("soc_final"), "inconsistent_value", "末端 SOC 与轨迹不一致。")
                if value.charge_required_kwh is not None and value.charge_required_kwh > 0:
                    issue(p("safe"), "inconsistent_value", "仍需补能的任务不能直接标为安全。")
                if value.available_energy_kwh is not None and value.required_energy_kwh is not None:
                    if value.required_energy_kwh > value.available_energy_kwh + 1e-6:
                        issue(p("required_energy_kwh"), "inconsistent_value", "需求超过可用预算。")
        elif isinstance(value, ToolResponse):
            required(value.tool, p("tool"))
            if value.status == Status.OK:
                required(value.payload, p("payload"))
                if value.missing_fields or value.questions or value.infeasible_type:
                    issue(p("status"), "inconsistent_value", "成功状态包含缺参或不可行信息。")
            elif value.status == Status.NEED_CLARIFICATION:
                if not value.missing_fields or not value.questions:
                    issue(path, "inconsistent_value", "追问状态需给出缺失字段和问题。")
            elif value.status == Status.INFEASIBLE:
                required(value.reason, p("reason"))
                required(value.infeasible_type, p("infeasible_type"))
            elif value.status in (Status.FAILED, Status.INVALID_INPUT):
                required(value.reason, p("reason"))
        elif isinstance(value, VoyagePlan):
            if value.status == Status.OK:
                for name in ("data", "optimization", "management"):
                    required(getattr(value, name), p(name))
                if not value.segments:
                    issue(p("segments"), "missing", "完整方案必须有航段。")
                if value.clarification_questions:
                    issue(p("status"), "inconsistent_value", "成功方案仍有追问。")
                opt, management = value.optimization, value.management
                if opt and not opt.feasible:
                    issue(p("optimization"), "inconsistent_value", "不可行方案不能标为成功。")
                if management and management.safe is not True:
                    issue(p("management.safe"), "inconsistent_value", "能量管理未通过，不能标为成功。")
                if value.data and value.data.vessel.soc_initial != value.request.soc_initial:
                    issue(p("data.vessel.soc_initial"), "inconsistent_value", "船态与请求 SOC 不一致。")
                if value.segments:
                    if value.segments[0].origin != value.request.origin or (
                        value.segments[-1].destination != value.request.destination
                    ):
                        issue(p("segments"), "inconsistent_value", "航段起终点与任务不一致。")
                    if any(a.destination != b.origin for a, b in zip(value.segments, value.segments[1:])):
                        issue(p("segments"), "inconsistent_value", "航段不连续。")
                if opt:
                    if [s.segment_id for s in value.segments] != [e.segment_id for e in opt.energy_results]:
                        issue(p("optimization.energy_results"), "inconsistent_value", "选定结果与航段顺序不一致。")
                    if opt.eta is not None:
                        departure = timestamp(value.request.departure_at, p("request.departure_at"))
                        eta = timestamp(opt.eta, p("optimization.eta"))
                        if departure is None:
                            issue(p("request.departure_at"), "missing", "ETA 必须有真实出发时间。")
                        elif eta and opt.total_duration_h is not None:
                            if abs((eta - departure).total_seconds() - opt.total_duration_h * 3600) > 1:
                                issue(p("optimization.eta"), "inconsistent_value", "ETA 与耗时不一致。")
                    for segment, energy in zip(value.segments, opt.energy_results):
                        if segment.max_speed_kmh is not None and energy.speed_kmh > segment.max_speed_kmh:
                            issue(p("optimization.energy_results"), "inconsistent_value", "航速超过航段限速。")
                        if segment.min_speed_kmh is not None and energy.speed_kmh < segment.min_speed_kmh:
                            issue(p("optimization.energy_results"), "inconsistent_value", "航速低于航段最低限制。")
                        if energy.speed_kmh > 0 and segment.waiting_h is not None:
                            expected_h = segment.distance_km / energy.speed_kmh + segment.waiting_h
                            if not isclose(energy.duration_h, expected_h, rel_tol=1e-6, abs_tol=1e-6):
                                issue(p("optimization.energy_results"), "inconsistent_value", "耗时与距离、航速、等待时间不一致。")
                        if value.data and value.data.vessel.max_power_kw is not None:
                            if energy.peak_power_kw is None:
                                issue(p("optimization.energy_results.peak_power_kw"), "missing", "功率约束需有峰值功率依据。")
                            elif energy.peak_power_kw > value.data.vessel.max_power_kw:
                                issue(p("optimization.energy_results.peak_power_kw"), "inconsistent_value", "峰值功率超过船舶限制。")
                    if opt.total_duration_h is not None:
                        if value.request.max_duration_h is not None and opt.total_duration_h > value.request.max_duration_h:
                            issue(p("optimization.total_duration_h"), "inconsistent_value", "耗时超过用户时限。")
                        departure = timestamp(value.request.departure_at, p("request.departure_at"))
                        deadline = timestamp(value.request.arrival_deadline, p("request.arrival_deadline"))
                        if departure and opt.eta is None:
                            issue(p("optimization.eta"), "missing", "已有出发时间时需给出 ETA。")
                        if departure and deadline and opt.total_duration_h * 3600 > (deadline - departure).total_seconds():
                            issue(p("optimization.total_duration_h"), "inconsistent_value", "方案无法满足到达截止时间。")
                if management:
                    if management.soc_initial != value.request.soc_initial:
                        issue(p("management.soc_initial"), "inconsistent_value", "能量管理与请求 SOC 不一致。")
                    if [s.segment_id for s in value.segments] != [s.segment_id for s in management.soc_trajectory]:
                        issue(p("management.soc_trajectory"), "inconsistent_value", "SOC 轨迹与航段顺序不一致。")
                    if value.data:
                        for name in ("capacity_kwh", "soc_min"):
                            if getattr(management, name) != getattr(value.data.vessel, name):
                                issue(p("management." + name), "inconsistent_value", "能量管理与采用船态参数不一致。")
                if opt and management and management.required_energy_kwh is not None:
                    expected = opt.total_energy_kwh
                    if opt.energy_scope == "propulsion":
                        if management.auxiliary_energy_kwh is None:
                            issue(p("management.auxiliary_energy_kwh"), "missing", "推进能耗口径需另计辅助负载。")
                        elif expected is not None:
                            expected += management.auxiliary_energy_kwh
                    if expected is not None and not isclose(
                        expected, management.required_energy_kwh, rel_tol=1e-6, abs_tol=1e-6
                    ):
                        issue(p("management.required_energy_kwh"), "inconsistent_value", "优化与能量预算口径不一致。")
        if is_dataclass(value):
            for definition in fields(value):
                walk(getattr(value, definition.name), p(definition.name))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")
        elif isinstance(value, dict):
            for name, item in value.items():
                walk(item, p(str(name)))

    walk(model)
    return _result(issues)


def require_valid(model: T) -> T:
    result = validate(model)
    if not result.valid:
        raise SchemaValidationError(result)
    return model


def parse_request(data: dict[str, Any]) -> tuple[VoyageRequest | None, ValidationResult]:
    """Malformed JSON structure returns None; partial requests retain their values."""
    try:
        request = VoyageRequest.from_dict(data)
    except SchemaValidationError as exc:
        return None, exc.result
    return request, validate(request)
