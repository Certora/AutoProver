"""What the deliverable says about a unit's edits to the program.

An edit changes the program the verdicts are about, so the report owes a reader that fact. It is said
through the shared `source_edits` hook, as EVM says it: one `SourceEditRecord` per delivered unit,
whose diff is that unit's overlay against the project — a patch the user can apply on its own.

No cargo and no network.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from composer.pipeline.ptypes import Delivered, GaveUp
from composer.spec.cvlr.harness import GeneratedHarness
from composer.spec.cvlr.pipeline import CvlrFormalizer
from composer.spec.source.munge.edit_store import AppliedEdit

pytestmark = pytest.mark.asyncio

_RESERVE = "programs/p/src/reserve.rs"
_SOURCE = "pub fn redeem_fees() -> Result<u64> {\n    let amount = calculate_fees()?;\n    Ok(amount)\n}\n"


def _formalizer() -> CvlrFormalizer:
    return CvlrFormalizer(
        GeneratedHarness, "prover", SimpleNamespace(), SimpleNamespace()  # type: ignore[arg-type]
    )


def _delivered(name: str, harness: GeneratedHarness) -> SimpleNamespace:
    return SimpleNamespace(
        feat=SimpleNamespace(slug=name.lower(), display_name=name),
        result=Delivered(result=harness, deliverable=Path(f"{name}.rs")),
    )


def _edited(feature: str, why: str) -> GeneratedHarness:
    munged = _SOURCE.replace(
        "pub fn redeem_fees", f'#[cfg_attr(feature = "{feature}", cvlr::early_panic)]\npub fn redeem_fees'
    )
    return GeneratedHarness(
        commentary="",
        harness="",
        applied_edits=[
            AppliedEdit(edit_id=f"id-{feature}", executive_summary="early_panic on redeem_fees", why_sound=why)
        ],
        vfs={_RESERVE: munged},
    )


@pytest.fixture
def run(tmp_path: Path) -> SimpleNamespace:
    (tmp_path / "programs" / "p" / "src").mkdir(parents=True)
    (tmp_path / _RESERVE).write_text(_SOURCE)
    return SimpleNamespace(source=SimpleNamespace(project_root=str(tmp_path)))


async def test_each_delivered_unit_gets_its_own_patch(run: SimpleNamespace):
    """Two units editing one file are two patches, each against the project, neither showing the
    other's change. Merging them is the user's."""
    records = await _formalizer().source_edits(
        [  # type: ignore[list-item]
            _delivered("Vault", _edited("unit_vault", "[3308] on the `?` path")),
            _delivered("Fees", _edited("unit_fees", "the fee path")),
        ],
        run,  # type: ignore[arg-type]
    )

    assert [r.component for r in records] == ["Vault", "Fees"]
    vault, fees = records
    assert vault.applied_edits[0].why_sound == "[3308] on the `?` path"
    assert vault.applied_edits[0].executive_summary == "early_panic on redeem_fees"
    assert f"a/{_RESERVE}" in vault.cumulative_diff
    assert '+#[cfg_attr(feature = "unit_vault", cvlr::early_panic)]' in vault.cumulative_diff
    assert "unit_fees" not in vault.cumulative_diff
    assert '+#[cfg_attr(feature = "unit_fees", cvlr::early_panic)]' in fees.cumulative_diff
    assert "unit_vault" not in fees.cumulative_diff


async def test_a_unit_that_edited_nothing_contributes_no_record(run: SimpleNamespace):
    """The record's presence is the claim. An empty one would say the outcomes are about modified
    code when they are not."""
    clean = _delivered("A", GeneratedHarness(commentary="", harness=""))
    gave_up = SimpleNamespace(
        feat=SimpleNamespace(slug="b", display_name="B"), result=GaveUp(reason="no")
    )
    assert await _formalizer().source_edits([clean, gave_up], run) == []  # type: ignore[arg-type]
