"""Safety cases for D1 adoption rules, independent of any real vessel values."""

from copy import deepcopy

import pytest

from tools.vessel_facts import ROOT, audit_facts, load_config


def configs():
    return load_config(ROOT / "configs/vessel_facts.yaml"), load_config(ROOT / "configs/limits.yaml")


def approved(facts, limits):
    source = {"source_id": "synthetic-test-only", "kind": "assumption", "locator": "test fixture",
              "confirmed": False, "note": "Synthetic values for code tests, not vessel facts"}
    for name, value in (("capacity_kwh", 100), ("max_power_kw", 50), ("auxiliary_power_kw", 0)):
        facts["parameters"][name].update(adopted={"value": value, "source": deepcopy(source)}, confirmed_by="A-test-fixture", confirmation_status="approved_A")
    facts["parameters"]["battery_group_capacity_kwh"].update(
        adopted={"value": 50, "source": deepcopy(source)},
        confirmed_by="A-test-fixture", confirmation_status="approved_A",
    )
    limits["limits"]["soc_min"].update(adopted={"value": .3, "source": source}, confirmed_by="A-test-fixture", confirmation_status="approved_A")
    limits["policy"].update(power_boundary="battery-electrical", capacity_boundary="effective-battery-capacity",
                            battery_topology="one-budget-no-switching", energy_scope="total")


def test_repository_config_records_explicit_A_D2_adoptions():
    facts, limits = configs()
    report = audit_facts(facts, limits)
    assert not report["errors"]
    assert report["status"] == "ok"
    assert report["adopted"]["capacity_kwh"] == 1567.85
    assert report["adopted"]["max_power_kw"] == 200
    assert report["adopted"]["auxiliary_power_kw"] == 30
    assert report["adopted"]["soc_min"] == .20
    assert report["calculation_ready"]


def test_explicit_test_approval_can_be_ready_without_duplicate_capacity():
    facts, limits = configs()
    approved(facts, limits)
    result = audit_facts(facts, limits)
    assert result["status"] == "ok"
    assert result["adopted"]["capacity_kwh"] == 100
    assert result["adopted"]["battery_group_capacity_kwh"] * 2 == 100


def test_adopted_group_capacities_cannot_double_count_total_capacity():
    facts, limits = configs()
    facts["parameters"]["battery_group_capacity_kwh"]["adopted"]["value"] = 1567.85
    result = audit_facts(facts, limits)
    assert result["status"] == "invalid_input"
    assert "total capacity must equal two adopted group capacities" in result["errors"]


@pytest.mark.parametrize("value", [True, "100", float("nan"), float("inf"), -1, 0, 10**1000])
def test_capacity_rejects_bad_adoption(value):
    facts, limits = configs()
    approved(facts, limits)
    facts["parameters"]["capacity_kwh"]["adopted"]["value"] = value
    assert audit_facts(facts, limits)["status"] == "invalid_input"


def test_adoption_requires_approver_and_source():
    facts, limits = configs()
    approved(facts, limits)
    facts["parameters"]["max_power_kw"]["confirmed_by"] = None
    facts["parameters"]["capacity_kwh"]["adopted"]["source"] = {}
    result = audit_facts(facts, limits)
    assert result["status"] == "invalid_input"
    assert not result["calculation_ready"]


def test_unconfirmed_document_cannot_be_adopted():
    facts, limits = configs()
    approved(facts, limits)
    facts["parameters"]["capacity_kwh"]["adopted"]["source"]["kind"] = "document"
    assert audit_facts(facts, limits)["status"] == "invalid_input"


@pytest.mark.parametrize("minimum", [20, -.1])
def test_soc_must_be_fraction(minimum):
    facts, limits = configs()
    approved(facts, limits)
    limits["limits"]["soc_min"]["adopted"]["value"] = minimum
    assert audit_facts(facts, limits)["status"] == "invalid_input"


def test_shutdown_is_not_automatically_planning_minimum():
    facts, limits = configs()
    approved(facts, limits)
    limits["limits"]["soc_shutdown"].update(adopted=deepcopy(limits["limits"]["soc_min"]["adopted"]), confirmed_by="A-test-fixture", confirmation_status="approved_A")
    limits["limits"]["soc_shutdown"]["adopted"]["value"] = .4
    assert audit_facts(facts, limits)["status"] == "invalid_input"


def test_malformed_candidate_is_reported():
    facts, limits = configs()
    facts["parameters"]["capacity_kwh"]["candidates"] = [None]
    assert audit_facts(facts, limits)["status"] == "invalid_input"


def test_candidate_source_must_be_a_string():
    facts, limits = configs()
    facts["parameters"]["capacity_kwh"]["candidates"][0]["source"] = []
    assert audit_facts(facts, limits)["status"] == "invalid_input"


def test_malformed_soc_candidate_is_reported_without_changing_approved_value():
    facts, limits = configs()
    assert limits["limits"]["soc_alarm"]["candidates"][0]["value"] == .25
    limits["limits"]["soc_alarm"]["candidates"][0]["value"] = 25
    result = audit_facts(facts, limits)
    assert result["status"] == "invalid_input"
    assert result["adopted"]["soc_alarm"] == .35


def test_unknown_limit_name_is_rejected():
    facts, limits = configs()
    limits["limits"]["soc_typo"] = {"unit": "fraction"}
    assert audit_facts(facts, limits)["status"] == "invalid_input"


def test_pending_approval_cannot_be_calculation_ready():
    facts, limits = configs()
    approved(facts, limits)
    facts["parameters"]["capacity_kwh"]["confirmation_status"] = "pending_A"
    assert not audit_facts(facts, limits)["calculation_ready"]


def test_malformed_source_definition_is_reported():
    facts, limits = configs()
    facts["sources"]["design"] = None
    assert audit_facts(facts, limits)["status"] == "invalid_input"


@pytest.mark.parametrize("body", ['{"a": 1, "a": 2}', '{"a": NaN}', '[]'])
def test_config_loader_rejects_ambiguous_or_invalid_json(tmp_path, body):
    path = tmp_path / "bad.yaml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)
