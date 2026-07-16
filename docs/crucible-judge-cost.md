# Plan — reducing the Crucible judge's runtime cost

**Status:** proposed. The Crucible reviewer/judge turn (added in the formalization loop — see
`docs/rust-backend-api.md` §4) is correct and e2e-verified, but it roughly **5×'d** the end-to-end
wall-clock (a `solana_vault` gate went from ~22 min to **1:53:04**). This plan measures where the
cost goes, contrasts the approach with the CVL and Foundry backends' judges, and proposes a phased
reduction that keeps the judge's catch rate.

## 1. Where the cost goes (measured, from the e2e run)

| Signal (one `solana_vault` gate, 13 properties) | Author | Judge |
|---|---|---|
| LLM turns | 23 | **22** |
| `code_explorer` sub-agent calls | 59 | **53** |
| Model tier | heavy (Opus) | **heavy (Opus)** |

Three drivers, in order of impact:

1. **The judge re-explores the program from scratch.** 53 Code Explorer sub-agents inside judge
   turns — almost as many as authoring's 59 — even though the program API (`api_facts`), the
   fixture, and the source were *already* gathered during authoring. Each `code_explorer` is itself
   a multi-call sub-agent, so this is the real multiplier.
2. **The judge runs on the heavy model** (`run_llm_agent` → `env.builder_heavy()`) for a bounded
   review task.
3. **It roughly doubles the turns per component** (22 judge ≈ 23 author), and each judge
   *rejection* (~6 in the run) adds a full author+judge re-cycle — all on each component's critical
   path.

## 2. How CVL and Foundry judge today — and why Crucible costs more

All three backends run essentially the same *kind* of review (a "are these tests/specs meaningful
evidence?" pass with the full `source_tools + rag_tools` belt on the heavy model). The cost gap is
**architectural**, not prompt size.

| Aspect | CVL / prover (`property_feedback_judge`) | Foundry (`feedback_tool`) | Crucible (`judge_prompt`) |
|---|---|---|---|
| **Driver** | author-invoked **tool** (in-graph) | author-invoked **tool** (in-graph) | **host-driven** turn (out-of-graph) |
| **Cadence** | when the author calls it; feedback can be addressed *without* re-invoking | when the author calls it | **unconditional — every author attempt**, before validate |
| **Context continuity** | shares the run's memory (`ctx.get_memory_tool()`) + the current spec (`get_cvl`); accumulates facts across rounds | shares the run's memory + `get_test`; prior-round conclusions assumed still valid | **fresh, stateless turn** — no memory tool, no authoring context handed in |
| **Source exploration** | has the belt, but works mostly from the passed artifact + memory | has the belt (source + rag) | has the belt and **re-derives everything** each turn |
| **Model** | heavy | heavy | heavy |
| **Acceptance** | author must clear feedback before `result` | judge must stamp the buffer before `result` | host parses `{accept, feedback}`; re-authors on reject |

The decisive differences:

- **In-graph, author-invoked (CVL/Foundry) vs. host-driven, unconditional (Crucible).** Because the
  CVL/Foundry judge is a tool the author calls *when it is ready*, it runs deliberately (often once)
  and inside the authoring conversation. Crucible's judge — by the **passive-service design**
  (`docs/rust-pure-app.md`: the wheel is stateless pure callouts, Python owns the loop) — is a
  *separate* turn the host fires after **every** author attempt.
- **Memory / context continuity.** CVL and Foundry judges attach `ctx.get_memory_tool()` and are
  handed the artifact under review, so they don't re-mine framework facts (the Foundry prompt even
  says prior-round memory "MAY be assumed still valid"). Crucible's judge turn binds only
  `all_tools` (= `source_tools + rag_tools`, **no memory**) and gets no authoring context, so it
  re-explores the program from zero — the 53 `code_explorer` calls.

So Crucible's cost is largely the price of statelessness: the same review, but re-derived from
scratch, unconditionally, on the heavy model. **Most of the plan below is about recovering the
context-sharing that CVL/Foundry get for free from being in-graph — within the service-shaped
constraint that the wheel stays a set of pure callouts.**

## 3. Plan

### Phase 1 — close the statelessness gap (biggest win, lowest risk)
Give the judge what authoring already knows, so it stops re-deriving:
- **Inject the gathered context into the judge prompt** — the `api_facts` block and the fixture are
  already in `input`; pass them in (they already are, partly) and add the program's key source
  facts, so the judge reasons from them instead of re-exploring.
- **Restrict the judge's tools + recursion.** Drop or tightly cap `code_explorer` for the judge
  turn (keep a bounded `get_file`/`grep` for spot-checks) and lower its `recursion_limit`. This is
  where the 53 explorations collapse.
- **Give the judge the run's memory tool** (the CVL/Foundry lever) so facts verified for one
  property carry to the next instead of being re-derived per component.

### Phase 2 — fix the cadence (match the author-invoked pattern)
- **Don't judge unconditionally every attempt.** Today every attempt is author→judge→validate, so a
  build-fail re-author re-judges. Only judge new *logic* (`failure is None or
  failure.kind == "judge"`) — a mechanical compile fix doesn't need re-review. (The `TEST_CHEAT_SHEET`
  lamports fix already reduces build-fail retries.)
- **Cap judge rounds** (e.g. accept after one revise) so a stubborn judge can't stack cycles — the
  bounded analogue of CVL/Foundry's "address feedback without re-invoking."

### Phase 3 — make it tunable (escape hatch + measurement knob)
- Descriptor flag / CLI arg for judge **enable** and **model tier**. Lets a run trade cost for rigor
  (off for quick iteration, on for a final gate), and is how we A/B the phases.
- **Optional divergent lever: judge on the lite model.** Unlike Phases 1–2 (which align Crucible with
  CVL/Foundry), CVL and Foundry both judge on the *heavy* model — so dropping Crucible to Sonnet is a
  deliberate divergence. Cheap, but validate the catch rate before making it the default; prefer it as
  an opt-down once Phases 1–2 land.

## 4. Validation

Each phase re-runs `test_crucible_e2e_gate` and compares **wall-clock + verdict parity**: do the
12 GOOD / 1 BAD outcomes hold, and does the judge still catch the fee-oracle class (now that the
`TEST_CHEAT_SHEET` fix is in)? The gate is ~2 h and paid, so iterate Phases 1–2 on a **smaller
scenario** (fewer properties) first and use the full gate only to confirm.

## 5. Architectural note

The judge is currently a separate host-driven turn because the current host loop runs it that way —
**not** because the passive-service design forbids the CVL/Foundry `feedback_tool` (author-invoked,
in-graph) pattern. The author loop already runs in Python (`run_llm_agent`), so the host *could* bind
a judge tool into it that reuses the wheel's `judge_prompt` — a host-side change that leaves the wheel
API untouched. That alternative is written up in **`docs/crucible-judge-in-loop.md`**.

The two paths trade off effort vs. shape: this doc's Phases 1–2 recover the bulk of the in-graph
efficiency (shared context, memory continuity, deliberate cadence) with a localized change to the
current single-shot author; the in-loop proposal removes the cost at its architectural source but
refactors the authoring loop. A reasonable sequence is Phase 1 now, converging toward the in-loop
design.
