"""D2 text UI skeleton for C-owned Tdata/Tseg status display."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from schemas.validate import parse_request
from tools.tdata import load_config, tdata
from tools.tseg import segment


ROOT = Path(__file__).resolve().parents[1]


def run(payload: dict) -> dict:
    request, validation = parse_request(payload)
    if request is None or not validation.valid:
        return {"status": validation.status.value, "missing_fields": validation.missing_fields,
                "questions": validation.questions}
    data_response = tdata(request)
    result = {"tdata": data_response.to_dict()}
    if data_response.payload:
        from schemas.types import DataContext
        context = DataContext.from_dict(data_response.payload)
        result["tseg"] = segment(
            request,
            context,
            route_config=load_config(ROOT / "configs/route_facts.yaml"),
            aliases_config=load_config(ROOT / "configs/aliases.yaml"),
            demo_policy_config=load_config(ROOT / "configs/demo_policy.yaml"),
        ).to_dict()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Ship-Agent D2 text UI skeleton")
    parser.add_argument("--input", default=ROOT / "configs/examples/voyage_request.json")
    args = parser.parse_args()
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    print(json.dumps(run(payload), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
