# Author-determined checks — a decision record

**Status: implemented.** The shipped contract lives in
[`docs/rust-applications.md` §6](rust-applications.md); this records *why* it is shaped that way,
including the three designs that were tried and rejected on the way. Read §6 first — this file is
only the reasoning that would otherwise be lost.

## What changed

`Backend::checks` — pure, pre-authoring, one `Check { property, name, target }` per property — is
gone. In its place:

- the author declares the property→checks mapping (`map_checks`), and its distinct names are what
  runs;
- `Check` is `{ name, properties, target }` — `properties` being the author's own claim, carried
  verbatim rather than guessed by the wheel — and the wheel answers only `target_for(input, check)`;
- `validate` carries a **verdict contract**: a check with no evidence it ran comes back `ERROR` or
  `UNKNOWN`, never `GOOD`;
- a stamping run records `ran` — the targets it covered, each with its checks — and the publish gate
  validates the mapping against that, in both directions.

## Why pre-declaration had to go

A backend whose decomposition is fixed by construction (Crucible: one fuzz campaign per component)
can name its checks from the properties alone. A CVL backend cannot: choosing how many rules express
a component's properties *is* the authoring task. The old seam could not express that, and could not
express a check covering several properties either — `Check.property` was a scalar, and
`live_checks` dropped a whole check when that one property was skipped.

## Why pre-declaration was not buying what it appeared to

Five invariants were at stake:

| | Invariant | Now enforced by |
| --- | --- | --- |
| I1 | Every non-skipped property is verified by ≥1 check | the publish gate |
| I2 | Every check that ran is tied back to a property | the publish gate |
| I3 | Every check the author claims is really there | **the verdict contract** |
| I4 | Every check a target covers gets exactly one verdict | `ValidateVerdicts.resolve` |
| I5 | What a check concluded is not a model claim | unchanged — the wheel answers |

`Check.properties` is worth distinguishing from the old `Check.property` it replaces. That one was a
*wheel guess* made before authoring, which could contradict the mapping the author later published,
and it was a scalar, so skipping one of a check's properties dropped the whole check. This one is the
author's declaration passed through: the host builds each check from the mapping, so there is nothing
to contradict. A first attempt at this design dropped the field entirely on the grounds that a run
should be property-blind; implementing Crucible showed why that is wrong — its assertions are tagged
with property titles, so its attribution has no way to place a finding without it.

Only I3 looked like it needed pre-declaration, and it never delivered it. A Crucible check name
(`c_fifo`) named *nothing*: not a function, not a symbol in the build, not a token in the campaign
output. The author writes one constant-named `fn invariants`; attribution matches crash text against
the property title. So a clean campaign stamped `GOOD` on a pre-declared row whether or not the
author had written an assertion for that property — or had written one the fuzzer can never reach.
The pre-declared set proved nothing about the artifact, which is why moving the check set to the
author costs nothing here and the verdict contract is a strict gain.

## Three rejected designs

**1. A `CheckPlan::Fixed | ::Discovered` split.** Modelling "some backends can pre-declare" as a
variant. Rejected: it is a special case shaped around one wheel, and `Fixed` mis-states what it
means — the real distinction was never "the wheel knows the names" but "the checker has no
enumeration surface". It also let a wheel promise cardinality it had no way to honour.

**2. An `enumerate` callout** — ask the artifact what it checks, before running it. Rejected on the
concrete question of what Crucible would implement. The available surface is the `[<title>]` tags in
assertion messages, and scanning source text for them is fooled both ways: a tag in a comment, a log
line, an uncalled helper, an unreachable branch, or a `fuzz_assert!(true, …)` all look like checks,
while `format!("[{}] …", title)` is a check that does not. The property that matters — *can this
assertion fail?* — is not syntactic; Crucible's own prompt already draws a line no parser can see,
between an action that **records** an outcome and one that **asserts** on it. The trustworthy
surface is dynamic, so it does not exist before the run, so a pre-run callout was the wrong shape.
Folding the same guarantee into `validate` needs no new seam surface at all.

**3. Judge-only.** The judge *is* a hard gate (`required.append(FEEDBACK_KEY)`, stamped only on
`good`), so the reliability bar is real. But it never sees the mapping —
`judge_instruction(input, spec)` gets the spec; the feedback tool passes `spec`, `skipped`,
`rebuttals` — so this would mean feeding it the mapping and asking an LLM to do exact name-matching
that a verdict does exactly. And a verdict is a *report row and a blocked gate*; a judge rejection
is a conversation. The judge keeps the residue no mechanism reaches: whether a check that
demonstrably ran genuinely verifies what it claims.

## What this costs

**Undeclared work is invisible.** With an author-declared set, a check written, seen to fail, and
quietly left out of the mapping simply never runs — where an enumerated set would have forced it to
be claimed (I2's teeth). This is a selection problem rather than a fabrication one, and the judge is
weak at it: it sees the spec but not the verdicts. A wheel whose checker reports results the target
never asked for could surface them; whether that becomes a channel on `ValidateOutcome` or an
`ERROR` is unresolved.

## What the first real run taught us

The 2026-08-11 IDL-path vault gate passed, and both component authors declared **one** check covering
their whole property set — `invariants` for one, `c_deposits_withdrawals` for the other. Attribution
worked (the tags placed the findings), but a check claiming ten properties is refuted by a finding
naming any one of them, so two genuine bugs marked all ten properties `BAD`.

The wording was the cause: "declare which harness function verifies which property" has exactly one
honest answer when a component has exactly one harness function. The deeper point is that a
backend's *unit of evidence* decides its check granularity — Crucible's is one tagged assertion, so
one-per-property is the only shape it can attribute, and it now says so in `validate` (before the
campaign, since a wrongly-shaped declaration is unattributable however long it runs) rather than
reporting a verdict it cannot stand behind. The seam still permits many-to-one, which is right: a CVL
rule genuinely can discharge three invariants because the Prover reports per rule.

A second finding came out of the same run's report: 15 checks rendered as **14 rule rows**. The
report identifies a rule by `(file, name)` — deliberately, so one definition seen through several
runs collapses into a single row — and Crucible's deliverable is one crate, so every component's
checks shared its file name. Two components whose authors both wrote `c_vault_authority_immutable`
collapsed into one row, keeping only the first verdict. Pre-existing, but far more reachable now
that names come from authors given the same property title rather than from per-batch slugs. Fixed
where the truth lives: a verdict now names the section file its assertion was written into
(`<feature>.rs`, one per component), which is what the wire's `Verdict.unit_file` was always for.

## Follow-ups

- **Crucible's corroboration is the open piece.** Its campaign reports crashes, not a list of what
  ran, so today it cannot distinguish "held" from "never exercised". The fix is in the crucible repo:
  `fuzz_assert!` and its siblings record each *evaluation*, not only each violation, and the campaign
  reports the tally. Until then the interim signal is the LCOV it already collects via `--coverage`
  (which needs a tag→line map, so it inherits some of the parse problem).
- **`expect_check_failure` accepts any non-`GOOD` verdict.** It exists for "the failure is the
  finding" — a real counterexample. Letting it waive an `UNKNOWN` lets "we never tested this" be
  marked away with a sentence. Restricting the mark to `BAD` is probably right.
- **The mapping is stored unvalidated.** `map_checks` records what it is given; every rule is
  checked at publish. A typo in a property title therefore surfaces one round trip later than it
  could. Deliberate — one place owns the mapping rules — but worth revisiting if authors trip on it.
