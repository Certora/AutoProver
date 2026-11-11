"""A prioritized run's focus, on the CVLR backend.

The shared driver ranks the properties and hands the formalizer one batch; what it cannot do is make
the author pursue that batch rather than retire it. These tests pin the CVLR half: the formalizer
binds the focus only when the run is prioritized, the author's prompt tells it what the tightened
exits mean, and the give-up gate counts the tool CVLR actually verifies with.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from composer.authoring.tools import FocusPolicy
from composer.pipeline.ptypes import GaveUp
from composer.pipeline.run_mode import RunMode
from composer.spec.cvlr import pipeline as cvlr_pipeline
from composer.spec.cvlr.pipeline import CvlrFormalizer
from composer.spec.cvlr.verify import VERIFY_TOOL, gate_tools
from composer.spec.types import PropertyFormulation, PropertyTitle

from tests.test_cvlr_knowledge import _author_task_prompt, _flat

PROPS = [
    PropertyFormulation(title="solvency", sort="invariant", description="assets cover shares"),
    PropertyFormulation(title="shares_sane", sort="invariant", description="no share inflation"),
]


class _NoTuning:
    def __init__(self, **_kw):
        pass

    def write(self, _summaries):
        pass

    def ensure_composite(self):
        pass


async def _focus_for(run_mode: RunMode, monkeypatch, tmp_path: Path) -> FocusPolicy | None:
    """Drive ``CvlrFormalizer.formalize`` with the author stubbed, and return the focus it bound."""
    seen: dict = {}

    async def fake_author(_ctx, **kwargs):
        seen.update(kwargs)
        return GaveUp(reason="stub")

    monkeypatch.setattr(cvlr_pipeline, "batch_cvlr_generation", fake_author)
    monkeypatch.setattr(cvlr_pipeline, "TuningFiles", _NoTuning)
    deps = SimpleNamespace(
        package_dir=Path("programs/vault"),
        preflight=SimpleNamespace(scaffold=SimpleNamespace(dialect=None), package="vault"),
        prover_opts=None,
        cex_analysis=None,
        versions="cvlr 0.6.1",
        caveats=(),
        editing=None,
    )
    build = SimpleNamespace(
        warm_failure=None,
        session=SimpleNamespace(workdir=tmp_path),
        tree=None,
        build_sem=None,
        submission_dir=tmp_path,
    )
    formalizer = CvlrFormalizer(object, "prover", deps, build, ())  # type: ignore[arg-type]
    run = SimpleNamespace(run_mode=run_mode, env=None)
    ctx = SimpleNamespace(abstract=lambda _ty: None)
    feat = SimpleNamespace(slug="vault_core")
    await formalizer.formalize("vault", feat, PROPS, ctx, run, None)  # type: ignore[arg-type]
    return seen["focus"]


@pytest.mark.asyncio
async def test_a_prioritized_run_protects_every_property_of_its_batch(monkeypatch, tmp_path):
    focus = await _focus_for(RunMode.PRIORITIZED, monkeypatch, tmp_path)
    assert focus is not None
    assert focus.protected == (PropertyTitle("solvency"), PropertyTitle("shares_sane"))


@pytest.mark.asyncio
async def test_a_comprehensive_run_binds_no_focus(monkeypatch, tmp_path):
    assert await _focus_for(RunMode.COMPREHENSIVE, monkeypatch, tmp_path) is None


def test_the_give_up_gate_counts_the_tool_that_submits_to_the_prover():
    # The gate counts calls by name, and CVLR's verify tool is not CVL's `submit_buffer`: what the
    # author hands the gate has to be the name the submitting tool is actually bound under.
    names = {t.name for t in gate_tools(SimpleNamespace(tuning=None), None, None)}  # type: ignore[arg-type]
    assert VERIFY_TOOL in names


def test_a_focused_author_is_told_its_properties_cannot_be_skipped():
    text = _flat(_author_task_prompt(focused=True))
    assert "`record_skip` will refuse them" in text
    assert "sort='environment'" in text
    assert "use `record_skip(property_title, reason)`" not in text


def test_an_unfocused_author_keeps_the_skip_instructions():
    text = _flat(_author_task_prompt(focused=False))
    assert "use `record_skip(property_title, reason)`" in text
    assert "will refuse them" not in text
