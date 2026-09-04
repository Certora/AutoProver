"""Orchestration — `ensure_ast` (generate the AST when not supplied) and `detect` (fetch + fuse all
signals, attach boundaries, resolve toxic-entrypoint boundaries). The public entry point of the package."""
import re
import subprocess
import sys
from pathlib import Path

from ..difficulty import DifficultyReport, fetch_difficulty
from .ast_scan import _function_locations, scan_ast
from .callgraph import (
    FnFacts, _add_external_edges, _ast_call_graph, _bfs, _by_qual_or_bare, _caller_boundaries, _cone_weights,
    _descend_to_prims, _entrypoint_in_edges, _fn_facts, _is_reference_typename, _shallowest_view_boundary,
    _survives, _unit_declaring, reachable_from_main,
)
from .catalog import _PRIMITIVE_CATEGORIES, _surviving_names, surviving_hostile
from .fuse import detect_from
from .model import Boundary, Candidate, DetectionReport


# ---------------------------------------------------------------- AST acquisition (optional-arg design)
_MISSING_IMPORT_RE = re.compile(r'\d+:\d+:"([^"]+)"')


def _touch_missing_imported_specs(*outputs: str) -> list:
    """Recreate empty placeholders for imports certoraRun reports as missing. The prover prints
    `... import declarations do not import existing .spec files:` then `<line>:<col>:"<path>"` entries.
    The real files were skipped on upload precisely because they are EMPTY, so an empty placeholder is
    faithful. Returns the paths created (empty list if the failure is something else)."""
    created: list = []
    for out in outputs:
        if "do not import existing" not in out:
            continue
        for m in _MISSING_IMPORT_RE.finditer(out):
            p = Path("".join(m.group(1).split()))     # certoraRun wraps long paths across lines in captured
            if not p.exists():                         # (non-tty) output — rejoin before treating as a path
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("")
                created.append(p)
    return created


def ensure_ast(ast_path: str | Path | None = None, *, conf: str | Path | None = None,
               solc_dir: str | Path | None = None) -> Path:
    """Return a path to a solc AST dump. If `ast_path` is given (from a prior `--dump_asts` run, or a
    prior run), use it. Otherwise run `certoraRun <conf> --compilation_steps_only --dump_asts` (standalone
    mode) and return the freshest `.asts.json` it writes under `.certora_internal/`. `solc_dir` is
    prepended to PATH so the conf's `solcN.NN` resolves. Raises if neither input suffices."""
    if ast_path is not None:
        p = Path(ast_path)
        if not p.exists():
            raise FileNotFoundError(f"ast_path does not exist: {p}")
        return p
    if conf is None:
        raise ValueError("provide ast_path (existing AST) or conf (to generate one via certoraRun)")
    conf = Path(conf)
    work = conf.parent
    env_path = None
    if solc_dir is not None:
        import os
        env_path = {**os.environ, "PATH": f"{solc_dir}:{os.environ.get('PATH', '')}"}
    cmd = ["certoraRun", conf.name, "--compilation_steps_only", "--dump_asts"]
    proc = subprocess.run(cmd, cwd=work, env=env_path, capture_output=True, text=True)
    # TEMPORARY WORKAROUND: the source-files upload skips EMPTY files, so an empty importable source that
    # another file imports is dropped while its importer is kept — the fetched scene then fails to compile
    # on the missing import (fixed upstream for NEW runs; existing runs stay broken). The missing file is
    # empty by definition, so recreate the missing imported spec(s) as empty placeholders and retry.
    for _ in range(5):
        if proc.returncode == 0:
            break
        created = _touch_missing_imported_specs(proc.stdout or "", proc.stderr or "")
        if not created:
            break
        print(f"[detect] created {len(created)} empty placeholder spec(s) for skipped-empty imports; retrying",
              file=sys.stderr)
        proc = subprocess.run(cmd, cwd=work, env=env_path, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or proc.stdout or "").strip().splitlines()[-25:])
        raise RuntimeError(
            f"certoraRun AST generation failed (exit {proc.returncode}) in {work} for {conf.name}:\n{tail}\n"
            f"(hint: if the error is a missing solc, pass solc_dir / --solc-dir so the conf's solcN.NN "
            f"resolves; if it is a missing source/package, the fetched scene may be incomplete.)")
    dumps = sorted((work / ".certora_internal").rglob("*.asts.json"), key=lambda p: p.stat().st_mtime)
    if not dumps:
        raise FileNotFoundError(f"certoraRun produced no .asts.json under {work}/.certora_internal")
    return dumps[-1]





def _surviving_reach(graphs: list[dict]) -> set[str]:
    """The reachability set (sig-stripped `Contract.fn`) over the postOptimize surviving graphs — the
    functions that actually reach SMT. This is the authoritative signal-2 gate."""
    return {name.split("(", 1)[0] for g in graphs for name, _ in _surviving_names(g)}


def detect(job_url: str | None = None, *, ast_path: str | Path | None = None,
           conf: str | Path | None = None, cut: str, solc_dir: str | Path | None = None,
           include_dependencies: bool = False,
           external_call_graph: str | Path | None = None,
           surviving_graphs: list[dict] | None = None,
           sources_root: str | Path | None = None) -> DetectionReport:
    """Orchestrate the detector. `job_url` (optional) supplies the difficulty report (signals 1+3); with
    none, only the static signal-2 (hashing) runs. The AST is resolved by `ensure_ast` (`ast_path` if
    given, else generated from `conf`). `cut` is the verified contract name.

    `surviving_graphs` are the prover's postOptimize SurvivingCallGraph dumps: they drive signal 4 (the
    surviving hostile primitives) AND give the authoritative signal-2 reachability gate (the functions that
    actually reach SMT). Absent them, signal-2 falls back to AST + `external_call_graph` reachability from
    the CUT (see `reachable_from_main`). `sources_root` (the project root the AST paths are relative to,
    defaulting to the conf's directory) lets each candidate's start line be resolved from the source."""
    ast = ensure_ast(ast_path, conf=conf, solc_dir=solc_dir)
    if sources_root is None and conf is not None:
        sources_root = Path(conf).parent
    hash_signals = scan_ast(ast)
    surviving = surviving_hostile(surviving_graphs or [])
    surviving_set = _surviving_reach(surviving_graphs) if surviving_graphs else None
    cone: dict[str, int] = {}
    reachable: set[str] = set()
    edges: dict[str, set[str]] = {}
    facts: dict[str, FnFacts] = {}
    merged = _unit_declaring(ast, cut)                     # the CUT's compilation unit, built once
    if merged is not None:
        edges, roots, by_name, sizes = _ast_call_graph(merged, cut)
        if external_call_graph is not None:
            _add_external_edges(edges, by_name, external_call_graph)
        reachable = _bfs(edges, roots)                     # for cone (rank) + the ecg fallback gate
        cone = _cone_weights(edges, reachable, sizes)
        facts = _fn_facts(merged)                          # signature/expressible/mutating/nondet-ok, one walk
    if surviving_set:                                     # authoritative: exactly what reached SMT
        survivors_bare = {n.rpartition(".")[2] for n in surviving_set}
        hash_signals = [h for h in hash_signals if _survives(h.function, surviving_set, survivors_bare)]
    elif merged is not None and external_call_graph is not None:   # fallback: complete cross-contract edges
        hash_signals = [h for h in hash_signals if h.function in reachable]
    difficulty = fetch_difficulty(job_url, limit=None) if job_url else DifficultyReport()   # detector filters
    report = detect_from(hash_signals, difficulty, cut=cut, cone_weight=cone,
                         include_dependencies=include_dependencies, surviving=surviving)
    # A hashing or surviving candidate is the cost LEAF (the exact function to summarize) — offer caller
    # boundaries too: where else the summary can be placed (a clean-signature caller subsumes the leaf and
    # is often more feasible to model — see Boundary).
    bounds_reach = surviving_set if surviving_set else (reachable or None)
    if edges:
        # nonlinear-only candidates: descend to the shared nonlinear PRIMITIVE they inline, and count how
        # many candidates share each (fan-in) — a primitive shared across many methods is the real target.
        # Filter by AST reachability, NOT the surviving set: library `using`-for primitives (e.g.
        # MathLib.mulDivDown) get no INTERNAL_FUNC_START annotation, so they are absent from the surviving
        # set even though they are genuinely inlined into a surviving method.
        nl = [c.function for c in report.candidates
              if "nonlinear" in c.signals and "hashing" not in c.signals
              and not any(s in _PRIMITIVE_CATEGORIES for s in c.signals)]
        prim_reach = {m: _descend_to_prims(edges, m, reachable or None) for m in nl}
        fanin: dict[str, int] = {}
        for prims in prim_reach.values():
            for p in prims:
                fanin[p] = fanin.get(p, 0) + 1
        for c in report.candidates:
            if "hashing" in c.signals or any(s in _PRIMITIVE_CATEGORIES for s in c.signals):  # walk UP to a caller
                # Only SOUND containers: a boundary is a place to `=> NONDET` INSTEAD of the leaf, so it must be
                # value/void-return (`nondet_ok`). Reference-returning / would-not-help callers are not offered
                # — a directly-summarizable primitive (e.g. an in-memory sort) then simply lists no boundaries.
                c.caller_boundaries = _caller_boundaries(edges, facts, c.function, bounds_reach, nondet=True)
            elif "nonlinear" in c.signals:                 # descend DOWN to the shared nonlinear primitive
                targets = sorted(prim_reach.get(c.function, {}).items(),
                                 key=lambda kv: (-fanin.get(kv[0], 0), kv[1], kv[0]))[:4]
                for p, d in targets:
                    f = facts.get(p) or FnFacts(p)
                    if not f.expressible:    # a shared primitive we can't express as a value is no target
                        continue
                    c.callee_boundaries.append(Boundary(p, d, f.signature, f.mutating, shared=fanin.get(p, 1)))
            elif "branching" in c.signals:                 # path-count: NONDET the loop, gated on return + writes
                f = _by_qual_or_bare(facts, c.function)
                ok = f.nondet_ok if f else False           # value/void return => NONDET-able
                mutating = f.mutating if f else True       # unknown -> assume mutating (conservative)
                # Offer summarizable boundaries for EVERY branching candidate (value/void-return callers
                # that wrap the loop, ranked view/pure-first), the same way the nonlinear/hashing/toxic
                # signals do — a reference-returning OR state-mutating hotspot can often be replaced by a
                # cleanly-sound (view/pure, value/void) boundary. Only view/pure value/void is
                # unconditionally sound to `=> NONDET`.
                c.caller_boundaries = _caller_boundaries(edges, facts, c.function, bounds_reach, nondet=True)
                has_bnd = bool(c.caller_boundaries)
                if ok and not mutating:                    # value/void return AND view/pure -> unconditionally sound
                    c.candidate_summary = ("=> NONDET (view/pure, value/void return): sound over-approximation, "
                                           "deletes the loop/path subproblem")
                elif ok:                                   # value/void return but STATE-MUTATING: NONDET drops writes
                    c.candidate_summary = ("state-MUTATING: `=> NONDET` drops its writes, so it is sound ONLY if the "
                                           "property does not read the state it mutates" + (
                                               "; else prefer a view/pure value/void boundary below"
                                               if has_bnd else "; else verify"))
                elif has_bnd:                              # reference return, but a value/void container exists
                    c.candidate_summary = ("returns a reference type (the prover rejects `=> NONDET` on it): "
                                           "NONDET a value/void caller/container that wraps its loop instead "
                                           "(see boundaries)")
                else:                                      # reference return, no in-scene value/void boundary found
                    c.candidate_summary = ("returns a reference type (the prover rejects `=> NONDET` on it) and no "
                                           "value/void caller/container that wraps its loop was found in scene; verify")
        # Prover-toxic CUT entrypoints (the rules' own external subjects) can't be summarized themselves, but
        # their shallowest sound inner boundary can. Surface that boundary as a candidate — this is what a
        # human summarizes when the method under test times out (e.g. liquidationCall ->
        # _calculateLiquidationAmounts), which no leaf-primitive signal catches.
        present = {c.function for c in report.candidates}
        for ep, degree, mag in report.toxic_entrypoints:
            boundary = _shallowest_view_boundary(edges, facts, ep)
            if boundary is None or boundary in present:
                continue
            present.add(boundary)
            score = round(mag / report.nl_max * 100.0, 1) if report.nl_max else mag
            report.candidates.append(Candidate(
                function=boundary, signals=("nonlinear", "toxic-entrypoint"), score=score,
                evidence=f"shallowest summarizable boundary of prover-toxic {ep} (whose rule's nonlinearity "
                         f"reaches polynomial degree {degree}) — the method under test can't be summarized, "
                         f"but this internal view can, cutting its whole subtree",
            ))
        report.candidates.sort(key=lambda c: (-c.score, c.function))     # re-rank with the new candidates
    # attach each candidate's source location (file always; line when the source is readable)
    locations = _function_locations(ast, sources_root)
    for c in report.candidates:
        if c.function in locations:
            c.file, c.line = locations[c.function]
    # attach a signature + view/pure flag (writing the summary needs the param/return types, and `mutating`
    # to know a value-summary would erase side effects) — the same AST map the boundaries use.
    # Exact qualified match, else bare name: the difficulty report attributes an inlined fn to its CALLING
    # contract (`HubInstanceHarness.calculatePremiumRay`), which resolves by the bare `calculatePremiumRay`.
    for c in report.candidates:
        f = _by_qual_or_bare(facts, c.function)
        if f:
            c.signature, c.mutating = f.signature, f.mutating
    return report


