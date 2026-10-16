"""Five professional-tool adapters using named simulation inputs and real solvers."""
from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

from research.agent.store import digest
from research.contracts import SourceRef, TimeGridSpec
from research.contracts.speed_optimization import SegmentConstraint, SpeedOptimizationRequest
from research.demand import AuxiliaryModel, ConversionModel, DemandBuilderSpec
from research.demand.artifact_store import DemandArtifactStore
from research.demand.fdp_bridge import prepare_fdp_request
from research.tspeed.eval import EnergyModelSpec, DEEvaluationContext, evaluate_candidate
from research.tspeed.predictor import AnalyticPowerPredictor, EvaluationGrid
from research.tspeed.search import DEConfig
from research.tspeed.tool import SimulationScenario, run_speed_optimization
from schemas.research_management import BatteryManagementRequest, BatteryParameters, FDPGridConfig
from schemas.types import SourceRef as LegacySource
from tools.fdp_verifier import verify_trajectory
from tools.tmanagement_fdp import run_management_fdp, DEFAULT_TRANSITIONS

ROOT = Path(__file__).resolve().parents[2]


def configuration(scenario_id):
    raw = json.loads((ROOT / "configs/research_agent.json").read_text(encoding="utf-8"))
    if scenario_id not in raw["scenarios"]:
        raise ValueError("unknown_scenario")
    return {**raw, **raw["scenarios"][scenario_id], "scenario_id": scenario_id}


def source(field):
    return SourceRef("simulation:" + field, "assumption", "configs/research_agent.json",
                     confirmed=False, mode="simulation", note="具名软件仿真值")


def finite(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError("invalid_simulation_value")
    return value


class ProfessionalTools:
    def __init__(self, store):
        self.store = store

    def payload(self, state, name):
        data = self.store.read(state["task_id"], state["refs"][name])["payload"]
        if data["request_revision"] != state["revision"] or data["scenario_id"] != state["scenario_id"]:
            raise ValueError("result_version_mismatch")
        return data

    def save(self, state, name, payload, code="ok", summary=None):
        full = {"request_revision": state["revision"], "scenario_id": state["scenario_id"],
                "outcome_code": code, **payload}
        ref = self.store.put(state["task_id"], name, full)
        return {"outcome_code": code, "artifact_ref": ref, "summary": summary or full.get("summary", {}),
                "diagnostics": full.get("diagnostics", [])}

    def data(self, state, call_id):
        cfg = configuration(state["scenario_id"])
        batteries = {}
        for i in (1, 2):
            values = dict(cfg["battery"])
            key = f"initial_soc_{i}"
            if state["request"].get(key) is not None:
                try:
                    values["initial_soc"] = finite(state["request"][key], 0, 1)
                except ValueError:
                    return self.save(state,"Tdata",{"diagnostics":[{
                        "code":"invalid_initial_soc","field_ref":key,
                        "detail":"初始 SOC 须为 0 到 1 的比例；百分数请明确写出 %。"}]},"invalid_input")
            if "battery_power_kw" in cfg:
                values.update(min_power_kw=0, max_power_kw=cfg["battery_power_kw"])
            values["sources"] = {k: source(f"battery_{i}.{k}").to_dict() for k in values}
            if state["request"].get(key) is not None:
                combined=bool(state["request"].get("soc_applied_to_both"))
                values["sources"]["initial_soc"] = SourceRef(
                    "user:overall_soc" if combined else "user:" + key,
                    "assumption" if combined else "user",confirmed=not combined,mode="simulation",
                    note="未区分双电池，按相同初始 SOC 作研究仿真" if combined else "用户指定仿真初态").to_dict()
            batteries[f"battery_{i}"] = values
        current = state["request"].get("current_speed_kmh")
        if current is not None:
            try:
                cfg["current_speed_kmh"] = finite(current, 0, cfg["max_speed_kmh"])
            except ValueError:
                return self.save(state,"Tdata",{"diagnostics":[{
                    "code":"invalid_current_speed","field_ref":"current_speed_kmh",
                    "detail":"当前航速须为选定情景限速范围内的有限数值。"}]},"invalid_input")
        body = {"config": cfg, "batteries": batteries, "source_mode": "simulation",
                "assumptions": [cfg["assumption"]], "call_id": call_id}
        body["field_sources"]={key:source(key).to_dict() for key in
            ("current_speed_kmh","environment","conversion_efficiency","auxiliary_kw","coefficient")}
        if current is not None:
            body["field_sources"]["current_speed_kmh"]=SourceRef("user:current_speed_kmh","user",
                confirmed=True,mode="simulation",note="用户指定仿真初态").to_dict()
        body["snapshot_id"] = digest({"task": state["task_id"], "revision": state["revision"], **body})
        return self.save(state, "Tdata", body, summary={"snapshot_id": body["snapshot_id"],
            "batteries": batteries, "source_mode": "simulation", "assumptions": body["assumptions"],
            "current_speed_kmh":cfg["current_speed_kmh"], "environment":cfg["environment"],
            "conversion_efficiency":cfg["conversion_efficiency"], "auxiliary_kw":cfg["auxiliary_kw"],
            "field_sources":body["field_sources"]})

    def segments(self, state, call_id):
        data = self.payload(state, "Tdata")
        cfg, req = data["config"], state["request"]
        aliases = json.loads((ROOT / "configs/aliases.yaml").read_text(encoding="utf-8"))["aliases"]
        routes = json.loads((ROOT / "configs/route_facts.yaml").read_text(encoding="utf-8"))
        origin = aliases.get(req.get("origin"), {}).get("canonical", req.get("origin"))
        dest = aliases.get(req.get("destination"), {}).get("canonical", req.get("destination"))
        selected = None
        if cfg.get("synthetic_route"):
            selected = [{"id": f"simulation-leg-{i}", "origin": a, "destination": b,
                         "distance_km": 0.25, "source": source("synthetic_route").to_dict()}
                        for i, (a, b) in enumerate((("仿真起点", "仿真中间点"), ("仿真中间点", "仿真终点")), 1)]
            if origin != "仿真起点" or dest != "仿真终点":
                selected = None
        else:
            by_id = {s["id"]: s for s in routes["segments"]}
            for route in routes["routes"].values():
                items = [by_id[i] for i in route["segment_ids"]]
                for start in range(len(items)):
                    for stop in range(start + 1, len(items) + 1):
                        candidate = items[start:stop]
                        if candidate[0]["origin"] == origin and candidate[-1]["destination"] == dest:
                            selected = candidate
                            break
                    if selected:
                        break
                if selected:
                    break
        if not selected:
            return self.save(state, "Tseg", {"diagnostics": [{"code": "route_not_registered",
                "detail": "起终点不构成登记的连续航线，请明确起终点。"}]}, "needs_input")
        for a, b in zip(selected, selected[1:]):
            if a["destination"] != b["origin"]:
                raise ValueError("route_discontinuous")
        segments = [SegmentConstraint(segment_id=s["id"], distance_km=s["distance_km"],
            min_speed_kmh=cfg["min_speed_kmh"], max_speed_kmh=cfg["max_speed_kmh"],
            origin_entity_id=s["origin"], destination_entity_id=s["destination"],
            waiting_duration_s=cfg["waiting_duration_s"], waiting_rule="具名仿真等待规则",
            phase_or_segment_type="cruise", quality_flags=("simulation",),
            source_refs=(SourceRef(**s["source"]), source("route_constraints"))).to_dict() for s in selected]
        return self.save(state, "Tseg", {"segments": segments, "snapshot_id": data["snapshot_id"],
            "call_id": call_id}, summary={"segments": segments, "distance_km": sum(s["distance_km"] for s in segments)})

    def context(self, state, call_id, segments=None, predictor=None, history=None, environment=None):
        data = self.payload(state, "Tdata")
        cfg = data["config"]
        if segments is None:
            segment_data = self.payload(state, "Tseg")
            if segment_data["snapshot_id"] != data["snapshot_id"]:
                raise ValueError("segment_snapshot_mismatch")
            segments = [SegmentConstraint(**{**s, "source_refs": tuple(SourceRef(**r) for r in s["source_refs"])})
                        for s in segment_data["segments"]]
        grid = TimeGridSpec(5, 5, 0.15, 0.20, 0, cfg["max_speed_kmh"], decelerate_to_stop_at_end=False)
        remaining = state["request"].get("remaining_time_s")
        if remaining is None:
            remaining = cfg["remaining_time_s"]
        finite(remaining, 1, 7 * 86400)
        request = SpeedOptimizationRequest(segments=tuple(segments), time_grid=grid,
            objective=state["request"].get("objective") or "min_energy",
            current_position_km=0, current_speed_kmh=cfg["current_speed_kmh"], remaining_time_s=remaining,
            history_ref=history or "simulation-history", environment_scenario_ref=environment or "simulation-environment",
            scenario_id=state["scenario_id"], snapshot_id=data["snapshot_id"], request_revision=state["revision"],
            task_id=state["task_id"], call_id=call_id, assumptions=(cfg["assumption"],))
        conversion = None if cfg["conversion_efficiency"] is None else ConversionModel(
            model_id="simulation-conversion", kind="constant_efficiency", efficiency=cfg["conversion_efficiency"],
            source_ref=source("conversion"))
        demand = DemandBuilderSpec(builder_id="agent-demand/1", conversion=conversion,
            reason="选定情景缺少转换依据" if conversion is None else None,
            auxiliary=AuxiliaryModel(model_id="simulation-auxiliary", sailing_kw=cfg["auxiliary_kw"],
                waiting_kw=cfg["auxiliary_kw"], berthing_kw=cfg["auxiliary_kw"], source_ref=source("auxiliary")))
        if predictor is None:
            store, task, parent = self.store, state["task_id"], call_id
            class ObservedPredictor(AnalyticPowerPredictor):
                def predict_profile(self, profile, **kwargs):
                    child = str(uuid4())
                    store.event(task, "tool_started", tool="Tenergy", call_id=child, parent_call_id=parent, backend="analytic")
                    start = time.monotonic()
                    result = super().predict_profile(profile, **kwargs)
                    ref = store.put(task, "Tenergy.candidate", {"candidate_id": kwargs.get("candidate_id"),
                        "profile": asdict(profile), "output": asdict(result), "backend": "analytic"})
                    store.event(task, "tool_completed", tool="Tenergy", call_id=child, parent_call_id=parent,
                        backend="analytic", artifact_ref=ref, duration_ms=round((time.monotonic()-start)*1000))
                    return result
            predictor = ObservedPredictor(coefficient=cfg["coefficient"])
        limit = None
        if state.get("feedback_limit_kw") is not None:
            limit = state["feedback_limit_kw"]
        spec = EnergyModelSpec(predictor=predictor,
            grid=EvaluationGrid(5, 5, {s.segment_id:s.distance_km for s in segments}),
            history_ref=request.history_ref, environment_ref=request.environment_scenario_ref,
            max_step_s=5, demand_spec=demand, demand_limit_kw=limit,
            model_version=predictor.model_version, integration_rule=predictor.integration_rule)
        return request, spec, data

    def speed(self, state, call_id):
        data = self.payload(state, "Tdata")
        if data["config"]["backend"] != "analytic":
            return self.save(state, "Tspeed", {"diagnostics": [{"code":"short_prediction_only"}]}, "not_supported")
        request, spec, data = self.context(state, call_id)
        if spec.demand_spec.conversion is None:
            return self.save(state, "Tspeed", {"diagnostics": [{"code":"conversion_missing"}]}, "needs_input")
        store = DemandArtifactStore(self.store.directory / "demand" / state["task_id"])
        run, result = run_speed_optimization(request, scenario=SimulationScenario(
            state["scenario_id"], spec.environment_ref, spec.history_ref, spec,
            assumptions=(data["config"]["assumption"],)),
            config=DEConfig(**data["config"]["de"]), call_id=call_id, demand_store=store)
        body = {"run":run.to_dict(), "result":result.to_dict(), "snapshot_id":data["snapshot_id"],
                "request":request.to_dict(), "diagnostics":[d.to_dict() for d in run.diagnostics]}
        if run.ok:
            series = store.get(result.selected_demand_ref)
            demand_ref = self.store.put(state["task_id"], "PowerDemandSeries", series.to_dict())
            body["demand_artifact_ref"] = demand_ref
            batteries = {}
            for name, values in data["batteries"].items():
                batteries[name] = BatteryParameters(battery_id=name, **{k:v for k,v in values.items() if k!="sources"},
                    sources={k:LegacySource(v["source_id"],v["kind"],v.get("locator"),v.get("confirmed",False),v.get("note"))
                             for k,v in values["sources"].items()})
            template = BatteryManagementRequest(schema_version="research-tools/2.0-draft",task_id=state["task_id"],
                request_revision=state["revision"], snapshot_id=data["snapshot_id"],scenario_id=state["scenario_id"],
                call_id=str(uuid4()), selected_candidate_id=result.selected_candidate_id,
                selected_demand_ref=result.selected_demand_ref,demand_series=[],**batteries,
                load_rate_reference_kw=100, load_rate_reference_source=LegacySource("simulation:load_reference","assumption"),
                allowed_mode_transitions=DEFAULT_TRANSITIONS,
                allowed_mode_transitions_source=LegacySource("simulation:mode_transitions","assumption"),
                grid=FDPGridConfig(**data["config"]["fdp"]),
                assumptions=[{"assumption_id":"agent-fixture","value":data["config"]["assumption"]}])
            fdp = prepare_fdp_request(speed_result=result, speed_run=run, demand_store=store, template=template,
                                     conversion_source=source("conversion"), auxiliary_source=source("auxiliary"))
            body["fdp_request"] = fdp.to_dict()
            body["summary"] = {"duration_s":result.total_duration_s,"distance_km":result.total_distance_km,
                "bus_energy_kwh":series.demand_energy_kwh(),"motor_energy_kwh":result.total_energy_kwh,
                "selected_demand_ref":result.selected_demand_ref,"demand_artifact_ref":demand_ref,
                "speed_vector_kmh":list(result.selected_speed_vector),"backend":"analytic"}
        return self.save(state,"Tspeed",body,run.outcome_code)

    def management(self,state,call_id):
        speed = self.payload(state,"Tspeed")
        data = self.payload(state,"Tdata")
        if speed["snapshot_id"] != data["snapshot_id"]:
            raise ValueError("snapshot_mismatch")
        req = BatteryManagementRequest.from_dict(speed["fdp_request"])
        demand = DemandArtifactStore(self.store.directory / "demand" / state["task_id"]).get(req.selected_demand_ref)
        exported = self.store.read(state["task_id"], speed["demand_artifact_ref"])["payload"]
        if digest(demand.to_dict()) != digest(exported):
            raise ValueError("demand_changed")
        req = replace(req,call_id=call_id,parent_call_id=speed["run"]["call_id"])
        result = run_management_fdp(req)
        payload = result.get("payload") or {}
        summary = {**speed.get("summary",{}), **payload.get("summary",{}),
                   "safe":payload.get("safe",False),"backend":"analytic"}
        saved = self.save(state,"Tmanagement", {"request":req.to_dict(),"result":result,
            "speed_ref":state["refs"]["Tspeed"],"snapshot_id":data["snapshot_id"],"summary":summary,
            "diagnostics":result.get("diagnostics",[])},result["outcome_code"])
        if payload.get("feedback"):
            saved["feedback"] = payload["feedback"]
        return saved

    def verify(self,state):
        speed = self.payload(state,"Tspeed")
        management = self.payload(state,"Tmanagement")
        data = self.payload(state,"Tdata")
        if management["speed_ref"] != state["refs"]["Tspeed"] or management["snapshot_id"] != data["snapshot_id"]:
            raise ValueError("plan_version_mismatch")
        req = BatteryManagementRequest.from_dict(management["request"])
        demand = DemandArtifactStore(self.store.directory / "demand" / state["task_id"]).get(req.selected_demand_ref)
        payload = management["result"]["payload"]
        check = verify_trajectory(req,payload["rows"],payload["cost_terms"])
        durations = sum(p.duration_s for p in demand.points)
        segments = self.payload(state,"Tseg")["segments"]
        checks = [check["passed"], all(x["passed"] for x in speed["result"]["constraint_checks"]),
            math.isclose(durations,speed["result"]["total_duration_s"],abs_tol=1e-6),
            math.isclose(sum(s["distance_km"] for s in segments),speed["result"]["total_distance_km"],abs_tol=1e-6),
            math.isclose(demand.demand_energy_kwh(),payload["summary"]["demand_bus_energy_kwh"],abs_tol=1e-6)]
        ref = self.store.put(state["task_id"],"plan_verification",{"passed":all(checks),"checks":checks,
            "fdp":check,"speed_ref":state["refs"]["Tspeed"],"management_ref":state["refs"]["Tmanagement"]})
        if not all(checks):
            raise ValueError("independent_verification_failed")
        return ref,management["summary"]

    def energy(self,state,call_id):
        from tools.tenergy_v2 import JsonArtifactStore, TenergyV2
        from research.tspeed.tenergy_v2_bridge import TenergyV2Predictor
        data = self.payload(state,"Tdata")
        cfg = data["config"]
        if cfg["backend"] != "lstm":
            return self.save(state,"Tenergy",{"diagnostics":[{"code":"select_lstm_scenario"}]},"not_supported")
        energy_store = JsonArtifactStore(self.store.directory/"energy"/state["task_id"])
        fixture_dir=ROOT/"configs/examples/tenergy_v2"
        history = json.loads((fixture_dir/"history.json").read_text(encoding="utf-8"))
        raw = json.loads((fixture_dir/"request.json").read_text(encoding="utf-8"))
        history["snapshot_id"] = data["snapshot_id"]
        raw.update(task_id=state["task_id"],request_revision=state["revision"],snapshot_id=data["snapshot_id"],
            scenario_id=state["scenario_id"],call_id=call_id,parent_call_id=None,history_ref=energy_store.put(history,"history"))
        duration=cfg["duration_s"]
        raw["candidates"]=[{"candidate_id":"short-fixed","trajectory":[{
            "segment_id":"lstm-short","phase":"moving","start_offset_s":i*5,"duration_s":5,
            "planned_speed_kmh":10.0,"environment_ref":None} for i in range(int(duration/5))]}]
        predictor=TenergyV2(ROOT/"models/tenergy-lstm-v5",energy_store)
        run=predictor.evaluate_batch(raw)
        result=energy_store.get(run["payload_ref"],kind="energy_batch_result") if run.get("payload_ref") else None
        entry=result["candidates"][0] if result else {}
        code=entry.get("outcome_code",run["outcome_code"])
        body={"request":raw,"run":run,"result":result,"backend":"lstm", "diagnostics":entry.get("diagnostics",[]),
              "summary":{"backend":"lstm","duration_s":duration,"usage":"research_only"}}
        if not body["diagnostics"]:
            body["diagnostics"] = run.get("diagnostics", [])
        if code=="ok":
            ref=entry["power_series_ref"]
            body["power_series"]=energy_store.get(ref,kind="power_series")
            bridge=TenergyV2Predictor(predictor,energy_store,history_ref=raw["history_ref"],environment_ref=None,
                snapshot_id=data["snapshot_id"],scenario_id=state["scenario_id"],model_version=raw["model_version"],
                task_id=state["task_id"])
            seg=SegmentConstraint("lstm-short",10*duration/3600,10,10,source_refs=(source("lstm_short"),))
            req,spec,_=self.context(state,call_id,segments=[seg],predictor=bridge,history=bridge.history_id,environment=bridge.environment_id)
            req=replace(req,current_speed_kmh=10)
            child=str(uuid4())
            self.store.event(state["task_id"],"tool_started",tool="Tenergy",call_id=child,parent_call_id=call_id,backend="lstm",purpose="Tspeed evaluator")
            context=DEEvaluationContext(request=req,spec=spec,environment_ref=bridge.environment_id,
                history_ref=bridge.history_id,config_hash=spec.config_hash() if hasattr(spec,"config_hash") else digest(raw))
            evaluated=evaluate_candidate((10.0,),context=context)
            if not evaluated.outcome.ok or not evaluated.demand or not evaluated.demand.bus_side_available:
                raise ValueError("lstm_speed_handoff_failed")
            body["demand"]=evaluated.demand.to_dict()
            body["summary"].update(motor_energy_kwh=evaluated.outcome.motor_energy_kwh,
                bus_energy_kwh=evaluated.demand.demand_energy_kwh(),tspeed_handoff_verified=True)
            self.store.event(state["task_id"],"tool_completed",tool="Tenergy",call_id=child,parent_call_id=call_id,backend="lstm")
        return self.save(state,"Tenergy",body,code)
