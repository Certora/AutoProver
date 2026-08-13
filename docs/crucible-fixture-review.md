# Proposal — the shared fixture is the one artifact nothing reviews

**Status:** not implemented. Recorded after a klend run lost ~3h to a single unchecked call in the
fixture. The prompt-side mitigation shipped (`crucible: a panic on fuzzed input costs the run`); this
doc is about the structural gap that let it reach a run at all.

## 1. The gap

`Backend::judge` returns `None` for `Authored::Setup`, with the reason stated at the call site
(`app.rs`): *"The shared fixture is scaffolding, not test evidence — the compile/dry-run gate already
vets it, and there is no property to judge it against."*

Both halves of that are true, and together they still leave the fixture the **only** authored
artifact in the pipeline with no reviewer:

| artifact | compile gate | judge turn |
| --- | --- | --- |
| preflight skeleton | yes | n/a (ours, not authored) |
| **setup fixture** | **yes** | **none** |
| component section | yes | yes (`request_review`, in-loop) |

The asymmetry is backwards with respect to blast radius. A section is built into one campaign; the
fixture is built into **every** campaign, so a defect in it is multiplied by the component count,
while the thing that reviews defects is attached only to the artifact that isn't.

## 2. Why the compile gate is not the missing reviewer

The gate proves the crate *builds* and that the skeleton *runs once* (`--dry-run` executes `setup()`
plus a single iteration). It cannot see a defect that needs adversarial input to surface. The klend
failure is the worked example:

- `Pubkey::find_program_address` panics when no bump yields an off-curve address. Fixed seeds never
  hit it; seeds an `action_*` argument reaches do.
- The dry-run's single iteration didn't reach it. 704 corpus inputs did.
- The panic lands **outside the fuzz target**, so LibAFL records no crash and the process aborts
  (134). `crucible run` reports only its own exit 1.
- Every property in the campaign returns ERROR — the one whose action panicked and the twenty-six
  that never ran.

So the fixture shipped, compiled, dry-ran clean, and still cost the run.

## 3. Why it spreads, which is what makes it structural

Campaigns share one corpus (`--corpus-in ./corpus --corpus-out ./corpus`). The input that reaches the
panic is written there, so every later component loads it and dies the same way — including
components whose own section contains no such call. On klend, Oracle-Driven Refresh had zero
`find_program_address` calls and failed identically to the two that did.

The observable shape is a run that looks *flaky and worsening*: healthy components go bad as the
corpus grows, campaigns die progressively earlier, and nothing in the output names a cause. Two
components, then three. That reading cost most of the three hours.

## 4. Options, cheapest first

1. **A lint over the authored fixture, host-side.** Reject a submitted fixture whose `action_*`
   bodies contain a known-panicking call on fuzzed input — `find_program_address`, `unwrap()` on a
   derivation — the way `_review_gate` already blocks `result` until a draft is accepted. Catches
   this exact class in milliseconds, needs no LLM turn, and the rule is already written down in the
   cheat sheet so the author has been told before it fires. Narrow: it only catches what is listed.
2. **A judge turn for setup, with its own criteria.** Not the suite criteria — there is no property
   to judge against, which is the original objection and still correct. The fixture's criteria are
   different: can any `action_*` panic on fuzzed input; does every action have a reachable success
   path; are the negative actions recording rather than asserting. Costs an LLM turn on the single
   longest step of a run (klend: 43 min to author), so it wants the in-loop `request_review` shape
   rather than a separate pass.
3. **Make the panic visible instead of preventing it.** Have the harness catch a panic in an
   `action_*` and report it as a rejected action. Removes the run-level blast radius without
   reviewing anything — but it belongs in the crucible fuzzer, not here, and it silently converts a
   harness defect into a dead-end state, which C3 then reads as a reachability gap.
4. **Stop sharing the corpus across components.** Removes the spread but not the failure, and gives
   up the cross-pollination the shared corpus is there for.

(1) and (3) compose: the lint stops the known shapes at authoring time, the fuzzer-side catch bounds
the damage from the unknown ones. (2) is the general answer and the expensive one.

## 5. What this does not argue

That the original call was wrong. Judging scaffolding against test criteria would have produced
noise, and the compile gate does vet what it claims to. The claim is only that *"has a compile gate"*
was doing more work in that reasoning than it can carry — the gate is a build check, and the defect
class that matters here survives building by construction.

See also `docs/crucible-unexercised-checks.md` for the mirror-image gap on the verdict side: a clean
campaign cannot distinguish "held" from "never evaluated", and marks both GOOD.
