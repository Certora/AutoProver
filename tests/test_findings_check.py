import copy
import json
from pathlib import Path
from typing import Any, Literal

import pytest

from composer.findings.check import check, main, schema

JOB = "https://prover.certora.com/output/1/abc?anonymousKey=k1"

RULE = {
    "rule": "withdraw_never_reverts",
    "spec": "certora/specs/vault.spec",
    "conf": "certora/confs/vault.conf",
    "result": "violated",
    "job_url": JOB,
}


def valid() -> dict[str, Any]:
    return {
        "schema": "certora-autoprover-findings/1",
        "run": {
            "source": {"repository": "https://github.com/acme/vault", "commit": "a" * 40},
            "results": {"repository": "https://github.com/acme/vault-fv", "commit": "b" * 40,
                        "certora_dir": "certora/"},
            "main_contract": "src/Vault.sol:Vault",
            "skill_version": "0.8.0",
            "generated_at": "2026-11-26T00:00:00Z",
            "property_generator_run_id": None,
        },
        "findings": [{
            "id": "F1",
            "title": "Withdraw reverts after a fee change",
            "severity": "medium",
            "trust_class": "unprivileged",
            "description": "`withdraw` reverts when the fee rounds up (src/Vault.sol:42).",
            "justification": {
                "pre_state": "totalAssets = 10, totalSupply = 3.",
                "steps": ["A holder calls `withdraw(1)`."],
                "reachability": "Not established by the run.",
                "not_a_model_artefact": "The revert is the vault's own require.",
            },
            "severity_justification": {"impact": "medium", "likelihood": "low",
                                       "reasoning": "Blocks an exit."},
            "evidence": [{"property": "withdraw_succeeds", "how": "Its rule fails.",
                          "rules": [RULE]}],
        }],
        "properties": [
            {"id": "withdraw_succeeds", "group": "Withdrawals", "sort": "safety_property",
             "description": "A holder can always withdraw.", "source": "user",
             "status": "violated", "rules": [RULE], "findings": ["F1"]},
            {"id": "fees_bounded", "group": "Fees", "sort": "invariant",
             "description": "The fee never exceeds 1%.", "source": "added",
             "status": "dropped", "status_note": "Duplicate of a property of the fee module.",
             "rules": [], "findings": []},
        ],
        "evaluation": None,
    }


def messages(raw: dict[str, Any], severity: Literal["error", "warning"] = "error") -> list[str]:
    return [f"{p.where}: {p.message}" for p in check(raw) if p.severity == severity]


def test_a_valid_file_has_no_problems() -> None:
    assert check(valid()) == []


def test_shape_errors_name_the_field() -> None:
    raw = valid()
    raw["findings"][0]["trust_class"] = "trusted_party"
    del raw["properties"][1]["status_note"]
    raw["run"]["source"]["commit"] = "abc"
    found = messages(raw)
    assert any(m.startswith("run.source.commit:") for m in found)
    assert any(m.startswith("findings[0]:") and "trusted_precondition" in m for m in found)
    assert any(m.startswith("properties[1]:") and "status_note" in m for m in found)


def test_unknown_fields_are_errors() -> None:
    raw = valid()
    raw["findings"][0]["reference"] = "x"
    assert any(m.startswith("findings[0].reference:") for m in messages(raw))


def test_ids_must_be_unique_and_resolve() -> None:
    raw = valid()
    raw["properties"][1]["id"] = "withdraw_succeeds"
    raw["properties"][0]["findings"].append("F9")
    found = messages(raw)
    assert "properties: id 'withdraw_succeeds' appears more than once" in found
    assert "properties[0].findings[1]: no finding has id 'F9'" in found


def test_evidence_must_name_a_property_that_lists_the_finding_and_has_its_rules() -> None:
    raw = valid()
    raw["properties"][0]["findings"] = []
    other = {**RULE, "rule": "withdraw_never_reverts_small"}
    changed = {**RULE, "result": "verified"}
    raw["findings"][0]["evidence"][0]["rules"] = [other, changed]
    found = messages(raw)
    assert any("does not list finding 'F1'" in m for m in found)
    assert any(m.startswith("findings[0].evidence[0].rules[0]:") and "is not among" in m
               for m in found)
    assert any(m.startswith("findings[0].evidence[0].rules[1]:") and "another result" in m
               for m in found)

    raw = valid()
    raw["findings"][0]["evidence"][0]["property"] = "nowhere"
    assert "findings[0].evidence[0].property: no property has id 'nowhere'" in messages(raw)


@pytest.mark.parametrize(("url", "why"), [
    ("https://prover.certora.com/output/1/abc", "No anonymousKey"),
    ("https://prover.certora.com/abc?anonymousKey=k1", "Unexpected cloud link format"),
    ("https://prover.certora.com/output/1/abc/?anonymousKey=k1", "ends its path with '/'"),
    ("http://prover.certora.com/output/1/abc?anonymousKey=k1", "not an https URL"),
])
def test_job_urls_are_anonymous_links(url: str, why: str) -> None:
    raw = valid()
    raw["properties"][0]["rules"][0] = {**RULE, "job_url": url}
    raw["findings"][0]["evidence"][0]["rules"][0] = {**RULE, "job_url": url}
    found = messages(raw)
    assert any(m.startswith("properties[0].rules[0].job_url:") and why in m for m in found)


def test_rules_are_under_the_results_directory() -> None:
    raw = valid()
    raw["run"]["results"]["certora_dir"] = "certora/vault"
    found = messages(raw)
    assert any(m.startswith("properties[0].rules[0].spec:") and "certora/vault/" in m
               for m in found)


def test_internal_vocabulary_is_a_warning() -> None:
    raw = valid()
    raw["findings"][0]["justification"]["pre_state"] = "The CEX of round 2 (judge): vm-F3."
    raw["properties"][1]["status_note"] = "Not modelled by the scene in run 4."
    assert messages(raw) == []
    found = messages(raw, "warning")
    assert len([m for m in found if m.startswith("findings[0].justification.pre_state:")]) == 4
    assert len([m for m in found if m.startswith("properties[1].status_note:")]) == 2


def test_main_fails_on_errors_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = valid()
    good["findings"][0]["title"] = "Withdraw reverts, see the CEX"
    bad = copy.deepcopy(good)
    bad["properties"][0]["findings"] = []
    (tmp_path / "good.json").write_text(json.dumps(good))
    (tmp_path / "bad.json").write_text(json.dumps(bad))
    assert main([str(tmp_path / "good.json")]) == 0
    assert main([str(tmp_path / "bad.json")]) == 1
    err = capsys.readouterr().err
    assert "warning: " in err and "error: " in err


def test_schema_describes_the_format() -> None:
    s = schema()
    assert s["$id"] == "certora-autoprover-findings/1"
    assert "schema" in s["properties"]
    rule = s["$defs"]["RuleResult"]
    assert "anonymousKey" in rule["properties"]["job_url"]["description"]
