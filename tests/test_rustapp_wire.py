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
    FailureKind,
    PreflightInput,
    ProgramCrate,
    SetupInput,
    RustAppModule,
    Unit,
    ValidateBuildFailed,
    ValidateVerdicts,
    parse_compile,
    parse_units,
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


def test_a_null_target_means_a_unit_is_its_own_validation_target():
    # `target` must be *present*; null is how a wheel says the unit is checked on its own. Absence is
    # not a third spelling of that — see the test above.
    units = parse_units('[{"property": "p", "unit": "rule_p", "target": null}]')
    assert units == [Unit(property="p", unit="rule_p", target=None)]
    assert units[0].target_or_unit() == "rule_p"
    # …and a shared target is what the host runs once for several units.
    assert Unit(property="p", unit="rule_p", target="c_vault").target_or_unit() == "c_vault"


def test_an_outcome_the_host_does_not_know_is_refused():
    # The outcome vocabulary is closed on both sides, so a label the host has never heard of is a
    # variant added to one `Outcome` and not the other — not something to render as UNKNOWN.
    with pytest.raises(ValidationError):
        parse_validate(json.dumps({"kind": "verdicts",
                                   "verdicts": [["u", wire_verdict("FLAKY")]]}))


def test_a_workspace_plan_that_only_places_files_needs_no_toolchain():
    files_only = parse_workspace_prep(json.dumps(
        wire_workspace_prep(files={"fuzz/Cargo.toml": "[package]"})))
    assert files_only.files and not files_only.needs_toolchain
    for asking in ({"warm_dirs": ["fuzz"]}, {"build_program": "lend"}, {"idl_dest": "idl.json"}):
        plan = parse_workspace_prep(json.dumps(wire_workspace_prep(**asking)))
        assert plan.needs_toolchain


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
        # Every part empty — the SDK's FFI boundary fills them from its own convention.
        "program_crate": {"dir": "", "package": "", "lib": "", "anchor": ""},
        "props": [],
        "setup": None,
        "idl": None,
        "args": {},
    }


def test_failure_kind_defaults_to_the_compile_gate():
    # A judge rejection has to be distinguishable: the draft compiled, so a revise prompt that
    # framed it as a build error would be misleading.
    assert FailureKind("compile") is FailureKind.COMPILE
    assert FailureKind.JUDGE.value == "judge"


def test_program_crate_carries_the_three_independent_names():
    crate = ProgramCrate(dir="programs/lend", package="example-lending", lib="example_lending")
    # None follows from another — the point of resolving them from the manifest.
    assert (crate.dir, crate.package, crate.lib, crate.anchor) == (
        "programs/lend", "example-lending", "example_lending", ""
    )


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
