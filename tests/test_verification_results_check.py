import copy
import json
import re
from pathlib import Path
from typing import Any, Literal

import pytest

import composer.pipeline.property_tags as pt
from composer.verification_results.check import check, main, schema

JOB = "https://prover.certora.com/output/1/abc"
SUITE = "certora/properties/withdrawals.property-suite.json"

RULE = {
    "rule": "withdraw_never_reverts",
    "spec": "certora/specs/vault.spec",
    "conf": "certora/confs/vault.conf",
    "result": "violated",
    "job_url": JOB,
}

SPEC = """\
/// @property withdraw_succeeds
rule withdraw_never_reverts(env e) {
    assert true;
}

rule helper(env e) {
    assert true;
}
"""


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project with the suite and a spec that tags one rule. The CVL parser is replaced by a
    reading of the ``rule`` lines, so the test needs no Java."""
    _write(tmp_path, SUITE, json.dumps({
        "component": {"name": "Withdrawals", "description": "Withdrawing assets from the vault."},
        "properties": [
            {"id": "withdraw_succeeds", "description": "A holder can always withdraw."},
            {"id": "fees_bounded", "description": "The fee never exceeds 1%.", "sort": "invariant"},
        ],
    }))
    _write(tmp_path, "certora/specs/vault.spec", SPEC)
    monkeypatch.setattr(pt, "rule_lines", lambda text: {
        m.group(1): (n, n + 2) for n, line in enumerate(text.splitlines(), start=1)
        if (m := re.match(r"rule (\w+)", line))
    })
    return tmp_path


def valid() -> dict[str, Any]:
    return {
        "schema": "certora-autoprover-results/1",
        "run": {
            "source": {"repository": "https://github.com/acme/vault", "commit": "a" * 40},
            "results": {"repository": "https://github.com/acme/vault-fv", "commit": "b" * 40,
                        "certora_dir": "certora/"},
            "main_contract": "src/Vault.sol:Vault",
            "property_suite": SUITE,
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
            {"id": "withdraw_succeeds", "status": "violated", "rules": [RULE], "findings": ["F1"]},
            {"id": "fees_bounded", "status": "dropped",
             "status_note": "Duplicate of a property of the fee module.", "rules": [], "findings": []},
        ],
    }


def messages(raw: dict[str, Any], root: Path, severity: Literal["error", "warning"] = "error") -> list[str]:
    return [f"{p.where}: {p.message}" for p in check(raw, root) if p.severity == severity]


def test_a_valid_file_has_no_problems(root: Path) -> None:
    assert check(valid(), root) == []


def test_shape_errors_name_the_field(root: Path) -> None:
    raw = valid()
    raw["findings"][0]["trust_class"] = "trusted_party"
    del raw["properties"][1]["status_note"]
    raw["run"]["source"]["commit"] = "abc"
    found = messages(raw, root)
    assert any(m.startswith("run.source.commit:") for m in found)
    assert any(m.startswith("findings[0]:") and "trusted_precondition" in m for m in found)
    assert any(m.startswith("properties[1]:") and "status_note" in m for m in found)


@pytest.mark.parametrize(("status", "rules", "message"), [
    ("verified", [], "a verified property needs at least one rule"),
    ("partial", [], "a partial property needs at least one rule"),
    ("dropped", [RULE], "a dropped property has no rules"),
])
def test_a_property_s_rules_fit_its_status(root: Path, status: str, rules: list[Any], message: str) -> None:
    raw = valid()
    raw["properties"][0] = {"id": "withdraw_succeeds", "status": status, "rules": rules, "findings": [],
                            "status_note": "A note long enough for every status."}
    assert any(m.startswith("properties[0]:") and message in m for m in messages(raw, root))


def test_a_not_proven_property_may_have_rules_or_none(root: Path) -> None:
    raw = valid()
    raw["findings"] = []
    raw["properties"][0].update(status="not_proven", findings=[],
                                status_note="Every rule timed out after two reformulations.")
    assert messages(raw, root) == []
    raw["properties"][0]["rules"] = []
    assert not [m for m in messages(raw, root) if m.startswith("properties[0]:")]


def test_unknown_fields_are_errors(root: Path) -> None:
    raw = valid()
    raw["findings"][0]["reference"] = "x"
    raw["properties"][0]["description"] = "the statement is in the suite"
    found = messages(raw, root)
    assert any(m.startswith("findings[0].reference:") for m in found)
    assert any(m.startswith("properties[0].description:") for m in found)


def test_ids_must_be_unique_and_resolve(root: Path) -> None:
    raw = valid()
    raw["properties"][1]["id"] = "withdraw_succeeds"
    raw["properties"][0]["findings"].append("F9")
    found = messages(raw, root)
    assert "properties: id 'withdraw_succeeds' appears more than once" in found
    assert "properties[0].findings[1]: no finding has id 'F9'" in found


def test_evidence_must_name_a_property_that_lists_the_finding_and_has_its_rules(root: Path) -> None:
    raw = valid()
    raw["properties"][0]["findings"] = []
    other = {**RULE, "rule": "withdraw_never_reverts_small"}
    changed = {**RULE, "result": "verified"}
    raw["findings"][0]["evidence"][0]["rules"] = [other, changed]
    found = messages(raw, root)
    assert any("does not list finding 'F1'" in m for m in found)
    assert any(m.startswith("findings[0].evidence[0].rules[0]:") and "is not among" in m
               for m in found)
    assert any(m.startswith("findings[0].evidence[0].rules[1]:") and "another result" in m
               for m in found)

    raw = valid()
    raw["findings"][0]["evidence"][0]["property"] = "nowhere"
    assert "findings[0].evidence[0].property: no property has id 'nowhere'" in messages(raw, root)


def test_rules_and_the_suite_are_under_the_results_directory(root: Path) -> None:
    raw = valid()
    raw["run"]["results"]["certora_dir"] = "certora/vault"
    found = messages(raw, root)
    assert any(m.startswith("properties[0].rules[0].spec:") and "certora/vault/" in m
               for m in found)
    assert any(m.startswith("run.property_suite:") and "certora/vault/" in m for m in found)


def test_the_file_has_one_entry_per_property_of_the_suite(root: Path) -> None:
    raw = valid()
    raw["properties"][1]["id"] = "fees_capped"
    found = messages(raw, root)
    assert "properties: suite property 'fees_bounded' has no entry" in found
    assert "properties[1].id: the suite defines no property 'fees_capped'" in found


def test_a_property_s_rules_are_the_rules_tagged_with_it(root: Path) -> None:
    raw = valid()
    raw["properties"][0]["rules"] = [{**RULE, "rule": "helper"}]
    raw["findings"][0]["evidence"][0]["rules"] = [{**RULE, "rule": "helper"}]
    found = messages(raw, root)
    assert found == [
        "properties[0].rules: rule 'withdraw_never_reverts' (certora/specs/vault.spec) is tagged "
        "with 'withdraw_succeeds' but not listed",
        "properties[0].rules: rule 'helper' (certora/specs/vault.spec) is listed but not tagged "
        "with 'withdraw_succeeds'",
    ]

    # The same rule run by two confs is one tagged rule.
    raw = valid()
    second = {**RULE, "conf": "certora/confs/vault_fast.conf"}
    raw["properties"][0]["rules"].append(second)
    assert messages(raw, root) == []


def test_a_suite_that_does_not_load_is_the_only_suite_error(root: Path) -> None:
    raw = valid()
    raw["run"]["property_suite"] = "certora/properties/missing.property-suite.json"
    found = messages(raw, root)
    assert len(found) == 1 and "missing.property-suite.json" in found[0]


def test_tag_problems_of_the_suite_are_reported(root: Path) -> None:
    _write(root, "certora/specs/other.spec",
           "/// @property typo_id\nrule other(env e) {\n    assert true;\n}\n")
    found = messages(valid(), root)
    assert any(m.startswith("certora/specs/other.spec") and "typo_id" in m for m in found)


def test_internal_vocabulary_is_a_warning(root: Path) -> None:
    raw = valid()
    raw["findings"][0]["justification"]["pre_state"] = "The CEX of round 2 (judge): vm-F3."
    raw["properties"][1]["status_note"] = "Not modelled by the scene in run 4."
    assert messages(raw, root) == []
    found = messages(raw, root, "warning")
    assert len([m for m in found if m.startswith("findings[0].justification.pre_state:")]) == 4
    assert len([m for m in found if m.startswith("properties[1].status_note:")]) == 2


def test_main_fails_on_errors_only(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = valid()
    good["findings"][0]["title"] = "Withdraw reverts, see the CEX"
    bad = copy.deepcopy(good)
    bad["properties"][0]["findings"] = []
    (root / "good.json").write_text(json.dumps(good))
    (root / "bad.json").write_text(json.dumps(bad))
    assert main([str(root / "good.json"), "--root", str(root)]) == 0
    assert main([str(root / "bad.json"), "--root", str(root)]) == 1
    err = capsys.readouterr().err
    assert "warning: " in err and "error: " in err


def test_schema_describes_the_format() -> None:
    s = schema()
    assert s["$id"] == "certora-autoprover-results/1"
    assert "schema" in s["properties"]
    assert s["$defs"]["Property"]["required"] == ["id", "status", "rules", "findings"]
    rule = s["$defs"]["RuleResult"]
    assert rule["required"] == ["rule", "spec", "conf", "result", "job_url"]
    assert rule["properties"]["spec"]["description"] == "Spec path in the results repository."
