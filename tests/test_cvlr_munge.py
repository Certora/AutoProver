"""Verification-only edits to the program's own source: where each lands, and what is refused.

No cargo and no network.
"""

import textwrap
from pathlib import Path

import pytest

from composer.layout import INTERNAL_DIR
from composer.sandbox.recipes import SANDBOX_CARGO_DIR
from composer.spec.cvlr.munge import (
    AlreadyMunged,
    EarlyPanic,
    FunctionAmbiguous,
    FunctionExtraction,
    FunctionItem,
    FunctionMunge,
    FunctionNotFound,
    MockFn,
    Munged,
    NoFunctionBody,
    SourceDrifted,
    amended,
    apply_munge,
    function_item,
    latest,
    merge_munges,
    munge_history,
)


# ---------------------------------------------------------------------------------------------
# the source half
#
# The charter (plan §7.6.3) is six kinds read off the one real source munge in the corpus, and two of
# them are CVLR attributes an author can apply mechanically. What makes that safe rather than an
# agent editing a program is that the vocabulary is closed, the insert is one line, and a compile
# gate sits behind it. What a compile gate cannot catch is naming the wrong function, which is what
# these refusals are for.

SOURCE = """\
use anchor_lang::prelude::*;

/// Redeem the protocol's accumulated fees.
pub fn redeem_fees(reserve: &mut Reserve, slot: Slot) -> Result<u64> {
    let amount = reserve.calculate_fees()?;
    Ok(amount)
}

fn redeem_fees_inner(x: u64) -> u64 {
    x
}

impl Reserve {
    pub fn calculate_fees(&self) -> Result<u64> {
        Ok(0)
    }
}
"""

FEATURE = "unit_vault"


def _munge(
    function: str,
    kind=None,
    path: str = "programs/p/src/reserve.rs",
    feature: str = FEATURE,
) -> FunctionMunge:
    return FunctionMunge(
        path=path,
        function=function,
        kind=kind or EarlyPanic(),
        why="[3308] on the `?` path",
        feature=feature,
    )


def test_the_attribute_lands_above_the_signature_and_below_the_doc_comment():
    """Below the doc comment keeps the insert a one-line edit whose diff reads as one change; the
    attribute works from either position."""
    result = apply_munge(SOURCE, _munge("redeem_fees"))
    assert isinstance(result, Munged)
    lines = result.source.splitlines()
    assert lines[result.line - 2] == '#[cfg_attr(feature = "unit_vault", cvlr::early_panic)]'
    assert lines[result.line - 1].startswith("pub fn redeem_fees(")
    assert lines[result.line - 3].startswith("/// Redeem")


def test_a_longer_name_sharing_a_prefix_is_not_the_same_function():
    """`fn redeem_fees` must not match `fn redeem_fees_inner`. The trailing `(` or `<` is what
    settles it, and getting this wrong munges a function nobody asked about."""
    result = apply_munge(SOURCE, _munge("redeem_fees_inner"))
    assert isinstance(result, Munged)
    assert result.source.splitlines()[result.line - 1].startswith("fn redeem_fees_inner(")


def test_a_mock_names_its_stand_in_in_the_attribute():
    result = apply_munge(
        SOURCE, _munge("calculate_fees", MockFn(stand_in="crate::certora::mocks::fees"))
    )
    assert isinstance(result, Munged)
    assert (
        '#[cfg_attr(feature = "unit_vault", cvlr::mock_fn(with = crate::certora::mocks::fees))]'
        in result.source
    )


def test_the_indentation_of_the_function_is_matched():
    """`calculate_fees` is inside an impl block. An attribute at column zero above an indented `fn`
    compiles and reads as though nobody looked."""
    result = apply_munge(SOURCE, _munge("calculate_fees"))
    assert isinstance(result, Munged)
    assert '    #[cfg_attr(feature = "unit_vault", cvlr::early_panic)]' in result.source


def test_a_function_the_file_does_not_define_is_refused_with_what_it_does():
    """A compile gate would catch this two minutes and one build later, and say nothing about the
    name that was meant."""
    result = apply_munge(SOURCE, _munge("redeem_fee"))
    assert isinstance(result, FunctionNotFound)
    assert "redeem_fees" in result.nearby


def test_two_functions_of_one_name_are_refused_rather_than_guessed_at():
    """The failure a compile *accepts*: munging the wrong one of two same-named functions builds
    fine, leaves the rule failing, and gives no indication which was changed."""
    twice = SOURCE + """
impl Collateral {
    pub fn calculate_fees(&self) -> Result<u64> {
        Ok(1)
    }
}
"""
    result = apply_munge(twice, _munge("calculate_fees"))
    assert isinstance(result, FunctionAmbiguous)
    assert len(result.lines) == 2


def test_re_applying_a_munge_is_recognized_rather_than_doubled():
    """`stage` re-applies every recorded munge on each build, from whatever is on disk. That is only
    safe because the second application reports the attribute already there."""
    once = apply_munge(SOURCE, _munge("redeem_fees"))
    assert isinstance(once, Munged)
    twice = apply_munge(once.source, _munge("redeem_fees"))
    assert isinstance(twice, AlreadyMunged)


def test_a_munge_invalidates_a_stamp_earned_before_it():
    """The same channel a summary uses, for the stronger version of the reason: a munge changes the
    program the previous run's verdicts were about."""
    assert munge_history(()) == ()
    early = munge_history((_munge("redeem_fees"),))
    mocked = munge_history((_munge("redeem_fees", MockFn(stand_in="crate::m")),))
    assert early != mocked


def test_rewording_a_justification_does_not_cost_a_submission():
    """Keyed on what the prover sees differently — the file, the function, the attribute — and not
    on `why`, exactly as `summary_history` is."""
    one = _munge("redeem_fees")
    two = FunctionMunge(
        path=one.path,
        function=one.function,
        kind=one.kind,
        why="clearer wording",
        feature=one.feature,
    )
    assert munge_history((one,)) == munge_history((two,))


def test_the_same_munge_recorded_twice_lands_once():
    """A reducer for the reason `merge_summaries` is one: several tool calls can land in one graph
    step, and LangGraph refuses two writes to an unreduced key."""
    one = _munge("redeem_fees")
    assert len(merge_munges([one], [one])) == 1
    assert len(merge_munges([one], [_munge("calculate_fees")])) == 2
    # Same function, different file: two munges, not one.
    other_file = _munge("redeem_fees", path="programs/p/src/other.rs")
    assert len(merge_munges([one], [other_file])) == 2


def test_a_re_record_of_a_held_munge_is_a_correction_of_its_prose():
    """The ids match, so the two differ only in `why` — and the later one is the amendment. Dropping
    it, which is what the reducer used to do, is why a landed justification could not be fixed."""
    one = _munge("redeem_fees")
    corrected = amended(one, "superseded by the harness as it now stands")

    assert merge_munges([one], [corrected]) == [corrected]
    assert munge_history((corrected,)) == munge_history((one,))
    # First appearance keeps the position: a typo fix must not reshuffle the report.
    other = _munge("calculate_fees")
    assert merge_munges([one, other], [corrected]) == [corrected, other]


def test_latest_reduces_a_concatenation_the_same_way():
    """`_held` in the editor is `committed + proposed`, and an amendment of a committed record lands
    in `proposed` — so the two lists genuinely do carry the same id twice."""
    one = _munge("redeem_fees")
    assert latest([one, amended(one, "second")]) == [amended(one, "second")]


# ---------------------------------------------------------------------------------------------
# what the deliverable says about it
#
# A munge changes the program the verdicts are about, so the report owes a reader that fact. It is
# said through the shared `source_edits` hook rather than a CVLR-specific one, because
# `SourceEditRecord`'s own docstring is already exactly this disclosure: its presence means "the
# component's outcomes are claims about the modified code, not the code as shipped".


def _target(workdir: Path, pristine: Path | None = None):
    """A `HarnessTarget` for path questions only — the rest is not `pristine_source`'s business."""
    import asyncio
    from types import SimpleNamespace

    from composer.spec.cvlr.harness import HarnessModule
    from composer.spec.cvlr.tree import SharedTree
    from composer.spec.cvlr.verify import HarnessTarget

    return HarnessTarget(
        session=SimpleNamespace(workdir=workdir),  # type: ignore[arg-type]
        module_path=workdir / "src" / "spec.rs",
        package="p",
        package_root=workdir,
        tuning=SimpleNamespace(),  # type: ignore[arg-type]
        unit=HarnessModule("vault"),
        tree=SharedTree(pristine=pristine or workdir, root=workdir),
        build_sem=asyncio.Semaphore(1),
    )


def test_a_path_leaving_the_workdir_is_refused(tmp_path):
    """The working tree is the run's copy of the project, so writing in it never touches the
    user's tree — but only while every write stays inside it.

    The path is validated against the tree and then *answered* against the pristine copy, because
    those are the bytes `reconcile` replays onto: a tool that captured a region from a tree already
    carrying a sibling's munges would record an edit no replay could apply.
    """
    from composer.spec.cvlr.verify import NotInWorkdir

    workdir, project = tmp_path / "work", tmp_path / "project"
    (workdir / "src").mkdir(parents=True)
    target = _target(workdir, pristine=project)
    assert target.pristine_source("src/lib.rs") == project / "src" / "lib.rs"
    assert isinstance(target.pristine_source("../outside.rs"), NotInWorkdir)
    assert isinstance(target.pristine_source("/etc/passwd"), NotInWorkdir)


def test_a_dependency_inside_the_workdir_is_refused_too(tmp_path):
    """Containment is not the question, and this is the case that shows why.

    Confinement gives the run a private ``CARGO_HOME`` under ``<workdir>/.certora_internal``, so
    every dependency's unpacked source sits inside the workdir, below the program. A
    check that stopped at "is it in the workdir" would let a munge rewrite Anchor — for every crate
    in the graph, including the ones the property is about. Same failure
    ``validate_rule_subjects`` prevents one axis over.
    """
    from composer.spec.cvlr.munge import NotProjectSource

    workdir = tmp_path / "work"
    (workdir / "src").mkdir(parents=True)
    target = _target(workdir)

    anchor = str(
        SANDBOX_CARGO_DIR / "registry/src/index.crates.io-6f17d22/anchor-lang-0.31.1/src/error.rs"
    )
    refusal = target.pristine_source(anchor)
    assert isinstance(refusal, NotProjectSource)
    # Judged on the first component: the cargo home lives under the internal directory, which is
    # what the rule names — it needs no entry of its own.
    assert refusal.directory == INTERNAL_DIR.name

    for built in ("target/debug/build/x/out/gen.rs", ".certora_internal/x.rs", "certora_out/y.rs"):
        assert isinstance(target.pristine_source(built), NotProjectSource), built


def test_the_copy_and_the_munge_rule_are_one_list():
    """What a copy of the project leaves out is what a munge of the project may not touch. Drift in
    one direction is the dangerous one: a directory added to the copy's ignore list and not to this
    one would become munge-able."""
    from composer.spec.cvlr.munge import NOT_PROJECT_SOURCE, is_project_source
    from composer.spec.cvlr.pipeline import WORK_DIR

    assert WORK_DIR.name in NOT_PROJECT_SOURCE
    # The sandbox's private CARGO_HOME is covered by the directory it lives under, not by an entry
    # of its own — so the rule keeps holding if another scratch directory joins it there.
    assert INTERNAL_DIR.name in NOT_PROJECT_SOURCE
    assert not is_project_source(str(SANDBOX_CARGO_DIR / "registry/src/x/anchor/src/lib.rs"))
    assert is_project_source("programs/vault/src/lib.rs")
    assert not is_project_source("target/debug/deps/x.rs")
    # An empty path names no file and is not source.
    assert not is_project_source("")


@pytest.mark.asyncio
async def test_a_delivered_units_munges_reach_the_report_as_source_edits(tmp_path):
    from types import SimpleNamespace

    from composer.pipeline.ptypes import Delivered
    from composer.spec.cvlr.harness import GeneratedHarness
    from composer.spec.cvlr.pipeline import CvlrFormalizer

    relative = "programs/p/src/reserve.rs"
    (tmp_path / "programs" / "p" / "src").mkdir(parents=True)
    (tmp_path / relative).write_text(SOURCE)

    munge = _munge("redeem_fees", path=relative)
    harness = GeneratedHarness(commentary="", harness="", munges=[munge])
    outcome = SimpleNamespace(
        feat=SimpleNamespace(slug="vault", display_name="Vault"),
        result=Delivered(result=harness, deliverable=Path("x.rs")),
    )
    run = SimpleNamespace(source=SimpleNamespace(project_root=str(tmp_path)))

    formalizer = CvlrFormalizer(
        GeneratedHarness, "prover", SimpleNamespace(), SimpleNamespace()  # type: ignore[arg-type]
    )
    (record,) = await formalizer.source_edits([outcome], run)  # type: ignore[arg-type]

    assert record.component == "Vault"
    (edit,) = record.applied_edits
    assert edit.why_sound == "[3308] on the `?` path"
    assert "redeem_fees" in edit.executive_summary
    assert "`?` rewritten to `.unwrap()`" in edit.executive_summary
    # Derived from the munge records against the pristine project — no working tree involved, and
    # none exists here. That is what makes a report survive the tree being deleted, and what keeps
    # one unit's diff from showing a sibling's dormant lines.
    assert '+#[cfg_attr(feature = "unit_vault", cvlr::early_panic)]' in record.cumulative_diff
    assert f"a/{relative}" in record.cumulative_diff


@pytest.mark.asyncio
async def test_a_unit_that_munged_nothing_contributes_no_record(tmp_path):
    """The record's presence is the claim. An empty one would say the outcomes are about modified
    code when they are not."""
    from types import SimpleNamespace

    from composer.pipeline.ptypes import Delivered, GaveUp
    from composer.spec.cvlr.harness import GeneratedHarness
    from composer.spec.cvlr.pipeline import CvlrFormalizer

    run = SimpleNamespace(source=SimpleNamespace(project_root=str(tmp_path)))
    clean = SimpleNamespace(
        feat=SimpleNamespace(slug="a", display_name="A"),
        result=Delivered(result=GeneratedHarness(commentary="", harness=""), deliverable=Path("a")),
    )
    gave_up = SimpleNamespace(
        feat=SimpleNamespace(slug="b", display_name="B"), result=GaveUp(reason="no")
    )
    formalizer = CvlrFormalizer(
        GeneratedHarness, "prover", SimpleNamespace(), SimpleNamespace()  # type: ignore[arg-type]
    )
    assert await formalizer.source_edits([clean, gave_up], run) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------------
# extraction — the one kind that is not an attribute
#
# ``docs/who-edits-the-program.md`` §8.4. Everything above inserts a line above a signature, which
# needs only the signature to still be there. This replaces a region, so it needs two things the
# attribute kinds get for free and neither is optional: the region has to be *the same region* it
# was recorded against, and the deployed build has to be untouched by construction rather than by
# whoever wrote the replacement remembering to gate it.

_EXTRACTABLE = '''\
//! a program
/// Settle the position.
#[inline]
pub fn settle(state: &mut State, amount: u64) -> Result<u64> {
    let hint = "a } brace in a string";
    let sep = '}';
    // } in a comment
    /* nested /* } */ still a comment */
    if amount > state.cap {
        return err!(Bad);
    }
    state.total += amount;
    Ok(state.total)
}

pub fn other() -> u64 {
    1
}
'''

_REPLACEMENT = '''\
pub fn settle(state: &mut State, amount: u64) -> Result<u64> {
    if amount > state.cap {
        return err!(Bad);
    }
    settle_transition(state, amount)
}'''

_EXTRACTED = '''\
pub fn settle_transition(state: &mut State, amount: u64) -> Result<u64> {
    state.total += amount;
    Ok(state.total)
}'''


def _extraction(source: str = _EXTRACTABLE, feature: str = FEATURE, **over) -> FunctionExtraction:
    item = function_item(source, "settle")
    assert isinstance(item, FunctionItem)
    fields = {
        "path": "programs/p/src/reserve.rs",
        "function": "settle",
        "extracted_name": "settle_transition",
        "original": item.text,
        "replacement": _REPLACEMENT,
        "extracted": _EXTRACTED,
        "why": "the rule has to drive the accounting step",
        "feature": feature,
    }
    return FunctionExtraction(**{**fields, **over})


def test_the_item_ends_at_its_own_closing_brace_and_not_a_brace_in_a_literal():
    """A brace in a string, a character literal or a comment is not a brace. An attribute needed
    only the signature line, so this scanner is the whole cost of the kind that is a rewrite —
    stopping one brace early captures a fragment and the replay silently rewrites the wrong region.
    """
    item = function_item(_EXTRACTABLE, "settle")
    assert isinstance(item, FunctionItem)
    assert item.text.startswith("pub fn settle(")
    assert item.text.rstrip().endswith("Ok(state.total)\n}")
    assert "pub fn other" not in item.text
    assert item.signature == "pub fn settle(state: &mut State, amount: u64) -> Result<u64>"


def test_a_declaration_with_no_body_has_nothing_to_extract():
    trait = "pub trait Settles {\n    fn settle(&mut self, amount: u64) -> Result<u64>;\n}\n"
    assert isinstance(function_item(trait, "settle"), NoFunctionBody)


def test_the_pair_preserves_the_deployed_build_verbatim():
    """The property that makes a restructuring acceptable at all. The original is captured text
    rather than something a model retyped, and both `cfg` lines come from the record — so there is
    no spelling of an extraction in which a build without the feature sees anything new."""
    applied = apply_munge(_EXTRACTABLE, _extraction())
    assert isinstance(applied, Munged)
    item = function_item(_EXTRACTABLE, "settle")
    assert isinstance(item, FunctionItem)
    assert f'#[cfg(not(feature = "{FEATURE}"))]\n{item.text}' in applied.source
    assert applied.source.count(f'#[cfg(feature = "{FEATURE}")]') == 2
    assert _EXTRACTED in applied.source
    # The doc comment and the pre-existing attribute stay where they were, above the pair.
    assert "/// Settle the position.\n#[inline]\n#[cfg(not" in applied.source


def test_the_pair_is_indented_to_sit_where_the_original_sat():
    """An impl block is where most munge-able functions live, and Rust does not care — but a reader
    reviewing the diff does, and this diff is the artifact the reviewer rules on."""
    body = _EXTRACTABLE[_EXTRACTABLE.index("pub fn settle") : _EXTRACTABLE.index("pub fn other")]
    nested = f"impl State {{\n{textwrap.indent(body.rstrip(), '    ')}\n}}\n"
    applied = apply_munge(nested, _extraction(nested))
    assert isinstance(applied, Munged)
    assert f'    #[cfg(not(feature = "{FEATURE}"))]' in applied.source
    assert "    pub fn settle_transition(" in applied.source


def test_source_that_has_moved_since_the_extraction_was_recorded_is_reported():
    """The drift detector, and the reason the record stores the text rather than a line range. A
    replay that could not find its region would otherwise rewrite whatever is there now."""
    edit = _extraction()
    moved = _EXTRACTABLE.replace("state.total += amount;", "state.total = state.total + amount;")
    assert isinstance(apply_munge(moved, edit), SourceDrifted)


def test_replaying_an_extraction_twice_is_recognized_rather_than_nested():
    """The rendered pair *contains* the original verbatim, so a second replay would find it inside
    the `#[cfg(not(..))]` half and nest one pair inside another. Asking "already applied" first is
    what stops that."""
    once = apply_munge(_EXTRACTABLE, _extraction())
    assert isinstance(once, Munged)
    assert isinstance(apply_munge(once.source, _extraction()), AlreadyMunged)


def test_changing_the_rewrite_changes_the_edit_id_and_rewording_the_reason_does_not():
    """An attribute's whole content is its name, so `edit_id` can spell it out. Here the compiler
    sees two blocks of written Rust, and an id blind to them would let a re-recorded extraction
    inherit the previous one's review and the previous one's prover stamp."""
    base = _extraction()
    assert base.edit_id == _extraction(why="a clearer sentence").edit_id
    changed = _extraction(extracted=_EXTRACTED.replace("+=", "= state.total +"))
    assert changed.edit_id != base.edit_id
    assert munge_history((changed,)) != munge_history((base,))


def test_a_sibling_unit_s_attribute_lands_above_the_pair():
    """The interesting half of `replay`'s order. Applied after the split, a `cfg_attr` would find
    two definitions of one name and refuse; applied before, it sits above the `#[cfg(not(..))]` —
    which is where it belongs, because the unit that recorded it builds with the extraction feature
    off and compiles the original."""
    from composer.spec.cvlr.tree import replay

    updated, drifted = replay(
        _EXTRACTABLE, (_extraction(), _munge("settle", path="programs/p/src/reserve.rs", feature="unit_other"))
    )
    assert not drifted
    assert (
        '#[cfg_attr(feature = "unit_other", cvlr::early_panic)]\n'
        f'#[cfg(not(feature = "{FEATURE}"))]\npub fn settle('
    ) in updated


def test_an_extraction_reaches_the_report_through_the_same_channel_as_an_attribute():
    """`describe()` is what `SourceEditRecord` and the judge briefing both read, so a kind the
    report cannot describe is a kind the report silently omits."""
    edit = _extraction()
    assert "settle_transition" in edit.describe()
    assert edit.edit_id in munge_history((edit,))[0]
