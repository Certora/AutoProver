"""The prover backend's pre-formalization step: what it runs, and what it does NOT.

``prepare_formalization`` is spawned as a bare task and joined at the barrier in
``composer/pipeline/core.py`` before any component is formalized, so whatever runs inside it
delays every property in the run. It is AutoSetup, then the custom summaries built on AutoSetup's
config — and nothing else. In particular no authoring agent runs here: structural invariants used
to be formulated and proven at this point, which put a full ``batch_cvl_generation`` (real prover
jobs) ahead of the barrier. Invariants are now written by the component author that needs one.

Stubs throughout — no LLM, no prover, no subprocess.
"""

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

import composer.spec.source.pipeline as pipeline
from composer.io.multi_job import TaskInfo
from composer.spec.source.autosetup import SetupSuccess
from composer.spec.source.task_ids import AUTOSETUP_TASK_ID, SUMMARIES_TASK_ID
from composer.spec.gen_types import CVLResource
from composer.ui.autoprove_app import AutoProvePhase

pytestmark = pytest.mark.asyncio

SUMMARIES_PATH = "specs/summaries/Counter.spec"

SETUP = SetupSuccess(
    prover_config={"files": ["src/Counter.sol"]},
    summaries_path=SUMMARIES_PATH,
    user_types=[],
)


@dataclass
class _Run:
    """Just enough ``PipelineRun``: a runner that awaits the thunk inline and records the
    ``TaskInfo`` it was handed."""
    seen: list[TaskInfo]

    ctx = None
    env = None
    source = SimpleNamespace(contract_name="Counter", project_root="/tmp/proj")

    async def runner(self, task_info, job=None):
        self.seen.append(task_info)
        return await (job or task_info)()


def _prepared(*, erc20s: bool) -> pipeline.ProverPrepared:
    sys_desc = SimpleNamespace(
        erc20_contracts=["Token"] if erc20s else [],
        external_interfaces=[],
    )
    return pipeline.ProverPrepared(
        main=None,
        _sys_desc=sys_desc,
        _harnessed=None,
        _prover_tool=None,
        _analyzed=None,
        _deps=pipeline._ProverPipelineDeps(
            prover_options=None, store=None, analysis_store=None, editing=None,
        ),
    )


@pytest.fixture
def stub_setup(monkeypatch):
    async def _autosetup(*_a, **_kw):
        return SETUP

    async def _summaries(**_kw):
        return CVLResource(
            path="certora/specs/custom_summaries.spec", required=True,
            description="Custom summaries", sort="import",
        )

    monkeypatch.setattr(pipeline, "run_autosetup_phase", _autosetup)
    monkeypatch.setattr(pipeline, "setup_summaries", _summaries)
    # ContractSetup validates a real SystemDescriptionHarnessed; it only carries config
    # through to the (stubbed) summaries agent, and this test is about task ordering.
    monkeypatch.setattr(pipeline, "ContractSetup", lambda **kw: SimpleNamespace(**kw))


async def test_only_autosetup_runs_before_the_barrier(stub_setup):
    run = _Run(seen=[])
    await _prepared(erc20s=False).prepare_formalization(run)
    assert [t.task_id for t in run.seen] == [AUTOSETUP_TASK_ID]


async def test_custom_summaries_follow_autosetup_when_the_system_needs_them(stub_setup):
    run = _Run(seen=[])
    await _prepared(erc20s=True).prepare_formalization(run)
    # Summaries are second, not concurrent: they consume AutoSetup's config.
    assert [t.task_id for t in run.seen] == [AUTOSETUP_TASK_ID, SUMMARIES_TASK_ID]


async def test_no_authoring_agent_runs_before_the_barrier(stub_setup):
    """The point of the change. A CVL_GEN task here is one every component waits behind."""
    run = _Run(seen=[])
    await _prepared(erc20s=True).prepare_formalization(run)
    assert not [t for t in run.seen if t.phase is AutoProvePhase.CVL_GEN]


async def test_the_formalizer_carries_only_the_setup_resources(stub_setup):
    """No ``invariants.spec`` is folded into the resource set any more, so nothing a component
    spec imports depends on work done ahead of it."""
    formalizer = await _prepared(erc20s=False).prepare_formalization(_Run(seen=[]))
    assert [str(r.path) for r in formalizer._resources] == [f"certora/{SUMMARIES_PATH}"]
    assert formalizer._prover_config == SETUP.prover_config
