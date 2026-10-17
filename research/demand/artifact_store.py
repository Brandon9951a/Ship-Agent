"""Immutable, content-addressed PowerDemandSeries artifacts for Tspeed/FDP handoff."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from research.contracts import ArtifactRef, Diagnostic, PowerDemandPoint, PowerDemandSeries, SourceRef
from research.contracts.power_demand import POWER_DEMAND_SCHEMA_VERSION


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


class DemandArtifactStore:
    """Persist the exact selected demand; IDs are SHA-256 of the stored JSON."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def put(self, series: PowerDemandSeries) -> ArtifactRef:
        if not series.points:
            raise ValueError("cannot persist an empty demand series")
        payload = series.to_dict()
        payload["artifact_ref"] = None  # self-reference cannot enter the hash
        document = {"kind": "power_demand_series", "schema_version": POWER_DEMAND_SCHEMA_VERSION,
                    "payload": payload}
        data = _canonical(document)
        sha = hashlib.sha256(data).hexdigest()
        path = self.directory / f"{sha}.json"
        try:
            with path.open("xb") as stream:
                stream.write(data)
        except FileExistsError:
            if path.read_bytes() != data:
                raise ValueError("existing demand artifact hash collision or corruption")
        return ArtifactRef(sha, sha, len(series.points), POWER_DEMAND_SCHEMA_VERSION)

    def get(self, artifact_id: str) -> PowerDemandSeries:
        if not isinstance(artifact_id, str) or re.fullmatch(r"[a-f0-9]{64}", artifact_id) is None:
            raise ValueError("demand artifact ID must be a SHA-256 digest")
        data = (self.directory / f"{artifact_id}.json").read_bytes()
        if hashlib.sha256(data).hexdigest() != artifact_id:
            raise ValueError("demand artifact content hash mismatch")
        document = json.loads(data)
        if (document.get("kind") != "power_demand_series" or
                document.get("schema_version") != POWER_DEMAND_SCHEMA_VERSION):
            raise ValueError("demand artifact kind/version mismatch")
        payload = document["payload"]
        if payload.get("schema_version") != POWER_DEMAND_SCHEMA_VERSION or payload.get("artifact_ref") is not None:
            raise ValueError("demand payload version/self-reference mismatch")
        points = tuple(PowerDemandPoint(**point) for point in payload["points"])
        sources = tuple(SourceRef(**source) for source in payload["source_refs"])
        diagnostics = []
        for item in payload["diagnostics"]:
            item = dict(item)
            item["source_refs"] = tuple(SourceRef(**source) for source in item.get("source_refs", ()))
            diagnostics.append(Diagnostic(**item))
        ref = ArtifactRef(artifact_id, artifact_id, len(points), POWER_DEMAND_SCHEMA_VERSION)
        return PowerDemandSeries(
            series_id=payload["series_id"], points=points,
            conversion_model_ref=payload["conversion_model_ref"],
            conversion_config_hash=payload["conversion_config_hash"],
            input_energy_ref=payload.get("input_energy_ref"), source_refs=sources,
            bus_side_available=payload["bus_side_available"],
            schema_version=payload["schema_version"],
            auxiliary_model_ref=payload.get("auxiliary_model_ref"),
            diagnostics=tuple(diagnostics), quality_flags=tuple(payload.get("quality_flags", ())),
            artifact_ref=ref, note=payload.get("note"))
