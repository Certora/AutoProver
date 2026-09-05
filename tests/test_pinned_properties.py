"""Pinning a run's analysis and properties so a later run starts at formalization.

The mechanism exists to make an end-to-end run over a large program cheap, so these tests are about
what would make it untrustworthy. Chiefly: pinning properties *without* pinning the analysis would
let a component's properties attach to a component that had changed underneath its name. That is
why the fixture carries both halves, and the round-trip test is the one that pins it down.
"""

import json
import pathlib

import pytest

from composer.pipeline.pinned import (
    PIN_VERSION,
    PinnedRun,
    git_head,
    load_pinned_run,
    write_pinned_run,
)
from composer.spec.solana.model import (
    ProgramComponent,
    SolanaApplication,
    SolanaInstruction,
    SolanaProgram,
)
from composer.spec.types import PropertyFormulation

PROPS = [
    {"sort": "safety_property", "title": "manager_must_sign",
     "description": "Initialize fails unless the manager signed."},
    {"sort": "invariant", "title": "supply_matches_reserve",
     "description": "Pool token supply equals the reserve excess."},
]

def _analysis() -> SolanaApplication:
    """A minimal but *real* analysis — one that actually yields a program and a component.

    Built rather than hand-written as JSON, because a hand-written literal validated happily while
    producing zero programs, which would have made the round-trip test pass on a model no unit
    could ever be derived from.
    """
    return SolanaApplication(
        application_type="Liquid staking pool",
        description="A stake pool that mints pool tokens against delegated stake.",
        components=[
            SolanaProgram(
                name="spl_stake_pool",
                program_identifier="spl_stake_pool",
                description="The stake pool program.",
                instructions=[
                    SolanaInstruction(
                        name="Initialize", description="Create a pool.",
                        requirements=["the manager signs"],
                    )
                ],
                components=[
                    ProgramComponent(
                        name="Pool Initialization",
                        description="Creating a pool and its validator list.",
                        instructions=["Initialize"], account_types=["StakePool"],
                        interactions=[], requirements=["the manager signs"],
                    )
                ],
            )
        ],
    )


ANALYSIS = _analysis().model_dump(mode="json")


def _fixture(tmp_path: pathlib.Path, **over: object) -> pathlib.Path:
    payload: dict[str, object] = {
        "version": PIN_VERSION,
        "target_commit": "22834f8",
        "analysis": ANALYSIS,
        "properties": {"Pool_Initialization": PROPS},
    }
    payload.update(over)
    p = tmp_path / "pin.json"
    p.write_text(json.dumps(payload))
    return p


def test_a_fixture_round_trips_through_the_real_model(tmp_path: pathlib.Path) -> None:
    """Write then read, with the ecosystem's own model doing the validating both ways.

    This is the property the whole design rests on: the analysis travels *with* the properties, so
    the units a replay formalizes against are the units the properties were written about.
    """
    analysis = SolanaApplication.model_validate(ANALYSIS)
    props = {"Pool_Initialization": [PropertyFormulation.model_validate(p) for p in PROPS]}
    path = tmp_path / "out" / "pin.json"

    write_pinned_run(path, analysis, props, tmp_path)
    back = load_pinned_run(path, SolanaApplication)

    assert back.analysis == analysis, "the analysis must survive the round trip intact"
    assert [p.title for p in back.properties["Pool_Initialization"]] == [
        "manager_must_sign", "supply_matches_reserve"
    ]
    assert back.total() == 2


def test_the_writer_creates_missing_directories(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "nested" / "deeper" / "pin.json"

    write_pinned_run(path, SolanaApplication.model_validate(ANALYSIS), {}, tmp_path)

    assert path.is_file()


def test_loading_validates_the_analysis_against_the_ecosystem_model(tmp_path: pathlib.Path) -> None:
    """A fixture from another ecosystem fails here, not at the first attribute that is missing."""
    path = _fixture(tmp_path, analysis={"nothing": "like a solana application"})

    with pytest.raises(Exception):
        load_pinned_run(path, SolanaApplication)


def test_a_future_pin_version_is_refused(tmp_path: pathlib.Path) -> None:
    """Fails on its version rather than on whatever downstream error the old shape produces."""
    path = _fixture(tmp_path, version=PIN_VERSION + 1)

    with pytest.raises(ValueError, match="pin format"):
        load_pinned_run(path, SolanaApplication)


@pytest.mark.parametrize("missing", ["analysis", "properties"])
def test_a_half_written_fixture_is_refused(tmp_path: pathlib.Path, missing: str) -> None:
    """Both halves or neither — a fixture with only properties is the design this replaced."""
    payload = json.loads(_fixture(tmp_path).read_text())
    del payload[missing]
    path = tmp_path / "half.json"
    path.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match=missing):
        load_pinned_run(path, SolanaApplication)


def test_an_empty_property_list_is_refused(tmp_path: pathlib.Path) -> None:
    """An empty list and an absent key mean the same thing to a run; only one may express it."""
    path = _fixture(tmp_path, properties={"Pool_Initialization": []})

    with pytest.raises(ValueError, match="drop the key instead"):
        load_pinned_run(path, SolanaApplication)


def test_no_components_pinned_is_refused(tmp_path: pathlib.Path) -> None:
    with pytest.raises(ValueError, match="no components pinned"):
        load_pinned_run(_fixture(tmp_path, properties={}), SolanaApplication)


def test_a_malformed_property_is_refused_at_load(tmp_path: pathlib.Path) -> None:
    """Validation happens while reading, not when a unit reaches formalization an hour in."""
    path = _fixture(tmp_path, properties={"Pool_Initialization": [{"title": "no_sort"}]})

    with pytest.raises(Exception):
        load_pinned_run(path, SolanaApplication)


def test_an_unknowable_checkout_is_silent(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A target that is not a git checkout cannot be compared, so nothing is claimed about it."""
    pinned = load_pinned_run(_fixture(tmp_path, target_commit="0" * 40), SolanaApplication)

    with caplog.at_level("WARNING"):
        pinned.check_target(tmp_path)

    assert not caplog.records


def test_no_recorded_commit_is_silent(tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture) -> None:
    pinned = load_pinned_run(_fixture(tmp_path, target_commit=None), SolanaApplication)

    with caplog.at_level("WARNING"):
        pinned.check_target(tmp_path)

    assert not caplog.records


def test_git_head_is_none_outside_a_repository(tmp_path: pathlib.Path) -> None:
    """The fixture must be usable against a plain directory, so this is a supported answer."""
    assert git_head(tmp_path) is None


def test_git_head_reads_a_real_repository() -> None:
    """And a real one round-trips, since that is what --pin-to records."""
    head = git_head(pathlib.Path(__file__).resolve().parent.parent)

    assert head is not None and len(head) == 40


def test_a_moved_checkout_is_reported_when_both_commits_are_known(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    repo = pathlib.Path(__file__).resolve().parent.parent
    pinned = PinnedRun(
        SolanaApplication.model_validate(ANALYSIS), {}, "0" * 40, tmp_path / "pin.json"
    )

    with caplog.at_level("WARNING"):
        pinned.check_target(repo)

    assert any("may no longer describe this source" in r.message for r in caplog.records)


def test_a_matching_checkout_is_silent(tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture) -> None:
    repo = pathlib.Path(__file__).resolve().parent.parent
    head = git_head(repo)
    assert head is not None
    pinned = PinnedRun(SolanaApplication.model_validate(ANALYSIS), {}, head, tmp_path / "pin.json")

    with caplog.at_level("WARNING"):
        pinned.check_target(repo)

    assert not caplog.records


def test_the_written_commit_is_the_projects_not_the_cwds(tmp_path: pathlib.Path) -> None:
    """``--pin-to`` records the *target's* commit; a fixture stamped with AutoProver's own HEAD
    would say nothing about the source the analysis describes."""
    path = tmp_path / "pin.json"

    write_pinned_run(path, SolanaApplication.model_validate(ANALYSIS), {}, tmp_path)

    assert json.loads(path.read_text())["target_commit"] is None


def test_units_derive_from_the_pinned_analysis(tmp_path: pathlib.Path) -> None:
    """The whole reason both halves travel together.

    A replay's units come from the fixture's own analysis, so the component a pinned slug names is
    the component its properties were written about — by construction, with no name to match
    against a freshly generated model and therefore nothing to drift.
    """
    from composer.pipeline.ecosystem import SOLANA

    pinned = load_pinned_run(_fixture(tmp_path), SolanaApplication)
    main = SOLANA.locate_main(pinned.analysis, _source(tmp_path))

    slugs = [u.slug for u in SOLANA.units(main)]

    assert set(pinned.properties) <= set(slugs), (
        f"pinned slugs {sorted(pinned.properties)} must be answerable by the fixture's own "
        f"analysis, which yields {slugs}"
    )


def _source(root: pathlib.Path):
    from composer.spec.context import SourceCode

    return SourceCode(
        project_root=str(root), relative_path="program/src/lib.rs",
        contract_name="spl_stake_pool", content=None, forbidden_read=None,
    )


# ---------------------------------------------------------------------------------------------
# the checked-in fixture


PIN_FILE = pathlib.Path(__file__).resolve().parent / "data" / "pins" / "spl-stake-pool.pin.json"


def test_the_checked_in_stake_pool_pin_still_loads() -> None:
    """A cheap guard on an expensive artifact.

    The fixture was recovered from a checkpoint database for a run that cost ~$100, and the run
    that would notice it had rotted is the one it exists to make cheap. So the format change that
    invalidates it should be caught here, in the fast suite, by the real loader.
    """
    from composer.pipeline.ecosystem import SOLANA

    pin = load_pinned_run(PIN_FILE, SOLANA.system_model)

    assert pin.total() == 219, "the run this was taken from logged 219 extracted properties"
    assert len(pin.properties) == 10
    assert pin.target_commit == "22834f8fee10484e8393a1be7a051035193ed312"


def test_every_pinned_component_answers_to_a_unit_of_its_own_analysis() -> None:
    """The invariant the two-halves design exists to give, checked on the real fixture rather than
    on a constructed one: a replay's units come from the pin, so every pinned key addresses a unit.
    """
    from composer.pipeline.ecosystem import SOLANA

    pin = load_pinned_run(PIN_FILE, SOLANA.system_model)
    main = SOLANA.locate_main(pin.analysis, _source(pathlib.Path("/nonexistent")))

    slugs = {u.slug for u in SOLANA.units(main)}

    assert set(pin.properties) == slugs, (
        "the fixture pins every component its analysis produces; a key that answered to nothing "
        "would formalize no rules while looking like a cheap pass"
    )


def test_the_component_the_gate_should_pin_is_present() -> None:
    """``Pool_Initialization`` is component 0 and the wrong default — the normative verification of
    this program has no Initialize rule, and no successful Initialize exists in the Prover's model
    with accounts unconstrained. ``Admin_Fee_Configuration`` covers ``set_fee``, which the
    normative verification does cover. See this directory's README."""
    from composer.pipeline.ecosystem import SOLANA

    pin = load_pinned_run(PIN_FILE, SOLANA.system_model)

    assert "Admin_Fee_Configuration" in pin.properties
    assert len(pin.properties["Admin_Fee_Configuration"]) == 23
