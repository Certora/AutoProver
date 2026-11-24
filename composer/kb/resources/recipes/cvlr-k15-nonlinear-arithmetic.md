> Where this recipe and the Solana/CVLR manual or other Certora materials diverge, this recipe governs.

### K15. A rule that cannot finish: nonlinear arithmetic `[RULE]` `[EDIT]` `[SKIP]`

**Trigger:** the job reports a rule at a low completion percentage — 1%, 4% — after the Prover has
split it into a large number of sub-problems, and the job eventually HALTs on its global timeout.
That is not a slow machine and not a bug in your rule. It means the solver was handed arithmetic it
cannot decide, and no amount of waiting will change the answer.

**What is hard.** Multiplying or dividing two values that are *both* symbolic — neither one a literal
— is **nonlinear**, and nonlinear integer arithmetic is where a solver stops being complete and starts
searching. One symbolic operand against a constant is fine; `price * amount` where the Prover chose
both is not. Fixed width makes it worse rather than better: a `u128` multiply is a bitvector
operation with an overflow check hanging off it, so the solver gets a hard multiply *and* a branch,
on every call.

`NativeInt` does not make a product linear. It takes the arithmetic off bitvectors, which is a real
and separate win, but `a * b` over two symbolic `NativeInt`s is exactly as nonlinear as it was.

Two things produce this, they are indistinguishable in the job report, and they take different
answers. Work out which one you have before reaching for either.

#### The arithmetic is the program's

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
bitvectors and into integers; partiality is **assumed** away rather than branched on (`.into()`
assumes the result fits in `u128`, and `safe_div` assumes a non-zero divisor), so the panicking path
is removed instead of explored; and the widths that were not the problem keep their
real implementations. Swapping only `u128` is normal and better than swapping all of them.

Two consequences to state rather than discover. These helpers usually live in a **dependency crate**
rather than the program, which makes the redirect **run-global** — in force for every unit of this
run, not only yours; the editor will tell you if the workspace is not wired for it. And a rule proved
against substituted arithmetic is a rule about the substitute: overflow is no longer reachable there,
so any property whose content is "this cannot overflow" is gone rather than proved. Say so in the
summary; a reviewer will ask.

#### The arithmetic is the property's

Sometimes the program is not the problem. A solvency or exchange-rate property compares two products
of symbolic quantities — `supply * price` against `position * other_price` — and the nonlinearity is
in the statement you were asked to prove. There is nothing to abstract, because the multiplication
*is* the property. Two moves, in this order.

**Bound the operands — this is a soundness step, not a speed one.** A `NativeInt` the rule creates
itself — from `nondet()`, or as a lemma's free context — has none of the program's limits, and the
Prover will then explore states the program cannot reach. (`NativeInt::from` of a stored `u64`
keeps that value's bound; it is the operands the rule makes up that lose it.) What
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

If the rule still does not finish, skip it, naming the timeout, what you bounded, and what you
lifted. A rule with no verdict is not a verification, and reporting it as one is worse than skipping
it.
