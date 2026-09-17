"""``WrappedProverRunner`` is the ``ProverRunner`` handed to plugins through
``CVLAuthorState``: one ad-hoc prover run that stages the spec and conf into the
working directory, forwards to ``run_prover``, and cleans up.

The prover core is mocked at ``composer.spec.source.author.run_prover``; the conf
the runner staged is read back from the path it passed on.
"""
from pathlib import Path

import pytest

from composer.prover.core import CexHandler, ProverCallbacks, ProverOptions, ProverReport
from composer.spec.source.author import WrappedProverRunner

from .conftest import conf_of_prover_call


class _NoCex(CexHandler):
    """The mocked prover never reports a violation, so this is never reached."""

    async def analyze(self, all_results, tool_call_id, callbacks, report_dir) -> str:
        raise AssertionError("no violation was reported")


def _runner() -> WrappedProverRunner:
    return WrappedProverRunner(
        config={"files": ["src/Foo.sol"]},
        prover_options=ProverOptions(),
        main_contract="Foo",
    )


@pytest.fixture
def staged_confs(monkeypatch) -> list[dict]:
    """Every conf the runner handed to ``run_prover``, in call order."""
    confs: list[dict] = []

    async def fake_run_prover(folder: Path, args: list[str], *_rest, **_kw) -> ProverReport:
        confs.append(conf_of_prover_call(folder, args))
        return ProverReport(
            result_str="ok", link="local://test", raw_rule_status={}, certora_run_stdout=""
        )

    monkeypatch.setattr("composer.spec.source.author.run_prover", fake_run_prover)
    return confs


async def _run(tmp_path: Path, **selection) -> ProverReport | str:
    return await _runner().run(
        curr_spec="rule a { assert true; }",
        working_dir=str(tmp_path),
        cex_handler=_NoCex(),
        callbacks=ProverCallbacks(),
        tool_call_id="tc",
        **selection,
    )


@pytest.mark.asyncio
class TestWrappedProverRunner:
    async def test_runs_with_a_rule_selection(self, tmp_path, staged_confs):
        # The shape every plugin call has (dz-strategy's ``lemma_prover`` included):
        # a rule list and nothing about exclusions.
        await _run(tmp_path, rules=["a"])
        [conf] = staged_confs
        assert conf["rule"] == ["a"]
        assert "exclude_rule" not in conf

    async def test_runs_with_no_selection(self, tmp_path, staged_confs):
        await _run(tmp_path)
        [conf] = staged_confs
        assert "rule" not in conf
        assert "exclude_rule" not in conf

    async def test_exclusions_reach_the_conf(self, tmp_path, staged_confs):
        await _run(tmp_path, exclude_rules=["b"])
        [conf] = staged_confs
        assert conf["exclude_rule"] == ["b"]
        assert "rule" not in conf

    async def test_per_run_config_overrides_reach_the_conf(self, tmp_path, staged_confs):
        await _run(tmp_path, rules=["a"], compilation_steps_only=True)
        [conf] = staged_confs
        assert conf["compilation_steps_only"] is True
