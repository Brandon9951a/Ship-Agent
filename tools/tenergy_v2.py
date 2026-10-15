"""Independent Tenergy v2 draft adapter with immutable local JSON artifacts.

No bus conversion, optimizer, FDP, or shared-contract ratification occurs here.
"""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from schemas.tenergy_v2 import EnergyBatchRequest, ToolRun, VERSION, finite, valid_ref
from tools.lstm_energy import FEATURES, HISTORY, HORIZON, evaluate, load_model

ADAPTER_VERSION = "tenergy-v2-draft/1"
PREPROCESSING_VERSION = "tenergy-clean-v1"


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def diagnostic(code, *, field=None, interval=None, detail="", observed=None, limit=None, source=None):
    return {"stage":"Tenergy", "code":code, "time_interval_or_segment_id":interval,
            "field_ref":field, "observed":observed, "limit":limit, "unit":None,
            "source_refs":[] if source is None else [source], "retryable":False, "detail":detail}


class JsonArtifactStore:
    """Content-addressed files. References never contain caller-controlled paths."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def put(self, payload, kind):
        if not isinstance(payload,dict):
            raise ValueError("Artifact payload must be an object")
        document = {"schema_version":VERSION, "kind":kind, "payload":payload}
        data = canonical_bytes(document)
        sha = hashlib.sha256(data).hexdigest()
        path = self.directory / f"{sha}.json"
        try:
            with path.open("xb") as stream:
                stream.write(data)
        except FileExistsError:
            if path.read_bytes() != data:
                raise ValueError("Existing artifact does not match its content hash")
        rows = len(payload.get("points", payload.get("candidates", [])))
        return {"artifact_id":sha,"sha256":sha,"row_count":rows,"schema_version":VERSION}

    def get(self, ref, *, kind=None):
        if not isinstance(ref, dict) or set(ref) != {"artifact_id","sha256","row_count","schema_version"}:
            raise ValueError("Malformed ArtifactRef")
        if (ref["artifact_id"] != ref["sha256"] or not isinstance(ref["sha256"], str) or
                re.fullmatch(r"[a-f0-9]{64}", ref["sha256"]) is None or
                ref["schema_version"] != VERSION or type(ref["row_count"]) is not int):
            raise ValueError("Invalid artifact identity/version")
        data = (self.directory / f"{ref['sha256']}.json").read_bytes()
        if hashlib.sha256(data).hexdigest() != ref["sha256"]:
            raise ValueError("Artifact hash mismatch")
        document = json.loads(data)
        payload = document["payload"]
        if not isinstance(payload,dict):
            raise ValueError("Artifact payload must be an object")
        if document["schema_version"] != VERSION or (kind and document["kind"] != kind):
            raise ValueError("Artifact kind/version mismatch")
        if len(payload.get("points", payload.get("candidates", []))) != ref["row_count"]:
            raise ValueError("Artifact row_count mismatch")
        return payload


def _source(payload):
    source = payload.get("source_ref")
    return (isinstance(source, dict) and isinstance(source.get("source_id"), str) and
            bool(source["source_id"].strip()) and source.get("kind") in
            ("measurement","assumption","document","user","tool"))


def _history(payload, extension):
    if not _source(payload):
        return None, diagnostic("history_source_missing",field="history_ref.source_ref")
    points = payload.get("points")
    if not isinstance(points,list) or len(points) < HISTORY:
        return None, diagnostic("missing_history",field="history_ref.points")
    points = points[-HISTORY:]
    fields = ("observed_offset_s","speed_kmh","motor_power_kw","dt_seconds")
    if any(not isinstance(p,dict) or any(not finite(p.get(f)) for f in fields) for p in points):
        return None, diagnostic("invalid_history",field="history_ref.points")
    if any(p["observed_offset_s"] > 0 or p["speed_kmh"] <= 0 or p["motor_power_kw"] <= 0 or
           not 4 <= p["dt_seconds"] <= 6 for p in points):
        return None, diagnostic("invalid_history",field="history_ref.points",detail="Future observations and nonmoving histories are forbidden")
    if abs(points[-1]["observed_offset_s"]) > 1e-6 or any(
        abs(b["observed_offset_s"]-a["observed_offset_s"]-b["dt_seconds"]) > 1e-6
        for a,b in zip(points,points[1:])):
        return None, diagnostic("history_time_discontinuity",field="history_ref.points")
    if extension and any(any(not finite(p.get(f)) for f in FEATURES[2:]) for p in points):
        return None, diagnostic("missing_history_environment",field="history_ref.points")
    return points, None


def _trajectory(candidate):
    rows = candidate.get("trajectory")
    if set(candidate) != {"candidate_id","trajectory"} or not isinstance(rows,list) or not rows:
        return None, diagnostic("invalid_trajectory",field="trajectory")
    end = 0.0
    for i,p in enumerate(rows):
        allowed = {"segment_id","phase","start_offset_s","duration_s","planned_speed_kmh","environment_ref"}
        if not isinstance(p,dict) or set(p) != allowed or not isinstance(p["segment_id"],str) or not p["segment_id"].strip():
            return None, diagnostic("invalid_interval",field=f"trajectory[{i}]")
        if p["environment_ref"] is not None and not valid_ref(p["environment_ref"]):
            return None, diagnostic("invalid_environment_ref",field=f"trajectory[{i}].environment_ref")
        if (p["phase"] not in ("moving","waiting","stopped") or not finite(p["start_offset_s"]) or
                not finite(p["duration_s"]) or p["duration_s"] <= 0 or abs(p["start_offset_s"]-end) > 1e-6 or
                not finite(p["planned_speed_kmh"]) or p["planned_speed_kmh"] < 0):
            return None, diagnostic("invalid_interval",field=f"trajectory[{i}]",detail="Intervals must be contiguous from zero")
        if (p["phase"] == "moving") != (p["planned_speed_kmh"] > 0):
            return None, diagnostic("phase_speed_mismatch",field=f"trajectory[{i}]")
        end += p["duration_s"]
    return rows,None


class TenergyV2:
    def __init__(self, model_directory, artifact_store):
        self.model_directory = Path(model_directory)
        self.store = artifact_store
        self.bundle = None
        self.model_identity = None
        self.domain = None

    def _load(self):
        if self.bundle is None:
            hashes = {name:hashlib.sha256((self.model_directory/name).read_bytes()).hexdigest()
                      for name in ("weights.pt","scaler.json","manifest.json","inference_domain.json")}
            bundle = load_model(self.model_directory)
            domain = json.loads((self.model_directory/"inference_domain.json").read_text(encoding="utf-8"))
            if any(domain["model_files_sha256"].get(name)!=hashes[name]
                   for name in ("weights.pt","scaler.json","manifest.json")):
                raise ValueError("Domain/model hash mismatch")
            if any(hashlib.sha256((self.model_directory/name).read_bytes()).hexdigest() != sha
                   for name,sha in hashes.items()):
                raise ValueError("Model files changed during load")
            self.bundle,self.domain = bundle,domain
            self.model_identity = {"model_version":self.bundle.manifest["version"],
                                   "file_sha256":hashes,"preprocessing_version":PREPROCESSING_VERSION,
                                   "source_sha256":self.bundle.manifest["source_sha256"]}

    def evaluate_batch(self, request):
        started = datetime.now(timezone.utc).isoformat()
        raw = request.to_dict() if isinstance(request,EnergyBatchRequest) else request
        # Canonical JSON rejects NaN and unsupported Python objects before any model work.
        try:
            input_hash = digest(raw)
        except (ValueError,TypeError):
            input_hash = hashlib.sha256(b"invalid-json-input").hexdigest()
        metadata = raw if isinstance(raw,dict) else {}
        metadata = {k:v for k,v in metadata.items() if (isinstance(v,str) or v is None or type(v) is int)}
        def finish(code, payload=None, diagnostics=None, failed=False):
            try:
                ref = self.store.put(payload,"energy_batch_result") if payload is not None else None
            except (OSError,ValueError) as exc:
                ref,code,failed = None,"tool_error",True
                diagnostics = [diagnostic("output_artifact_failed",detail=type(exc).__name__)]
            return ToolRun(VERSION,"Tenergy",metadata.get("task_id"),metadata.get("call_id"),
                           metadata.get("parent_call_id"),input_hash,metadata.get("request_revision"),
                           metadata.get("snapshot_id"),metadata.get("scenario_id"),started,
                           datetime.now(timezone.utc).isoformat(),"failed" if failed else "completed",
                           code,ref,diagnostics or [],metadata.get("model_version"),
                           digest({"adapter":ADAPTER_VERSION,"integration":"right_endpoint",
                                   "model":self.model_identity})).to_dict()
        try:
            req = EnergyBatchRequest.from_dict(raw)
            canonical_bytes(raw)
        except (ValueError,TypeError) as exc:
            return finish("invalid_input",diagnostics=[diagnostic("invalid_request",detail=str(exc))])
        try:
            request_ref = self.store.put(raw,"energy_batch_request")
        except (OSError,ValueError) as exc:
            return finish("tool_error",diagnostics=[diagnostic("input_artifact_failed",detail=type(exc).__name__)],failed=True)
        context = {"schema_version":VERSION,"input_request_ref":request_ref,
                   "task_id":req.task_id,"request_revision":req.request_revision,
                   "snapshot_id":req.snapshot_id,"scenario_id":req.scenario_id}
        def reject_all(code,issue,failed=False):
            return finish(code,{**context,"model_identity":self.model_identity,
                "usage":"research_only" if req.usage=="research" else "planning",
                "candidates":[self._rejected(c["candidate_id"],code,issue) for c in req.candidates]},
                diagnostics=[issue],failed=failed)
        try:
            self._load()
        except Exception as exc:  # Torch deserializers use several exception classes.
            return reject_all("tool_error",diagnostic("model_load_failed",detail=type(exc).__name__),failed=True)
        if req.model_version != self.bundle.manifest["version"]:
            return finish("invalid_input",diagnostics=[diagnostic("model_version_mismatch",field="model_version")])
        # Bind exact weights/scaler/preprocessor to the request fingerprint.
        input_hash = digest({"request":raw,"model_identity":self.model_identity})
        if req.history_ref is None:
            return reject_all("needs_input",diagnostic("missing_history",field="history_ref"))
        try:
            history_payload = self.store.get(req.history_ref,kind="history")
            if history_payload.get("snapshot_id") != req.snapshot_id:
                return reject_all("invalid_input",diagnostic("history_snapshot_mismatch",field="snapshot_id"))
            history,issue = _history(history_payload,self.bundle.manifest["extension"])
        except (OSError,ValueError,KeyError,TypeError) as exc:
            return reject_all("tool_error",diagnostic("history_artifact_unavailable",detail=type(exc).__name__),failed=True)
        if issue:
            code = "needs_input" if issue["code"].startswith(("missing_","history_source")) else "invalid_input"
            return reject_all(code,issue)
        results = []
        for candidate in req.candidates:
            try:
                results.append(self._candidate(candidate,req,history))
            except (OSError,ValueError,KeyError,TypeError,RuntimeError) as exc:
                results.append(self._rejected(candidate["candidate_id"],"tool_error",
                    diagnostic("candidate_execution_failed",detail=type(exc).__name__)))
        codes = {r["outcome_code"] for r in results}
        code = next(iter(codes)) if len(codes)==1 else "ok"
        return finish(code,{**context,"model_identity":self.model_identity,
                            "usage":"research_only" if req.usage=="research" else "planning",
                            "candidates":results},failed=code=="tool_error")

    @staticmethod
    def _rejected(candidate_id, code, issue):
        return {"candidate_id":candidate_id,"outcome_code":code,"selectable_for_planning":False,
                "power_series_ref":None,"coverage_ratio":0.0,"segment_summary":[],
                "motor_energy_kwh":None,"covered_motor_energy_kwh":0.0,"peak_motor_power_kw":None,
                "waiting_energy_kwh":None,"auxiliary_energy_kwh":None,"battery_energy_kwh":None,
                "diagnostics":[issue]}

    def _candidate(self,candidate,req,history):
        cid = candidate["candidate_id"]
        rows,issue = _trajectory(candidate)
        if issue:
            return self._rejected(cid,"invalid_input",issue)
        if req.usage == "planning" and not (self.bundle.manifest.get("planner_approved") is True and
                                            self.bundle.manifest.get("approval_ref")):
            return self._rejected(cid,"model_unapproved",diagnostic("model_unapproved",detail="Holdout/measurement gates not passed"))
        if len(rows) > HORIZON:
            return self._rejected(cid,"model_out_of_domain",diagnostic("forecast_horizon_unvalidated",limit=HORIZON,observed=len(rows)))
        # Only the uninterrupted moving prefix has a valid observed history.
        prefix = next((i for i,p in enumerate(rows) if p["phase"] != "moving"),len(rows))
        moving = rows[:prefix]
        previous = history[-1]["speed_kmh"]
        for row in moving:
            dt = row["duration_s"]
            acceleration = (row["planned_speed_kmh"]-previous)/3.6/dt
            for field,value in (("speed_kmh",row["planned_speed_kmh"]),("acceleration_mps2",acceleration)):
                bounds = self.domain["future_feature_ranges"][field]
                if not bounds[0]-1e-6 <= value <= bounds[1]+1e-6:
                    return self._rejected(cid,"model_out_of_domain",diagnostic("feature_out_of_training_range",
                        field=field,interval=row["segment_id"],observed=value,limit=bounds))
            previous = row["planned_speed_kmh"]
        envs = None
        if self.bundle.manifest["extension"]:
            envs = []
            for row in moving:
                ref = row["environment_ref"] or req.environment_scenario_ref
                if ref is None:
                    return self._rejected(cid,"needs_input",diagnostic("missing_environment",field="environment_scenario_ref"))
                payload = self.store.get(ref,kind="environment")
                if payload.get("scenario_id") != req.scenario_id:
                    return self._rejected(cid,"invalid_input",diagnostic("environment_scenario_mismatch",field="scenario_id"))
                if not _source(payload):
                    return self._rejected(cid,"needs_input",diagnostic("environment_source_missing",field="environment_scenario_ref.source_ref"))
                env_points = payload.get("points")
                if not isinstance(env_points,list) or any(not isinstance(p,dict) for p in env_points):
                    return self._rejected(cid,"invalid_input",diagnostic("invalid_environment_points",field="environment_scenario_ref.points"))
                matches = [p for p in env_points if
                           p.get("start_offset_s")==row["start_offset_s"] and p.get("duration_s")==row["duration_s"]]
                if len(matches)!=1 or any(not finite(matches[0].get(f)) for f in FEATURES[2:]):
                    return self._rejected(cid,"needs_input",diagnostic("missing_environment_interval",interval=row["segment_id"]))
                envs.append(dict(matches[0],source=payload["source_ref"]["source_id"]))
        output = None
        if moving:
            plan = [{"segment_id":p["segment_id"],"speed_kmh":p["planned_speed_kmh"],
                     "dt_seconds":p["duration_s"],"waiting_seconds":0} for p in moving]
            output = evaluate(plan,envs,history,req.model_version,bundle=self.bundle,usage=req.usage)
            if output["status"] not in ("experimental","planning_prediction"):
                mapping = {"outside_training_domain":"model_out_of_domain","long_horizon_unvalidated":"model_out_of_domain",
                           "missing_environment":"needs_input","missing_history":"needs_input","model_unapproved":"model_unapproved"}
                return self._rejected(cid,mapping.get(output["status"],"invalid_input"),diagnostic(output["status"]))
        points = []
        summaries = {}
        covered_energy,covered_duration = 0.0,0.0
        for i,row in enumerate(rows):
            predicted = output["steps"][i] if i < prefix else None
            power = predicted["motor_power_kw"] if predicted else None
            energy = predicted["motor_energy_kwh"] if predicted else None
            point = {k:v for k,v in row.items() if k != "environment_ref"}
            point.update({"predicted_motor_power_kw":power,"motor_energy_kwh":energy,
                          "applicability_flag":"in_domain" if predicted else "not_predicted",
                          "quality_flags":[predicted["quality"]] if predicted else ["waiting_or_post_wait_unvalidated"],
                          "environment_source":predicted["environment_source"] if predicted else None})
            points.append(point)
            seg = summaries.setdefault(row["segment_id"],{"segment_id":row["segment_id"],"duration_s":0.0,
                    "moving_s":0.0,"waiting_s":0.0,"covered_duration_s":0.0,"covered_motor_energy_kwh":0.0})
            seg["duration_s"] += row["duration_s"]
            seg["moving_s" if row["phase"]=="moving" else "waiting_s"] += row["duration_s"]
            if predicted:
                covered_energy += energy
                covered_duration += row["duration_s"]
                seg["covered_duration_s"] += row["duration_s"]
                seg["covered_motor_energy_kwh"] += energy
        duration = sum(p["duration_s"] for p in rows)
        complete = prefix == len(rows)
        for seg in summaries.values():
            seg["coverage_ratio"] = seg["covered_duration_s"]/seg["duration_s"]
            seg["motor_energy_kwh"] = seg["covered_motor_energy_kwh"] if seg["coverage_ratio"]==1 else None
        ref = self.store.put({"measurement_boundary":"propulsion_motor_observation",
                              "integration_rule":"right_endpoint","model_identity":self.model_identity,
                              "snapshot_id":req.snapshot_id,"scenario_id":req.scenario_id,
                              "history_ref":req.history_ref,"environment_scenario_ref":req.environment_scenario_ref,
                              "candidate_input_hash":digest({"trajectory":rows,"history_ref":req.history_ref,
                                  "environment_scenario_ref":req.environment_scenario_ref,
                                  "snapshot_id":req.snapshot_id,"scenario_id":req.scenario_id,
                                  "model_identity":self.model_identity}),
                              "points":points},"power_series")
        return {"candidate_id":cid,"outcome_code":"ok" if complete else "needs_input",
                "selectable_for_planning":complete and req.usage=="planning",
                "power_series_ref":ref,"coverage_ratio":covered_duration/duration,
                "segment_summary":list(summaries.values()),"motor_energy_kwh":covered_energy if complete else None,
                "covered_motor_energy_kwh":covered_energy,
                "peak_motor_power_kw":max((p["predicted_motor_power_kw"] for p in points if p["predicted_motor_power_kw"] is not None),default=None),
                "waiting_energy_kwh":None,"auxiliary_energy_kwh":None,"battery_energy_kwh":None,
                "diagnostics":[] if complete else [diagnostic("waiting_load_unknown",detail="Post-wait moving history is also unvalidated")]}


def evaluate_batch(request, *, predictor):
    """Public EnergyBatchRequest entry; predictor owns model snapshot and artifact resolver."""
    return predictor.evaluate_batch(request)
