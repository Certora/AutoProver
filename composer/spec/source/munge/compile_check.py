"""Confirm that an editor agent's VFS edits still compile.

Materializes a :class:`VFSState` into a scratch tree, runs ``certoraRun
--build_only`` over the supplied compilation config, and — if the build
succeeds — checks that every edited file was actually parsed by solc. A VFS key
that never shows up in the build's source list is an edit that doesn't reach the
compilation under verification (orphaned file, dropped import), which is as much
a failure as a hard compile error.
"""

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from graphcore.tools.vfs import VFSState, VFSAccessor
from composer.prover.core import run_prover_inner


@dataclass(frozen=True)
class BuildFailed:
    """``certoraRun --build_only`` did not produce a clean build."""
    reason: str


@dataclass(frozen=True)
class EditsNotCompiled:
    """The build succeeded, but ``files`` are VFS-edited paths that solc never
    parsed — the edits don't reach the compilation."""
    files: frozenset[str]


@dataclass(frozen=True)
class EditsCompiled:
    """The build succeeded and every VFS-edited file was parsed by solc."""
    touched: frozenset[str]


CompileCheck = BuildFailed | EditsNotCompiled | EditsCompiled


_CONF_NAME = "compile_check.conf"


def _config_paths(config: dict[str, Any]) -> set[str]:
    """The file paths under ``config['files']``, stripped of any
    ``:ContractName`` suffix certora allows on a file entry."""
    return {str(entry).split(":", 1)[0] for entry in config.get("files", [])}


def _find_build_json(folder: Path) -> Path | None:
    latest = folder / ".certora_internal" / "latest" / ".certora_build.json"
    if latest.exists():
        return latest
    # `latest` is normally a symlink to the timestamped run dir; fall back to the
    # newest run dir by name (they sort chronologically) if it's absent.
    candidates = sorted(folder.glob(".certora_internal/*/.certora_build.json"))
    return candidates[-1] if candidates else None


def _scrape_touched(build_json: Path) -> set[str]:
    """Union the ``srclist`` values across every SDC in ``.certora_build.json``.
    Each srclist is solc's ``sources`` map for one compilation unit — the input
    file plus all transitive imports — so the union is every file the build
    parsed. Paths are ``.certora_sources``-relative (the instrumented tree)."""
    build = json.loads(build_json.read_text())
    touched: set[str] = set()
    for sdc in build.values():
        touched.update(sdc.get("srclist", {}).values())
    return touched


def _strip_anchor(p: PurePosixPath) -> PurePosixPath:
    """Drop everything up to and including a ``.certora_sources`` component, so a
    ``.certora_sources``-relative build path can be compared to a project-relative
    VFS key."""
    parts = p.parts
    if ".certora_sources" in parts:
        i = len(parts) - 1 - parts[::-1].index(".certora_sources")
        return PurePosixPath(*parts[i + 1:])
    return p


def _is_touched(vfs_key: str, touched: set[str]) -> bool:
    """A VFS key counts as compiled if its path is a trailing sub-path of some
    touched file. Suffix matching absorbs the prefix rewriting certora applies
    when it copies sources into the instrumented tree."""
    key_parts = _strip_anchor(PurePosixPath(vfs_key)).parts
    n = len(key_parts)
    return any(
        _strip_anchor(PurePosixPath(t)).parts[-n:] == key_parts
        for t in touched
    )


def _noop_err(code: int | None, stdout: str, stderr: str) -> None:
    pass


async def _noop_stdout(line: str) -> None:
    pass


async def check_edits_compile(
    state: VFSState,
    accessor: VFSAccessor[VFSState],
    config: dict[str, Any],
    files: list[str],
) -> CompileCheck:
    """Build ``config`` over the materialized ``state`` and confirm the edits hold.

    ``files`` are the paths the caller expects to be compiled; every one must
    appear under ``config['files']`` (a caller contract — a mismatch is a bug,
    not a build outcome, so it raises). The returned :class:`CompileCheck`
    distinguishes a failed build, a build that silently omits edited files, and a
    clean build that parsed them all.
    """
    conf_paths = _config_paths(config)
    absent = [f for f in files if f not in conf_paths]
    if absent:
        raise ValueError(
            f"files not present under config['files']: {absent}"
        )

    with accessor.materialize(state) as tmp:
        folder = Path(tmp)
        (folder / _CONF_NAME).write_text(json.dumps(config))

        result, stdout = await run_prover_inner(
            folder,
            [_CONF_NAME, "--build_only"],
            _noop_err,
            _noop_stdout,
        )

        # run_prover_inner surfaces a hard subprocess failure as a str; the
        # wrapper reports a build exception as a {"sort": "failure"} payload
        # (it always exits 0). A clean --build_only run returns None.
        if isinstance(result, str):
            return BuildFailed(reason=result)
        if isinstance(result, dict) and result.get("sort") == "failure":
            return BuildFailed(reason=f"{result.get('exc_str', '')}\n{stdout}".strip())

        build_json = _find_build_json(folder)
        if build_json is None:
            return BuildFailed(reason=f"build produced no .certora_build.json\n{stdout}".strip())

        touched = _scrape_touched(build_json)

    missing = {k for k in state["vfs"] if not _is_touched(k, touched)}
    if missing:
        return EditsNotCompiled(files=frozenset(missing))
    return EditsCompiled(touched=frozenset(touched))
