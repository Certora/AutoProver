> Where this recipe and the Solana/CVLR manual or other Certora materials diverge, this recipe governs.

### K15. A 128-bit fixed-point type the property is decided by: `narrow_fixed` `[EDIT]`

**Trigger:** the program keeps fractions in the `fixed` crate's 128-bit type (`FixedU128<Frac>`,
usually an alias such as `pub use fixed::types::U68F60 as Fraction;`), and the property is decided
by its multiplications and divisions: a health check, an eligibility, a cap or a ratio computed
from them. Timed-out rules' statistics name `fixed`'s own `mul`/`div` (functions in the `fixed`
crate) as the dominant function. A program's own wide integer is not this trigger: read the next
section first.

**Why a stand-in does not answer it.** Each fixed-point multiply or divide on that type runs through
a 256-bit intermediate, and a decision computed from a few dozen of them is beyond the Prover. A
`mock_fn` or bounded stand-in (K8) helps only a property that *consumes* the result; one that is
*about* the result is then a property about the stand-in. An exact characterization of the division
(`q * d + r == n`) is slower than the division.

**What `narrow_fixed` does.** The editor replaces the alias, under your unit's feature, with
`cvlr_fixed::narrow::NarrowFixedU128<Frac, NFrac>`: the wide type's API and its `to_bits()` /
`from_bits()` encoding, computed at 64 bits with `NFrac` fractional bits. On one lending program's
borrowing unit it took the unit from 10 verified rules and 4 timeouts to 12 verified, with the
time spent on verified rules cut by about half; rules that timed out in 25 minutes verified in
seconds to minutes.

**What it does not reach: wide arithmetic on the type's bits.** The narrow type keeps the wide
type's `to_bits()` encoding, so a program that takes a value's bits into a wide integer of its own
and computes there still computes at full width. Examples are a `U256` from `construct_uint!`
(K16), a "big fraction" for an interest index, or a helper that multiplies and divides a value's
encoding by integers in 256 bits. Before asking, look at where the timed-out rule's time goes: if
the dominant function is the program's (`uint_types::U256::div_impl`, a `BigFraction` operator)
rather than one in `fixed`, narrowing will not move it.

Measured on the same lending program's valuation properties: with the type narrowed (`NFrac = 14`),
the program's own 256-bit division still held 41-62 % of every timed-out rule's branching and no
rule got a verdict. The properties that verified did so through K8 stand-ins for the market-value
functions, which they only consumed. The ones decided by the valuation (LTV, minimum position value)
were skipped. On the same program, rules on the post-borrow health check, which compares the type's
values directly, went from timeouts to verdicts once narrowed.

**What every verdict in the unit then means.**

* Every result is the wide result rounded down to `2^-NFrac`. **No property about rounding,
  precision or dust can be proven** under it; skip those, citing this recipe.
* **A value below `2^-NFrac` is zero.** With `NFrac = 14` that is anything below about `6.1e-5`: a
  5% annual rate per slot is exactly zero, and so is the interest it accrues. A rule about interest,
  fees or any small factor needs a `cvlr_satisfy!` witness that the factor is non-zero on the paths
  it covers, or it may hold because there is nothing to check.
* **Values must fit below `2^(64 - NFrac)`.** A result above that is assumed away by checked,
  wrapping and operator forms, and clamped to the narrow maximum by `saturating_*` and `from_bits`.
  Say so in the commentary of every rule whose amounts could exceed it. A rule that passes raw
  encodings in (a `_sf` field, a `from_bits` argument) assumes each of them below
  `2^(64 - NFrac + Frac)` before the call; otherwise two different values clamp to the same maximum
  and the counterexample is about the clamp.

**Check the program's constants before you ask.** List every constant and configured value the
unit's properties compare against: `fraction!` and `from_num` literals, minimum-value and threshold
constants, configured percentages read as fractions. Each one below `2^-NFrac` reads as zero once
narrowed, and a property decided by it cannot be checked under narrowing. Keep that property out of
the narrowed unit, or skip it citing this recipe. On the lending program a minimum position value of
`0.000001` was below `2^-14`: two minimum-value rules verified narrowed, the judge rejected both
because the shipped threshold had become zero, and the narrowing had to be taken back for the rules
that needed it too.

**Choosing `NFrac`.** Large enough that the smallest factor a property depends on stays non-zero;
small enough that the largest amount fits below `2^(64 - NFrac)`. 14 suits values up to about
`10^15` with four decimal digits of precision. Name it in the request; the editor refuses one
without it.

**Decide it when the unit starts.** It changes the scale every value lives at, so it cannot be
slipped under rules already written. It also applies to every rule in the unit: a unit that holds
exact accounting properties (settled amounts, exact deltas, rounding direction) loses those verdicts
to it, so it suits a unit whose properties are decided by the arithmetic. Write rules that build values through the program's own type
and constants (`Fraction::ONE`, `Fraction::from_num`, the program's scale constants), not raw
encodings computed by hand. A raw encoding off the narrow grid is rounded down when read, and a
harness constant built that way can become zero.

**What to send the editor.** The file that declares the alias, the `NFrac` you chose and why, and the
timed-out rules that call for it. The editor also adjusts the literal macro (`fraction!`) and any
constant built with `from_bits`, and refuses if the program uses an operation the narrow type lacks
(`%`, shifts, bitwise operators, `sqrt`, `recip`, `mul_add`).
