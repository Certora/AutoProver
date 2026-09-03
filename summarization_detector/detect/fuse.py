"""Signal fusion — `detect_from` folds the five signals into the ranked `Candidate` list (scoring,
soundness gates, per-category caps). Hotspot procId decoding and the cap bucketer live here too."""
from ..difficulty import DifficultyReport, Hotspot
from .catalog import _PRIMITIVE_CATEGORIES
from .model import (
    ALREADY_SUMMARISED_MIN_PCT, BRANCHING_MIN_PATHS, BRANCHING_MIN_PCT, Boundary, Candidate, DetectionReport,
    HashSignal, MAX_PER_CATEGORY, NONLINEAR_MIN_MAGNITUDE, NONLINEAR_MIN_PCT, SURVIVING_SCORE,
    _br_magnitude, _contract_of, _ghost_summary_name, _is_cvl_ghost, _nl_magnitude, _nl_ops_contributed,
    _normalize, _paths_contributed, _paths_contributed_value, _strip_procid,
)


# ---------------------------------------------------------------- fusion + classifier
def _decode_hotspot(h: Hotspot) -> tuple[bool, str, str, str]:
    """Decode a difficulty hotspot's procId into `(internal, fn, contract, bare)` — the shared prefix of
    the nonlinear and branching signal loops. `internal` = the prover's `(internal)` inlined-fn marker;
    `fn` = `Contract.fn` (marker stripped); `contract` = its host; `bare` = the name after the last `.`."""
    internal = h.function.strip().startswith("(internal)")
    fn = _strip_procid(h.function)
    return internal, fn, _contract_of(fn), fn.rpartition(".")[2]


def _parse_location(loc: str) -> tuple[str, int | None]:
    """Split a difficulty hotspot location (`file:line`, or just `file`) into `(file, line)`. `line` is
    `None` when it is absent or non-numeric. Source paths carry no `:`, so the last segment is the line."""
    file, _, line = loc.rpartition(":")
    if file and line.isdigit():
        return file, int(line)
    return loc, None


def _bucket(c: Candidate) -> str:
    """The cap bucket a candidate falls in (its score is only comparable within it). Any catalog hard-op
    signal -> "primitive"; else nonlinear -> "nonlinear"; else "hashing". `external` is a modifier, not a
    bucket of its own."""
    if any(s in _PRIMITIVE_CATEGORIES for s in c.signals):
        return "primitive"
    if "already-summarised" in c.signals:
        return "already-summarised"
    if "branching" in c.signals and "nonlinear" not in c.signals:
        return "branching"
    if "nonlinear" in c.signals:
        return "nonlinear"
    return "hashing"


def detect_from(hash_signals: list[HashSignal], difficulty: DifficultyReport, *, cut: str,
                include_dependencies: bool = False,
                cone_weight: dict[str, int] | None = None,
                surviving: dict[str, dict] | None = None) -> DetectionReport:
    """Fuse the four signals into a ranked candidate list. `difficulty` supplies signals 1 (nonlinear)
    and 3 (its hotspots whose contract != `cut` are resolved-expensive externals); `hash_signals` supply
    signal 2; `surviving` (from `surviving_hostile`) supplies signal 4 — raw prover-hostile primitives
    (bitwise / sort / exp / mulDiv) that survive optimization, keyed by function, carrying a category, the
    entry methods that reach it, the prover's `summarizable` flag, and a candidate summary. `cut` is the
    verified contract (its own methods are NOT "external"). Dependency-tree functions (lib/) are dropped
    from the hashing signal unless `include_dependencies` (surviving primitives are kept regardless — they
    are in the real problem). `cone_weight` re-weights the build-phase hashing signal by how much code
    consumes the result. CVL/ghost hotspots are already-applied summaries: a cheap one is dropped (it IS
    the summary), but one still contributing nonlinear ops is surfaced as an `already-summarised` candidate
    to coarsen (its summary is itself still nonlinear) — handled in-loop, not pre-filtered."""
    hotspots = difficulty.hotspots
    cand: dict[str, Candidate] = {}
    # Raw per-candidate severities for the two MAGNITUDE signals (nonlinear degree, branching path count).
    # Accumulated here, then normalized 0..100 across the run's candidates and folded into `score` — the raw
    # magnitudes are not comparable across categories, only within one, so each is scaled by its own sample.
    raw_nl: dict[str, float] = {}
    raw_br: dict[str, float] = {}

    def _bump(key: str, sig: str, score: float, evidence: str):
        c = cand.get(key)
        if c is None:
            cand[key] = Candidate(function=key, signals=(sig,), score=score, evidence=evidence)
        else:
            if sig not in c.signals:
                c.signals = (*c.signals, sig)
            c.score += score
            c.evidence += " | " + evidence

    # signal 1 (nonlinear) + 3 (external): the difficulty hotspots. The prover's procIds carry a visibility
    # marker; three kinds are handled distinctly:
    #  - unmarked `<CUT>.m`     -> the CUT's OWN external method = a rule subject, never summarized -> DROP
    #  - unmarked `<other>.m`   -> a cross-contract callee (e.g. HubInstanceHarness.previewRemoveByShares)
    #                              -> keep + flag as a resolved external (signal 3)
    #  - `(internal) <ctx>.m`   -> an inlined internal fn (the ray/bit math). The prover attributes it to the
    #                              CALLING contract, so the same fn recurs under several contexts and (as
    #                              `<CUT>.fls`) collides with a surviving primitive -> dedup by bare name,
    #                              keeping the highest-contribution instance (hotspots are pct-sorted).
    surviving_bare = {raw.split("(", 1)[0].rpartition(".")[2] for raw in (surviving or {})}
    seen_internal: set[str] = set()
    toxic_entrypoints: list[tuple[str, int, float]] = []
    for h in hotspots:
        if _is_cvl_ghost(h.function):                        # an already-applied summary still in the problem
            if h.pct < ALREADY_SUMMARISED_MIN_PCT:           # cheap ghost -> genuinely handled, drop
                continue
            mag = _nl_magnitude(h.degree, h.nl_ops, h.pct)
            if mag < NONLINEAR_MIN_MAGNITUDE:                # trivially nonlinear (low degree AND few ops) -> drop
                continue
            name = _ghost_summary_name(h.function)
            _bump(name, "already-summarised", 0.0,          # score folded in at normalization (raw_nl below)
                  f"already-applied summary still adds ~{_nl_ops_contributed(h.nl_ops, h.pct)} nonlinear ops "
                  f"(polynomial degree {h.degree}) — its summary is itself nonlinear; coarsen it to an "
                  f"uninterpreted/NONDET summary where the property does not read its result (a sound "
                  f"over-approximation)")
            raw_nl[name] = raw_nl.get(name, 0.0) + mag
            c = cand[name]
            if "nonlinear" not in c.signals:                 # nonlinearity is the problem; already-summarised qualifies
                c.signals = ("nonlinear", *c.signals)
            continue
        if h.pct < NONLINEAR_MIN_PCT:                        # drop the long, low-contribution tail
            continue
        internal, fn, contract, bare = _decode_hotspot(h)
        mag = _nl_magnitude(h.degree, h.nl_ops, h.pct)
        if mag < NONLINEAR_MIN_MAGNITUDE:                    # trivially nonlinear (low degree AND few ops) -> drop
            continue
        if contract == cut and not internal:                # the CUT's own external method (rule subject)
            toxic_entrypoints.append((fn, h.degree, mag))
            continue                                         # not summarizable itself; descend to a boundary later
        if bare in surviving_bare:                          # already a correctly-named surviving candidate
            continue
        if internal:
            if bare in seen_internal:                       # caller-attribution dup -> keep the top one
                continue
            seen_internal.add(bare)
        _bump(fn, "nonlinear", 0.0,                          # score folded in at normalization (raw_nl below)
              f"~{_nl_ops_contributed(h.nl_ops, h.pct)} nonlinear ops, polynomial degree {h.degree}")
        raw_nl[fn] = raw_nl.get(fn, 0.0) + mag
        if h.location:                                      # the difficulty report carries the location
            cand[fn].file, cand[fn].line = _parse_location(h.location)
        if contract and contract != cut and not internal:   # a genuine cross-contract external call
            # `external` is a context TAG, not a difficulty amount — it must not add score, or a modifier
            # would out-rank a genuinely harder (higher-magnitude) nonlinear candidate. The nonlinear
            # magnitude already scored above is what ranks it.
            _bump(fn, "external", 0.0, f"resolved external in {contract} (not the CUT)")

    # signal 5 (branching / path count): the SIBLING difficulty node. When a rule times out on loop/path
    # explosion rather than math, the nonlinearity node can be empty (the math is already summarized) while
    # this one names the loop-heavy functions. Same procId conventions as the nonlinear loop. The CUT's own
    # external method (the rule subject) is skipped — its internal loop callees carry the actionable signal.
    # The value/void-return soundness gate (a reference-returning branch fn can't be `=> NONDET`ed) is
    # applied in `detect()`, where the AST return types are available.
    seen_branch: set[str] = set()
    for h in difficulty.branching:
        if h.pct < BRANCHING_MIN_PCT:                        # drop the long, low-contribution tail
            continue
        if _paths_contributed_value(h.path_count, h.pct) < BRANCHING_MIN_PATHS:   # not a real explosion source
            continue
        internal, fn, contract, bare = _decode_hotspot(h)
        if contract == cut and not internal:                # CUT's own external method = rule subject -> skip
            continue
        if bare in seen_branch:                             # caller-attribution dup -> keep the top one
            continue
        seen_branch.add(bare)
        _bump(fn, "branching", 0.0,                          # score folded in at normalization (raw_br below)
              f"~{_paths_contributed(h.path_count, h.pct)} paths (loop/path count)")
        raw_br[fn] = raw_br.get(fn, 0.0) + _br_magnitude(h.path_count, h.pct)
        if h.location and cand[fn].file == "":              # keep the nonlinear location if already set
            cand[fn].file, cand[fn].line = _parse_location(h.location)

    # signal 2 (hashing/encoding): the AST scan. Dynamic-input hashing (unbounded bytes/string/array) is
    # the costly kind; a fixed-size digest (typical EIP-712) is bounded — score it far lower so the noise
    # of signature digests sinks below the real candidates rather than being dropped outright.
    for h in hash_signals:
        if h.is_dependency and not include_dependencies:
            continue
        cls = "dynamic-input" if h.dynamic_input else "fixed-size"
        _bump(h.function, "hashing", 20.0 if h.dynamic_input else 4.0,
              f"{'/'.join(h.patterns)} [{cls}] ({h.mutability or 'n/a'} {h.visibility})")

    # signal 4 (surviving hostile primitives): raw prover-hostile primitives still in the sanity run's
    # postOptimize TAC — a direct candidate. Score by reach (how many entry methods keep it); attach the
    # catalog category, candidate summary, reaching methods, and the prover's summarizable flag. A surviving
    # name carries a signature (`C.f(sig)`) while the difficulty/hashing keys are sig-less (`C.f`) — strip it
    # so the same function unifies into one candidate.
    for raw_fn, rec in (surviving or {}).items():
        fn = raw_fn.split("(", 1)[0]
        n = len(rec["reaching_methods"])
        _bump(fn, rec["category"], SURVIVING_SCORE,       # the hard-op class IS the signal
              f"{rec['category']}, reaches {n} method(s)")
        c = cand[fn]
        c.reaching_count = n
        c.summarizable = rec["summarizable"]
        c.candidate_summary = rec["candidate_summary"]

    # NONLINEAR score: population-normalized to 0..100 across the run's nonlinear candidates (degree x ops has
    # no natural absolute scale, so relative severity is what's readable). The scale spans the toxic
    # entrypoints too (their boundaries are scored later in `detect`, against this same `nl_max`), so one
    # candidate can't out-scale another purely by which code path surfaced it.
    nl_max = max([*raw_nl.values(), *(m for _, _, m in toxic_entrypoints)], default=0.0)
    for fn, s in _normalize(raw_nl, nl_max).items():
        cand[fn].score += s
    # BRANCHING score: ABSOLUTE, NOT population-normalized. Path explosion only matters in absolute terms —
    # 2^40 paths is a real problem, 4 paths is nothing — so a weak branching population must not inflate to
    # 100 the way divide-by-max would. The score is log2(paths this function contributes) (= raw_br / 100,
    # since raw_br = log2(rule paths) x within-rule %): ~2 for 4 paths, ~40 for 2^40. Nonlinear thus dominates
    # branching unless the path count is astronomical.
    for fn, mag in raw_br.items():
        cand[fn].score += round(mag / 100.0, 1)

    # cone-of-influence re-weighting for the hashing signal: build-phase cost has no per-function measure,
    # so scale a hashing candidate by how much reachable code consumes its result (normalized within the
    # candidate set, factor in [1, 2], so it stays comparable to the measured nonlinear pct). A hash whose
    # id threads the protocol rises; a leaf digest (tiny cone) stays low.
    if cone_weight:
        hashing = [c for c in cand.values() if "hashing" in c.signals]
        mx = max((cone_weight.get(c.function, 0) for c in hashing), default=0) or 1
        for c in hashing:
            w = cone_weight.get(c.function, 0)
            c.score *= 1 + w / mx
            c.evidence += f" | cone={w}"

    ranked = sorted(cand.values(), key=lambda c: (-c.score, c.function))   # score desc, name for stable ties
    kept: list[Candidate] = []
    per_cat: dict[str, int] = {}
    for c in ranked:                                    # score-ordered, so per category we keep the top ones
        cat = _bucket(c)
        if per_cat.get(cat, 0) < MAX_PER_CATEGORY.get(cat, 0):
            per_cat[cat] = per_cat.get(cat, 0) + 1
            kept.append(c)
    return DetectionReport(
        candidates=kept,
        toxic_entrypoints=sorted(toxic_entrypoints, key=lambda t: -t[2])[:5],  # top 5 by nonlinear magnitude
        nl_max=nl_max,
    )

