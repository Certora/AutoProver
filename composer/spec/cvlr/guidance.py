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

**Soroban's counterpart** (``SOROBAN_CVLR_GUIDANCE``) differs where the chain does: state is
contract storage rather than passed accounts, authorization is ``require_auth`` rather than signer
flags, and the 0.4 CVLR line has no parametric rule form. The chain-neutral half is not factored
out until the Soroban text has met a prover.
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

SOROBAN_CVLR_GUIDANCE: str = """\
These properties will be checked by the Certora Soroban Prover. It symbolically executes the
contract's wasm build, so a property becomes a **rule**: a function whose arguments are arbitrary
values, which calls one contract function and asserts something about the result. Extract
properties that fit that shape — and prefer the ones that fit it best, because a property no rule
can express is a property nobody will check.

What makes a property checkable here:

1. **It names one contract function** — one entry point of the contract's `#[contractimpl]` block
   (`transfer`, `mint`, `withdraw`). A rule calls that function directly, the way the contract's own
   tests do; there is no dispatcher to go through.
2. **It has a before and an after.** The most useful shape by far is "before the call, X held in
   storage; after it, Y holds." State the precondition as well as the conclusion; a property whose
   precondition is left implicit becomes a rule with an unjustified assumption in it.
3. **It is about the contract's own storage.** State lives in the contract's instance, persistent and
   temporary storage, keyed by the contract's storage keys, and a rule reads it back through the
   contract's own getters. State each property over specific keys — "the sender's balance", "the
   total supply", "the admin" — rather than over every entry that could ever exist; a claim
   quantified over all of storage has no rule, so re-state it as an invariant each function
   preserves.
4. **Authorization is a property of the contract's code.** The host enforces `require_auth`, but
   *which* address a function requires, and whether it requires one at all, is the contract's
   decision — and a rule can check it: "if `mint` succeeds, the admin authorized it". That is the
   form an access-control property takes here.

Property classes that are especially well served, and worth looking for specifically:

- **Access control.** A privileged function (mint, upgrade, set-admin, pause) that does not require
  the privileged address's authorization; a function that requires the wrong address's; a
  `from`/`spender` confusion in an allowance flow. These are the highest-value properties on this
  chain and they express directly.
- **Conservation and accounting** — a transfer moves exactly the amount from one balance to the
  other and changes no third balance; a mint raises the supply by exactly the minted amount;
  balances never go negative; an allowance is decreased by exactly what was spent.
- **Exact arithmetic.** The prover checks the contract's arithmetic exactly, so "the fee is exactly
  one percent" is stated directly. Where an operation would *wrap* rather than abort, the wrong value
  is a real state-integrity bug and a rule catches it.
- **Reachability** — "a legitimate caller can actually complete this flow" — which catches the case
  where a guard is so strict the function is dead.

What has no rule, and should not be extracted as a property:

- **Panic freedom.** "This function cannot be made to panic" has no rule here: a contract that panics
  — or returns an error — fails the invocation, and the host rolls back every storage write it made.
  So a panicking path cannot leave the contract in a state that violates an invariant, and the
  analysis prunes those paths to agree with the host. A reachable panic is an availability concern,
  not a state-integrity one. (Treating traps as assertion violations is a per-run conf option, and
  one conf covers a whole component's rules, so it cannot be enabled for one property without
  corrupting the verdicts of its neighbours.) Arithmetic that *wraps* is a different matter and
  belongs above.
- **"This call does not fail."** The same pruning means a rule sees only the calls that succeed, so
  it can show what a successful call did, and that a *failing* condition was ruled out by the call
  — but not that some call is accepted. State acceptance as reachability instead.
- Anything about off-chain events: key compromise, phishing, front-running, governance process.
- Anything that depends on cryptography behaving as intended — signature validity, hash collisions.
  The host's authorization machinery is assumed to work; what is checkable is whether the contract
  asks it the right question.
- Anything about what a *different* contract does. Only this contract's wasm is analyzed, so a claim
  about a callee's behavior is an assumption the rule makes, not something it checks. A property
  about *which* contract is called, with what arguments, is fine — that is this contract's code.
- Event emission. The CVLR release this backend builds against has no way to observe published
  events, so a property whose content is "an event is emitted" cannot be stated.
- Storage TTL and archival, resource fees, and CPU or memory budgets — host economics, not contract
  logic.
- Anything implied by the type system alone, and anything the SDK enforces for the contract — a
  `u32` is non-negative, a typed storage read cannot return another type.

Two things to state rather than omit when they apply, because they change how a rule is written
rather than whether it exists:

- **Sequences.** A property about several calls in order (initialize, then mint; propose, then
  execute after a delay) is expressible but more expensive than a single-call one. Extract it if it
  matters, and say in the description that it spans a sequence.
- **Unbounded collections.** A property quantified over a growable `Vec` or `Map` needs a bound to be
  checked, since loops are unrolled to a fixed depth. Say what the bound should be if you know it.

Finally: state each property as one sentence that could be read out to a protocol engineer, and make
the violation consequence explicit. That sentence becomes the rule's name and its doc comment, and
it is what a reviewer compares the rule against.
"""
