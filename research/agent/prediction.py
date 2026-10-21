"""Standalone analytic prediction; no search or voyage feasibility assertion."""
from __future__ import annotations

import math

from research.contracts import CandidateTrajectory, EnergyBatchRequest, TrajectoryPoint
from research.demand import build_power_demand, DemandBuilderSpec, AuxiliaryModel, ConversionModel
from research.tspeed.predictor import AnalyticPowerPredictor, EvaluationGrid
from research.tspeed.trajectory import expand_trajectory
from research.agent.environment import RiverPowerPredictor


def missing_prediction_fields(request, backend):
    if request.get("trajectory") or backend == "lstm":
        return []
    if request.get("prediction_duration_s") is not None:
        return ["current_speed_kmh"] if request.get("current_speed_kmh") is None else []
    return [key for key in ("origin", "destination") if not request.get(key)]


def analytic_energy(tools, state, call_id):
    data = tools.payload(state, "Tdata")
    cfg, req = data["config"], state["request"]
    from research.agent.professional import source
    demand_spec = DemandBuilderSpec(builder_id="agent-demand/1",
        conversion=None if cfg["conversion_efficiency"] is None else ConversionModel(
            model_id="simulation-conversion",kind="constant_efficiency",
            efficiency=cfg["conversion_efficiency"],source_ref=source("conversion")),
        reason="选定情景缺少转换依据" if cfg["conversion_efficiency"] is None else None,
        auxiliary=AuxiliaryModel(model_id="simulation-auxiliary",sailing_kw=cfg["auxiliary_kw"],
            waiting_kw=cfg["auxiliary_kw"],berthing_kw=cfg["auxiliary_kw"],source_ref=source("auxiliary")))
    missing = missing_prediction_fields(req, "analytic")
    def reject(code, fields, detail):
        return tools.save(state, "Tenergy", {"backend":"analytic", "diagnostics":[
            {"code":code,"field_ref":key,"detail":detail} for key in fields]}, "needs_input")
    if missing:
        return reject("prediction_input_missing", missing, "请提供预测航段起终点，或显式时间轨迹。")
    segment_ref = None
    try:
        route_conditions = None
        if req.get("trajectory") is not None:
            points = tuple(TrajectoryPoint(**p) for p in req["trajectory"])
            distances = {}
            for p in points:
                if p.phase == "sailing" and p.planned_speed_kmh is None:
                    return reject("prediction_input_missing", ["trajectory.planned_speed_kmh"], "航行区间须提供航速。")
                if p.planned_speed_kmh is not None:
                    speed = p.planned_speed_kmh
                    if speed > cfg["max_speed_kmh"] or (p.phase != "sailing" and speed != 0) or (p.phase == "sailing" and speed == 0):
                        raise ValueError("trajectory_speed_outside_scenario")
                distances[p.segment_id] = distances.get(p.segment_id, 0) + (p.planned_speed_kmh or 0)*p.duration_s/3600
        elif req.get("prediction_duration_s") is not None:
            from research.agent.professional import finite
            duration = finite(req["prediction_duration_s"], 0.001, 7*86400)
            speed = cfg["current_speed_kmh"]
            if speed <= 0:
                return reject("prediction_speed_missing", ["current_speed_kmh"], "航行预测须提供正航速；静止应使用等待阶段轨迹。")
            points = (TrajectoryPoint("prediction-duration", "sailing", 0, duration, speed),)
            distances = {"prediction-duration":speed*duration/3600}
        else:
            # Resolve the registered continuous route inside this Tenergy call.
            resolved = tools.segments(state, call_id)
            if resolved["outcome_code"] != "ok":
                return reject("route_not_registered", ["origin","destination"], "请提供已登记的连续航段起终点。")
            segment_ref = resolved["artifact_ref"]
            route_conditions = tools.store.read(state["task_id"], segment_ref)["payload"]["route_environment"]
            local = {**state,"refs":{**state["refs"],"Tseg":segment_ref}}
            request, _, _ = tools.context(local, call_id)
            speed = cfg["current_speed_kmh"]
            if not cfg["min_speed_kmh"] <= speed <= cfg["max_speed_kmh"]:
                return reject("prediction_speed_missing", ["current_speed_kmh"], "航段预测须给出情景限速内的巡航航速。")
            expanded = expand_trajectory(tuple(speed for _ in request.segments), request=request, max_step_s=5)
            if expanded.violations or not expanded.distance_consistent:
                raise ValueError("prediction_trajectory_invalid")
            points = tuple(TrajectoryPoint(segment, interval.phase, interval.start_offset_s,
                interval.duration_s, interval.planned_speed_kmh) for segment, interval in expanded.ordered_intervals())
            distances = {s.segment_id:s.distance_km for s in request.segments}
        candidate = CandidateTrajectory("standalone", points)
        if candidate.time_gaps() or points[0].start_offset_s != 0 or candidate.total_duration_s > 7*86400:
            raise ValueError("trajectory_time_axis_invalid_or_over_budget")
        predictor = RiverPowerPredictor(coefficient=cfg["coefficient"], environment=cfg["environment"],
            segments=route_conditions, allow_unlocated=segment_ref is None)
    except (ValueError, TypeError, KeyError) as exc:
        return tools.save(state,"Tenergy",{"backend":"analytic","diagnostics":[{
            "code":"invalid_prediction_trajectory","field_ref":"trajectory","detail":str(exc)}]},"invalid_input")
    environment_ref = tools.store.put(state["task_id"], "prediction_environment", {
        "snapshot_id":data["snapshot_id"],"environment":cfg["environment"],
        "coefficient":cfg["coefficient"],"source":data["field_sources"]["coefficient"]})
    raw = EnergyBatchRequest(candidates=(candidate,), history_ref=state["refs"]["Tdata"],
        environment_scenario_ref=environment_ref, model_version=predictor.model_version,
        integration_rule=predictor.integration_rule, task_id=state["task_id"],
        request_revision=state["revision"],snapshot_id=data["snapshot_id"],
        scenario_id=state["scenario_id"],call_id=call_id)
    result = predictor.predict(raw,grid=EvaluationGrid(5,5,distances))
    entry = result.results[0]
    power_ref = tools.store.put(state["task_id"],"prediction_power_series",entry.to_dict())
    if entry.outcome_code != "ok":
        return tools.save(state, "Tenergy", {"backend": "analytic", "result": result.to_dict(),
            "power_series_ref": power_ref, "environment_ref": environment_ref,
            "diagnostics": [d.to_dict() for d in entry.diagnostics]}, entry.outcome_code)
    demand = build_power_demand(entry,spec=demand_spec,series_id="standalone",
                               input_energy_ref=power_ref)
    demand_ref = tools.store.put(state["task_id"],"prediction_demand",demand.to_dict())
    summary = {"backend":"analytic","model_version":predictor.model_version,
        "duration_s":candidate.total_duration_s,"distance_km":sum(distances.values()),
        "motor_energy_kwh":entry.motor_energy_kwh,"bus_energy_kwh":demand.demand_energy_kwh(),
        "usage":"simulation_prediction_only"}
    return tools.save(state,"Tenergy",{"backend":"analytic","snapshot_id":data["snapshot_id"],
        "request":raw.to_dict(),"result":result.to_dict(),"power_series_ref":power_ref,
        "demand_ref":demand_ref,"segment_ref":segment_ref,"environment_ref":environment_ref,
        "summary":summary,"units":{"power":"kW","energy":"kWh","time":"s","speed":"km/h"},
        "time_range_s":[0,candidate.total_duration_s],"integration_rule":predictor.integration_rule,
        "sources":{"snapshot_ref":state["refs"]["Tdata"],"trajectory":"user" if req.get("trajectory") or req.get("prediction_duration_s") is not None else "named_simulation"},
        "diagnostics":[d.to_dict() for d in (*entry.diagnostics,*demand.diagnostics)]},entry.outcome_code)


def verify_prediction(tools, state):
    """Read hashed child artifacts and independently recompute the motor integral."""
    body = tools.payload(state,"Tenergy")
    if body["outcome_code"] != "ok":
        raise ValueError("prediction_not_completed")
    data = tools.payload(state,"Tdata")
    if body["snapshot_id"] != data["snapshot_id"]:
        raise ValueError("prediction_snapshot_mismatch")
    power = tools.store.read(state["task_id"],body["power_series_ref"])["payload"]
    points = power["power_points"] if body["backend"] == "analytic" else power["points"]
    power_key = "predicted_motor_power_kw"
    energy = sum(p[power_key]*p["duration_s"]/3600 for p in points)
    offset = 0.0
    for p in points:
        if not math.isclose(p["start_offset_s"],offset,abs_tol=1e-6) or p["duration_s"] <= 0:
            raise ValueError("prediction_time_axis_mismatch")
        if not math.isclose(p["motor_energy_kwh"],p[power_key]*p["duration_s"]/3600,abs_tol=1e-9):
            raise ValueError("prediction_interval_integral_mismatch")
        offset += p["duration_s"]
    if not math.isclose(offset,body["summary"]["duration_s"],abs_tol=1e-6):
        raise ValueError("prediction_duration_mismatch")
    if not math.isclose(energy,body["summary"]["motor_energy_kwh"],rel_tol=1e-7,abs_tol=1e-9):
        raise ValueError("prediction_integral_mismatch")
    if body["backend"] == "analytic":
        environment = tools.store.read(state["task_id"],body["environment_ref"])["payload"]
        if environment["snapshot_id"] != data["snapshot_id"]:
            raise ValueError("prediction_environment_mismatch")
        if body.get("segment_ref"):
            tools.store.read(state["task_id"],body["segment_ref"])
        demand = tools.store.read(state["task_id"],body["demand_ref"])["payload"]
        if demand["bus_side_available"]:
            bus_energy = sum(p["demand_bus_kw"]*p["duration_s"]/3600 for p in demand["points"])
            if not math.isclose(bus_energy,body["summary"]["bus_energy_kwh"],rel_tol=1e-7,abs_tol=1e-9):
                raise ValueError("prediction_demand_mismatch")
    else:
        from tools.tenergy_v2 import JsonArtifactStore
        store = JsonArtifactStore(tools.store.directory/"energy"/state["task_id"])
        history = tools.store.read(state["task_id"],body["history_artifact_ref"])["payload"]
        if history != store.get(body["request"]["history_ref"],kind="history"):
            raise ValueError("prediction_history_mismatch")
        store.get(body["result"]["candidates"][0]["power_series_ref"],kind="power_series")
    return body["summary"]
