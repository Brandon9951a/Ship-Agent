"""Run the website's 72 ordered node pairs through the real analytic tool chain."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from uuid import uuid4

from research.agent.professional import ProfessionalTools
from research.agent.store import Store

ROOT = Path(__file__).resolve().parents[1]


def _route_nodes() -> list[str]:
    return [
        "平顶山港", "军李船闸", "马湾船闸", "漯河船闸", "漯河港",
        "大路李船闸", "葫芦湾船闸", "周口船闸", "周口港",
    ]


def run_pair(tools: ProfessionalTools, store: Store, origin: str, destination: str,
             sequence: int) -> dict:
    task_id = f"fdp-72-{sequence:02d}-{uuid4().hex[:8]}"
    store.create(task_id, "coverage", {"status": "running"})
    state = {
        "task_id": task_id, "owner": "coverage", "scenario_id": "route_success",
        "revision": 1,
        "request": {"origin": origin, "destination": destination,
                     "objective": "min_energy"},
        "refs": {},
    }
    started = time.perf_counter()
    record = {"sequence": sequence, "origin": origin, "destination": destination,
              "task_id": task_id, "stages": {}, "outcome_code": None}
    try:
        for name, fn in (("Tdata", tools.data), ("Tseg", tools.segments),
                         ("Tspeed", tools.speed), ("Tmanagement", tools.management)):
            result = fn(state, f"coverage-{sequence}-{name}")
            record["stages"][name] = {
                "outcome_code": result.get("outcome_code"),
                "diagnostics": result.get("diagnostics", []),
            }
            if result.get("artifact_ref"):
                state["refs"][name] = result["artifact_ref"]
            if result.get("outcome_code") != "ok":
                record["outcome_code"] = (
                    "solver_unresolved" if result.get("outcome_code") in ("grid_unresolved", "search_unresolved")
                    else result.get("outcome_code")
                )
                return record
        verification_ref, summary = tools.verify(state)
        record["verification"] = {"passed": True, "artifact_ref": verification_ref}
        record["summary"] = summary
        record["outcome_code"] = "research_feasible"
    except Exception as exc:
        record["outcome_code"] = "tool_error"
        record["error"] = {"type": type(exc).__name__, "detail": str(exc)[:400]}
    finally:
        record["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="artifacts/fdp-72/coverage.json")
    parser.add_argument("--limit", type=int, default=72)
    args = parser.parse_args()
    output = Path(args.output)
    if not output.is_absolute():
        output = ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    store = Store(output.parent / "store")
    tools = ProfessionalTools(store)
    nodes = _route_nodes()
    pairs = [(origin, destination) for origin in nodes for destination in nodes
             if origin != destination][:args.limit]
    records = [run_pair(tools, store, origin, destination, index + 1)
               for index, (origin, destination) in enumerate(pairs)]
    payload = {
        "schema_version": "fdp-72-coverage/1",
        "source": "ui/frontend/app.js ALL_ROUTE_NODES and configs/route_facts.yaml",
        "standard_conditions": "route_success analytic backend; min_energy; no model override",
        "requested_count": len(pairs), "completed_count": len(records),
        "records": records,
        "outcome_counts": {
            code: sum(item["outcome_code"] == code for item in records)
            for code in sorted({item["outcome_code"] for item in records})
        },
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "requested_count": len(pairs),
                      "outcome_counts": payload["outcome_counts"]}, ensure_ascii=False))
    return 0 if len(records) == len(pairs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
