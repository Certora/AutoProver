# CVLR and the Certora Solana Prover

What is true of CVLR, of the Solana Prover, and of the programs they run against, for any agent
working on a CVLR verification — the author who writes rules, the judge who reviews them, the editor
who changes the program under them.

Nothing here is about one agent's tools or one run's configuration. Those belong to whichever agent
has them, and are in that agent's own instructions.

## What a rule is

Two forms, and the choice matters:

* `#[rule] fn rule_<name>() { ... }` — one rule. The attribute makes the function a prover entry
  point; the rule's name is the function's name.
* `cvlr_rules! { name: "<name>", spec: <spec>, bases: [base_a, base_b] }` — one *specification*
  checked against several base functions, which generates one rule per base (`<name>_a`, `<name>_b`,
  with a `base_` prefix stripped). Reach for this when one property genuinely applies across several
  handlers: it is far cheaper to write than the same rule restated per handler, and each generated
  rule gets its own verdict and its own counterexample.

A rule body is: create nondeterministic inputs, assume the preconditions, call the program, assert
the postcondition.

**The code under test is the program's, always.** A rule that drives a function *you* wrote proves a
property of your function. A reader of the report cannot tell that apart from a verdict about the
program — the two look identical — and if your version and the program's have drifted by so much as
an operator, the report is wrong in the confident direction. Helpers, mocks, `Nondet` and `CvlrLog`
implementations all belong in the harness. The state transition the property is *about* does not.

At publish time you will declare, per rule, which program function it drives, spelled from the
crate root — `crate::<program module>::<handler>`. That spelling is a requirement and the gate
enforces it:
this harness is a module *inside* the program's crate, so everything the program defines is reachable
as `crate::…`, and a path rooted anywhere else names a dependency.

Which is the trap worth naming. A rule can drive a *library* function, verify cleanly, and read like
a verification of the program. `cvlr_assume!(!accounts[1].is_signer); cvlr_assert!(Signer::try_from(&accounts[1]).is_err());`
demonstrates that Anchor rejects a non-signer — true, and nothing whatever about the program you were
given. If a property is enforced by a dependency or by Anchor's account validation rather than by the
program's own code, that is a skip with that reason, not a rule about the dependency.

If you cannot reach the program's own code, that is a result to report — see the escalation ladder
in the task prompt. It is never a reason to move the rule onto something you can reach.

## Counterexamples are only as legible as your `clog!`s

A violated rule comes back as a counterexample, and what you can see of it is exactly what you
logged. **Log every value the assertion depends on, before the assertion**, using `clog!`. A rule
that fails with nothing logged tells you that something is wrong and nothing about what; you will
then spend a prover run — minutes — learning what one `clog!` would have told you. This is the single
highest-return habit on this backend.

If a type you assert over has no `CvlrLog` implementation, write one **in your own harness module**
rather than formatting it by hand. `put_harness` replaces that module and nothing else: the sibling
files the scaffold created (`log.rs`, `nondet.rs`, `mocks/`) exist for a human continuing this work
by hand, and you have no tool that writes them — do not try. A trait impl may live anywhere in the
crate as long as the *type* is this crate's own, which is true of everything you assert over here.

## Assumptions are the sharpest tool and the easiest way to prove nothing

`cvlr_assume!` restricts the states the prover considers. That is how you exclude inputs the program
genuinely cannot receive — and it is also how a rule becomes vacuous, passing because no state
satisfies its preconditions at all. Two contradicting assumptions make every assertion true.

So: assume the *least* you can get away with. Vacuity is reported rather than mistaken for success
whenever the conf sets `rule_sanity` — check the conf your run was given, because one that omits it
gets no such report and a vacuous rule reads as a clean pass. If a rule passes immediately and you
are surprised, check for vacuity before believing it.

## Nondeterminism is the quantifier

`nondet()` produces an unconstrained value, and the prover reasons over *all* of them. That is what
makes a rule a proof rather than a test: you do not choose inputs, you constrain them. So a property
of the form "for all amounts" needs no loop and no sampling — one `let amount: u64 = nondet();` is
the universal quantifier.

**Never skip a property because it is universally quantified.** That shape is what this tool is for.

For account inputs, `cvlr-solana` provides the nondet account machinery; read its source for the
current spelling rather than guessing, since it has changed across releases.

**A nondet account has a nondet data length, and that is what breaks rules.** The account machinery
gives you unconstrained bytes *and* an unconstrained `data_len()`. Every deserializer the program
runs on that account then has to cope with a buffer of unknown size, which is where the Prover stops
being able to help you. Three separate failures all come from it, and none of them names the cause:

| what you see | where it comes from |
|---|---|
| `Pointer domain: statically unknown length ... at sol_memcpy_` | serializing into `data.borrow_mut()[..]` |
| `[3308] illegal dereference of an absolute address` | unpacking a token account or mint |
| `unexpected pointer arithmetic` (an internal Prover error) | deserializing a state enum |

So **constrain the shape of every account your property touches, before anything reads it**:

```rust
// A mint with no Token-2022 extensions. One assumption, and the extension parser
// inside the program now has a length it can reason about.
cvlr_assume!(mint_info.data_len() == spl_token_2022::state::Mint::SIZE_OF);
```

**Constrain the shape; let the program do the parsing.** The rule says how big the account is, and
the handler unpacks it as it normally would. Do not deserialize account data *inside the rule* to
build up preconditions — `unpack`, `try_from_slice_unchecked` and `borsh::to_writer` in a rule body
are where these errors come from, and a precondition you established by parsing is one the program is
about to parse again anyway. Reach for a `data_len()` assumption first, every time.

This is also the honest reading of a success path that will not go through. If no execution of a
handler succeeds — every success rule comes back `SANITY_FAILED` while the rejection rules verify —
the usual cause is not that success is impossible but that nothing has pinned the accounts down
enough for it to be *reachable*. Add the shape assumptions before concluding anything about the
program.

**An enum input needs an impl, not a literal.** `nondet()` covers the scalars, and a struct you can
build field by field — but there is no way to write "some variant of this enum" inline. Implement
the trait in your harness module and dispatch on a nondet discriminant:

```rust
impl cvlr::nondet::Nondet for FeeType {
    fn nondet() -> Self {
        match nondet::<u64>() {
            0 => Self::SolReferral(nondet()),
            1 => Self::Epoch(nondet()),
            _ => Self::SolWithdrawal(nondet()),
        }
    }
}
```

The catch-all arm is the part that matters. A `match` whose every arm names a discriminant leaves
the remaining values unmapped, and the rule then quantifies over only the variants you listed —
which reads as a proof about the enum and is a proof about a subset of it.

## Loops are bounded, and the bound is not assumed away

The conf's `loop_iter` unrolls loops that many times, and it does **not** set `optimistic_loop`. So a
loop that can run longer than the bound comes back as a violated assertion reading *"Unwinding
condition in a loop"* — a real result, not a configuration accident: the prover is telling you it
cannot account for the remaining iterations.

`optimistic_loop` would silence it by *assuming* the loop always finishes within the bound, which
hides any violation that needs more iterations to reach. That is a last resort and it is not yours to
reach for. In order:

1. **Constrain what determines the trip count.** Usually the loop is over a collection whose length is
   nondeterministic, so `cvlr_assume!` a bound on that length. This is the honest fix: the rule then
   states the bound it proves under, and the report says so.
2. **Ask whether the loop is even in your property's way.** A loop in a borsh deserialization path or
   a logging helper is not what the property is about; a summary for that function is the same trade
   as any other summary, with the same obligation to justify it.
3. **Raise the bound, having tried the first two.** `adjust_prover_config` sets `loop_iter`. Cost
   grows exponentially with it, so raise it to the smallest number the property needs and say in
   the `why` what you bounded first and why it was not enough. Above 3 or 4 it grows faster than
   the answer is usually worth; if the property genuinely needs more than that, a skip naming the
   count you needed against the count you have is the more useful result.

An unwinding violation reported as a property failure is a wrong answer, so read the counterexample
before concluding the program is at fault: if the failing assertion is the unwinding condition rather
than your own, the rule never reached its property.

## When the Prover cannot follow the program's own data

Four symptoms, one family, and none of them names its cause:

| symptom | what it is telling you |
|---|---|
| `[3005] memcpy with dynamically sized length` | a copy whose length the analysis cannot pin down |
| `[3308] illegal dereference of an absolute address` | a pointer it could not resolve to an object |
| an internal error out of `sbf.domains.ScalarDomain` | the same analysis giving up rather than reporting |
| **a submission that fails with no per-rule verdicts at all** | the same thing, before any rule was encoded |

That last row is the one to watch for, because it reports the least. A job that comes back failed
with no rule results is not an infrastructure hiccup to retry — retrying it costs a job and returns
the same answer.

None of these is a rule defect and no rewording clears them. They come from the program moving state
whose *size the analysis cannot fix*. `cvlr_assume!` on an account's `data_len()` is still worth
doing and is **not** enough on its own: the length that defeats the analysis is usually inside the
encoding or the accessor, not on the account.

**Two shapes cause this, and only one of them you can do anything about.** Read the handler and find
which:

**A variable-length collection** — a byte-slice-backed vector of entries, a packed list, anything
indexed at a stride computed at runtime. **Ask the editor to `mock_fn` its accessors** — the
find / find_mut / retain / iterate methods, not the handler that calls them. Write stand-ins that
serve a small fixed number of entries, dispatching on whatever index or key the program uses, and
`cvlr_assert!(false)` on the fall-through so that reaching an entry your model does not have is a
*reported failure* rather than a silent aliasing of two entries into one. This is the case where
asking early beats asking last: every rule in the unit needs the same stand-ins, so discovering it
after three prover jobs wastes three.

**A fixed-size state struct behind a derived (de)serializer** — the handler does
`try_from_slice_unchecked::<State>(&info.data.borrow())` and `borsh::to_writer(&mut
info.data.borrow_mut()[..], &state)`, and `State` has `Option` or enum fields, so its *encoding* is
variable length even though the struct is not. **Ask the editor for a `swap_derive`**: it moves the
derived (de)serializer behind your unit's feature so you can supply the impls instead. Name the type
and the two calls.

You then owe the impls, in this module, the way you owe a `mock_fn` stand-in. They read and write a
value held in a `static mut` that each rule initialises with
`cvlr::nondet::havoc::alloc_havoced::<State>()`, so the program's own `try_from_slice_unchecked` and
`to_writer` calls move a fixed-layout value in memory instead of parsing a buffer. `derive(Copy)` is
what lets an impl assign `*self` to it, and the editor adds that.

Two things to get right, because nothing else will catch either:

* **A rule that reads state back after the handler wrote it needs the impls to share one value.** If
  each call mints a fresh one instead, the write is invisible and a transition property is violated
  for a reason that has nothing to do with the program. A rule that only checks *who signed* never
  reads back and does not need the sharing — for those, an impl returning a fresh unconstrained
  value is simpler and safer.
* **One value per type means two accounts of that type are the same account.** If the handler takes
  a source and a destination of the swapped type, this munge cannot express it — `record_skip`
  rather than prove something about a program that cannot tell them apart.

And the cost, which the judge will hold you to: with the deserializer gone, deserialization can no
longer fail and never reads the bytes. Any property about *encoding* — a malformed account rejected,
a truncated one rejected, a field surviving a round trip — is unprovable under a swap and will pass
while meaning nothing. Do not ask for one to serve a property of that shape.

The two are told apart by what the handler touches, not by the error code: the same `[3005]` comes
from both. If the handler never indexes a collection, it is the second shape.

**A munge is the most consequential thing in this run, so treat asking for one that way.** It changes
the program the verdicts are about. Prefer, in order: a different rule; a summary, if the code in the
way is not what the property depends on; then the editor. Read the diff it hands back — if you
disagree, `revert_munge`.

The editor can refuse, and a refusal is information. If what a property needs is none of its seven
kinds — a hand-unrolled loop, a restructured state machine, a shrunk collection — the editor will
say so and name the kind it would have taken. `record_skip` and pass that on. That is a decision for a
person, and naming it is what makes the skip actionable.

A munge is scoped to your unit. Every unit of this run shares one copy of the project, and the
attribute is gated on your unit's cargo feature — so it applies to your rules and to nothing else, and
a sibling unit munging the same function is a second line rather than a conflict. You never have to
coordinate with another unit about one.

## Arithmetic, and why panic-freedom is not on the menu

Arithmetic correctness is a real property class here, and a stronger one than on EVM: rules reason
over exact integers, so "the fee is exactly one percent" or "supply is conserved" is stated directly.
Use `NativeInt` (`cvlr::mathint`) when you need a value without the bit-width in the way — but treat
widening and bounding as **one move, not two**. `NativeInt::from(x)` removes the bit width and does
not put anything back: a quantity the program can only hold as a `u64` becomes, in your rule, a value
with no upper limit at all. Widen it and re-state its bound in the same breath (`cvlr_assume!(x.is_u64())`),
and convert back the same way. The section on nonlinear arithmetic below says what the missing half
costs; it is not a performance note.

**"This handler cannot be made to panic" is not something you can check, and you should not try.**
Two independent reasons, and the first is the one that matters:

* **A panic reverts.** On Solana a panic — like a returned `Err` — aborts the instruction and the
  runtime rolls back every account mutation. So a panicking path cannot violate an invariant: there
  is no surviving state for the invariant to be false about. Pruning those paths is not a gap in the
  analysis, it is the analysis agreeing with the runtime. A reachable panic is an *availability*
  concern, not a state-integrity one, and it is not a finding you can produce from a rule.
* **The switch is per-run, not per-rule.** Treating panics as assertion violations is a conf option,
  and this backend writes one conf per unit. Turning it on to state one panic-freedom property would
  make every *other* rule in the unit report its pruned panicking paths as violations. There is no
  configuration in which one rule sees panics and its neighbours do not.

So do not write a rule asserting a handler never panics, and do not report a panicking path as an
attack. If a property in your batch asks for panic-freedom, skip it and say that a panic reverts, so
the property is not about state the program can be left in.

One thing this makes *more* important rather than less: if an unchecked operation can **wrap** rather
than panic, that is a genuine state-integrity bug and it is checkable — assert the post-state equals
the mathematically correct value and the rule fails on the wrapping input. Whether a build wraps or
panics is a profile setting (`overflow-checks`), so read it rather than assuming.

## When the Prover cannot finish: nonlinear arithmetic

A rule can be correct, compile, and still never produce a verdict. The symptom is specific enough to
recognise on sight: the job reports a rule at a low completion percentage — 1%, 4% — after the Prover
has split it into a large number of sub-problems, and the job eventually HALTs on its global timeout.
That is not a slow machine and not a bug in your rule. It means the solver was handed arithmetic it
cannot decide, and no amount of waiting will change the answer.

**What is hard.** Multiplying or dividing two values that are *both* symbolic — neither one a literal
— is **nonlinear**, and nonlinear integer arithmetic is where a solver stops being complete and starts
searching. One symbolic operand against a constant is fine; `price * amount` where the Prover chose
both is not. Fixed width makes it worse rather than better: a `u128` multiply is a bitvector
operation with an overflow check hanging off it, so the solver gets a hard multiply *and* a branch,
on every call.

`NativeInt` does not make a product linear. It removes the bit width, which is a real and separate
win, but `a * b` over two symbolic `NativeInt`s is exactly as nonlinear as it was.

Two things produce this, they are indistinguishable in the job report, and they take different
answers. Work out which one you have before reaching for either.

### The arithmetic is the program's

Any project past a certain size routes its arithmetic through shared helpers — a `SafeMath` trait,
a `u256`/`bn` module, fixed-point scaling. Underneath, those are `checked_mul` and friends: the
bitvector-multiply-plus-branch shape above, repeated at every call site in the handler.

This is a `redirect_module` (ask the editor). The substitute keeps the module's public
surface — same trait, same signatures — and reimplements the operations over `NativeInt`:

```rust
impl SafeMath for u128 {
    fn safe_mul(self, v: Self) -> Result<Self> {
        Ok((NativeInt::from(self) * NativeInt::from(v)).into())
    }
    fn safe_div(self, v: Self) -> Result<Self> {
        cvlr_assume!(v > 0);
        Ok((NativeInt::from(self) / NativeInt::from(v)).into())
    }
}
```

Three separate things are happening there and each is deliberate: the arithmetic moves off
bitvectors and into integers; partiality is **assumed** away rather than branched on, so the
panicking path is removed instead of explored; and the widths that were not the problem keep their
real implementations. Swapping only `u128` is normal and better than swapping all of them.

Two consequences to state rather than discover. These helpers usually live in a **dependency crate**
rather than the program, which makes the redirect **run-global** — in force for every unit of this
run, not only yours; the editor will tell you if the workspace is not wired for it. And a rule proved
against substituted arithmetic is a rule about the substitute: overflow is no longer reachable there,
so any property whose content is "this cannot overflow" is gone rather than proved. Say so in the
summary. The judge will ask.

### The arithmetic is the property's

Sometimes the program is not the problem. A solvency or exchange-rate property compares two products
of symbolic quantities — `supply * price` against `position * other_price` — and the nonlinearity is
in the statement you were asked to prove. There is nothing to abstract, because the multiplication
*is* the property. Two moves, in this order.

**Bound the operands — this is a soundness step, not a speed one.** Widening to `NativeInt` throws
the program's own limits away, and the Prover will then explore states the program cannot reach. What
comes back is not "slow": it is a **counterexample you have no way to distinguish from a real
defect**. A run that skipped this got a violation whose witness had a stored supply of ~2.7 x 10^62 —
a number that cannot exist in a `u64` field — and cost a submission and a round of analysis to
identify as an artefact of the rule rather than a bug in the program. Being faster is a side effect.

The program's own guarantees are what you re-state: a price is at least its precision constant, a
stored amount fits in `u64`, an updated value is at most double its predecessor.

```rust
cvlr_assume!(price >= EXCHANGE_PRICE_PRECISION);
cvlr_assume!(position_amount.is_u64());
cvlr_assume!(new_price <= old_price * 2);
```

`is_u64`, `is_u128` and `u128_max` are `NativeInt`'s own, and a bound against a *constant* stays
linear. Every one of these is also a hole in the proof, so assume what the program enforces — not
what makes the rule finish — and `clog!` them so a reviewer sees the domain you proved over.

**Then lift the algebra into a lemma.** If it still does not finish, the rule is doing two jobs:
driving the handler, and proving an inequality. Separate them. `cvlr_lemma!` states the algebraic
step over a context of free `NativeInt`s with no program in it, and gives you two operations on it:

* `.verify()` — havoc the context, assume `requires`, assert `ensures`. This is the obligation, and
  it belongs in a `#[rule]` of its own. It is cheap, because there is no handler in it.
* `.apply(&ctx)` — **assert** `requires` at this point, then **assume** `ensures`. This is how the
  heavy rule uses it: it must still discharge the precondition where it stands, but it gets the
  conclusion without the solver redoing the nonlinear step.

That pairing is the whole point — the algebra is proved once, in isolation, and consumed everywhere
else. Look up `cvlr_lemma!` and `CvlrLemma` in the CVLR documentation for the exact syntax before
writing one; both forms of the macro and the `Nondet`/`CvlrLog` derives its context needs are
documented there.

**The lemma failing is a good outcome, not a setback.** If the isolated algebra does not verify, the
property as stated is false and no prover budget was ever going to prove it — you have found that out
in a cheap rule with a readable counterexample instead of a two-hour timeout with none. The usual
cause is a missing slack term: the naive inequality ignores a rounding direction or a price movement,
and the counterexample shows you which.

**And if the decomposition is not available, the solver portfolio is.** `adjust_prover_config` turns
on twelve solver instances with different random seeds and both arithmetic theories enabled, which
is what the practitioners who hit this wall reach for: on a nonlinear query the seed genuinely
decides whether an answer arrives.

It works, and it is worth knowing how well. Measured on this backend's hardest real harness, with
the lemma decomposition *removed* so that four solvency rules had to find the whole ring
rearrangement inline with a live Anchor handler in the query. Without the portfolio: **109 minutes**,
nine rules verified and one — the hardest — timed out with no verdict at all. With it: **4.6
minutes**, all ten verified. So it is worth reaching for on both counts, and it is not slow —
twelve solvers run in parallel, so it buys wall-clock with compute we own.

It still comes after the lemma, and the reason is not cost. The lemma verifies at about the same
speed, and it leaves behind something the portfolio does not: the algebra proved once, in isolation,
in a rule a reviewer can read and check. A rule that verifies because a solver found a good seed is
a weaker artifact than one whose hard step was discharged separately, even when both are green. Use
the portfolio when the property will not decompose, or when the decomposition did not land — not
instead of trying.

It does nothing for a rule whose difficulty is an operand you did not bound. If it does not finish
the rule either, `record_skip`
naming the timeout, what you bounded, and what you lifted. A rule with no verdict is not a
verification, and reporting it as one is worse than skipping it.
