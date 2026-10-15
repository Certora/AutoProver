# Specifying the design, not the code: a plan

Certora's Solana reviewers read the spec AutoProver generated for the stake program and made this
point, among others (`docs/stake-benchmark.md`, "Expert review"):

> A good spec is one that holds for an entire class of implementations. … Solvency is a good spec
> because all protocols must remain solvent. But rules that check so and so fields are set
> correctly, or the program reverts under some condition are not very interesting. … Solvency is
> attempted, but only partially. Properties about math of staking and de-staking was skipped.

This plan addresses that. It changes how properties are chosen, not how rules are written. Points
1 and 3 of the same review are done (`8d99f006`, `d129bacf`, `57e33e67`, `91f4261d`).

## What the run shows

The third stake run delivered 69 properties: 50 safety properties, 10 invariants and 9 attack
vectors. Of the 50 it skipped, 35 were safety properties.

**Solvency appeared four times, and never whole.** The stake program's solvency condition is
that every stake account keeps `lamports >= rent_exempt_reserve + delegation.stake` after every
instruction. Four components each stated their own slice of it:

| component | property |
| --- | --- |
| Account Creation | `initialized_meta_reserve_is_canonical_and_covered_by_balance` |
| Delegation | `recorded_delegation_never_exceeds_account_lamports` |
| Split & Merge | `stake_account_solvency_after_split_and_merge` |
| Withdrawal | `withdraw_preserves_active_stake_plus_rent_reserve` |

`MoveStake` and `MoveLamports` are the two instructions most likely to break it. Their component,
Stake & Lamport Movement, delivered nothing, so no rule covers them. Lamport conservation was
stated once, in Withdrawal, and skipped.

**The staking math was never attempted as a bound.** It was skipped because the stake-interface
crate was external, which is now fixed. A probe since showed that "effective stake never exceeds
the delegation" verifies over an unconstrained schedule.

## Why it happens

### Why EVM has not needed this

The EVM backend uses the same per-component pipeline and has not hit this problem. That is because
of how CVL checks properties, not because EVM picks better components.

- **CVL checks across the whole contract; CVLR does not.** On EVM the component limits what one
  agent writes, not what the Prover checks. An `invariant` is proved inductively against every
  external function of the contract, and a parametric rule (`method f`) ranges over all of them.
  A solvency invariant written in a "Token" component is therefore checked against the admin
  functions in another component as well. CVLR has no invariant construct: a rule calls the
  handlers it names. On Solana, the component limits what is checked too.
- **The Solana extraction prompt forbids cross-component properties.** EVM's does not.
  `solana/component_context.j2` lists the other components and says: "They are context, not your
  subject — do not write properties about them." It also gives account detail only for the
  component's own instructions. The text came in with the ecosystem abstraction (#96).
- **The state model works against component boundaries.** A contract's storage tends to divide by
  component (balances, admin config, fee parameters), so an invariant usually lives inside one
  component's state. In the stake program every instruction reads and writes the same
  `StakeStateV2`. The analysis prompt says to group instructions by "the account state they
  share", which taken literally makes the whole program one component. The analyzer could not
  follow that rule, so it grouped by operation instead, with no warning.

The first point is reasoned from the Prover's semantics and the prompts, not measured. No EVM run
is available locally to confirm that EVM specs do contain contract-wide invariants. Checking one
real EVM run's spec before step 3 starts would settle it.

### The causes, in order of weight

1. **A property is checked only against its own component's handlers.** `_solana_units` makes one
   unit per component of the main program, and `_extract_all` runs one extraction agent per unit.
   With no CVLR counterpart of a CVL invariant, a property over every instruction has no unit to
   belong to. When every instruction shares one state type, no way of dividing the instructions
   puts solvency inside a single component, so better boundaries alone do not fix this.
2. **The prompt forbids the property even where it could be stated.** This is the sibling
   prohibition above.
3. **The backend guidance ranks implementation checks first.** `SOLANA_CVLR_GUIDANCE` calls
   account validation "the highest-value properties on this chain". It excludes "rent economics",
   which keeps the rent-exempt reserve, half of the solvency condition, off the list. The
   extraction prompt's examples for its third category are all missing-check patterns.
4. **Nothing tests a property against the design.** The extraction prompt says "derive invariants
   and safety properties from first principles", but no step asks whether a property would hold
   for any correct implementation of this kind of protocol. That question is the reviewers'
   criterion.

## The plan

The order is set by one open question: once the extraction prompts stop forbidding it, do the
component extractors state protocol-wide properties by themselves? The third run suggests they
might, since four components each extracted part of solvency. If they do, a grouping step is
enough. If not, a dedicated extraction pass is needed. An extraction-only experiment answers the
question for a fraction of the cost of building the pass first.

1. Change the prompts. No structural change.
2. Run an extraction-only experiment, and decide.
3. Hoist the protocol-wide properties into one shared unit, so each is formalized once.
4. Show the extractor where the protocol's arithmetic lives, so bounds on it are extracted.
5. Formalize the shared unit, and measure on a full run.

Step 4 was originally a dedicated protocol-property pass. Step 2's result replaced it; see the
decision under step 2.

### 1. Change the prompts

**Lift the sibling prohibition.** In `solana/component_context.j2`, replace "do not write
properties about them" with: a property about the state this component maintains may range over
every instruction that writes that state, and should name those instructions. Give the other
components' instructions by name, so the extractor can name them in a property's scope.

**Fix the grouping rule in the analysis prompt.** In `solana/analysis_prompt.j2`, say what to do
when every instruction shares one state type: group by the operation each instruction performs,
and say in the component description that the state is shared. Today the rule fails silently on a
common Solana shape.

**Rewrite the extraction guidance** in `composer/spec/cvlr/guidance.py` and
`composer/templates/solana/property_prompt.j2`:

- **Lead with the design test.** Before a property is kept, ask: would it hold for every correct
  implementation of a protocol of this application type? Solvency, conservation, monotonicity of
  value per share, no dilution, and authority separation pass. "This field is set to that value"
  usually does not.
- **Re-rank account validation.** It is still worth extracting. It is not the highest-value class,
  and a missing-check property should name the consequence it guards against, such as funds
  leaving or authority changing hands, not the check.
- **Narrow the rent exclusion** to the economics of rent: fee levels, rent collection. The
  rent-exempt reserve is a solvency term and stays in.
- **Replace the attack-vector examples** with ones phrased as broken design properties, for
  example "a sequence of splits and merges creates lamports", rather than missing checks.
- **A skip that cites a Prover limitation must cite a measurement.** The run skipped the staking
  math on a belief. The author already measures most of the time; the guidance should require
  it.

None of this is safe to formalize on its own: with the prohibition lifted, several components
will restate the same property. Step 3 has to land before a full run.

**Status: done, uncommitted (2026-10-12).** The skip rule went into the CVLR author's prompt, where
`record_skip` is taught, rather than the extraction guidance. Every new example is drawn from
vaults, pools and lending, never staking: the step 2 experiment measures whether extraction finds
the stake program's solvency condition, and an example that states it would leak the answer.

### 2. Experiment: what does extraction produce now?

Rerun extraction only on stake, from the cached analysis, with no formalization and no Prover
time. Analysis and extraction together cost about $30 on the third run, so this costs less.

Rerun the analysis too, in a second arm, so the grouping-rule change is measured. That costs a
little more and can change the components, so keep it separate from the first arm.

Count, against the third run's extraction:

- **Rubric coverage.** For each rubric item (below, under step 5), whether it is stated whole
  across every instruction it concerns, stated in parts, or absent.
- **Duplication.** How many components state each protocol-wide property, and with what
  instruction scopes.
- **Design versus implementation share,** by the design test from step 1.

**Decision rule, fixed before the run.** If every rubric item is stated whole by at least one
component, step 4 is not needed and the shared unit's properties come from hoisting alone. If any
rubric item is missing or only in parts, build step 4. In both cases, keep the step 1 changes.

**Result (2026-10-12).** Both arms ran on a clean worktree of the same snapshot, with the third
run's design-doc discovery seeded so all three runs had the same inputs. Arm 1 replayed the third
run's analysis from cache; arm 2 redid it. Each run stopped once the pin fixture was written, so no
formalization ran. Spend was about $82, at the log estimate plus 15%. Scoring used one fixed
instruction file for all three property sets, written before any output was read, with the same
model scoring each set separately. The verdicts below were spot-checked by hand.

| | third run | arm 1 (old analysis) | arm 2 (new analysis) |
| --- | --- | --- | --- |
| properties | 142 | 145 | 132 |
| invariants | 16 | 39 | 33 |
| cross-component properties | 2 | 53 | 44 |
| labelled design-level | 80% | 86% | 86% |
| R1 lamport conservation | parts | **whole** | **whole** |
| R2 stake-account solvency | parts | **whole** | **whole** |
| R3 staker/withdrawer separation | parts | **whole** | parts |
| R4 lockup on withdraw | whole | whole | whole |
| R5 stake-math bounds | absent | absent | absent |

- **Lifting the prohibition worked.** Conservation and solvency each went from slices to whole
  properties over every instruction. Arm 1's #93, for example, states solvency over all sixteen
  handlers that write a stake account.
- **The duplication is as predicted.** In arm 1, eleven properties state some form of solvency and
  seven state conservation, spread across components. Formalizing them as they stand would pay
  for the hardest property several times over, so step 3 is required before a full run.
- **The stake math is still absent in every arm.** Nobody extracted a bound like "effective stake
  never exceeds the delegation", although a probe showed it verifies. The math lives in the
  interface crate rather than the program, which may be why component extractors pass over it.
- **The grouping change made little difference here.** Arm 2 regrouped delegation with
  deactivation and split authority from lockup, and its R3 fell back to parts. With one sample per
  arm that is within noise.
- **The design share barely moves.** The classifier counts "a mechanic plus the guarantee it
  protects" as design, so it rated the third run 80% design-level although the reviewers read it
  as mostly code-describing. The metric is too lenient to show this change. The rubric is the
  signal that moved. A stricter classifier, or the reviewers' own read, is needed for that metric.

**Decision.** The rule fixed above says to build the dedicated pass, because R5 is absent in both
arms. That rule was wrong, and the pass is dropped. Its case was that properties spanning
components need a home, and R1 to R3 are now extracted whole without it; deduplicating them is
step 3's job. R5's absence has a different cause. The stake-math bound is a bound on a
computation (`Delegation::stake`, in the stake-interface crate), not on state some instructions
write. Nothing in the component context shows the extractor where the program's arithmetic lives,
and an application-type pass that also sees only instructions would not either. Even CVL-style
invariants would not have helped: they change what is checked, not what is extracted. Step 4 is
now the change aimed at that cause.

### 3. Hoist protocol-wide properties into one shared unit

A new step after extraction, before formalization. It finds properties whose scope goes beyond
their own component, groups the equivalent ones across components, and moves each group into one
shared unit as a single property. Components keep what is specific to them.

- **Matching** is a model call: properties are worded differently by different agents, and
  "solvency after split and merge" is a weaker version of "solvency after every instruction",
  not a different property. Keep the strongest wording and the widest scope, and record which
  components' properties it absorbed, so the report can say so.
- **Where it lives.** It's a step in the shared driver between `_extract_all` and formalization,
  as an optional `Ecosystem` hook, implemented for Solana first. EVM does not need it while CVL
  invariants range over the whole contract; see "Why EVM has not needed this". Soroban's CVLR
  rules have no invariant construct either, so Soroban will need it. Crucible shares the Solana
  ecosystem. A whole-program property suits a fuzzer that runs every action anyway, but Crucible
  should opt in rather than inherit it silently.
- **Caching.** The hoist's output is what formalization keys on, so it needs its own cache layer,
  like the prioritization step has.

**Status: built, uncommitted (2026-10-12).** `composer/spec/hoist.py` holds the plan model,
its validation and the call; `_hoist` in `composer/pipeline/core.py` applies it after the pin is
written, so a pinned replay hoists from the raw extraction. The opt-in is a `shared_unit`
property on the backend: `None` everywhere except CVLR, so Crucible and EVM are unchanged. The
Solana unit is `protocol_unit`, a "Protocol invariants" component spanning every instruction,
appended to a copy of the analyzed model. Two choices beyond the design above:

- **The model never writes a property.** A group's shared property is one of its members, copied
  verbatim; validation checks that it names an entry point outside its own component.
- **A plan that does not validate is skipped, not fatal.** The run formalizes the units unhoisted
  and pays for the duplicates.

On arm 1's 145 properties the plan validated on its first attempt: 15 groups absorbing 64
properties, leaving 96 to formalize (15 shared, 81 in their components). Solvency's representative
stands for 8 properties and conservation's for 7.

Open:

- **The report does not yet say what was absorbed.** Resolved by schema 3.4; see the follow-up
  below.
- **The shared unit is large.** Fifteen properties over eighteen handlers. Step 5 measures whether
  it fits a unit's budget.

### 4. Show the extractor where the arithmetic lives

The analysis records, per component, the functions where the protocol's arithmetic happens,
including functions in dependencies the program relies on (`Delegation::stake`,
`Rent::minimum_balance`). The component context shows them, and the extraction prompt asks for
bounds on their results as the program uses them, such as "withdraw never releases lamports the
effective stake still requires".

EVM has a coarser version of this: a Solidity `library` is modelled as a contract of its own and
becomes an extraction subject. Internal functions and dependency math are not surfaced there
either. Build it Solana-first, shaped so it could move into the shared model.

**Status: built, uncommitted (2026-10-12).** `Computation` (function path, crate, what it
computes, the instructions that use it) is an optional field on `ProgramComponent`, so every cached
analysis still validates; `_solana_validate` checks that `used_by` names declared instructions.
The shared unit carries every component's computations, once each.

The risk this adds is properties that restate what the extractor believes about a computation:
a formula or a constant holds for this implementation only, invites a rule that mirrors the
arithmetic, and is wrong whenever the belief is. Arm 1 already has 42 properties saying
"exactly", up from 31. Four mitigations are built in:

- The extraction prompt asks for bounds, conservation and direction of change, never the formula,
  and forbids the implementation's constants unless the design fixes them.
- The analysis prompt tells the analyzer not to describe a computation by its formula, so the
  extractor is not handed one to restate.
- A property resting on a computation must cite its source line in the extractor's reasoning.
- Scoring v2 (`design-exp-scores/scoring-v2.md`) adds a count of formula-restating properties.

**Measurement.** Arm 1 replayed an analysis that has no computations, so this needs an
arm-2-style run: fresh analysis and extraction, about $40. Score it, and rescore the third run and
both arms, with v2. The items to watch are R5 and the formula count.

**Result (2026-10-12).** Arm 3 ran on a fresh worktree with the third run's design-doc discovery
seeded and a fresh analysis, and stopped at the pin: about $40 at the log estimate plus 15%. All
four property sets were scored with v2 by the same method as before.

| | third run | arm 1 | arm 2 | arm 3 (computations) |
| --- | --- | --- | --- | --- |
| properties | 142 | 145 | 132 | 133 |
| cross-component | 2 | 53 | 44 | 52 |
| formula-restating | 23 | 13 | 16 | **7** |
| R1 lamport conservation | parts | whole | whole | parts |
| R2 stake-account solvency | parts | whole | whole | whole |
| R3 staker/withdrawer separation | parts | whole | parts | parts |
| R4 lockup on withdraw | whole | whole | whole | whole |
| R5 stake-math bounds | absent | absent* | absent | **whole** |

\* v2 labels arm 1's R5 "parts", but its own note says no property bounds the math; read by hand,
it is absent.

- **The computations change produced the stake-math properties.** The analysis listed
  `Delegation::stake` for withdraw and `stake_activating_and_deactivating` for delegation and
  splitting, described without formulas or constants. Arm 3's #58 states that effective plus
  activating stake never exceeds the delegation, framed by how withdraw, split and merge use the
  result. #59 states that the schedule is monotone over epochs. Neither restates the formula.
- **The formula count fell rather than rose.** 7 against 13 to 23 elsewhere. The mitigations held.
- **R1 regressed in arm 3, and that is variance, not this change.** Arm 3 states conservation per
  instruction group (#64, #89, #101, with #7 and #43 for the instructions that move nothing), where
  arms 1 and 2 each had one whole property. R3 also moves between arms. With one sample per arm,
  whether a whole protocol property appears is not reliable.
- **The hoist cannot repair that.** It only groups and chooses; it never writes. When no member
  states the whole claim, the slices stay slices, each formalized in its own unit.
- **The design-share metric is noise.** It ranges from 78% to 99% across arms and does not track
  the rubric. Drop it.

**Follow-up: the hoist may synthesize (2026-10-13).** To repair arm 3's sliced conservation, a
group's shared property is now either `Chosen` (a member, verbatim) or `Synthesized` (one property
written by the hoist, exactly the conjunction of its members). `Hoisted.synthesized` records which.
Validation holds a synthesized property to its members: it may not name an entry point no member
names, must name at least one of every member's, and must span more than one component. The rule
first required exactly the union of the entry points members name, but members also name
instructions they exclude ("every handler except `delegate_stake`"), and that rejected sound
syntheses. On the last of three attempts, a plan that still does not validate keeps its valid,
non-overlapping groups (`salvage`) instead of being discarded.

Rerun on the fixtures alone (one model call each, a dollar or two):

- **Arm 3** validated on its second attempt: 18 shared properties, 7 of them synthesized,
  absorbing 80. Conservation came out whole, as `lamport_moving_handlers_conserve_lamports_exactly`,
  the conjunction of its four slices (#7, #64, #89, #101) with nothing added. Read by hand, that
  makes R1 whole after hoisting.
- **Arm 1** validated on its first attempt with nothing synthesized: 15 shared properties absorbing
  72, every one a member chosen verbatim. An earlier run of the same call failed three times on one
  group, which is what `salvage` is for.

**The report now says what was hoisted (schema 3.4).** `AutoProverReport.hoisted` lists one
`HoistedClaim` per shared property: the property as formalized, its origin (`chosen`, naming the
component property it came from, or `synthesized`), and every component property it stands for,
under the component names the rest of the report uses. The HTML renders them in a
"Protocol-wide properties" section, with each synthesized property marked "written when hoisted"
above the properties it was written from, so a reviewer can check one against the other.

### 5. Formalize the shared unit, and measure

The shared unit is one more formalization unit beside the components, with a synthetic component
that spans every instruction.

- **Rule shape:** one specification checked against every handler, through `cvlr_rules!` (one
  rule and one verdict per handler). The author already knows the macro
  (`cvlr_baseline_facts.md`); the unit's prompt should make it the default here.
- **Munges:** the unit needs whatever the handlers need to be reachable, such as
  `extract_function` for private handlers and stand-ins for CPIs. Munges are feature-gated per
  unit and `tree.replay` handles two units extracting the same function, so this works today.
  It does mean the unit re-establishes munges its siblings also make, which costs time.
- **Scheduling:** this unit is dispatched first. U21 showed that units queued late under budget
  pressure start, get told to wrap up immediately, and author nothing. The properties the
  reviewers care most about should not be the ones at risk.
- **Size:** a unit that drives 18 handlers through several invariants is large. Start with one
  unit and measure. If it runs out of budget, split it by property, not by handler: splitting by
  handler recreates the problem.

**Runs.** A full run on stake, then the vault as a regression check against its hand-written
spec. The baseline is the third stake run.

**Metrics, decided before the runs:**

- **The rubric written before the first run** (`stake-benchmark.md`, "Grading it"): lamport
  conservation across Split, Merge, MoveStake and MoveLamports; `delegation.stake +
  rent_exempt_reserve <= lamports`; staker and withdrawer authority separation; lockup on
  Withdraw. Add the reviewers' stake-math bounds. Score each item as verified whole, verified in
  parts, or absent.
- **Design versus implementation share,** with a fixed classifier prompt and a human spot-check.
  The target is not 100%: some implementation properties are worth having.
- **Delivery:** whether the shared unit publishes, and its per-handler verdicts.
- **The reviewers.** Send them the new spec. Theirs is the judgment this plan is trying to
  satisfy.

## Open questions

1. **Does the synthetic unit belong in the analyzed model or only in the unit list?** Appending a
   component to the model makes it visible to every consumer, including the report and Crucible.
   Keeping it out means the report needs another way to name the unit. The first is simpler, and
   the report would show "Protocol invariants" as a component.
2. **What about applications with several programs?** The stake benchmark has one. Hoisting
   and the dedicated pass are both per program here. Leave multi-program for later, and note it.
3. **Who classifies properties for the metric?** An LLM classifier is cheap and repeatable but
   shares the extractor's blind spots. A human pass over 100-odd properties takes about an hour.
   I'd use both on the first comparison.

## Not in this plan

- A per-protocol-class library of properties (staking, AMM, lending). It would raise the score on
  known shapes and hide whether the prompts generalize, which is the question the stake benchmark
  exists to answer.
- Changes to the judge. The judge checks that a rule establishes its property; whether the
  property was worth stating is extraction's job. If step 5 shows implementation-level rules
  still dominate, revisit.
