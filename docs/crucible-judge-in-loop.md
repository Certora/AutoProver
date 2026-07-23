# Proposal (alternate) — move the Crucible judge into an author-loop tool

**Status:** implemented (host-side). This is the **alternate** to the phased mitigation in
`docs/crucible-judge-cost.md`, and it is now the *only* judge architecture — the previous
host-driven, per-attempt judge turn has been removed. The judge becomes a **tool the rustapp author
agent calls**, matching how the Foundry (`feedback_tool`) and CVL (`property_feedback_judge`) backends
already work, removing the statelessness and the unconditional cadence at their root (the judge ~5×'d
the e2e; see the cost doc §1).

**What shipped:** a `request_review` tool (`composer/rustapp/adapter.py`) bound into the author agent
**whenever the wheel supplies a judge for the input** (detected by probing the pure `judge_prompt`
callout — it returns `None` exactly when there is no judge), running that `judge_prompt` as an
in-session sub-agent; a `bind_standard` `_review_gate` validator that blocks `result` until the
submitted draft was accepted; the run memory tool shared across author/judge/components; and the host
loop no longer runs a separate `_judge_turn`. `crucible_app` has a component judge, so it always runs
in-loop; `echoprover` (no judge) keeps the single-shot author. The final validation below (e2e
wall-clock + verdict parity) is still pending.

## 1. Why this is possible (correcting the cost doc's §5)

The cost doc claimed the passive-service design *prevents* the Foundry pattern. That's wrong. The
Rust wheel is passive, but the **author loop already runs in Python**: `_author_turn` →
`run_llm_agent` → `bind_standard(...).with_tools(...)` + `run_to_completion` is a full tool-enabled
agent. The wheel only supplies the *prompt string* (`author_prompt`); Python owns the agent
machinery and the tool belt. So Python can bind a judge/`request_review` tool into that loop that
invokes the wheel's existing `judge_prompt` — a **host-side change; the wheel API does not change**.
The judge is pure LLM review (no toolchain), so nothing about the sandbox boundary ("the LLM controls
file contents, never argv") is in the way — that constraint only pins `compile`/`validate`, which stay
host-driven.

## 2. The design

Model it on Foundry's author (`composer/foundry/author.py`): a stateful
write → review → revise → publish agent, with the judge behind a tool and a completion gate.

**For a judge-enabled wheel, the authoring turn becomes a richer agent** (today it's a single-shot
`doc`-style agent whose final answer *is* the artifact). It gains:

- **A draft buffer** — `put_spec` / `get_spec` tools (the peers of Foundry's `put_test_raw` /
  `get_test`). The author writes its candidate spec into agent state instead of returning it as the
  final answer.
- **A `request_review` tool** (the peer of `feedback_tool`). When called, it runs the **judge
  sub-agent** built from the wheel's `judge_prompt(input, current_draft)`, with the run's memory
  (`ctx.get_memory_tool()`), `get_spec`, and a *bounded* source/rag belt; it returns the feedback and
  records whether the **current** draft was accepted. This is the direct analogue of
  `_build_feedback_thunk`.
- **A completion gate** — a `bind_standard` `validator` (the mechanism CVL/Foundry use, e.g.
  `did_rough_draft_read`) that **rejects finalization unless the judge accepted the current draft**.
  The author must clear review before it can publish, so it can't skip the judge.

**The host loop simplifies.** `RustFormalizer.formalize` / `author_and_compile` drop the separate
`_judge_turn`; they run the (judge-integrated) author once to get a **judge-accepted** draft, then
`compile` → `validate` as today. A build failure still re-invokes the author (the host loop), but a
*judge* rejection is now handled **inside** the author session — the author self-revises against the
feedback rather than being re-invoked fresh.

**The wheel is unchanged.** `judge_prompt(input, spec)` is reused verbatim as the sub-agent's prompt;
no new callout. A descriptor flag (e.g. `judge_in_loop: bool`) can gate the behavior so the host
knows whether to bind the tool.

**Non-judge wheels are unaffected.** `echoprover` returns `judge_prompt → None`; the author keeps its
current single-shot `doc` shape and none of the buffer/review/gate machinery is bound.

### Sketch

```
run_llm_agent(env, author_prompt, ..., judge=JudgeSpec(module, input))   # judge optional
  bind_standard(builder_heavy, ST, validator=require_judge_accepted)
    .with_tools(bounded_source + rag + memory + [put_spec, get_spec,
                                                 request_review, result])
  # request_review -> run_to_completion(judge sub-agent from module.judge_prompt(input, get_spec()),
  #                                     tools = memory + get_spec + bounded source/rag)
  # result -> allowed only if the last request_review accepted the current draft
returns: the judge-accepted spec
```

## 3. Why it fixes the cost (vs. the phased plan)

| Cost driver (cost doc §1) | Phased plan | In-loop judge |
|---|---|---|
| Judge re-explores from scratch (53 `code_explorer`) | inject context + restrict tools + add memory | **memory shared in-session + author's context already present** → same effect, structurally |
| Judge runs every attempt (unconditional) | skip on compile-retries, cap rounds | **subsumed** — the author calls review deliberately, and self-revises in-session; no per-attempt host judge |
| Judge on heavy model | optional opt-down to lite (divergent) | orthogonal — can still restrict the sub-agent's tools; heavy matches CVL/Foundry |

Net: the in-loop design *is* the endpoint the phased plan approximates. It recovers the in-graph
efficiency (shared memory, deliberate cadence, self-revision) that CVL/Foundry get for free — and
gives one mental model across all three backends.

## 4. What this makes vestigial

- `_judge_turn` / `_parse_judge` (host-side judge turn) — replaced by the `request_review` tool.
- The `FailureKind::Judge` re-author path (`judge_revise_suffix`) — the author now self-revises, so a
  judge rejection no longer re-enters `author_prompt`. `FailureKind` collapses back toward compile-only
  (keep it for build failures).
- The host-emitted `judge` verdict event — replaced by the review tool's own events (as Foundry's
  feedback UI does).

These were added for the current host-driven judge; adopting this proposal would remove or repurpose
them.

## 5. Risks / tradeoffs

- **More machinery in the *generic* host.** The `rustapp` author gains a buffer + review tool + gate,
  moving it closer to Foundry's bespoke author. Genericity is preserved (all driven by the wheel's
  `judge_prompt`), but the shared loop is heavier and less "single-shot".
- **Loop-ownership shift.** The author/judge micro-loop moves from the host into the agent; the
  `docs/rust-backend-api.md` "Python owns the loop" story now applies to author→compile→validate, with
  the author owning the inner review cycle. Compile/validate stay host-driven.
- **Gaming the judge.** The author decides *when* to review; the completion gate (must be accepted
  before `result`) is what prevents a perfunctory pass — same safeguard Foundry relies on.
- **Bigger, higher-risk change** than the phased plan's localized tweaks; needs its own e2e validation.

## 6. Rollout

1. Add the buffer + `request_review` + gate to `run_llm_agent` behind a `judge` parameter; wire the
   judge sub-agent from `module.judge_prompt`. Add the run memory tool to the author loop.
2. Gate on a descriptor flag (`judge_in_loop`) so it can be enabled per wheel and A/B'd against the
   current host-driven judge.
3. Remove `_judge_turn` from the host loop for in-loop wheels; keep the compile/validate host steps.
4. Validate on `test_crucible_e2e_gate`: **wall-clock + verdict parity** (12 GOOD / 1 BAD hold; the
   fee-oracle class still caught), iterating on a smaller scenario first (the gate is ~2 h / paid).

## 7. Recommendation

If we only want the cost down with minimal risk, the phased plan (cost doc) is the pragmatic path. If
we're willing to refactor the authoring loop, this proposal is the better long-term shape: it unifies
Crucible with CVL/Foundry, deletes the bespoke host-driven judge plumbing, and removes the cost at its
architectural source. A reasonable middle path is to ship phased **Phase 1** now (immediate relief)
and treat this proposal as the target the loop converges to.
