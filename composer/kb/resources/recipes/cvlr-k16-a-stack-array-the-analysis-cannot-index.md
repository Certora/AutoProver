> Where this recipe and the Solana/CVLR manual or other Certora materials diverge, this recipe governs.

### K16. A stack array the analysis cannot index: `[3001]` in a wide-integer library `[EDIT]` `[SKIP]`

**Trigger:** a rule ends in `[3001] access to an indeterminate stack location` rather than a verdict.
The message's `source:` line is the place to read. The common case on Solana programs is a file in a
wide-integer library: the `uint` crate's `construct_uint!` (`U256`, `U192`, `U128` types, most often
reached through a fixed-point or "big fraction" helper), or a hand-rolled `[u64; N]` bignum in the
program itself.

**Why.** The pointer analysis has to know, at every load and store, which stack slot is touched.
A library that keeps a number as `[u64; N]` on the stack and walks it in loops whose bounds depend
on the operands (`for i in word_shift..N { ret[i] = ... }` in a shift, the normalisation loop of a
long division) indexes that array with a value only known at run time. That is what `[3001]`
reports. It is a property of the code's *shape*, not of the values, so no assumption in the rule
changes it, and no `loop_iter` does either.

**What does not work, so do not spend submissions on it.**

* **`summarize_for_prover` on the callers.** The arithmetic is generic code instantiated into its
  callers, and the callers are usually small enough to be inlined into theirs, so the directive
  matches nothing. Asking the editor for `inline_never` gives the summary a symbol to bind to, but
  even when it binds, the summarized function is usually the one the property is about (a market
  value, a debt amount): it now returns an unconstrained number that the program's call and the
  rule's call do not share, and the property cannot be stated. Measured: one unit registered four
  such summaries plus an `inline_never` edit and the `[3001]` was unchanged on every rule.
* **Bounding the inputs.** The offset is still computed at run time.

**What does: an exact straight-line stand-in for the type, through `redirect_module`.** Ask the
editor to redirect the module that defines the wide type, under your unit's feature, to a file that
defines the *same* types with the same public surface (the tuple of limbs, `From`/`TryFrom`, the
operators, comparisons) and computes **exactly the same values with the same panics**, written as
straight-line code over `u128` halves: no loop, and no index into an array that is not a literal.
This is not a stand-in in the K8 sense. It is the same function written in a shape the analysis can
follow, so the properties that go through it keep their meaning, and you say so in the rule's
comment. When the type is defined by an inline `mod` (`mod uint_types { construct_uint! { ... } }`),
the request is to gate that block off under the feature and declare a file module with the same
name under it. Name the operators the program actually uses; the editor has to implement each one.

Measured on one lending program: with the 256-bit type redirected this way, a rule driving the
borrow calculation went from `[3001]` to a verdict, and with the elevation-group and referrer flags
fixed it verified with no bounds on the amounts.

**What the redirect does not fix.** The arithmetic is still 256-bit, so a decision built from many
such operations can now time out instead. That is a K8 problem. Narrowing a `fixed` type (K15)
does not reach it: code that builds the wide type from a fixed-point value's bits still computes on
the full encoding. For a property that only consumes such values, stand in for the function that
computes them (K8); for one decided by them, fetch K17 for the editor's two kinds that make the
arithmetic cheaper.

**When the source is not a wide-integer library**, the Prover's own help applies: a loop over a
stack array whose bound is not a constant, or a local whose address is taken and dereferenced
through a computed offset. If the code is the program's own and small, `mock_fn` (K8) for a
consumer of the result; otherwise skip the property with the `[3001]` and its source line.
