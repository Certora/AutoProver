# A fixture assertion condemns every component that does not own it

> **Status: proposed.** Found by the 2026-08-07 Crucible e2e run, which passed. Eight of one
> component's twelve properties were reported **BAD** — refuted by a counterexample that says nothing
> about them. Nothing in the run failed; the report is simply wrong.
>
> Not a regression. The mechanism predates component units (`crucible-component-units.md` §8.1) and
> was harmless while there was one unit. It became reachable the moment K ≥ 2.

## 1. What was observed

`tests/test_crucible_e2e_gate.py` on `solana_vault`, K = 2, 20 properties. The run was green.

| Component | Verdicts | Crash |
|---|---|---|
| Vault Initialization | 1 BAD, 7 GOOD | `crash_85b4286101583571` |
| Lamport Management | **8 BAD, 0 GOOD** | `crash_d67ddc48faf14617` |

Every one of Lamport Management's eight verdicts carries the *same* detail, naming the same
assertion:

```
[authority_must_sign_initialization] init without the authority signing must fail
```

`authority_must_sign_initialization` is a **Vault Initialization** property. It is not among Lamport
Management's twelve, and it appears nowhere in `c_lamport_management.rs`. The assertion that produced
it lives in the **shared fixture**, inside `action_init_without_signer`.

The fixture carried five such assertions, and their titles span both components:

| Fixture action | Assertion tag | Owned by |
|---|---|---|
| `action_init_without_signer` | `authority_must_sign_initialization` | Vault Initialization |
| `action_reinitialize` | `initialize_at_most_once_per_authority` | Vault Initialization |
| `action_deposit_overflow` | `deposit_overflow_prevented` | Lamport Management |
| `action_withdraw_excess` | `withdrawal_bounded_by_recorded_balance` | Lamport Management |
| `action_withdraw_unauthorized` | `withdrawal_authority_only` | Lamport Management |

## 2. Mechanism

Five facts compose, each correct on its own.

1. **The setup prompt asks for this.** `author_setup.j2`: *"Negative attempts are actions too… write
   an action that attempts X and asserts it failed — `fuzz_assert!(res.is_err(), "[<property
   title>] …")`."* The fixture did exactly what it was told.
2. **The fixture is in every component's build.** It is the crate root. Feature gating isolates
   *sections*; the fixture below them is unconditional, by design — it is the shared surface every
   section drives.
3. **Every campaign draws every action.** The fuzzer explores the whole action space regardless of
   which section's invariant is selected. This is deliberate and stated in the component prompt:
   *"the fuzzer drives the WHOLE program… the sequence interleaves every `action_*`."*
4. **So a tagged fixture assertion can trip in any component's campaign**, carrying a title that
   belongs to exactly one of them.
5. **Attribution is scoped to the target.** `attribute_finding` matches the finding's title against
   *this target's* checks, and when nothing matches marks them **all** BAD rather than hide a real
   counterexample:

   ```rust
   let refuted = |c: &Check| !c.property.is_empty() && d.contains(&c.property);
   let unattributable = !target.checks.iter().any(refuted);
   ```

The asymmetry is the defect. The **owner's** campaign attributes correctly — Vault Initialization got
1 BAD / 7 GOOD, exactly right. Every **non-owner's** campaign gets a title it has never heard of,
concludes "unattributable", and condemns its entire suite.

One fixture assertion, drawn once, falsely refutes up to K−1 whole suites.

## 3. Why it matters more than the failure it resembles

§17's failure was a delivered crate that did not compile: wrong, but *visible* the moment anyone
built it. This one produces a **false refutation** — the report claims the program violates
properties that were never tested against the counterexample. A green run, a clean crate, and eight
wrong rows.

The safety net that produced it is not itself wrong. "Never silently pass a real counterexample" is
the right default when a finding cannot be placed. The bug is that a foreign title is not the same
thing as an unplaceable one, and the wheel currently cannot tell them apart.

## 4. Options

### 4.1 Teach the wheel which titles the run owns — *recommended*

Give the validate callout every property title the run extracted, across all components. Attribution
becomes three-way instead of two-way:

| Finding's title | Verdicts for this target | Rationale |
|---|---|---|
| one of **my** checks | that check BAD, the rest GOOD | today's behaviour, unchanged |
| **another component's** | `Outcome::Unknown`, detail naming the owner | not mine to refute; the owner's own campaign reports it |
| **unknown to the run** | all BAD | today's safety net, unchanged |

**Why `Unknown` and not `Good`.** The campaign really did stop at the violation, so this component's
properties were not explored to budget. `Outcome::Unknown` already exists for exactly this — *"no
conclusive result"* — and `Good` would overclaim on a truncated run. The detail should say why, so
the report reads *"not determined: the campaign ended on Vault Initialization's
`authority_must_sign_initialization`"* rather than leaving a bare Unknown.

**The safety net survives intact.** A title nobody in the run owns still condemns everything. The
change only distinguishes *placeable elsewhere* from *unplaceable*.

**Where on the wire.** `AuthorInput` — run-level context, a peer of `program`, `source_unit` and
`prep_facts`, which the callouts already receive for the same reason. Confirmed to have no cache
impact: `_setup_identity` hashes an explicit field whitelist, and component cache keys come from
`_component_digest(feat)` plus the properties, never the wire payload.

`Target` was considered — it is the tighter scope, since only `validate` reads this — and rejected:
a target is *"one invocation of the checker and the checks it covers"*, and the run's other
components are neither.

**No new callout.** Unlike `crate_root`, the host already holds this: `RustStagedFormalizer.begin`
receives every job with its properties (that is how the fixture is authored from the union). The
union of titles is stashed on the formalizer beside `_setup_result` and threaded onto
`ComponentInput`.

### 4.2 Keep property assertions out of the shared fixture — the principled direction

Split the *attempt* from the *assertion*. The fixture action attempts X and **records** the outcome;
the owning component's invariant fn asserts on that record. Then a tagged assertion always sits in
the section of the component that owns the title, and the failure class is gone by construction.

The prompt's current reasoning — *"A rejected call changes nothing, so this is safe inside an action.
It cannot go in the test"* — is about **sending**, not asserting. The attempt must be in an action
because the test may not send instructions; the *assertion* has no such constraint once the outcome
is observable.

Costs: the fixture must expose per-attempt outcomes, the setup prompt grows a more subtle rule, and —
decisively — it depends on a model following it. A model can still write a tagged assertion in the
fixture, and then §4.1 is the only thing standing between that and eight false refutations.

### 4.3 Feature-gate the fixture's assertions — rejected

`#[cfg(feature = "c_x")]` around each fixture assertion is expressible (the fixture is authored after
components are known), but it puts component knowledge into the shared surface, and it *narrows*
checking: the assertion would then only run in its owner's campaign, when running it everywhere is
the point of a whole-program fuzzer.

### 4.4 Drop the safety net — rejected

Making unattributable findings non-fatal would fix the symptom by reintroducing the failure the
design explicitly refuses: silently passing a real counterexample.

## 5. Recommendation

**Land §4.1 now.** It is small, preserves the safety net, and does not depend on model behaviour.

**Treat §4.2 as the direction**, not a substitute. The two compose: §4.2 makes a fixture assertion
rare, §4.1 makes it harmless. Shipping only §4.2 would leave the report one disobedient model away
from the same eight wrong rows.

## 6. Change list

**SDK** (`rust/autoprover-sdk`)
- `AuthorInput` gains `run_properties: Vec<String>` — every property title the run extracted, across
  components; documented as run-level context alongside `prep_facts`.
- `wire_echo` + `test_wire_roundtrip` registration for the new field.

**Wheel** (`rust/crucible-app`)
- `attribute_finding` takes the run titles and classifies three ways (§4.1 table).
- The `Unknown` verdict carries a detail naming the owning component, so the report explains itself.

**Host** (`composer/rustapp`)
- `RustStagedFormalizer.begin` stashes the union of titles; `RustFormalizer` carries it onto
  `ComponentInput`.
- `wire.py` mirror.

**Tests**
- Rust: the three cases — owner attributes as today; foreign leaves this suite non-BAD; unknown still
  condemns everything.
- Python: wire roundtrip, and that the titles actually reach the validate payload.
- e2e: a rerun should show Lamport Management's eight properties no longer BAD. That is the only
  check that exercises the real path, since the defect needs a live model to author a fixture
  assertion in the first place.

## 7. Deliberately not covered

- **Whether the underlying finding is real.** `init_without_signer` tripping its own assertion means
  the rejected init *succeeded*, which is either a program bug or a fixture modelling error. Nothing
  here decides that. Note the `SUSPECT HARNESS BUG` label on that verdict was a false positive fired
  by a separate defect (fixed in `cc6aac1`) and is not evidence either way — after that fix the label
  means something again, so a rerun's triage is worth reading.
- **Cross-readable section files.** Since sections became files
  (`crucible-component-units.md` §17), one component's author can read another's — observed in the
  same run. Related in spirit (component isolation), separate in mechanism.
