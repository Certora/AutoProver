"""The timeout resolver's workspace over a fetched prover run, and the adapters
built on it. Nothing here touches the network or a prover: the fetched layout
is written into ``tmp_path`` and ``run_prover`` / ``certoraRun`` are mocked.
"""
import json
from pathlib import Path

import pytest

from composer.prover.core import ProverCallbacks, ProverReport
from composer.prover.ptypes import RulePath
from composer.spec.types import VerificationArtifact

from composer.resolver import preflight as preflight_mod
from composer.resolver.artifacts import FileArtifactRegistrar
from composer.resolver.author_state import author_state_for
from composer.resolver.patches import patches_for, write_patches
from composer.resolver.runner import (
    CONF_DIR, RUN_SPECIFIC_KEYS, WorkspaceProverRunner, compose_conf, prover_options,
)
from composer.resolver.workspace import (
    SOURCES_DIR, WorkspaceError, find_run_dir, open_workspace, verify_target,
)

from .conftest import conf_of_prover_call
from .test_wrapped_prover_runner import _NoCex

URL = "https://prover.certora.com/output/123/abc?anonymousKey=k"
SPEC = "rule slow { assert true; }\nrule fast { assert true; }\n"
CONF = {
    "files": ["src/Foo.sol"],
    "verify": "Foo:certora/specs/foo.spec",
    "solc": "solc8.24",
    "optimistic_loop": False,
    "global_timeout": "600",
    "msg": "the original run",
    "rule": ["slow"],
    "server": "production",
    "prover_version": "master",
}


def _tree_node(name: str, status: str, **extra) -> dict:
    return {
        "name": name, "output": [], "children": [], "status": status,
        "nodeType": "ROOT", "errors": [], **extra,
    }


def fetched_run(root: Path, *, cwd_rel: Path = Path("."), conf: dict = CONF) -> Path:
    """Lay out ``root`` the way ProverOutputUtility fetches a run: the sources
    under ``inputs/.certora_sources`` with ``run.conf`` at that root and ``.cwd``
    in the directory the run was invoked from, and the tree view under
    ``Reports/treeView``."""
    sources = root / SOURCES_DIR
    run_dir = sources / cwd_rel
    (run_dir / "src").mkdir(parents=True)
    (run_dir / "src" / "Foo.sol").write_text("contract Foo {}\n")
    (run_dir / "certora" / "specs").mkdir(parents=True)
    (run_dir / "certora" / "specs" / "foo.spec").write_text(SPEC)
    (run_dir / ".cwd").touch()
    (sources / "run.conf").write_text(json.dumps(conf))
    tree = root / "Reports" / "treeView"
    tree.mkdir(parents=True)
    (tree / "treeViewStatus_0.json").write_text(json.dumps({
        "rules": [_tree_node("slow", "TIMEOUT"), _tree_node("fast", "VERIFIED")]
    }))
    return root


class TestOpenWorkspace:
    def test_describes_a_run_staged_from_its_root(self, tmp_path):
        ws = open_workspace(fetched_run(tmp_path), URL)
        assert ws.run_dir == tmp_path / SOURCES_DIR
        assert ws.main_contract == "Foo"
        assert ws.spec_path == Path("certora/specs/foo.spec")
        assert ws.spec_text() == SPEC
        assert ws.original.status_of(RulePath(rule="slow")) == "TIMEOUT"
        assert ws.original.status_of(RulePath(rule="fast")) == "VERIFIED"
        assert ws.original.status_of(RulePath(rule="absent")) is None
        assert ws.original.global_timeout == 600.0

    def test_the_run_directory_is_where_the_cwd_marker_is(self, tmp_path):
        ws = open_workspace(fetched_run(tmp_path, cwd_rel=Path("proj")), URL)
        assert ws.run_dir == tmp_path / SOURCES_DIR / "proj"
        assert ws.spec_file.is_file()

    def test_a_run_without_a_marker_ran_from_the_sources_root(self, tmp_path):
        fetched_run(tmp_path)
        (tmp_path / SOURCES_DIR / ".cwd").unlink()
        assert find_run_dir(tmp_path / SOURCES_DIR) == tmp_path / SOURCES_DIR

    def test_two_markers_are_ambiguous(self, tmp_path):
        fetched_run(tmp_path)
        (tmp_path / SOURCES_DIR / "src" / ".cwd").touch()
        with pytest.raises(WorkspaceError, match="more than one"):
            find_run_dir(tmp_path / SOURCES_DIR)

    def test_a_missing_run_conf_names_the_candidates(self, tmp_path):
        fetched_run(tmp_path)
        sources = tmp_path / SOURCES_DIR
        (sources / "run.conf").rename(sources / "other.conf")
        with pytest.raises(WorkspaceError, match="other.conf"):
            open_workspace(tmp_path, URL)

    def test_a_missing_spec_is_a_workspace_error(self, tmp_path):
        fetched_run(tmp_path)
        (tmp_path / SOURCES_DIR / "certora" / "specs" / "foo.spec").unlink()
        with pytest.raises(WorkspaceError, match="foo.spec"):
            open_workspace(tmp_path, URL)

    def test_a_missing_tree_view_is_a_workspace_error(self, tmp_path):
        fetched_run(tmp_path)
        (tmp_path / "Reports" / "treeView" / "treeViewStatus_0.json").unlink()
        with pytest.raises(WorkspaceError, match="tree view"):
            open_workspace(tmp_path, URL)

    def test_the_default_timeout_applies_when_the_conf_has_none(self, tmp_path):
        conf = {k: v for k, v in CONF.items() if k != "global_timeout"}
        ws = open_workspace(fetched_run(tmp_path, conf=conf), URL)
        assert ws.original.global_timeout == 7200.0


class TestVerifyTarget:
    def test_string_form(self):
        assert verify_target({"verify": "A:x/y.spec"}) == ("A", Path("x/y.spec"))

    def test_single_element_list_form(self):
        assert verify_target({"verify": ["A:x/y.spec"]}) == ("A", Path("x/y.spec"))

    @pytest.mark.parametrize("bad", [None, "nocolon", ":spec", "A:", ["a:b", "c:d"]])
    def test_anything_else_is_rejected(self, bad):
        with pytest.raises(WorkspaceError):
            verify_target({"verify": bad})


class TestComposeConf:
    def test_keeps_the_run_and_drops_what_the_resolver_sets(self, tmp_path):
        ws = open_workspace(fetched_run(tmp_path), URL)
        conf = compose_conf(ws, rules=["slow"], exclude_rules=None, overrides={"msg": "mine"})
        assert conf["files"] == ["src/Foo.sol"]
        assert conf["solc"] == "solc8.24"
        assert conf["optimistic_loop"] is False
        assert conf["prover_version"] == "master"
        assert conf["verify"] == "Foo:certora/specs/foo.spec"
        assert conf["rule"] == ["slow"]
        assert conf["msg"] == "mine"
        for key in RUN_SPECIFIC_KEYS - {"rule", "msg"}:
            assert key not in conf

    def test_the_timeout_and_server_ride_on_the_options(self, tmp_path):
        ws = open_workspace(fetched_run(tmp_path), URL)
        assert prover_options(ws, cloud=False).extra_args == ["--global_timeout", "600"]
        cloud = prover_options(ws, cloud=True).extra_args
        assert cloud[:2] == ["--global_timeout", "600"]
        assert cloud[2] == "--server"


@pytest.fixture
def prover_calls(monkeypatch) -> list[dict]:
    """Every ``(conf, extra_args, spec text at run time)`` the runner produced."""
    calls: list[dict] = []

    async def fake_run_prover(folder: Path, args: list[str], _tid, opts, *_rest) -> ProverReport:
        conf = conf_of_prover_call(folder, args)
        spec_at_run = (folder / conf["verify"].split(":", 1)[1]).read_text()
        calls.append({"folder": folder, "conf": conf, "extra_args": opts.extra_args, "spec": spec_at_run})
        return ProverReport(
            result_str="ok", link="local://run", raw_rule_status={}, certora_run_stdout=""
        )

    monkeypatch.setattr("composer.resolver.runner.run_prover", fake_run_prover)
    return calls


@pytest.mark.asyncio
class TestWorkspaceProverRunner:
    async def test_verifies_the_buffer_at_the_spec_original_path(self, tmp_path, prover_calls):
        ws = open_workspace(fetched_run(tmp_path), URL)
        runner = WorkspaceProverRunner(ws, cloud=False)
        await runner.run(
            curr_spec="rule slow { assert false; }",
            working_dir=str(ws.run_dir),
            cex_handler=_NoCex(),
            callbacks=ProverCallbacks(),
            tool_call_id="t",
            rules=["slow"],
            msg="lemma run",
        )
        [call] = prover_calls
        assert call["folder"] == ws.run_dir
        assert call["spec"] == "rule slow { assert false; }"
        assert call["conf"]["rule"] == ["slow"]
        assert call["conf"]["msg"] == "lemma run"
        assert call["extra_args"] == ["--global_timeout", "600"]
        # The run's own spec is back in place afterwards, and the conf is gone.
        assert ws.spec_text() == SPEC
        assert not (ws.run_dir / CONF_DIR).exists() or not any((ws.run_dir / CONF_DIR).iterdir())

    async def test_per_run_overrides_reach_the_conf(self, tmp_path, prover_calls):
        ws = open_workspace(fetched_run(tmp_path), URL)
        await WorkspaceProverRunner(ws, cloud=False).run(
            curr_spec=SPEC, working_dir=str(ws.run_dir), cex_handler=_NoCex(),
            callbacks=ProverCallbacks(), tool_call_id="t", compilation_steps_only=True,
        )
        [call] = prover_calls
        assert call["conf"]["compilation_steps_only"] is True
        assert "rule" not in call["conf"]


@pytest.mark.asyncio
class TestPreflight:
    async def test_a_clean_compile_returns_nothing(self, tmp_path, monkeypatch):
        ws = open_workspace(fetched_run(tmp_path), URL)
        seen: list[tuple[tuple[str, ...], Path]] = []

        async def fake_captured(*argv: str, cwd: Path, timeout: float) -> tuple[int, str]:
            seen.append((argv, cwd))
            conf = json.loads((cwd / argv[1]).read_text())
            assert conf["compilation_steps_only"] is True
            return 0, ""

        monkeypatch.setattr(preflight_mod, "_run_captured", fake_captured)
        assert await preflight_mod.compile_only(ws) is None
        [(argv, cwd)] = seen
        assert argv[0] == "certoraRun"
        assert cwd == ws.run_dir

    async def test_a_failed_compile_returns_the_compiler_output(self, tmp_path, monkeypatch):
        ws = open_workspace(fetched_run(tmp_path), URL)

        async def fake_captured(*_argv: str, cwd: Path, timeout: float) -> tuple[int, str]:
            return 1, "Error: solc8.24 not found"

        monkeypatch.setattr(preflight_mod, "_run_captured", fake_captured)
        assert await preflight_mod.compile_only(ws) == "Error: solc8.24 not found"


class TestArtifactsAndPatches:
    def test_registered_artifacts_become_files(self, tmp_path):
        registrar = FileArtifactRegistrar(tmp_path / "out")
        registrar.register(VerificationArtifact(
            name="slow_lemmas.lean", kind="lean-proof", description="d", content="theorem t : 1 = 1 := rfl",
        ))
        [(artifact, path)] = registrar.written
        assert path == tmp_path / "out" / "slow_lemmas.lean"
        assert path.read_text() == "theorem t : 1 = 1 := rfl"

    def test_patches_are_diffs_against_the_fetched_run(self, tmp_path):
        ws = open_workspace(fetched_run(tmp_path), URL)
        patches = patches_for(
            ws,
            vfs={"src/Foo.sol": "contract Foo { uint x; }\n"},
            final_spec=SPEC + "rule lemma1 { assert true; }\n",
        )
        assert "+contract Foo { uint x; }" in patches.source
        assert "-contract Foo {}" in patches.source
        assert "+rule lemma1" in patches.spec
        assert "b/certora/specs/foo.spec" in patches.spec
        written = write_patches(patches, tmp_path / "out")
        assert [p.name for p in written] == ["source.patch", "spec.patch"]

    def test_unchanged_products_yield_no_patches(self, tmp_path):
        ws = open_workspace(fetched_run(tmp_path), URL)
        patches = patches_for(ws, vfs={}, final_spec=SPEC)
        assert patches.is_empty
        assert write_patches(patches, tmp_path / "out") == []


@pytest.mark.asyncio
class TestAuthorState:
    async def test_the_state_reads_the_run_and_records_proposals(self, tmp_path):
        ws = open_workspace(fetched_run(tmp_path), URL)
        state, proposer = author_state_for(ws, cloud=False)
        assert state.working_dir == ws.run_dir
        assert state.curr_spec == SPEC
        edit_id = await state.edit_store.propose(
            {"src/Foo.sol": "x"}, executive_summary="hooks", why_sound="inert",
        )
        assert edit_id == "proposal-1"
        [proposal] = proposer.proposals
        assert proposal.vfs == {"src/Foo.sol": "x"}
        assert (proposal.executive_summary, proposal.why_sound) == ("hooks", "inert")
