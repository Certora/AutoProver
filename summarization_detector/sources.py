"""Turn a prover-run URL into everything the detector needs, so `--url` is the only input:

  fetch the job SOURCES -> find its `run.conf` -> derive the main contract (the conf's `verify` field)
  -> generate the AST (`certoraRun --compilation_steps_only --dump_asts`) -> best-effort the external call
  graph -> run `detect` with the difficulty report the URL also provides.

Live I/O (POU fetch + certoraRun), kept out of `detect.py`'s pure analysis core. Reuses POU
(`ProverOutputAPI.fetch_job_sources`) and `detect.ensure_ast`. vaas-dev URLs need `AISS_ENV=dev` — set
here from the host so the caller needn't.
"""
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Callable, TypeVar

from .detect import DetectionReport, detect, ensure_ast

_T = TypeVar("_T")

_TRANSIENT = ("connection reset", "connection aborted", "timed out", "temporarily unavailable",
              "read timed out", "remotedisconnected", "max retries")


def _is_transient(e: Exception) -> bool:
    return any(s in str(e).lower() for s in _TRANSIENT)


def _retry_transient(fn: Callable[[], _T], tries: int = 4) -> _T:
    """Call fn, retrying only TRANSIENT network errors (the intermittent vaas-dev/prover connection
    resets) with linear backoff; a non-transient error (auth, 404) is raised immediately."""
    for attempt in range(tries):
        try:
            return fn()
        except Exception as e:
            if attempt == tries - 1 or not _is_transient(e):
                raise
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("unreachable: retry loop exhausted without return or raise")


def _aiss_env_for(url: str) -> None:
    """vaas-dev jobs require AISS_ENV=dev for POU auth; set it from the URL host if unset."""
    if "vaas-dev" in url:
        os.environ.setdefault("AISS_ENV", "dev")


def fetch_sources(url: str, dest: str | Path) -> Path:
    """Fetch a job's `.certora_sources` tree to `dest` (POU), retrying transient resets. Returns the root."""
    _aiss_env_for(url)
    from prover_output_utility import ProverOutputAPI
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    api = ProverOutputAPI(use_local=False)
    return Path(_retry_transient(lambda: api.fetch_job_sources(url, dest)))


def find_run_conf(sources_dir: str | Path) -> Path:
    """Locate the job's run conf in a fetched sources tree — the canonical
    `inputs/.certora_sources/run.conf`, else the shallowest `run.conf`/`*.conf` outside lib/ and the
    `.certora_internal` machinery."""
    root = Path(sources_dir)
    canonical = root / "inputs" / ".certora_sources" / "run.conf"
    if canonical.exists():
        return canonical
    for pattern in ("run.conf", "*.conf"):
        cands = [p for p in root.rglob(pattern)
                 if "/lib/" not in str(p) and "/.certora_internal/" not in str(p)]
        if cands:
            return min(cands, key=lambda p: len(p.parts))
    raise FileNotFoundError(f"no run.conf found under {root}")


def cut_from_conf(conf_path: str | Path) -> str:
    """The main (verified) contract — the part before ':' in the conf's `verify` field
    (`"Router:certora/specs/x.spec"` -> `"Router"`), falling back to the first `parametric_contracts`."""
    conf = json.loads(Path(conf_path).read_text())
    verify = conf.get("verify") or ""
    if isinstance(verify, str) and ":" in verify:
        return verify.split(":", 1)[0]
    parametric = conf.get("parametric_contracts") or []
    return parametric[0] if parametric else ""


def find_external_call_graph(sources_dir: str | Path) -> Path | None:
    """The prover's `externalCallGraph.json` if a local tree already carries one (e.g. a local prover run's
    `Reports/`). None otherwise. For a job URL use `fetch_external_call_graph` — it's a Reports artifact,
    NOT a source file, so it isn't in `fetch_job_sources` output."""
    hits = list(Path(sources_dir).rglob("externalCallGraph.json"))
    return hits[0] if hits else None


def fetch_external_call_graph(url: str, dest: str | Path) -> Path | None:
    """Fetch just `Reports/externalCallGraph.json` — the prover writes it under `Reports/`, not into
    `.certora_sources`, so the source-files fetch misses it. Uses POU's single-file endpoint
    (`fetch_output_file` -> `/f/<rel_path>`), NOT the whole output tarball. Returns the written path, or
    None when the run produced none (a prover build without the collector) or on any fetch error.
    NB: needs an up-to-date POU (the ProverCLI repo) — older installs lack `fetch_output_file`."""
    _aiss_env_for(url)
    try:
        from prover_output_utility import ProverOutputAPI
        api = ProverOutputAPI(use_local=False)
        # fetch_output_file needs a current POU (ProverCLI); older installs lack it -> AttributeError ->
        # caught below -> no ecg (graceful). type: ignore because the installed stub may be stale.
        content = _retry_transient(
            lambda: api.fetch_output_file(url, "externalCallGraph.json"))  # type: ignore[attr-defined]
        if not content:
            return None
        out = Path(dest) / "externalCallGraph.json"
        out.write_text(content if isinstance(content, str) else json.dumps(content))
        return out
    except Exception:
        return None


def fetch_surviving_graphs(url: str) -> list[dict]:
    """Fetch a run's postOptimize SurvivingCallGraph dumps and return them PARSED. Reads the manifest
    `survivingCallGraph_map.json` (POU single-file endpoint), then each rule's postOptimize file. Empty
    list when the run produced none (a prover build without the collector) or on any fetch error. The
    detector derives both signal 4 (surviving hostile primitives) and the signal-2 reachability gate from
    these. NB: needs an up-to-date POU (the ProverCLI repo)."""
    _aiss_env_for(url)
    graphs: list[dict] = []
    try:
        from prover_output_utility import ProverOutputAPI
        api = ProverOutputAPI(use_local=False)
        # fetch_output_file needs a current POU; older installs lack it -> AttributeError -> caught -> [].
        raw = _retry_transient(
            lambda: api.fetch_output_file(url, "survivingCallGraph_map.json"))  # type: ignore[attr-defined]
        if not raw:
            return []
        manifest = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return []
    for files in manifest.values():
        for fn in files:
            if "postOptimize" not in fn:
                continue
            try:
                content = _retry_transient(
                    lambda fn=fn: api.fetch_output_file(url, fn))  # type: ignore[attr-defined]
                if content:
                    graphs.append(json.loads(content) if isinstance(content, str) else content)
            except Exception:
                continue
    return graphs


def detect_url(url: str, *, work_dir: str | Path | None = None, solc_dir: str | Path | None = None,
               cut: str | None = None, external_call_graph: str | Path | None = None,
               include_dependencies: bool = False) -> DetectionReport:
    """The one-input entry: from a prover-run URL, fetch + derive everything and run the detector. `cut`
    and `external_call_graph` override what would otherwise be derived/fetched. `work_dir` (default: a
    temp dir) holds the fetched sources + generated AST."""
    work = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="detect-"))
    src = fetch_sources(url, work / "sources")
    conf = find_run_conf(src)
    cut = cut or cut_from_conf(conf)
    if not cut:
        raise ValueError(f"could not derive the main contract from {conf} — pass cut=")
    ast = ensure_ast(conf=conf, solc_dir=solc_dir)
    ecg = external_call_graph or fetch_external_call_graph(url, work)   # Reports artifact — from the tarball
    surviving_graphs = fetch_surviving_graphs(url)                      # signal 4 + the signal-2 gate
    return detect(url, ast_path=ast, cut=cut, external_call_graph=ecg,
                  surviving_graphs=surviving_graphs, include_dependencies=include_dependencies,
                  sources_root=conf.parent)
