"""The runtime ABI's Python side (``composer.rustapp.wire``).

Every string crossing the FFI is one of these models, so this is where a shape mismatch with
``rust/autoprover-sdk/src/lib.rs`` should surface. Two properties matter:

* the **tagged results** are discriminated unions — a build failure and a set of verdicts are
  different types, and neither can be asked for the other's fields;
* the models are **strict**: both halves of the seam ship together, so a wheel that omits a field
  or sends one the host doesn't declare has drifted, and either fails naming the field. Nothing here
  reads an absent key as a default.

No wheel needed — these are the payloads, not the callouts. For whether the two sides agree field by
field, see ``test_wire_roundtrip.py``, which checks it against the real serde types.
"""

import json
import sys
import types

import pytest
from pydantic import TypeAdapter, ValidationError

from composer.rustapp.host import load_module
from composer.rustapp.wire import (
    CALLOUTS,
    AuthorInput,
    CompileFailed,
    CompileOk,
    ComponentInput,
    PreflightInput,
    SetupInput,
    RustAppModule,
    Check,
    Target,
    ValidateBuildFailed,
    ValidateCoverageError,
    ValidateVerdicts,
    parse_compile,
    parse_validate,
    parse_workspace_prep,
)
from composer.spec.source.report.schema import Outcome
from tests.conftest import wire_verdict, wire_workspace_prep


def test_compile_result_is_discriminated_on_status():
    assert isinstance(parse_compile('{"status": "ok"}'), CompileOk)
    failed = parse_compile('{"status": "failed", "errors": "E0432"}')
    assert isinstance(failed, CompileFailed) and failed.errors == "E0432"


def test_validate_outcome_is_discriminated_on_kind():
    build_failed = parse_validate('{"kind": "build_failed", "errors": "no method `foo`"}')
    assert isinstance(build_failed, ValidateBuildFailed)

    got = parse_validate(json.dumps(
        {"kind": "verdicts", "verdicts": [["rule_a", wire_verdict("BAD", detail="cex")]]}
    ))
    assert isinstance(got, ValidateVerdicts)
    unit, verdict = got.verdicts[0]
    assert (unit, verdict.outcome, verdict.detail) == ("rule_a", Outcome.BAD, "cex")


def test_an_unknown_tag_is_refused_rather_than_read_as_the_other_variant():
    # A discriminated union, not a `kind == "build_failed"` test: an unrecognized tag has to be
    # named here rather than fall through to the other variant's fields and die reading them.
    with pytest.raises(ValidationError):
        parse_validate('{"kind": "verdict", "verdicts": []}')
    with pytest.raises(ValidationError):
        parse_compile('{"status": "OK"}')


def test_an_omitted_field_is_refused_rather_than_read_as_a_default():
    # There is no wheel old enough to be excused one: the two halves ship together, so an absent
    # `errors` is a mirror that drifted, and reading it as "" would put an empty revise prompt in
    # front of the author instead of saying so.
    with pytest.raises(ValidationError):
        parse_compile('{"status": "failed"}')
    with pytest.raises(ValidationError):
        parse_validate('{"kind": "build_failed"}')


def test_a_field_the_host_does_not_declare_is_refused():
    # The other half of the same rule (`extra="forbid"` / `#[serde(deny_unknown_fields)]`): a key
    # only one side knows means the mirrors disagree, and dropping it silently is how a wheel ends up
    # reporting something no one reads.
    with pytest.raises(ValidationError):
        parse_compile('{"status": "ok", "warnings": ["unused"]}')


def test_a_null_target_means_a_check_is_its_own_validation_target():
    # `target` must be *present*; null is how the grouping says the check runs on its own. Absence
    # is not a third spelling of that — see the test above.
    own = Target.model_validate_json(
        '{"name": "rule_p", "checks": [{"name": "rule_p", "properties": ["p"], "target": null}]}')
    assert own.checks == [Check(name="rule_p", properties=["p"], target=None)]
    assert own.checks[0].target_or_name() == "rule_p"
    # …and a shared target is what the host runs once for several checks.
    assert Check(name="rule_p", properties=["p"], target="c_vault").target_or_name() == "c_vault"


def test_an_outcome_the_host_does_not_know_is_refused():
    # The outcome vocabulary is closed on both sides, so a label the host has never heard of is a
    # variant added to one `Outcome` and not the other — not something to render as UNKNOWN.
    with pytest.raises(ValidationError):
        parse_validate(json.dumps({"kind": "verdicts",
                                   "verdicts": [["u", wire_verdict("FLAKY")]]}))


def _verdicts(*named: tuple[str, str]) -> ValidateVerdicts:
    parsed = parse_validate(json.dumps(
        {"kind": "verdicts", "verdicts": [[n, wire_verdict(o)] for n, o in named]}))
    assert isinstance(parsed, ValidateVerdicts)
    return parsed


_SHARED = Target(name="c_vault", checks=[
    Check(name="rule_p", properties=["p"], target="c_vault"),
    Check(name="rule_q", properties=["q"], target="c_vault"),
])


def test_a_verdict_resolves_to_the_check_the_host_sent():
    # The wire keys a verdict by name; upstream wants the `Check` the host sent, never one the
    # wheel echoed back. Order is the target's, not the wheel's answer's.
    resolved = _verdicts(("rule_q", "BAD"), ("rule_p", "GOOD")).resolve(_SHARED)
    assert [(c.name, v.outcome) for c, v in resolved] == [
        ("rule_p", Outcome.GOOD), ("rule_q", Outcome.BAD),
    ]


def test_a_check_left_without_a_verdict_is_refused():
    # The one that matters: an unanswered check is not a failing check, so nothing downstream has
    # anything to object to — a wheel that answered for none of them would otherwise stamp the
    # publish gate on a component it never checked.
    with pytest.raises(ValidateCoverageError, match="rule_q"):
        _verdicts(("rule_p", "GOOD")).resolve(_SHARED)
    with pytest.raises(ValidateCoverageError):
        _verdicts().resolve(_SHARED)


def test_a_verdict_for_a_check_the_target_does_not_cover_is_refused():
    # A name no check has can only be a wheel that invented one or misspelled one, and either way
    # the verdict is about nothing the host can report under.
    with pytest.raises(ValidateCoverageError, match="rule_r"):
        _verdicts(("rule_p", "GOOD"), ("rule_q", "GOOD"), ("rule_r", "GOOD")).resolve(_SHARED)


def test_two_verdicts_for_one_check_are_refused_rather_than_one_winning():
    # Keying by name would quietly keep the last, which is how a BAD becomes a GOOD.
    with pytest.raises(ValidateCoverageError, match="rule_p"):
        _verdicts(("rule_p", "BAD"), ("rule_p", "GOOD"), ("rule_q", "GOOD")).resolve(_SHARED)


def test_a_workspace_plan_that_only_places_files_needs_no_toolchain():
    files_only = parse_workspace_prep(json.dumps(
        wire_workspace_prep(files={"fuzz/Cargo.toml": "[package]"})))
    assert files_only.files and not files_only.needs_toolchain
    # Whatever the request *says* is the chain's business; that there is one at all is the host's.
    asking = parse_workspace_prep(json.dumps(
        wire_workspace_prep(toolchain_request={"build_program": "lend"})))
    assert asking.needs_toolchain


def test_author_input_requires_what_the_wheel_requires():
    # `kind` selects the variant and `program` has no sensible default — omitting either is a host
    # bug, and the wheel would otherwise be prompted about a program called "".
    adapter = TypeAdapter(AuthorInput)
    with pytest.raises(ValidationError):
        adapter.validate_python({"program": "vault"})
    with pytest.raises(ValidationError):
        adapter.validate_python({"kind": "component"})
    with pytest.raises(ValidationError):
        adapter.validate_python({"kind": "not_a_kind", "program": "vault"})


def test_each_author_kind_carries_only_its_own_payload():
    # The unit and the analyzed model belong to one kind each, so neither is a field the other two
    # carry empty. A preflight has neither: it runs before anything is analyzed.
    adapter = TypeAdapter(AuthorInput)
    comp = adapter.validate_python(
        {"kind": "component", "program": "vault", "unit": {"slug": "farms"}}
    )
    assert isinstance(comp, ComponentInput) and comp.unit == {"slug": "farms"}
    assert not hasattr(comp, "model")
    setup = adapter.validate_python(
        {"kind": "setup", "program": "vault", "model": {"components": []}}
    )
    assert isinstance(setup, SetupInput) and setup.model == {"components": []}
    assert not hasattr(setup, "unit")
    pre = adapter.validate_python({"kind": "preflight", "program": "vault"})
    assert not hasattr(pre, "unit") and not hasattr(pre, "model")


def test_author_input_serializes_the_shape_the_wheel_deserializes():
    inp = PreflightInput(program="vault")
    assert inp.model_dump() == {
        "kind": "preflight",
        "program": "vault",
        # Empty rather than absent: the wheel is told the host resolved nothing and established
        # nothing, and applies its own convention. Nothing here is filled in for it.
        "source_unit": {},
        "props": [],
        "setup": None,
        "prep_facts": {},
        "args": {},
    }


def test_the_chain_shaped_payloads_cross_the_seam_uninterpreted():
    # source_unit / prep_facts / toolchain_request belong to the analyzed project's build system, and
    # this side declares no schema for any of them: two chains with nothing in common go through
    # unchanged. A field here would be the framework taking sides between them.
    cargo = {"dir": "programs/lend", "package": "example-lending", "lib": "example_lending"}
    move = {"package_dir": "sources", "named_addresses": {"vault": "0x1"}}
    for unit in (cargo, move):
        inp = TypeAdapter(AuthorInput).validate_json(
            SetupInput(program="vault", source_unit=unit, prep_facts={"idl": "a.json"})
            .model_dump_json()
        )
        assert inp.source_unit == unit and inp.prep_facts == {"idl": "a.json"}


def test_the_callout_list_is_derived_from_the_protocol():
    # The import-time check in `load_module` iterates this, so it must stay the protocol itself
    # rather than a hand-kept copy of it.
    assert set(CALLOUTS) == set(RustAppModule.__annotations__)
    assert "workspace_prep" in CALLOUTS and "sandbox_grants" in CALLOUTS


def test_a_module_missing_callouts_is_refused_at_load(monkeypatch):
    # `import_module` can't be checked statically, so the cast in `load_module` is guarded by this:
    # a wheel built against an older SDK names its gaps here instead of dying with an AttributeError
    # somewhere mid-run.
    stub = types.ModuleType("not_a_wheel")
    for name in CALLOUTS:
        if name not in ("workspace_prep", "finalize"):
            setattr(stub, name, lambda *_a: "{}")
    monkeypatch.setitem(sys.modules, "not_a_wheel", stub)

    with pytest.raises(TypeError, match="workspace_prep, finalize"):
        load_module("not_a_wheel")


def test_a_complete_module_loads():
    stub = types.ModuleType("a_wheel")
    for name in CALLOUTS:
        setattr(stub, name, lambda *_a: "{}")
    sys.modules["a_wheel"] = stub
    try:
        assert load_module("a_wheel") is stub
    finally:
        del sys.modules["a_wheel"]
