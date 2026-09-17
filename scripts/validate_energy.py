"""Reproducible D2 artificial examples, never read historical ship measurements."""

import json
import platform

from tools.energy_math import integrate_power, segment_energy, split_groups


def run_examples():
    segment = segment_energy(8, 4, .5, .125, 2, scope="total")
    integral = integrate_power([(0, 10), (3600, -20)], max_gap_seconds=3600)
    train, validation = split_groups([{"group_id": "synthetic-a"}, {"group_id": "synthetic-b"}],
                                    train_groups=["synthetic-a"], validation_groups=["synthetic-b"])
    return {"evidence_kind": "synthetic_numerical_examples", "real_ship_validation": False,
            "python": platform.python_version(),
            "assumptions": ["Artificial coefficient 0.125; not calibrated to any vessel.",
                            "Zero waiting propulsion, constant 2 kW auxiliary, consistent hypothetical boundary.",
                            "Signed power area is mathematics, not confirmed battery regeneration."],
            "segment_input": {"distance_km": 8, "speed_kmh": 4, "waiting_h": .5,
                              "coefficient": .125, "auxiliary_kw": 2, "scope": "total"},
            "segment": segment, "integral": integral,
            "split_sizes": {"train": len(train), "validation": len(validation)}}


if __name__ == "__main__":
    print(json.dumps(run_examples(), ensure_ascii=False, indent=2, allow_nan=False))
