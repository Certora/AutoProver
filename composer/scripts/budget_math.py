"""Budget calibration + live-test matrix generation from autoprove run trails.

Reconstructs, per LLM call, exactly what ``CostAccumulator`` would have accrued —
``graphcore.utils.get_normalized_token_usage`` for the token buckets and
``composer.llm.pricing`` for the rates — at both cache-write TTLs (the trail doesn't
record which TTL a conversation used, so both bounds are shown; calibration uses the
1h bound, since the authors run with the long cache). Sub-agent threads
(``from_tool_id``) fold into their root thread, matching how budget scopes accrue: a
sub-agent spends from its parent's cost center. Phase attribution comes straight from
``ThreadMeta.cost_center`` (the thread logger stamps the ambient named-budget scope
into every thread record, budget or no budget); trails recorded before cost-center
tracking existed have no such field and classify entirely as unattributed.

The run trail comes from one of:

  * an ``ap-trail export`` dump — ``.json.gz`` (gzipped) or plain ``.json``;
  * a bare run id, fetched live from the store/checkpointer DB (the same wiring as
    ``ap-trail export``), honoring ``--uid`` / ``$AUTOPROVER_USER_ID``.

``--emit-matrix DIR`` additionally writes the calibrated live-test budget matrix —
ready-made ``--budget`` files plus a ``manifest.md`` stating each test's expected
outcome and the observables checklist:

    t1_control.json                  ample everything: must behave like an unbudgeted run
    t2_formalization_curtail.json    trips the component author mid-batch
    t4_pool_pressure.json            trips the shared pool partway through the run
    (T5, the caching interplay test, reuses t2 across two runs — see the manifest)

Calls whose model has no pricing-table entry accrue ZERO cost in production, so a
budget can never trip on them — the script warns loudly when it sees any.

Usage:
    uv run scripts/budget_math.py <dump.json[.gz] | run-id> [--uid U]
        [--cap-fraction 0.75] [--wrapup-turns 4] [--emit-matrix DIR]
"""

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from langchain_core.messages import AIMessage

from graphcore.utils import get_normalized_token_usage

from composer.diagnostics.budget import BUDGET_PRESSURE_THRESHOLD
from composer.io.run_index import ExportedMessage, ExportedRun, read_export
from composer.io.thread_logging import ThreadMeta
from composer.llm.pricing import price_per_mtok
from composer.pipeline.ptypes import PhaseBudget

PHASES: tuple[str, ...] = tuple(PhaseBudget.__annotations__)
UNATTRIBUTED = "unattributed"


def _phase_of(meta: ThreadMeta) -> str:
    """The phase a thread accrued to, straight from its recorded ``cost_center``.
    ``None`` (work outside any named scope: the pool, or pre-pipeline code) and any
    center that isn't a `PhaseBudget` phase land in UNATTRIBUTED."""
    cc = meta.get("cost_center")
    return cc if cc in PHASES else UNATTRIBUTED


# ---------------------------------------------------------------------------
# Per-call costing (mirrors CostAccumulator._cost_of at both cache-write TTLs)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Call:
    model: str | None
    fresh_input: int
    cache_read: int
    cache_write: int
    output: int
    cost_5m: float
    cost_1h: float

    @property
    def priced(self) -> bool:
        return self.cost_1h > 0 or not (self.fresh_input or self.cache_read or self.output)


def _cost_of(msg: AIMessage) -> Call | None:
    usage = get_normalized_token_usage(msg)
    total_input = usage["total_input_tokens"]
    output = usage["total_output_tokens"]
    if not (total_input or output):
        return None
    cache_read = usage["cache_read_tokens"]
    cache_write = usage["cache_write_tokens"]
    fresh = max(0, total_input - cache_read - cache_write)
    model = usage["model_name"]
    tier = price_per_mtok(model, total_input)
    if tier is None:
        return Call(model, fresh, cache_read, cache_write, output, 0.0, 0.0)

    def price(write_rate: float) -> float:
        return (
            fresh * tier.input
            + cache_read * tier.cache_read
            + cache_write * write_rate
            + output * tier.output
        ) / 1_000_000

    return Call(model, fresh, cache_read, cache_write, output,
                price(tier.cache_write), price(tier.cache_write_1h))


# ---------------------------------------------------------------------------
# Root folding + aggregation
# ---------------------------------------------------------------------------

@dataclass
class Root:
    description: str
    phase: str
    members: list[str]
    calls: list[Call]

    def cost(self, long_cache: bool = True) -> float:
        return sum(c.cost_1h if long_cache else c.cost_5m for c in self.calls)


def _fold_roots(run: ExportedRun) -> list[Root]:
    """Fold sub-agent threads into their spawning root via ``from_tool_id`` chains."""
    owner_of_tool: dict[str, int] = {}
    for i, t in enumerate(run.threads):
        for entry in t.timeline:
            if isinstance(entry, ExportedMessage) and isinstance(entry.data, AIMessage):
                for tc in entry.data.tool_calls:
                    if t_id := tc.get("id"):
                        owner_of_tool[t_id] = i

    def root_of(idx: int) -> int:
        seen: set[int] = set()
        while idx not in seen:
            seen.add(idx)
            from_tool = run.threads[idx].meta.get("from_tool_id")
            if from_tool is None or from_tool not in owner_of_tool:
                return idx
            idx = owner_of_tool[from_tool]
        return idx

    groups: dict[int, list[int]] = {}
    for i in range(len(run.threads)):
        groups.setdefault(root_of(i), []).append(i)

    roots: list[Root] = []
    for root_idx, members in sorted(groups.items()):
        meta = run.threads[root_idx].meta
        calls: list[Call] = []
        for i in members:
            for entry in run.threads[i].timeline:
                if isinstance(entry, ExportedMessage) and isinstance(entry.data, AIMessage):
                    if (c := _cost_of(entry.data)) is not None:
                        calls.append(c)
        roots.append(Root(
            description=meta["description"],
            phase=_phase_of(meta),
            members=[run.threads[i].meta["description"] for i in members],
            calls=calls,
        ))
    return roots


# ---------------------------------------------------------------------------
# Input loading
# ---------------------------------------------------------------------------

async def _fetch_from_db(run_id: str, uid: str | None) -> ExportedRun:
    # Imported lazily: pulls the full service stack, which file-based invocations skip.
    from composer.io.run_index import build_export
    from composer.workflow.services import checkpointer_context, store_context
    async with store_context() as store, checkpointer_context() as checkpointer:
        return await build_export(store, checkpointer, run_id, uid=uid)


def load_run(source: str, uid: str | None) -> ExportedRun:
    p = Path(source)
    if p.exists():
        if p.name.endswith(".gz"):
            return read_export(str(p))
        return ExportedRun.model_validate_json(p.read_text())
    return asyncio.run(_fetch_from_db(source, uid))


# ---------------------------------------------------------------------------
# Matrix generation
# ---------------------------------------------------------------------------

def _cap(x: float) -> float:
    """Round a computed cap to cents, never below one cent (a 0.00 cap means
    'curtail immediately', which is a test in itself but not what calibration wants)."""
    return max(0.01, round(x, 2))


@dataclass
class Matrix:
    """The calibrated budget files, as {filename: {total, caps}} plus manifest prose."""
    files: dict[str, dict]
    manifest: str


def build_matrix(
    run: ExportedRun,
    roots: list[Root],
    *,
    cap_fraction: float,
    wrapup_turns: int,
) -> Matrix:
    theta = BUDGET_PRESSURE_THRESHOLD
    run_total = sum(r.cost() for r in roots)
    phase_spend = {p: sum(r.cost() for r in roots if r.phase == p) for p in (*PHASES, UNATTRIBUTED)}

    # The author proxy: the priciest formalization root, falling back to the priciest
    # root overall (e.g. a fully cache-warm source run), stated as a proxy in the manifest.
    formalization_roots = [r for r in roots if r.phase == "formalization"]
    author = max(formalization_roots or roots, key=lambda r: r.cost())
    author_is_proxy = not formalization_roots
    author_cost = author.cost()
    per_call = [c.cost_1h for c in author.calls]
    max_call = max(per_call, default=0.0)
    late = per_call[-max(1, len(per_call) // 4):]
    late_mean = sum(late) / len(late) if late else 0.0

    ample = max(10.0, round(20 * run_total))
    ample_caps = {p: ample for p in PHASES}

    t2_cap = _cap(cap_fraction * author_cost)
    t4_total = _cap(theta * run_total)

    files: dict[str, dict] = {
        "t1_control.json": {"total": ample, "caps": dict(ample_caps)},
        "t2_formalization_curtail.json": {
            "total": ample, "caps": {**ample_caps, "formalization": t2_cap},
        },
    }
    files["t4_pool_pressure.json"] = {"total": t4_total, "caps": dict(ample_caps)}

    def headroom_note(cap: float) -> str:
        headroom = (1 - theta) * cap
        if not max_call:
            return "no per-call data"
        note = (f"headroom ${headroom:.2f} ≈ {headroom / late_mean:.1f} typical late turns"
                f" / {headroom / max_call:.1f} worst-case turns")
        if headroom / max_call < wrapup_turns:
            note += (f" — thinner than a {wrapup_turns}-turn worst case, so a hard stop"
                     " (`Curtailed(partial=None)`) mid-wrap-up is a TOLERATED outcome")
        return note

    unobserved = [p for p in PHASES if phase_spend[p] == 0]
    models = sorted({c.model for r in roots for c in r.calls}, key=str)

    lines: list[str] = [
        f"# Budget test matrix — calibrated from run `{run.run_id}`",
        "",
        f"Source window: {run.run['start_time']} .. {run.run['end_time']}  ",
        f"Models: {', '.join(map(str, models))}  ",
        f"Observed live LLM spend (1h cache-write bound): **${run_total:.2f}**  ",
        f"Warn threshold θ = {theta} (warn at θ·cap; hard stop is cooperative, at cost > cap)",
        "",
        "## Observed baselines (sub-agents folded into their root)",
        "",
        "| root | phase | calls | $ (5m..1h) |",
        "|---|---|---:|---|",
        *(f"| {r.description} | {r.phase} | {len(r.calls)} | "
          f"${r.cost(False):.4f}..${r.cost():.4f} |" for r in roots),
        "",
        f"Author baseline: `{author.description}` — ${author_cost:.4f} over "
        f"{len(author.calls)} calls (max ${max_call:.4f}, late-turn mean ${late_mean:.4f})."
        + (" **Proxy**: no formalization-phase thread ran live in the source run (cache-warm)."
           if author_is_proxy else ""),
    ]
    if unobserved:
        lines += [
            "",
            f"Phases with NO observed spend in the source run (cache-warm or subprocess): "
            f"{', '.join(unobserved)} — their caps below are ample/uncalibrated. For a fully "
            "calibrated matrix, source a cold-cache run.",
        ]

    lines += [
        "",
        "## Tests",
        "",
        "Run each against a **fresh `--cache-ns` and `--memory-ns`** (warm phases spend ~$0 "
        "and can never trip a budget), e.g.:",
        "",
        "    console-autoprove <project> <path:Contract> [system-doc] \\",
        "        --budget <this dir>/t2_formalization_curtail.json \\",
        "        --cache-ns budget-t2-$(date +%s) --memory-ns budget-t2",
        "",
        f"### T1 — control (`t1_control.json`: total ${ample}, all caps ${ample})",
        "Expected: identical behavior to an unbudgeted run. No wrap-up alerts in any "
        "transcript, no `.unverified` files, report has no budget appendix, exit 0. Guards "
        "against monitor-injection regressions.",
        "",
        f"### T2 — formalization curtailment (`t2_formalization_curtail.json`: "
        f"formalization cap ${t2_cap})",
        f"Warn fires at ≥ ${theta * t2_cap:.2f} of author spend "
        f"({cap_fraction:.0%} of the observed ${author_cost:.2f} batch → warn lands "
        f"~{theta * cap_fraction:.0%} of the way through). Expected: `Curtailed(partial)` — "
        "wrap-up alert in the author transcript, no prover/judge calls after it, "
        "`autospec_*.spec.unverified` on disk with no runnable sibling/conf, component in the "
        f"report appendix, exit code reflects `all_failed` for single-component scenarios. "
        f"{headroom_note(t2_cap)}.",
    ]
    lines += [
        "",
        f"### T4 — pool pressure (`t4_pool_pressure.json`: total ${t4_total}, caps ample)",
        f"The shared pool warns at ≥ ${theta * t4_total:.2f} cumulative (observed full run "
        f"${run_total:.2f}), so every agent from that point starts life in the wrap-up window. "
        "Expected: one or more curtailed components, `all_failed` exit, extraction quietly "
        "running fewer bug rounds under pressure (by design — observe, don't fail on it).",
        "",
        "### T5 — caching interplay (two runs, reuses `t2_formalization_curtail.json`)",
        "Run A: T2 budget + fresh shared cache-ns → curtailed; verify via `cache-autoprove` "
        "that NO generation cache entry exists for the curtailed component. Run B: same "
        "cache-ns, `t1_control.json` → the author re-runs from scratch (no stale partial) and "
        "delivers. Proves curtailed work is redone by a better-funded run.",
        "",
        "## Observables checklist (T2–T4)",
        "",
        "- exit code / `BUDGET:` lines in the failure summary",
        "- `*.unverified` present; unsuffixed sibling absent; no conf for curtailed stems",
        "- `report.json`: `curtailed_components` dispositions, coverage warning + count, "
        "groups/`prover_links` exclude curtailed",
        "- rendered HTML (`autoprove-report-render`) shows the budget appendix",
        "- thread trail (`ap-trail export` + this script): the `<system-alert>` wrap-up "
        "appears in the author transcript; no `verify_spec`/`feedback_tool` calls after it",
        "- `components_to_prover_runs.json` lacks curtailed entries",
        "",
        "## Caveats",
        "",
        "- An unpriced model accrues $0 — budgets can NEVER trip on it. Check the models "
        "line above against `composer/llm/pricing.py` before running.",
        "- Calibration uses the 1h cache-write bound (authors run the long cache); the 5m "
        "bound runs ~20% cheaper.",
        "- AutoSetup's subprocess LLM calls never pass through `CostAccumulator`, and the "
        "custom-summaries agent installs no budget monitor, so `formalization_preparation` "
        "is attribution-only: it accrues into the pool but can neither warn nor stop.",
        "",
        f"Regenerate: `uv run scripts/budget_math.py {run.run_id} --emit-matrix <dir>`",
        "",
    ]
    return Matrix(files=files, manifest="\n".join(lines))


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _print_analysis(run: ExportedRun, roots: list[Root]) -> None:
    print(f"run {run.run_id}  ({run.run['start_time']} .. {run.run['end_time']})")
    print(f"tags: {run.run['tags']}\n")

    hdr = (f"{'root (sub-agents folded)':52} {'phase':26} {'calls':>5} "
           f"{'$5m':>8} {'$1h':>8}")
    print(hdr)
    print("-" * len(hdr))
    for r in roots:
        print(f"{r.description[:52]:52} {r.phase:26} {len(r.calls):>5} "
              f"{r.cost(False):>8.4f} {r.cost():>8.4f}")
        for m in r.members:
            if m != r.description:
                print(f"  + {m[:76]}")

    run5 = sum(r.cost(False) for r in roots)
    run1 = sum(r.cost() for r in roots)
    print(f"\nwhole-run LLM spend: ${run5:.4f} (all-5m) .. ${run1:.4f} (all-1h)")

    models: dict[str | None, int] = {}
    unpriced: dict[str | None, int] = {}
    for r in roots:
        for c in r.calls:
            models[c.model] = models.get(c.model, 0) + 1
            if not c.priced:
                unpriced[c.model] = unpriced.get(c.model, 0) + 1
    print("models: " + ", ".join(f"{m}×{n}" for m, n in sorted(models.items(), key=lambda kv: -kv[1])))
    if unpriced:
        print("\n!! UNPRICED MODELS (accrue $0 in production — budgets can NEVER trip on these):")
        for m, n in unpriced.items():
            print(f"   {m!r}: {n} call(s)")

    fat = max(roots, key=lambda r: r.cost())
    print(f"\npriciest root: {fat.description!r} — ${fat.cost():.4f} over {len(fat.calls)} calls")
    running, marks = 0.0, {0.25, 0.5, 0.75}
    total = fat.cost() or 1.0
    for n, c in enumerate((c.cost_1h for c in fat.calls), 1):
        running += c
        share = running / total
        if (crossed := {m for m in marks if share >= m}):
            marks -= crossed
            print(f"  call {n:>3}: ${running:.4f}  ({share:.0%})")


def main() -> int:
    curr_doc = __doc__
    assert curr_doc is not None
    ap = argparse.ArgumentParser(description=curr_doc[0])
    ap.add_argument("source", help="run-trail source: a .json / .json.gz export, or a run id (fetched from the DB)")
    ap.add_argument("--uid", default=None,
                    help="user id for DB fetch (defaults to $AUTOPROVER_USER_ID / _anonymous)")
    ap.add_argument("--cap-fraction", type=float, default=0.75,
                    help="T2 curtailment cap as a fraction of the observed author-batch spend")
    ap.add_argument("--wrapup-turns", type=int, default=4,
                    help="worst-case turns of headroom a graceful wrap-up wants")
    ap.add_argument("--emit-matrix", type=Path, default=None, metavar="DIR",
                    help="write the calibrated budget files + manifest.md into DIR")
    args = ap.parse_args()

    run = load_run(args.source, args.uid)
    roots = _fold_roots(run)
    if not any(r.calls for r in roots):
        print("no priced LLM calls found in this run trail", file=sys.stderr)
        return 1
    if run.threads and not any("cost_center" in t.meta for t in run.threads):
        print(
            "note: this trail predates cost-center tracking (no ThreadMeta.cost_center); "
            "every root is unattributed and per-phase calibration is unavailable.",
            file=sys.stderr,
        )

    _print_analysis(run, roots)

    if args.emit_matrix is not None:
        matrix = build_matrix(
            run, roots, cap_fraction=args.cap_fraction, wrapup_turns=args.wrapup_turns,
        )
        outdir: Path = args.emit_matrix
        outdir.mkdir(parents=True, exist_ok=True)
        for name, payload in matrix.files.items():
            (outdir / name).write_text(json.dumps(payload, indent=2) + "\n")
        (outdir / "manifest.md").write_text(matrix.manifest)
        print(f"\nwrote {len(matrix.files)} budget file(s) + manifest.md -> {outdir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
