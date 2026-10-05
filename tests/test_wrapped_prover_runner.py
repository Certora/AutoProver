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
from composer.spec.source.prover import component_slug
from composer.spec.source.spec_buffers import NamedBuffer

from .conftest import conf_of_prover_call


class _NoCex(CexHandler):
    """The mocked prover never reports a violation, so this is never reached."""

    async def analyze(self, all_results, tool_call_id, callbacks, report_dir) -> str:
        raise AssertionError("no violation was reported")


def _runner(**buffer_set) -> WrappedProverRunner:
    return WrappedProverRunner(
        config={"files": ["src/Foo.sol"]},
        prover_options=ProverOptions(app="evm"),
        main_contract="Foo",
        **buffer_set,
    )


#: An author with a shared buffer and a run-target buffer that imports it and the
#: summaries a directory up, the way the author is told to write imports.
SHARED = "ghost mathint total;\n"
TARGET = 'import "shared.spec";\nimport "../summaries/erc20.spec";\nrule a { assert true; }\n'
BUFFERS = {
    "shared": NamedBuffer(name="shared", cvl=SHARED, is_run_target=False),
    "target": NamedBuffer(name="target", cvl=TARGET, property_rules={"p": ["a"]}),
}


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


async def _run(tmp_path: Path, runner: WrappedProverRunner | None = None, **selection) -> ProverReport | str:
    return await (runner or _runner()).run(
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
        # The shape every plugin call has (a plugin's lemma runner included):
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


@pytest.mark.asyncio
class TestBufferRuns:
    """A run on a named buffer stages the author's whole buffer set, with that
    buffer replaced by the text under test, under the component's spec dir."""

    async def test_the_buffer_set_is_staged_around_the_edited_text(self, tmp_path, monkeypatch):
        seen: dict = {}

        async def fake_run_prover(folder: Path, args: list[str], *_rest, **_kw) -> ProverReport:
            conf = conf_of_prover_call(folder, args)
            specs = folder / "certora" / "specs" / "foo"
            seen.update(
                conf=conf,
                conf_path=args[0],
                target=(specs / "target.spec").read_text(),
                shared=(specs / "shared.spec").read_text(),
            )
            return ProverReport(result_str="ok", link="local://test", raw_rule_status={}, certora_run_stdout="")

        monkeypatch.setattr("composer.spec.source.author.run_prover", fake_run_prover)
        edited = TARGET + "rule lemma_a { assert true; }\n"
        runner = _runner(buffers=BUFFERS, slug="foo")
        await runner.run(
            curr_spec=edited, working_dir=str(tmp_path), cex_handler=_NoCex(), callbacks=ProverCallbacks(),
            tool_call_id="tc", rules=["a"], buffer="target", msg="lemma run", compilation_steps_only=True,
        )
        assert seen["conf"]["verify"] == "Foo:certora/specs/foo/target.spec"
        assert seen["conf_path"].startswith("certora/confs/verify_target")
        assert seen["conf"]["rule"] == ["a"]
        assert seen["conf"]["msg"] == "lemma run"
        assert seen["conf"]["compilation_steps_only"] is True
        # The text under test replaces the author's draft; the sibling it imports sits beside it.
        assert seen["target"] == edited
        assert seen["shared"] == SHARED
        # Nothing staged outlives the run.
        assert not (tmp_path / "certora" / "specs" / "foo").exists() or not any((tmp_path / "certora" / "specs" / "foo").iterdir())
        assert not list((tmp_path / "certora" / "confs").glob("*.conf"))

    async def test_without_a_buffer_name_the_text_is_staged_alone(self, tmp_path, staged_confs):
        await _run(tmp_path, runner=_runner(buffers=BUFFERS, slug="foo"), rules=["a"])
        [conf] = staged_confs
        assert conf["verify"] == "Foo:certora/specs/adhoc_run.spec"

    async def test_an_unknown_buffer_is_refused(self, tmp_path, staged_confs):
        with pytest.raises(ValueError, match="no buffer named 'other'"):
            await _run(tmp_path, runner=_runner(buffers=BUFFERS, slug="foo"), buffer="other")
        assert staged_confs == []

    async def test_a_buffer_run_needs_the_component_slug(self, tmp_path, staged_confs):
        with pytest.raises(ValueError, match="component slug"):
            await _run(tmp_path, runner=_runner(buffers=BUFFERS), buffer="target")
        assert staged_confs == []


def test_component_slug_strips_the_prefix_and_falls_back_to_the_contract():
    assert component_slug("autospec_vault", "Vault") == "vault"
    assert component_slug("custom", "Vault") == "custom"
    assert component_slug(None, "Vault") == "Vault"
