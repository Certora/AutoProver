"""Points-to summaries the authoring loop adds, and what adding one invalidates.

A summary is unsound. The justification is written into the tuning file, and
adding one drops the prover stamp.

No cargo, no network, no prover.
"""

import dataclasses
from pathlib import Path

import pytest

from composer.authoring.state import checked_digest, make_validation_stamper
from composer.spec.cvlr.conf import TunableConf
from composer.spec.cvlr.env_paths import PathDialect
from composer.spec.cvlr.state import PROVER_VALIDATION_KEY, tuning_history
from composer.spec.cvlr.tuning import (
    ENV_FAMILIES,
    INLINING,
    SummaryDirective,
    TuningFiles,
    merge_summaries,
)

_WHY = "[3308] on the #[error_code] Display impl; no property asserts over an error message"
_DISPLAY = "^<vault::VaultError as core::fmt::Display>::fmt$"


_UNIT = "deposits"


@pytest.fixture
def tuning(tmp_path: Path) -> TuningFiles:
    """An envs directory with every family's package composite present."""
    envs = tmp_path / "envs"
    envs.mkdir()
    for family in ENV_FAMILIES:
        (envs / family.composite).write_text("; composed\n")
    return TuningFiles(envs_dir=envs, dialect=PathDialect(), unit=_UNIT)


# ---------------------------------------------------------------------------------------------
# what lands on disk


def test_the_directive_reaches_the_file_the_conf_names(tuning: TuningFiles):
    tuning.write((SummaryDirective(pattern=_DISPLAY, why=_WHY),))
    assert _DISPLAY in tuning.composite_path().read_text()


def test_the_layer_is_rewritten_rather_than_appended_to(tuning: TuningFiles):
    """Each write replaces the file. Appending would keep directives the state no longer has."""
    tuning.write((SummaryDirective(pattern="^a$", why="w"),))
    tuning.write((SummaryDirective(pattern="^b$", why="w"),))
    composite = tuning.composite_path().read_text()
    assert "^b$" in composite and "^a$" not in composite


def test_the_inlining_family_is_left_alone(tuning: TuningFiles):
    """Writing summaries does not create or change an inlining file."""
    before = (tuning.envs_dir / INLINING.composite).read_text()
    tuning.write((SummaryDirective(pattern=_DISPLAY, why=_WHY),))
    assert (tuning.envs_dir / INLINING.composite).read_text() == before
    assert not (tuning.envs_dir / INLINING.unit_composite(_UNIT)).exists()


def test_dropping_every_directive_removes_it_from_the_file(tuning: TuningFiles):
    """Writing an empty list removes a directive that an earlier write put on disk."""
    tuning.write((SummaryDirective(pattern=_DISPLAY, why=_WHY),))
    tuning.write(())
    assert _DISPLAY not in tuning.composite_path().read_text()


def test_an_absent_unit_composite_is_not_a_missing_tuning_file(tuning: TuningFiles):
    """A directive can be recorded before the first build. The target creates the file."""
    assert not tuning.composite_path().is_file()
    assert tuning.missing() == ()


def test_ensuring_the_composite_leaves_an_existing_file(tuning: TuningFiles):
    """Constructing the target again must not drop directives a build has written."""
    tuning.ensure_composite()
    path = tuning.composite_path()
    path.write_text(path.read_text() + ";;; kept\n")
    tuning.ensure_composite()
    assert ";;; kept\n" in path.read_text()


def test_two_units_do_not_see_each_others_directives(tmp_path: Path):
    envs = tmp_path / "envs"
    envs.mkdir()
    for family in ENV_FAMILIES:
        (envs / family.composite).write_text("; composed\n")
    a = TuningFiles(envs_dir=envs, dialect=PathDialect(), unit="a")
    b = TuningFiles(envs_dir=envs, dialect=PathDialect(), unit="b")
    a.write((SummaryDirective(pattern="^only_a$", why="w"),))
    b.write((SummaryDirective(pattern="^only_b$", why="w"),))
    assert "^only_b$" not in a.composite_path().read_text()
    assert "^only_a$" not in b.composite_path().read_text()


# ---------------------------------------------------------------------------------------------
# what it costs


def test_a_summary_invalidates_a_stamp_earned_before_it():
    """A summary changes the digest without changing the draft.

    The generic digest hashes the buffer and the skips. The summary has to be
    in the history or a stamp from before it would still apply.
    """
    draft = "#[rule]\nfn rule_a() {}\n"
    state = {
        "curr_spec": draft,
        "skipped": [],
        "expected_failures": {},
        "summaries": [],
        "vfs": {},
        "version_history": [],
        "prover_settings": TunableConf(),
        "validations": {},
        "required_validations": [PROVER_VALIDATION_KEY],
    }
    stamp = make_validation_stamper(PROVER_VALIDATION_KEY)(state, tuning_history(state))  # type: ignore[arg-type]

    after = {**state, "summaries": [SummaryDirective(pattern=_DISPLAY, why=_WHY)]}
    assert stamp[PROVER_VALIDATION_KEY] != checked_digest(after, tuning_history(after))  # type: ignore[arg-type]


def test_rewording_a_justification_does_not_cost_a_submission():
    """Rewording ``why`` does not change the history token. The prover does not read it."""
    one = (SummaryDirective(pattern=_DISPLAY, why="first wording"),)
    two = (dataclasses.replace(one[0], why="a clearer second wording"),)
    assert tuning_history({"summaries": list(one), "version_history": [], "prover_settings": TunableConf()}) == tuning_history({"summaries": list(two), "version_history": [], "prover_settings": TunableConf()})  # type: ignore[arg-type]


def test_changing_the_return_shape_does_cost_one():
    one = (SummaryDirective(pattern="^f$", why="w"),)
    two = (SummaryDirective(pattern="^f$", why="w", returns="(*i32)(r1+0):num"),)
    assert tuning_history({"summaries": list(one), "version_history": [], "prover_settings": TunableConf()}) != tuning_history({"summaries": list(two), "version_history": [], "prover_settings": TunableConf()})  # type: ignore[arg-type]


def test_the_conf_is_always_in_the_history_and_the_tuning_files_only_when_used():
    """The history always includes the conf. Summaries appear only when there are some.

    A verdict under one loop bound or ``rule_sanity`` does not apply to another.
    """
    empty = tuning_history({"summaries": [], "version_history": [], "prover_settings": TunableConf()})  # type: ignore[arg-type]
    assert len(empty) == 1 and empty[0].startswith("conf:")

    changed = tuning_history({"summaries": [], "version_history": [], "prover_settings": TunableConf(loop_iter=3)})  # type: ignore[arg-type]
    assert changed != empty


# ---------------------------------------------------------------------------------------------
# two directives in one graph step


def test_two_directives_recorded_in_one_step_both_survive():
    """Two directives from one graph step both survive.

    LangGraph allows one write per step on a field with no reducer. Each call
    contributes only its own directive.
    """
    a = SummaryDirective(pattern="^a$", why="w")
    b = SummaryDirective(pattern="^b$", why="w")
    assert [d.pattern for d in merge_summaries([], [a])] == ["^a$"]
    assert [d.pattern for d in merge_summaries([a], [b])] == ["^a$", "^b$"]


def test_the_same_pattern_twice_is_recorded_once():
    a = SummaryDirective(pattern="^a$", why="first")
    again = SummaryDirective(pattern="^a$", why="second")
    merged = merge_summaries([a], [again])
    assert [d.why for d in merged] == ["first"]
