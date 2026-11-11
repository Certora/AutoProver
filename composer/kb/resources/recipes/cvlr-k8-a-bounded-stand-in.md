> Where this recipe and the Solana/CVLR manual or other Certora materials diverge, this recipe governs.

### K8. A rule that times out: its shape first, then a bounded stand-in `[RULE]` `[MOCK]` `[EDIT]`

**Trigger:** a rule that came back TIMEOUT in a job that returned verdicts; a rule whose statistics
line says `static analysis`; one function holding most of a timed-out rule's branching or nonlinear
share, including a function in a stand-in that is already in place.

**Before a stand-in, the rule's own shape.** Three shapes time out on their own, and each has a fix
that keeps the program as it is. Measured on one program, where each timed out and its fix verified:
- **One rule over several cases.** A rule ranging over several account-list lengths, or over both
  pre-state variants, timed out; one rule per length or variant verified. Split it that way.
- **An unbounded wide product.** A product of two `u64`s into a `u128` timed out twice with the
  factors unbounded, and verified with both below `2^32`. Bound both factors, and say in the
  commentary that the bound is the domain of the proof, not a fact about the program. A property
  about behaviour at the top of the `u64` range cannot take that bound.
- **A walk nothing bounds.** A loop of one iteration per epoch, with nothing in the program bounding
  how far activation is from now, did not finish at a higher `loop_iter`. Assume a small distance
  and record it as the domain.

Re-running an unchanged timing-out rule will time out again. If the rule's shape is none of these,
or fixing it did not help, read on.

**Read where a rule stood before you guess.** The prover report ends with a line for each rule that
timed out: the phase it reached, its path count, and the functions with the largest share of its
branching and of its nonlinear operations. `static analysis` means the rule never reached the
solver, because the program it drives is too large; running it alone in a smaller job does not
change that. A function that holds most of the share is where the time goes. It is often a callee
of the function the rule drives, and it is the one to consider replacing below.

Before you skip every property whose path crosses the function, sort them:

* **Properties about what the function computes**: its bounds, its rounding, how its output moves
  with its inputs. These need the real function. If its probe times out, skip them and cite the probe.
* **Properties that only consume its result**: where the tokens it computes go, that the state
  records what moved, which accounts change, what a handler refuses whatever the amount. These hold
  for whatever the function returns, so they do not need the real one.

**Sort by what the rule asserts over, not by what it drives.** A rule can drive a function whose
own output its property is about, while the time goes to a callee whose result that function only
consumes. The property is then of the second kind for the callee: keep the function the rule drives
real, and replace the callee. Look one call down before skipping. The function that timed out alone
is often not the one the rule is about.

For the second kind, replace the function with a stand-in. Write a `pub fn` in this module with the
function's signature that returns `nondet()` values, and ask `code_editor` for a `mock_fn` on the
function, or a `redirect_module` if it is a method (see the editor section of your instructions). An unconstrained
stand-in over-approximates: a rule that verifies against it holds for any value the real function
could return.

Constrain the stand-in only by what the real function guarantees. Each constraint narrows the
over-approximation, and one the real function does not keep makes a verdict unsound in a way no report
shows. So take each constraint from a clamp, a `min` or a check you can point to in the function's
own code, or prove it with a rule over the real function. If that rule times out too, leave the
constraint out, and skip any property that needs it.

For example, in an AMM, say `swap_output(amount_in, reserve_in, reserve_out)` times out. A rule that
the pool's input vault rises by exactly `amount_in`, and the user receives exactly the output the
pool records, holds for any output. It can run against a stand-in returning a `nondet()` output
below `reserve_out`, if the function's code enforces that bound. A rule that the output never
exceeds the constant-product quote is about the function itself, and needs the real one. Now say
`swap_output` itself first calls `pool_fee_tier(pool)`, and that call is what times out. A rule about
the output of `swap_output` crosses `pool_fee_tier`, but uses only the rate it returns. A stand-in
for `pool_fee_tier` returning a `nondet()` rate within the bounds its code enforces lets that rule
run on the real `swap_output`. The names are illustrative.

Three things follow from a stand-in:

* **It replaces the function for every rule in this unit.** The munge is gated on the unit's
  feature, so a property about the function itself cannot be checked here, beside rules that use the
  stand-in.
* **A counterexample may use a value the real function never returns.** Before marking such a rule
  as expected to fail, show that the real function can return that value. Otherwise, add the
  guarantee that excludes it, under the previous paragraph's rule.
* **Say what ran against it.** Name the stand-in in `rule_subjects` for every rule whose path
  crosses it, and list its constraints in the commentary, each with where it comes from.

**When the function holding the time is a stand-in.** A stand-in can be the hot function itself. A
`redirect_module` of a wide-integer module whose stand-in still computes a 256-bit division exactly
costs what the real one did; the statistics name it like any other function, at its path in the
stand-in file. Sort the properties as above. One that only consumes the division's result can take
a cheaper stand-in: ask the editor to revise it so the operation returns `nondet()` values
constrained only by what the operation guarantees and the properties need, such as a quotient no
larger than the dividend and a remainder below the divisor. The exact identity, quotient times
divisor plus remainder equals the dividend, brings back the nonlinear term the revision was meant
to remove, so add it only for a property that needs it. A property about the exact result needs the
exact operation: skip it, citing the timeout under the stand-in. Measured on one program: two of
three rewrites of such a stand-in left its timeouts in place, and three units that never revised
theirs spent about 21 submissions on the same timeouts before skipping.
