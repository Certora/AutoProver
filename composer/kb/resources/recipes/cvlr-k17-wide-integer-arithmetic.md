> Where this recipe and the Solana/CVLR manual or other Certora materials diverge, this recipe governs.

### K17. A property decided by the program's own wide-integer arithmetic: `native_muldiv` and `narrow_wide_int` `[EDIT]`

**Trigger:** a rule times out and its statistics put the time in the program's own wide integer:
a `U256` operator (`div_impl`, `full_mul`, `overflowing_mul`), whether the `uint` crate's or the
stand-in a K16 redirect put in its place, or a helper that multiplies a value by a ratio through
it (`full_mul_int_ratio`, `mul_div`, a "big fraction" operator). The property is *decided* by that
arithmetic: a cap, a health check, a valuation compared against a limit. If the property only
consumes the result, a K8 stand-in is cheaper and this recipe is not needed.

**Why narrowing the fixed-point type does not answer it.** K15's narrow type keeps the wide type's
`to_bits()` encoding, so a program that takes a value's bits into its own `U256` computes there at
full width. The 256-bit product and long division cost what they did.

**Two editor kinds, in this order.**

* **`native_muldiv`, first, because it is exact.** A function that multiplies then divides through
  the wide type gets, under your unit's feature, a body that computes the same value with the same
  failures as one operation on the Prover's mathematical integers (`cvlr::mathint::NativeInt`'s
  `muldiv`). It reaches methods, so a helper trait on the fixed-point type is in scope. It changes
  no verdict's meaning, and the rules keep their commentary. Ask for it on each such helper the
  statistics name. The editor refuses a function that does more than the multiply and the divide.
* **`narrow_wide_int`, for the operators that remain.** The editor redirects the wide type's
  module (or revises the K16 stand-in already in place) so that its multiplication and division
  compute in the low 128 bits, with both operands' high halves and any product beyond 128 bits
  assumed away; every other operator stays exact. **State the bound the unit's values fit within**,
  and why: the editor refuses a request without one, and every verdict in the unit is proved only
  below `2^128` in that type. Under K15's narrow type in the same unit the encodings stay below
  `2^64`, so a product of two of them always fits; what the assumption still excludes is a product
  with a large integer (a raw token amount, a `10^decimals` scale), which is the case to check.
  Give every rule that reaches a narrowed operator a `cvlr_satisfy!` witness that it still does.

**What they bought, measured.** On one lending program, the rules that check a borrow stays within
the maximum loan-to-value (exact, all-available and at-most borrow sizes) timed out at full width
with `2^129` to `2^312` paths. With the fixed-point type narrowed (K15) and the wide type narrowed,
the exact-size rule verified in seconds; the other two still timed out, down to `2^15`-`2^33`
paths. With `native_muldiv` on the multiply-by-ratio helper as well, they still timed out as
written. Split as the next paragraph says, both verified, every piece in under ten minutes.

**What may remain: a proof that has to be split.** After these kinds, or after `exact_fixed`
(K15), a rule that chains several products and quotients of symbolic values can still time out on
a small nonlinear core. `verify_rules` attaches this recipe when it sees the sign: a timeout on
`2^64` paths or fewer with no function holding most of the time. No stand-in cuts that, because
nothing is left to cut. Split it. Write each step
the property depends on as a rule of its own over fresh values (the computed amount is at most
the quotient; the market value is monotone; multiplying then dividing by a factor rounds down),
then a rule that drives the program once, assumes those steps at the values it computed with
`cvlr_assume!`, and asserts the property. Each lemma holds for all inputs because its own rule
proved it, so assuming an instance of it is sound. Say in the composed rule's commentary which
rules it assumes, and keep all of them in the same unit so they are proved under the same
edits. Assume only what a rule of the unit proves: a `cvlr_assume!` of anything else is an
unproved premise, and the composed rule's verdict means nothing without it. A choice between
outcomes splits the same way: klend's at-most borrow was proved by a rule that its amount is the
exact amount or the all-available amount, the rules proving the property of those two, and a rule
composing them.
