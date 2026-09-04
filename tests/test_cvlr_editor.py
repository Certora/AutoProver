"""The munge editor: the one agent allowed to change the program under verification.

``docs/who-edits-the-program.md``. What is worth pinning is the ownership rule and the two gates that
make delegating an edit better than doing it inline — the approval a later edit voids, and the check
that the edit actually reached the compiler.

No LLM, no network, no cargo except in the one test that reads a dep-info fixture.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from composer.cargo.depinfo import compiled_sources
from composer.spec.cvlr import editor as editor_mod
from composer.spec.cvlr.editor import (
    ApplyEarlyPanic,
    ApplyHookOnEntry,
    ApplyHookOnExit,
    ApplyInlineNever,
    ApplyMockFn,
    DropMunge,
    EditsProposed,
    MungeFunction,
    RequestReview,
    SubmitEdits,
    kind_of,
)
from composer.spec.cvlr.harness import HarnessModule
from composer.spec.cvlr.munge import (
    DropMunges,
    EarlyPanic,
    FunctionMunge,
    HookOnEntry,
    HookOnExit,
    InlineNever,
    MockFn,
    merge_munges,
)
from composer.spec.cvlr.tree import SharedTree

_RESERVE = "programs/p/src/reserve.rs"
_SOURCE = """\
pub fn redeem_fees(reserve: &mut Reserve) -> Result<u64> {
    let amount = reserve.calculate_fees()?;
    Ok(amount)
}

pub fn calculate_fees(reserve: &Reserve) -> Result<u64> {
    Ok(0)
}
"""


def _target(tmp_path: Path):
    """A `HarnessTarget` over a tree holding one munge-able file."""
    from composer.spec.cvlr.verify import HarnessTarget

    root = tmp_path / "work"
    (root / "programs" / "p" / "src").mkdir(parents=True)
    (root / _RESERVE).write_text(_SOURCE)
    return HarnessTarget(
        session=SimpleNamespace(workdir=root),  # type: ignore[arg-type]
        module_path=root / "programs/p/src/certora/specs/vault.rs",
        package="p",
        tuning=SimpleNamespace(),  # type: ignore[arg-type]
        unit=HarnessModule("vault"),
        tree=SharedTree(pristine=root, root=root),
        build_sem=asyncio.Semaphore(1),
    )


def _author_state(**over):
    """A `CvlrGenerationState`-shaped dict, filled with the keys the tools declare."""
    base = {
        "curr_spec": "// draft\n",
        "skipped": [],
        "validations": {},
        "required_validations": [],
        "property_rules": [],
        "rule_subjects": [],
        "summaries": [],
        "munges": [],
        "expected_failures": {},
        "prover_link": None,
        "failed": None,
        "budget_curtailed": False,
        "messages": [],
    }
    return {**base, **over}


def _editor_state(**over):
    base = {
        "request": "rule_x has no verdict; the trace stops on the `?` in calculate_fees",
        "feature": "unit_vault",
        "draft": "// draft\n",
        "summaries": [],
        "committed": [],
        "proposed": [],
        "reviewed_digest": None,
        "memory": None,
        "messages": [],
    }
    return {**base, **over}


async def _run(tool_cls, deps, state, **fields):
    token = tool_cls._dep_ctx.set(deps) if deps is not None else None
    try:
        answer = tool_cls(state=state, tool_call_id="tc", **fields).run()
        return await answer if asyncio.iscoroutine(answer) else answer
    finally:
        if token is not None:
            tool_cls._dep_ctx.reset(token)


# ---------------------------------------------------------------------------------------------
# ownership


def test_the_author_cannot_edit_the_program():
    """The whole point of the topology, and the one thing a reader should be able to check quickly:
    `munge_function` is the editor's tool and appears on no author-facing belt."""
    import composer.spec.cvlr.verify as verify_mod

    assert not hasattr(verify_mod, "MungeFunction")
    assert "munge_function" not in verify_mod.gate_tools.__doc__ or "deliberately absent" in (
        verify_mod.gate_tools.__doc__ or ""
    )


@pytest.mark.asyncio
async def test_the_tool_sets_the_feature_not_the_model(tmp_path: Path):
    """A munge is scoped to one unit because it is gated on that unit's cargo feature. If the model
    could name the feature it could name another unit's, or `certora`, and the scoping that lets
    every unit share one working tree would be advisory."""
    target = _target(tmp_path)
    result = await _run(
        MungeFunction,
        target,
        _editor_state(),
        path=_RESERVE,
        function="calculate_fees",
        munge=ApplyEarlyPanic(),
        why="[3308] on the error type's Display impl, reached by this `?`",
    )
    (record,) = result.update["proposed"]
    assert record.feature == "unit_vault" == target.unit.feature
    assert "unit_vault" in record.attribute_line("")


@pytest.mark.asyncio
async def test_a_munge_of_a_dependency_is_refused(tmp_path: Path):
    """Containment is not the question. The sandbox puts `CARGO_HOME` inside the tree, so a check
    that stopped at "is it in the tree" would let the editor rewrite Anchor for every crate."""
    answer = await _run(
        MungeFunction,
        _target(tmp_path),
        _editor_state(),
        path=".sandbox_cargo/registry/src/idx/anchor-lang-0.31.1/src/error.rs",
        function="fmt",
        munge=ApplyEarlyPanic(),
        why="w",
    )
    assert isinstance(answer, str) and ".sandbox_cargo" in answer


@pytest.mark.asyncio
async def test_an_unexplained_munge_is_refused(tmp_path: Path):
    answer = await _run(
        MungeFunction,
        _target(tmp_path),
        _editor_state(),
        path=_RESERVE,
        function="calculate_fees",
        munge=ApplyEarlyPanic(),
        why="   ",
    )
    assert isinstance(answer, str) and "non-empty `why`" in answer


# ---------------------------------------------------------------------------------------------
# the vocabulary


def test_every_choice_maps_to_a_kind_and_renders():
    """Five kinds, and the two the editor added are the ones that answer problems the closed
    two-kind vocabulary had to skip."""
    cases = [
        (ApplyEarlyPanic(), EarlyPanic, "cvlr::early_panic"),
        (
            ApplyMockFn(stand_in="crate::certora::specs::vault::f"),
            MockFn,
            "cvlr::mock_fn(with = crate::certora::specs::vault::f)",
        ),
        (ApplyInlineNever(), InlineNever, "inline(never)"),
        (ApplyHookOnEntry(call="saw()"), HookOnEntry, "cvlr::hook_on_entry(saw())"),
        (ApplyHookOnExit(call="left()"), HookOnExit, "cvlr::hook_on_exit(left())"),
    ]
    for choice, expected, rendered in cases:
        kind = kind_of(choice)
        assert isinstance(kind, expected)
        assert kind.attribute() == rendered
        assert kind.describe()


# ---------------------------------------------------------------------------------------------
# the approval gate


def _munge(function: str = "calculate_fees") -> FunctionMunge:
    return FunctionMunge(
        path=_RESERVE, function=function, kind=EarlyPanic(), why="w", feature="unit_vault"
    )


@pytest.mark.asyncio
async def test_submitting_without_an_approval_is_refused(tmp_path: Path):
    answer = await _run(
        SubmitEdits,
        _target(tmp_path),
        _editor_state(proposed=[_munge()], reviewed_digest=None),
        summary=EditsProposed(executive_summary="s", why_sound="w"),
    )
    assert isinstance(answer, str) and "not approved" in answer


@pytest.mark.asyncio
async def test_an_approval_is_void_the_moment_the_edits_change(tmp_path: Path):
    """EVM's discipline and the reason for it: an approved diff and a submitted one must be the same
    diff, or the review is of something nobody shipped."""
    held = [_munge()]
    stale = editor_mod._digest(held)
    answer = await _run(
        SubmitEdits,
        _target(tmp_path),
        _editor_state(proposed=[*held, _munge("redeem_fees")], reviewed_digest=stale),
        summary=EditsProposed(executive_summary="s", why_sound="w"),
    )
    assert isinstance(answer, str) and "voided it" in answer


@pytest.mark.asyncio
async def test_applying_a_munge_voids_a_standing_approval(tmp_path: Path):
    result = await _run(
        MungeFunction,
        _target(tmp_path),
        _editor_state(reviewed_digest="whatever"),
        path=_RESERVE,
        function="calculate_fees",
        munge=ApplyEarlyPanic(),
        why="[3308] on the `?`",
    )
    assert result.update["reviewed_digest"] is None


@pytest.mark.asyncio
async def test_dropping_a_munge_voids_a_standing_approval():
    result = await _run(
        DropMunge,
        None,
        _editor_state(proposed=[_munge()], reviewed_digest="whatever"),
        edit_id=_munge().edit_id,
    )
    assert result.update["reviewed_digest"] is None
    assert result.update["proposed"] == DropMunges(frozenset({_munge().edit_id}))


def test_the_digest_ignores_prose_and_not_substance():
    """Keyed on `edit_id` for the reason `munge_history` is: re-wording a justification must not cost
    a review, and changing what the compiler sees must."""
    import dataclasses

    one = _munge()
    reworded = dataclasses.replace(one, why="a clearer second wording")
    other = _munge("redeem_fees")
    assert editor_mod._digest([one]) == editor_mod._digest([reworded])
    assert editor_mod._digest([one]) != editor_mod._digest([one, other])


@pytest.mark.asyncio
async def test_reviewing_nothing_is_refused(tmp_path: Path):
    answer = await _run(
        RequestReview,
        SimpleNamespace(pristine=tmp_path, review=None),
        _editor_state(proposed=[]),
        summary=EditsProposed(executive_summary="s", why_sound="w"),
    )
    assert isinstance(answer, str) and "no munges" in answer


# ---------------------------------------------------------------------------------------------
# undo


def test_a_reverted_munge_leaves_the_state():
    """`revert_munge` is the author's final say, and it works because the reducer can remove: the
    tree is rebuilt from the pristine copy and replayed, so an id that leaves the list leaves the
    file."""
    one, two = _munge(), _munge("redeem_fees")
    assert merge_munges([one, two], DropMunges(frozenset({one.edit_id}))) == [two]


def test_reverting_an_unknown_id_is_reported_rather_than_silent():
    from composer.spec.cvlr.editor import RevertMunge

    answer = RevertMunge(
        state=_author_state(munges=[_munge()]),  # type: ignore[arg-type]
        tool_call_id="tc",
        edit_id="nope",
    ).run()
    assert isinstance(answer, str) and "No munge has that id" in answer


def test_reverting_a_known_id_removes_it_from_the_build():
    from composer.spec.cvlr.editor import RevertMunge

    one = _munge()
    result = RevertMunge(
        state=_author_state(munges=[one]),  # type: ignore[arg-type]
        tool_call_id="tc",
        edit_id=one.edit_id,
    ).run()
    assert result.update["munges"] == DropMunges(frozenset({one.edit_id}))


# ---------------------------------------------------------------------------------------------
# did the edit reach the compiler


def _dep_info(tmp_path: Path, listed: list[str]) -> Path:
    root = tmp_path / "work"
    deps = root / "target" / "debug" / "deps"
    deps.mkdir(parents=True)
    for name in listed:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    rhs = " ".join(listed)
    (deps / "p-abc123.d").write_text(f"{deps}/p-abc123.d: {rhs}\n\n" + "".join(f"{n}:\n" for n in listed))
    return root


def test_a_file_the_build_never_read_is_visible(tmp_path: Path):
    """The Solana `EditsNotCompiled`. An attribute in a file no enabled feature reaches changes
    nothing and reports nothing, and the report still claims a source edit."""
    marker = "src/certora/specs/vault.rs"
    root = _dep_info(tmp_path, ["src/lib.rs", marker])
    (root / _RESERVE).parent.mkdir(parents=True, exist_ok=True)
    (root / _RESERVE).write_text(_SOURCE)

    reached = compiled_sources(root, "p", marker=root / marker)
    assert reached is not None
    assert (root / "src/lib.rs").resolve() in reached
    assert (root / _RESERVE).resolve() not in reached


def test_dep_info_that_does_not_name_the_marker_is_not_this_build(tmp_path: Path):
    """Feature variants of one crate share a `target/`, and nothing in a `.d` filename says which
    produced it. The marker is the discriminator; without a match the honest answer is "unknown",
    not a false pass."""
    root = _dep_info(tmp_path, ["src/lib.rs"])
    assert compiled_sources(root, "p", marker=root / "src/certora/specs/vault.rs") is None


def test_a_crate_name_with_a_hyphen_finds_its_dep_info(tmp_path: Path):
    """Cargo normalises `-` to `_` in crate names and therefore in the dep-info filename; a package
    named `my-program` writes `my_program-<hash>.d`."""
    root = tmp_path / "work"
    deps = root / "target" / "debug" / "deps"
    deps.mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "lib.rs").write_text("")
    (deps / "my_program-abc.d").write_text(f"{deps}/my_program-abc.d: src/lib.rs\n")
    assert compiled_sources(root, "my-program", marker=root / "src/lib.rs") is not None
