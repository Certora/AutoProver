# Two spec writes in one turn kill the authoring session

> **Status: proposed, not implemented.** Found by the 2026-08-07/08 klend run and reproduced on its
> resume. Each occurrence costs a whole component — no spec, no verdicts, a `compile_error!` section
> in the delivered crate.
>
> Not a regression. `curr_spec` has been a single-write channel since the shared authoring state was
> introduced; it needs the model to batch two spec-writing tool calls in one turn to fire.

## 1. What was observed

Three components died on the same exception, across two runs of the same cache namespace
(`klend-restructure-0807`), 15 components each:

| Run | Component | Properties lost |
|---|---|---|
| first | Market & Global Governance | 25 |
| resume | Borrowing | 28 |
| resume | Obligation Orders (Conditional Deleverage) | 21 |

```
InvalidUpdateError: At key 'curr_spec': Can receive only one value per step.
Use an Annotated key to handle multiple values.
```

Nondeterministic, and it swaps which component it takes: Market & Global Governance died in the first
run and succeeded on retry; Borrowing did the reverse. The resume lost 49 of 411 properties to it —
and that resume was re-running only the 10 components the first run had already failed to deliver, so
the exposure is per session attempted, not per run.

The failure is terminal for the component. `formalize` raises, the driver records it, and `finalize`
puts an honest `compile_error!` behind the feature (`Section::gave_up`) — so nothing is silently
reported as verified, but the properties are simply absent.

## 2. Mechanism

Three facts compose.

1. **Every spec write replaces the whole buffer.** [`apply_spec_update`](../composer/authoring/buffer.py)
   returns a `Command` setting `curr_spec` to the complete new text — for a put *and* for an edit,
   since `edit_spec` renders `replace_unique`'s result and writes that.
2. **Tool calls in one assistant turn are one graph step.** The model may emit several; LangGraph
   dispatches them together, and each returns its own state update.
3. **`curr_spec` has no reducer.** [`composer/authoring/state.py`](../composer/authoring/state.py):

   ```python
   class AuthoringExtra(TypedDict):
       curr_spec: str | None                                    # ← plain LastValue
       skipped: Annotated[list[SkippedProperty], merge_skips]
       validations: Annotated[dict[str, str], merge_validation]
   ```

   Its two siblings have reducers precisely because they take several writes per step. The buffer
   never got one, so `LastValue.update` raises on the second write and the exception propagates out
   of `graph.astream`.

Worth noting: every backend that extends `AuthoringExtra` adds its own
`expected_failures: Annotated[dict[str, str], merge_expected_failures]`. Each author hit the
multi-write problem for the field they were adding and gave it a rule. Nobody went back to the
shared buffer they all inherit.

## 3. Why the obvious fix is wrong

A reducer that takes the last write would stop the crash and corrupt the draft.

`edit_spec` computes its replacement from the `curr_spec` it read at the *start* of the step. Two
edits in one turn therefore both build on the same base text, and neither contains the other's
change. Keeping the last one silently discards the first — the model believes both edits landed, and
the lost one is invisible until the compile fails for reasons nobody can trace to a dropped edit.

That trades a loud, attributable failure for a quiet wrong answer. The same objection rules out
first-write-wins and any merge that is not a real three-way diff.

## 4. The fix

**Reject every spec write after the first in a turn, at the write path, with a message the model
reads.** The first write applies; the rest come back as ordinary tool results saying to re-issue them
one at a time. Cost is one extra turn; no edit is ever lost.

This reuses machinery that already exists. `apply_spec_update` is documented as the one place a write
can be refused, and already signals refusal by returning `str` (buffer untouched, agent sees the
reason) rather than `Command`:

```python
if validator is not None and (err := validator(text)) is not None:
    return err
```

The batch guard is a second such refusal, ahead of the validator.

### 4.1 What it needs

To know it is not the first writer in the batch, the guard needs the current turn's tool calls and
the set of tool names that write the buffer:

- **The batch** — `state["messages"][-1].tool_calls`, in call order. The shared `edit_spec_tool`
  already takes `Annotated[ty, InjectedState]`. The put tools do **not**: `PutSpec`
  ([rustapp/session.py](../composer/rustapp/session.py)) and Foundry's put tool
  ([foundry/author.py](../composer/foundry/author.py)) carry `WithInjectedId` only, so they gain
  `WithInjectedState`.
- **The writer names** — the tools are built per session with backend-chosen names (`put_spec`,
  `edit_spec`, `put_test_raw`, …), so the set has to be supplied rather than inferred. Passing it to
  each factory is explicit and typed; a module-level registry would be less code and more shared
  mutable state.

Given both, the rule is: among the batch's calls whose name is a writer, if this `tool_call_id` is
not the first, return the refusal.

### 4.2 Where it goes

In [`composer/authoring/buffer.py`](../composer/authoring/buffer.py), not in any backend. Every write
in the repo funnels through `apply_spec_update`:

| Caller | Tool |
|---|---|
| `composer/authoring/buffer.py` | `edit_spec_tool` (shared; used by Crucible and CVL) |
| `composer/rustapp/session.py` | `PutSpec` |
| `composer/foundry/author.py` | Foundry's put |
| `composer/cvl/tools.py` | CVL's put |

## 5. Scope

All three backends inherit the defect — none redeclares `curr_spec`:

| Backend | State class |
|---|---|
| Foundry | `FoundryGenerationExtra` (`composer/foundry/state.py`) |
| Crucible / Rust | `RustSpecExtra` (`composer/rustapp/session.py`) |
| CVL | `CVLGenerationExtra` (`composer/spec/cvl_generation.py`) |

Only Crucible has been *observed* failing this way. That is an inference about exposure, not
evidence: the trigger is a batched pair of spec edits, and Crucible's sessions are the long ones —
20–40 properties through a compile-and-revise loop. Whether Foundry has been hitting it at a lower
rate and having it read as a generic component failure has not been checked.

Fixing it at `apply_spec_update` fixes all three at once.

## 6. Test

The failure needs no LLM to reproduce: drive an authoring graph with a synthetic assistant message
carrying two `edit_spec` calls and assert the session survives, the first edit is in the buffer, and
the second call's result tells the model to re-issue. A companion test should assert the single-write
path is untouched — one write per turn must not pay for the guard.
