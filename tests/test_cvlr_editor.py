"""The code editor: the one agent allowed to change the program under verification.

``docs/who-edits-the-program.md``. What is worth pinning is what
the editor may write, the two gates that make delegating an edit better than doing it inline — the
approval a later edit voids, and the check that the edit actually reached the compiler — and that
what comes back to the author is committed, not applied.

No LLM, no network, no cargo.
"""

import asyncio
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command

from graphcore.tools.vfs import VFSState, vfs_tools

from composer.cargo.depinfo import compiled_sources
from composer.cargo.session import CompileFailed, Compiled
from composer.sandbox.recipes import SANDBOX_CARGO_DIR
from composer.spec.cvlr.conf import TunableConf
from composer.spec.cvlr.editor import (
    Accepted,
    CodeEditor,
    DoesNotCompile,
    EditorDeps,
    NotCompiled,
    SubmitDeps,
    SubmitEdits,
    editable_paths,
    gate_edits,
)
from composer.spec.cvlr.harness import HarnessModule
from composer.spec.cvlr.tree import ProjectSource, SharedTree
from composer.spec.source.munge.edit_store import EditStore, vfs_digest
from composer.spec.source.munge.munge_agent import CommonMungeDescription, MungeRefusal

_RESERVE = "programs/p/src/reserve.rs"
_MARKER = "programs/p/src/certora/specs/vault.rs"
_SOURCE_DIRS = ProjectSource(
    roots=(PurePosixPath("programs/p/src"),), manifests=(PurePosixPath("programs/p/Cargo.toml"),)
)
_SOURCE = """\
pub fn redeem_fees(reserve: &mut Reserve) -> Result<u64> {
    let amount = reserve.calculate_fees()?;
    Ok(amount)
}
"""
_GATED = _SOURCE.replace(
    "pub fn redeem_fees", '#[cfg_attr(feature = "unit_vault", cvlr::early_panic)]\npub fn redeem_fees'
)
_SUMMARY = CommonMungeDescription(executive_summary="s", how_to_apply=None, why_sound="w")


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    (root / "programs" / "p" / "src").mkdir(parents=True)
    (root / _RESERVE).write_text(_SOURCE)
    return root


def _mat(root: Path):
    return vfs_tools({"immutable": True, "fs_layer": str(root)}, VFSState)[1]


class _Session:
    """Answers ``check`` with a fixed verdict and records what the tree held when it was asked."""

    def __init__(self, workdir: Path, verdict) -> None:
        self.workdir = workdir
        self.verdict = verdict
        self.saw: str | None = None

    async def check(self, *, manifest_dir, features):
        self.saw = (self.workdir / _RESERVE).read_text()
        return SimpleNamespace(verdict=self.verdict, duration_ms=1)


async def _target(project: Path, verdict=None):
    """A target whose tree is a materialized copy of ``project``, beside it."""
    from composer.spec.cvlr.verify import HarnessTarget

    tree = SharedTree(pristine=project, root=project.parent / "build", source=_SOURCE_DIRS)
    await tree.materialize()
    root = tree.root
    return HarnessTarget(
        session=_Session(root, verdict or Compiled()),  # type: ignore[arg-type]
        module_path=root / _MARKER,
        package="p",
        package_root=root / "programs" / "p",
        tuning=SimpleNamespace(write=lambda _summaries: None),  # type: ignore[arg-type]
        unit=HarnessModule("vault"),
        tree=tree,
        build_sem=asyncio.Semaphore(1),
    )


def _editor_state(**over):
    base = {
        "request": "rule_x has no verdict; the trace stops on the `?` in redeem_fees",
        "vfs": {},
        "orig_vfs": {},
        "reviewed_digest": None,
        "memory": None,
        "draft": "// draft\n",
        "summaries": [],
        "messages": [],
    }
    return {**base, **over}


def _author_state(**over):
    """A `CvlrGenerationState`-shaped dict, filled with the keys the tools declare."""
    base = {
        "curr_spec": "// draft\n",
        "skipped": [],
        "validations": {},
        "required_validations": [],
        "property_rules": [],
        "rule_subjects": [],
        "summaries": [],
        "vfs": {},
        "version_history": [],
        "prover_settings": TunableConf(),
        "expected_failures": {},
        "prover_link": None,
        "failed": None,
        "budget_curtailed": False,
        "messages": [],
    }
    return {**base, **over}


async def _run(tool_cls, deps, state, **fields):
    token = tool_cls._dep_ctx.set(deps)
    try:
        return await tool_cls(state=state, tool_call_id="tc", **fields).run()
    finally:
        tool_cls._dep_ctx.reset(token)


# ---------------------------------------------------------------------------------------------
# what the editor may write


def test_the_editor_writes_the_programs_own_source_and_manifests(tmp_path: Path):
    excluded = editable_paths(_project(tmp_path), _SOURCE_DIRS, Path("programs/p"))
    assert not excluded(PurePosixPath(_RESERVE))
    assert not excluded(PurePosixPath("programs/p/src/certora/mocks/invokes.rs"))
    assert not excluded(PurePosixPath("programs/p/Cargo.toml"))


def test_the_editor_never_writes_a_dependency_or_the_authors_files(tmp_path: Path):
    """A dependency's behaviour changes for every crate in the graph. The harness modules and the
    tuning files are the author's, and the tree writes them from the author's state."""
    excluded = editable_paths(_project(tmp_path), _SOURCE_DIRS, Path("programs/p"))
    for path in (
        str(SANDBOX_CARGO_DIR / "registry/src/i/anchor-lang-0.31.1/src/error.rs"),
        "programs/p/src/certora/specs/vault.rs",
        "programs/p/src/certora/envs/cvlr_summaries.txt",
        "programs/p/build.rs",
        "Cargo.toml",
        "../outside.rs",
    ):
        assert excluded(PurePosixPath(path)), path


# ---------------------------------------------------------------------------------------------
# the approval gate


@pytest.mark.asyncio
async def test_submitting_without_an_approval_is_refused(tmp_path: Path):
    root = _project(tmp_path)
    answer = await _run(
        SubmitEdits,
        SubmitDeps(await _target(root), _mat(root)),
        _editor_state(vfs={_RESERVE: _GATED}),
        summary=_SUMMARY,
    )
    assert isinstance(answer, str) and "not been approved" in answer


@pytest.mark.asyncio
async def test_an_approval_is_void_the_moment_the_edits_change(tmp_path: Path):
    """EVM's discipline and the reason for it: an approved diff and a submitted one must be the same
    diff, or the review is of something nobody shipped."""
    root = _project(tmp_path)
    approved = {_RESERVE: _GATED}
    answer = await _run(
        SubmitEdits,
        SubmitDeps(await _target(root), _mat(root)),
        _editor_state(vfs={_RESERVE: _GATED + "\n"}, reviewed_digest=vfs_digest(approved)),
        summary=_SUMMARY,
    )
    assert isinstance(answer, str) and "voids" in answer


@pytest.mark.asyncio
async def test_an_approved_edit_that_changes_nothing_is_refused(tmp_path: Path):
    root = _project(tmp_path)
    unchanged = {_RESERVE: _SOURCE}
    answer = await _run(
        SubmitEdits,
        SubmitDeps(await _target(root), _mat(root)),
        _editor_state(vfs=unchanged, reviewed_digest=vfs_digest(unchanged)),
        summary=_SUMMARY,
    )
    assert isinstance(answer, str) and "changed nothing" in answer


@pytest.mark.asyncio
async def test_an_approved_compiling_edit_is_accepted_after_building_with_it(tmp_path: Path):
    root = _project(tmp_path)
    target = await _target(root)
    edits = {_RESERVE: _GATED}
    answer = await _run(
        SubmitEdits,
        SubmitDeps(target, _mat(root)),
        _editor_state(vfs=edits, reviewed_digest=vfs_digest(edits)),
        summary=_SUMMARY,
    )
    assert isinstance(answer, Command)
    assert answer.update["result"] == _SUMMARY
    # The overlay was in the tree when the build ran: the gate compiled the edit, not the project.
    assert target.session.saw == _GATED  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_an_edit_that_does_not_compile_is_sent_back_with_the_diagnostics(tmp_path: Path):
    root = _project(tmp_path)
    edits = {_RESERVE: _GATED}
    answer = await _run(
        SubmitEdits,
        SubmitDeps(
            await _target(root, CompileFailed(diagnostics="error[E0425]", exit_code=101)), _mat(root)
        ),
        _editor_state(vfs=edits, reviewed_digest=vfs_digest(edits)),
        summary=_SUMMARY,
    )
    assert isinstance(answer, str) and "error[E0425]" in answer


# ---------------------------------------------------------------------------------------------
# did the edit reach the compiler


def _dep_info(root: Path, listed: list[str]) -> None:
    deps = root / "target" / "debug" / "deps"
    deps.mkdir(parents=True, exist_ok=True)
    for name in listed:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("")
    rhs = " ".join(listed)
    (deps / "p-abc123.d").write_text(
        f"{deps}/p-abc123.d: {rhs}\n\n" + "".join(f"{n}:\n" for n in listed)
    )




@pytest.mark.asyncio
async def test_an_edited_file_the_build_never_read_is_reported(tmp_path: Path):
    """The Solana `EditsNotCompiled`. An edit in a file no enabled feature reaches changes nothing
    and reports nothing, and the report would still claim a source edit."""
    target = await _target(_project(tmp_path))
    _dep_info(target.session.workdir, ["programs/p/src/lib.rs", _MARKER])
    outcome = await gate_edits(
        target, draft="//\n", summaries=(), overlay={_RESERVE: _GATED}, edited=[_RESERVE]
    )
    assert outcome == NotCompiled((_RESERVE,))


@pytest.mark.asyncio
async def test_only_the_packages_rust_files_are_held_to_dep_info(tmp_path: Path):
    """A manifest is never in dep-info, and only the verified package's dep-info is read, so holding
    either to it would refuse every such edit."""
    target = await _target(_project(tmp_path))
    _dep_info(target.session.workdir, ["programs/p/src/lib.rs", _RESERVE, _MARKER])
    outcome = await gate_edits(
        target,
        draft="//\n",
        summaries=(),
        overlay={_RESERVE: _GATED},
        edited=[_RESERVE, "programs/p/Cargo.toml", "crates/m/src/lib.rs"],
    )
    assert outcome == Accepted()


@pytest.mark.asyncio
async def test_a_failed_build_is_reported_before_dep_info_is_read(tmp_path: Path):
    outcome = await gate_edits(
        await _target(_project(tmp_path), CompileFailed(diagnostics="boom", exit_code=101)),
        draft="//\n",
        summaries=(),
        overlay={},
        edited=[],
    )
    assert outcome == DoesNotCompile("boom")


def test_dep_info_that_does_not_name_the_marker_is_not_this_build(tmp_path: Path):
    """Feature variants of one crate share a `target/`, and nothing in a `.d` filename says which
    produced it. The marker is the discriminator; without a match the honest answer is "unknown",
    not a false pass."""
    root = tmp_path / "work"
    _dep_info(root, ["src/lib.rs"])
    assert compiled_sources(root, "p", marker=root / "src/certora/specs/vault.rs") is None


def test_a_crate_name_with_a_hyphen_finds_its_dep_info(tmp_path: Path):
    """Cargo normalises `-` to `_` in crate names and therefore in the dep-info filename; a package
    named `my-program` writes `my_program-<hash>.d`."""
    root = tmp_path / "work"
    deps = root / "target" / "debug" / "deps"
    deps.mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "lib.rs").write_text("")
    (deps / "my_program-abc.d").write_text(f"{deps}/my_program-abc.d: src/lib.rs\n")
    assert compiled_sources(root, "my-program", marker=root / "src/lib.rs") is not None


# ---------------------------------------------------------------------------------------------
# what the author gets back


async def _editor_deps(root: Path, result, store: EditStore) -> EditorDeps:
    async def runner(inp, _tid):
        assert "unit_vault" in inp["input"][0]
        return {**inp, "vfs": {_RESERVE: _GATED}, "result": result}

    return EditorDeps(target=await _target(root), runner=runner, store=store, mat=_mat(root))


@pytest.mark.asyncio
async def test_a_finished_edit_is_committed_and_not_applied(tmp_path: Path):
    """As on EVM: the author reads the diff and applies the edit with `commit_edit`. An edit the
    author never commits is never in force."""
    root = _project(tmp_path)
    store = EditStore(InMemoryStore(), ("edits",))
    answer = await _run(
        CodeEditor, await _editor_deps(root, _SUMMARY, store), _author_state(), request="r"
    )
    assert isinstance(answer, str)
    key = vfs_digest({_RESERVE: _GATED})
    assert f"commit_edit({key})" in answer
    assert '+#[cfg_attr(feature = "unit_vault", cvlr::early_panic)]' in answer
    stored = await store.read(key)
    assert stored is not None and stored.vfs == {_RESERVE: _GATED}
    assert stored.why_sound == "w"


@pytest.mark.asyncio
async def test_a_refusal_commits_nothing(tmp_path: Path):
    root = _project(tmp_path)
    store = EditStore(InMemoryStore(), ("edits",))
    answer = await _run(
        CodeEditor,
        await _editor_deps(root, MungeRefusal(explanation="needs a tenth kind"), store),
        _author_state(),
        request="r",
    )
    assert "needs a tenth kind" in answer
    assert await store.read(vfs_digest({_RESERVE: _GATED})) is None


@pytest.mark.asyncio
async def test_there_is_nothing_to_edit_against_before_a_draft(tmp_path: Path):
    root = _project(tmp_path)
    store = EditStore(InMemoryStore(), ("edits",))
    answer = await _run(
        CodeEditor, await _editor_deps(root, _SUMMARY, store), _author_state(curr_spec=None), request="r"
    )
    assert "No harness written yet" in answer

