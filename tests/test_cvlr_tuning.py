"""Adding a points-to summary from the authoring loop, and what it invalidates.

``docs/cvlr-backend-plan.md`` §7.6. The Solana Prover's remedy for a path its pointer analysis
refuses is a summary, and until now the authoring loop had no way to add one: the tuning files were
written once by the scaffold and `put_harness` writes the harness module and nothing else. An
end-to-end run worked that out for itself — *"the only writing tool available is put_harness (writes
this module only)"* — derived the exact directive it needed, and then gave up on two of three units.

The tool that closes that gap hands the author an unsound instrument, so most of what is worth
pinning here is about *not* losing track of it: the justification survives into the file and the
report, and adding one costs the prover stamp.

No cargo, no network, no prover.
"""

import dataclasses
from pathlib import Path

import pytest

from composer.authoring.state import SkippedProperty, make_validation_stamper, spec_digest
from composer.spec.cvlr.env_paths import PathDialect
from composer.spec.cvlr.scaffold import ENV_FAMILIES, INLINING, SUMMARIES
from composer.spec.cvlr.state import PROVER_VALIDATION_KEY, tuning_history
from composer.spec.cvlr.tuning import (
    SummaryDirective,
    TuningFiles,
    merge_summaries,
    package_layer,
)

_WHY = "[3308] on the #[error_code] Display impl; no property asserts over an error message"
_DISPLAY = "^<vault::VaultError as core::fmt::Display>::fmt$"


@pytest.fixture
def tuning(tmp_path: Path) -> TuningFiles:
    """A scaffolded envs directory: every family's composite present, package layers headed."""
    envs = tmp_path / "envs"
    envs.mkdir()
    for family in ENV_FAMILIES:
        (envs / family.package).write_text(f"; {family.package}, yours to edit\n")
        (envs / family.composite).write_text("; composed\n")
    return TuningFiles(envs_dir=envs, dialect=PathDialect())


# ---------------------------------------------------------------------------------------------
# what lands on disk


def test_the_directive_reaches_the_file_the_conf_names(tuning: TuningFiles):
    """Both files, not just the package layer. The conf names the *composite*, so a directive
    written only to the layer the author owns would change nothing about the next submission — the
    tool would report success and the author would get an identical [3308]."""
    tuning.write((SummaryDirective(pattern=_DISPLAY, why=_WHY),))
    for name in (SUMMARIES.package, SUMMARIES.composite):
        assert _DISPLAY in (tuning.envs_dir / name).read_text(), name


def test_the_justification_is_written_beside_the_pattern(tuning: TuningFiles):
    """A summary is unsound, so the file has to say why this one was acceptable. Somebody reading
    this project's tuning file six months from now is the audience."""
    tuning.write((SummaryDirective(pattern=_DISPLAY, why=_WHY),))
    layer = (tuning.envs_dir / SUMMARIES.package).read_text()
    assert f"; {_WHY}" in layer


def test_the_scaffolds_header_survives_a_rewrite(tuning: TuningFiles):
    tuning.write((SummaryDirective(pattern=_DISPLAY, why=_WHY),))
    assert "yours to edit" in (tuning.envs_dir / SUMMARIES.package).read_text()


def test_a_return_shape_is_wrapped_as_a_type_annotation(tuning: TuningFiles):
    tuning.write((SummaryDirective(pattern="^foo$", why="w", returns="(*i32)(r1+0):num"),))
    assert "#[type((*i32)(r1+0):num)]" in (tuning.envs_dir / SUMMARIES.package).read_text()


def test_no_annotation_is_emitted_for_an_unconstrained_return(tuning: TuningFiles):
    """The common case — a formatting function nobody asserts over — and an empty `#[type()]` is
    not the same directive as no `#[type]` at all."""
    tuning.write((SummaryDirective(pattern="^foo$", why="w"),))
    assert "#[type" not in (tuning.envs_dir / SUMMARIES.package).read_text()


def test_the_layer_is_rewritten_rather_than_appended_to(tuning: TuningFiles):
    """The state is the source of truth and the file is derived from it. Appending would let the two
    drift, and a re-run replaying its recorded directives would double them."""
    tuning.write((SummaryDirective(pattern="^a$", why="w"),))
    tuning.write((SummaryDirective(pattern="^b$", why="w"),))
    layer = (tuning.envs_dir / SUMMARIES.package).read_text()
    assert "^b$" in layer and "^a$" not in layer


def test_the_inlining_family_is_left_alone(tuning: TuningFiles):
    """Summaries and inlining directives are different instruments and only one is on offer here."""
    before = (tuning.envs_dir / INLINING.package).read_text()
    tuning.write((SummaryDirective(pattern=_DISPLAY, why=_WHY),))
    assert (tuning.envs_dir / INLINING.package).read_text() == before


def test_dropping_every_directive_restores_the_scaffolded_layer(tuning: TuningFiles):
    tuning.write((SummaryDirective(pattern=_DISPLAY, why=_WHY),))
    tuning.write(())
    assert _DISPLAY not in (tuning.envs_dir / SUMMARIES.package).read_text()


# ---------------------------------------------------------------------------------------------
# refusing when the write would be inert


def test_an_unscaffolded_project_is_reported_rather_than_written_to(tmp_path: Path):
    """The failure that is otherwise mute. With no composite there is no tuning file in the conf, so
    a summary has no effect — and the author would read a successful tool result, re-run, and get
    the same error with no way to tell that the remedy never applied."""
    empty = TuningFiles(envs_dir=tmp_path, dialect=PathDialect())
    assert set(empty.missing()) == {f.composite for f in ENV_FAMILIES}


def test_a_scaffolded_project_reports_nothing_missing(tuning: TuningFiles):
    assert tuning.missing() == ()


# ---------------------------------------------------------------------------------------------
# what it costs


def test_a_summary_invalidates_a_stamp_earned_before_it():
    """The soundness-relevant half. A summary changes what the prover analyzed without changing a
    character of the draft, so the generic digest — which hashes the buffer and the skips — would
    carry a green stamp straight across it. `version_history` exists for exactly this ("a stamp
    earned before a source edit goes stale with it"), so the directives feed it."""
    draft = "#[rule]\nfn rule_a() {}\n"
    state = {
        "curr_spec": draft,
        "skipped": [],
        "summaries": [],
        "munges": [],
        "validations": {},
        "required_validations": [PROVER_VALIDATION_KEY],
    }
    stamp = make_validation_stamper(PROVER_VALIDATION_KEY)(state, tuning_history(state))  # type: ignore[arg-type]

    after = {**state, "summaries": [SummaryDirective(pattern=_DISPLAY, why=_WHY)]}
    assert stamp[PROVER_VALIDATION_KEY] != spec_digest(draft, [], tuning_history(after))  # type: ignore[arg-type]


def test_rewording_a_justification_does_not_cost_a_submission():
    """The other direction, and the reason the token is keyed on the pattern rather than the whole
    directive: correcting the wording of a `why` changes nothing the prover does, and charging a
    six-minute run for it would teach the author to leave justifications alone."""
    one = (SummaryDirective(pattern=_DISPLAY, why="first wording"),)
    two = (dataclasses.replace(one[0], why="a clearer second wording"),)
    assert tuning_history({"summaries": list(one), "munges": []}) == tuning_history({"summaries": list(two), "munges": []})  # type: ignore[arg-type]


def test_changing_the_return_shape_does_cost_one():
    one = (SummaryDirective(pattern="^f$", why="w"),)
    two = (SummaryDirective(pattern="^f$", why="w", returns="(*i32)(r1+0):num"),)
    assert tuning_history({"summaries": list(one), "munges": []}) != tuning_history({"summaries": list(two), "munges": []})  # type: ignore[arg-type]


def test_the_history_is_empty_when_nothing_was_summarized():
    """So a run that never touches a tuning file hashes exactly as it did before this existed."""
    assert tuning_history({"summaries": [], "munges": []}) == ()  # type: ignore[arg-type]


def test_a_skip_still_moves_the_digest():
    """Guarding the composition rather than the addition: threading a history through every stamp
    site must not displace what the digest already covered."""
    draft = "#[rule]\nfn rule_a() {}\n"
    bare = spec_digest(draft, [], ())
    skipped = spec_digest(draft, [SkippedProperty(property_title="p", reason="r")], ())
    assert bare != skipped


def test_the_rendered_layer_ends_with_a_newline():
    """Directives are line-oriented and the composite concatenates layers; a missing terminator
    would join the last directive to whatever follows it."""
    rendered = package_layer("; header", (SummaryDirective(pattern="^f$", why="w"),))
    assert rendered.endswith("\n")


# ---------------------------------------------------------------------------------------------
# two directives in one graph step


def test_two_directives_recorded_in_one_step_both_survive():
    """The bug that took two units down. `summaries` was a plain state key, so LangGraph refused the
    second write in a step with `InvalidUpdateError: Can receive only one value per step` — and the
    author was making several calls per turn while hunting for a pattern that matched.

    The reducer is what makes concurrent calls correct rather than merely legal: each tool
    contributes only its own directive, so neither has to have seen the other's."""
    a = SummaryDirective(pattern="^a$", why="w")
    b = SummaryDirective(pattern="^b$", why="w")
    assert [d.pattern for d in merge_summaries([], [a])] == ["^a$"]
    assert [d.pattern for d in merge_summaries([a], [b])] == ["^a$", "^b$"]


def test_the_same_pattern_twice_is_recorded_once():
    a = SummaryDirective(pattern="^a$", why="first")
    again = SummaryDirective(pattern="^a$", why="second")
    merged = merge_summaries([a], [again])
    assert [d.why for d in merged] == ["first"]


def test_the_merge_preserves_the_order_directives_were_added_in():
    """Directives are line-oriented and the prover applies them in file order, so the order the
    reducer settles on is the order the tuning file gets."""
    ds = [SummaryDirective(pattern=f"^{c}$", why="w") for c in "abc"]
    merged = merge_summaries(ds[:1], ds[1:])
    assert [d.pattern for d in merged] == ["^a$", "^b$", "^c$"]
