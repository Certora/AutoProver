"""A prover run's submitted snapshot, fetched and described.

The prover uploads the sources it compiled as ``inputs/.certora_sources/`` with
the run's ``run.conf`` at that root and an empty ``.cwd`` marker in the
directory ``certoraRun`` was invoked from; every path inside the conf is
relative to that directory, not to the sources root. The run's verdicts live
in ``Reports/treeView/``. ProverOutputUtility fetches both halves without the
output tarball, into the same layout the tarball would extract to.
"""
import json
from dataclasses import dataclass
from pathlib import Path

from composer.prover.core import DEFAULT_GLOBAL_TIMEOUT
from composer.prover.ptypes import RulePath, StatusCodes
from composer.prover.results import read_and_format_run_result

SOURCES_DIR = Path("inputs") / ".certora_sources"
RUN_CONF = "run.conf"
CWD_MARKER = ".cwd"


class WorkspaceError(Exception):
    """The fetched run cannot serve as a workspace: something the resolver needs
    is missing or ambiguous. The message names what, so a caller can report it
    as a dependency error rather than retry."""


@dataclass(frozen=True)
class OriginalRun:
    """What the run itself recorded, read once at fetch time."""
    url: str
    #: Verdict per rule path, as the run's final tree view reports them.
    statuses: dict[RulePath, StatusCodes]
    #: The ``--global_timeout`` the run verified under; the resolver's own runs
    #: keep it so a resolution never rests on a longer budget.
    global_timeout: float

    def status_of(self, target: RulePath) -> StatusCodes | None:
        return self.statuses.get(target)


@dataclass(frozen=True)
class RunWorkspace:
    #: The fetched report layout: ``inputs/.certora_sources`` and ``Reports/treeView``.
    root: Path
    #: The directory ``certoraRun`` ran from (holds ``.cwd``); every conf path is
    #: relative to it, and every prover run of the resolver executes in it.
    run_dir: Path
    #: The canonical ``run.conf`` contents.
    conf: dict
    main_contract: str
    #: The verified spec, relative to ``run_dir``.
    spec_path: Path
    original: OriginalRun

    @property
    def sources(self) -> Path:
        return self.root / SOURCES_DIR

    @property
    def spec_file(self) -> Path:
        return self.run_dir / self.spec_path

    def spec_text(self) -> str:
        return self.spec_file.read_text(encoding="utf-8")


def find_run_conf(sources: Path) -> Path:
    """The canonical ``run.conf`` at the sources root. Other ``*.conf`` files in the
    tree (a mock's, a sub-run's) are never guessed at; the error lists them."""
    canonical = sources / RUN_CONF
    if canonical.is_file():
        return canonical
    others = sorted(
        str(p.relative_to(sources)) for p in sources.rglob("*.conf")
        if "/lib/" not in str(p) and "/.certora_internal/" not in str(p)
    )
    hint = f" (other conf files present: {', '.join(others)})" if others else ""
    raise WorkspaceError(f"no {RUN_CONF} at the sources root {sources}{hint}")


def find_run_dir(sources: Path) -> Path:
    """The directory that carries the ``.cwd`` marker. A tree without one was
    staged from its root; more than one cannot be resolved."""
    markers = sorted(p.parent for p in sources.rglob(CWD_MARKER))
    if not markers:
        return sources
    if len(markers) > 1:
        listed = ", ".join(str(m.relative_to(sources)) for m in markers)
        raise WorkspaceError(f"more than one {CWD_MARKER} marker under {sources}: {listed}")
    return markers[0]


def verify_target(conf: dict) -> tuple[str, Path]:
    """``(main contract, spec path)`` from the conf's ``verify`` entry."""
    raw = conf.get("verify")
    if isinstance(raw, list) and len(raw) == 1:
        raw = raw[0]
    if not isinstance(raw, str) or ":" not in raw:
        raise WorkspaceError(f"{RUN_CONF} has no usable verify entry: {raw!r}")
    contract, spec = raw.split(":", 1)
    if not contract or not spec:
        raise WorkspaceError(f"{RUN_CONF} has a malformed verify entry: {raw!r}")
    return contract, Path(spec)


def _global_timeout(conf: dict) -> float:
    raw = conf.get("global_timeout")
    if raw is None:
        return DEFAULT_GLOBAL_TIMEOUT
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise WorkspaceError(f"{RUN_CONF} has a non-numeric global_timeout: {raw!r}") from None


def read_original(root: Path, url: str, conf: dict) -> OriginalRun:
    parsed = read_and_format_run_result(root)
    if isinstance(parsed, str):
        raise WorkspaceError(f"the run's tree view could not be read: {parsed}")
    return OriginalRun(
        url=url,
        statuses={r.path: r.status for r in parsed.values()},
        global_timeout=_global_timeout(conf),
    )


def open_workspace(root: Path, url: str) -> RunWorkspace:
    """Describe an already fetched run at ``root``."""
    sources = root / SOURCES_DIR
    if not sources.is_dir():
        raise WorkspaceError(f"no {SOURCES_DIR} under {root}")
    conf_path = find_run_conf(sources)
    try:
        conf = json.loads(conf_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise WorkspaceError(f"{conf_path} is not valid JSON: {e}") from None
    if not isinstance(conf, dict):
        raise WorkspaceError(f"{conf_path} is not a JSON object")
    run_dir = find_run_dir(sources)
    main_contract, spec_path = verify_target(conf)
    if not (run_dir / spec_path).is_file():
        raise WorkspaceError(f"the verified spec {spec_path} is missing under {run_dir}")
    return RunWorkspace(
        root=root,
        run_dir=run_dir,
        conf=conf,
        main_contract=main_contract,
        spec_path=spec_path,
        original=read_original(root, url, conf),
    )


def _aiss_env_for(url: str) -> None:
    """ProverOutputUtility authenticates against ``AISS_ENV`` (default prod); a
    vaas-dev or vaas-stg URL names its environment in the host."""
    import os
    if "vaas-dev" in url:
        os.environ.setdefault("AISS_ENV", "dev")
    elif "vaas-stg" in url:
        os.environ.setdefault("AISS_ENV", "stg")


def fetch_workspace(url: str, dest: Path) -> RunWorkspace:
    """Fetch the run's sources and tree view into ``dest`` and describe them.
    Both fetches are idempotent (each leaves a completion marker), so a
    re-run over the same ``dest`` reuses the files."""
    _aiss_env_for(url)
    from prover_output_utility import ProverOutputAPI

    dest.mkdir(parents=True, exist_ok=True)
    api = ProverOutputAPI(use_local=False)
    api.fetch_job_sources(url, dest)
    api.fetch_job_treeview(url, dest)
    return open_workspace(dest, url)
