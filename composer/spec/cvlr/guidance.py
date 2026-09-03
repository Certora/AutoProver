"""What the property extractor is told about the backend that will check its output.

``backend_guidance`` is inlined verbatim into the property-analysis *system* prompt
(:mod:`composer.spec.prop_inference`), so it is fixed for a whole run and read before a single
property is written. Its job is not to teach CVLR — it is to keep the extractor from producing a
list whose good entries cannot be checked and whose checkable entries are not worth checking.

The EVM text (``CERTORA_BACKEND_GUIDANCE``) is almost entirely a list of exclusions, and copying its
shape here would be wrong twice over. One of its exclusions **inverts** on Solana: an EVM property that spans
many functions is expensive, while ``cvlr_rules!`` fans one property across a whole grid of handlers
for the price of one more line.

Arithmetic is a subtler case and an earlier version of this text got it wrong. A Solana release build
may not check arithmetic, so a *wrapping* result is a real state bug that a rule catches — but
"cannot be made to panic" is not checkable at all, because a panic reverts and so cannot violate an
invariant, and because the flag that would report panics as violations is per-conf where a conf covers
a whole component. The exclusion list below says so; runs were extracting panic-freedom properties
that could only ever be skipped. So the text below spends most of its words on *shape* —
what a property has to look like for a rule to exist — and reserves exclusion for the handful of
things that genuinely have no rule.

Everything asserted here is traceable: the Methodology chapter of the published Solana manual
(items 4, 6, 7, 8, 10, 15). Where the
manual and the surveyed projects disagree, the manual wins — see ``docs/cvlr-capture-plan.md`` §4.5
for why recurrence alone is the wrong axis.

**Soroban's counterpart is deliberately absent.** ``docs/cvlr-backend-plan.md`` §4.4 says not to
write Soroban's pieces until Solana verifies a real property, and a second constant written now
would be a guess dressed as a deliverable. The chain-neutral half of this text is not factored out
for the same reason: two implementations decide where the seam goes, and there is one.
"""

SOLANA_CVLR_GUIDANCE: str = """\
These properties will be checked by the Certora Solana Prover. It symbolically executes the
program's Rust, so a property becomes a **rule**: a function that builds an arbitrary starting
state, calls one handler, and asserts something about the result. Extract properties that fit that
shape — and prefer the ones that fit it best, because a property no rule can express is a property
nobody will check.

What makes a property checkable here:

1. **It names one instruction handler**, not the program's dispatcher. Verification starts at a
   concrete handler (`deposit`, `withdraw`, an Anchor instruction), never at `process_instruction`
   — starting there is the single most reliable way to produce a rule that times out.
2. **It has a before and an after.** The most useful shape by far is "before the handler ran, X held;
   after it ran, Y holds." State the precondition as well as the conclusion; a property whose
   precondition is left implicit becomes a rule with an unjustified assumption in it.
3. **It is about values the handler can read.** State lives in accounts the caller passes in, so a
   property is stated over *the accounts this handler is given* — their fields, their owners, their
   signers, their derivation. A claim quantified over every account of some type that exists on
   chain has no rule; re-state it as an invariant each handler preserves.
4. **A property that applies to many handlers is cheaper, not dearer.** One property checked across
   a grid of handlers is a single specification instantiated per handler. So "every handler that
   moves funds preserves solvency" is a *better* extraction than five separate per-handler
   restatements — say it once, and name the handlers it ranges over.

Property classes that are especially well served, and worth looking for specifically:

- **Account validation.** Missing signer, owner, or authority checks; an account substituted for
  another of the same shape; a PDA whose seeds or bump go unverified; duplicate mutable accounts;
  reinitialization. These are the highest-value properties on this chain and they express directly.
- **Arithmetic invariants** — solvency, monotonicity, conservation, no share dilution. These are
  first-class, and are checked over exact integers rather than wrapping ones.
- **Exact-integer arithmetic.** Rules reason over unbounded integers rather than wrapping ones, so
  "the fee is exactly one percent", "supply is conserved" and "no share dilution" are stated
  directly. Where an unchecked operation would *wrap* rather than abort, the wrong value is a real
  state-integrity bug and a rule catches it.
- **Reachability** — "a legitimate user can actually complete this flow" — which catches the case
  where a guard is so strict the handler is dead.

What has no rule, and should not be extracted as a property:

- **Panic freedom.** "This handler cannot be made to panic" has no rule here, for a reason worth
  stating: a panic aborts the instruction and the runtime rolls back every account mutation, exactly
  as a returned error does. So a panicking path cannot leave the program in a state that violates an
  invariant, and the analysis prunes those paths to agree with the runtime. A reachable panic is an
  availability concern, not a state-integrity one. (Treating panics as assertion violations is a
  per-run conf option, and one conf covers a whole component's rules, so it cannot be enabled for one
  property without corrupting the verdicts of its neighbours.) Arithmetic that *wraps* is a different
  matter and belongs above.
- Anything about off-chain events: key compromise, phishing, front-running by an off-chain actor,
  governance process.
- Anything that depends on cryptography behaving as intended — signature validity, hash collisions,
  address grinding. These are opaque to the Prover.
- Anything about what a *different* program does. Cross-program invocations are replaced by
  unconstrained stand-ins, so a claim about the callee's behavior is an assumption the rule makes,
  not something it checks. A property about *which* program is invoked, or with whose authority, is
  fine — that is the caller's code.
- Log or event emission, and anything about fees, rent economics, or compute-unit budgets.
- Anything implied by the type system alone (a `u64` is non-negative), and anything already enforced
  by a framework the code uses — an Anchor `Account<T>` has its owner checked for you.

Two things to state rather than omit when they apply, because they change how a rule is written
rather than whether it exists:

- **Sequences.** A property about several transactions in order (a two-step multisig, a timelock)
  is expressible but much more expensive than a single-handler one. Extract it if it matters, and
  say in the description that it spans a sequence.
- **Unbounded collections.** A property quantified over a growable collection needs a bounded stand-in
  to be checked, since loops are unrolled to a fixed depth. Say what the bound should be if you know
  it.

Finally: state each property as one sentence that could be read out to a protocol engineer, and make
the violation consequence explicit. That sentence becomes the rule's name and its doc comment, and
it is what a reviewer compares the rule against.
"""
