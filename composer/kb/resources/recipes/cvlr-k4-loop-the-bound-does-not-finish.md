> Where this recipe and the Solana/CVLR manual or other Certora materials diverge, this recipe governs.

### K4. A loop the bound does not finish `[CONF]` `[RULE]`

**Trigger:** a rule stops on the assertion *"Unwinding condition in a loop"*: a VIOLATED with the
loop in its trace, or a vacuous rule whose every path runs through one. The conf's `loop_iter`
unrolls loops that many times and does not set `optimistic_loop`, so this is a real result, not a
configuration accident: the prover cannot account for the remaining iterations.

`optimistic_loop` silences it by *assuming* the loop always finishes within the bound, which hides
any violation that needs more iterations to reach. It is the last resort, below everything else, and
what follows is the order to work through first:

1. **Constrain what determines the trip count.** Usually the loop is over a collection whose length is
   nondeterministic, so `cvlr_assume!` a bound on that length, in the rule, and record it as an
   assumption. This is the honest fix: the rule then states the bound it proves under, and the report
   says so. Shrinking the collection in the program instead is unsound in a direction nothing
   reports.
2. **Ask whether the loop is even in your property's way.** A loop in a borsh deserialization path or
   a logging helper is not what the property is about. Summarize the function that holds it with
   `summarize_for_prover`, or ask `code_editor` to munge it; either is the same trade as any other
   summary, with the same obligation to justify it. A summary directive names a function, never a
   loop. If the property is *about* the loop, neither is
   honest: `record_skip` and name the loop.
3. **Raise the bound, having tried the first two.** `adjust_prover_config` sets `loop_iter`. Cost
   grows exponentially with it, so raise it to the smallest number the property needs and say in
   the `why` what you bounded first and why it was not enough. Above 3 or 4 it grows faster than
   the answer is usually worth; if the property genuinely needs more than that, a skip naming the
   count you needed against the count you have is the more useful result.
4. **Assume it, having established that no bound will do.** `adjust_prover_config` also sets
   `optimistic_loop`, and this is the rung the first three do not reach: a trip count the analysis
   cannot fix is not answered by constraining the inputs, by a summary, or by unrolling further —
   the bound goes up and the reported iteration goes up with it, forever. Establish that it is that
   case by moving the bound and reading what comes back, because a loop that discharges at 3 is not.
   A CPI's instruction encoder is the measured example.

   What it costs is not confined to the loop you were fighting: **every rule in the unit's next
   submission is verified under the assumption**, so each of their verdicts becomes conditional on
   something nothing checked. A rule whose property *is* the loop's termination or its later
   iterations proves nothing under it, and for that rule `record_skip` naming the loop is the honest
   result. What it buys is the rest of the handler, which is otherwise unreachable.

An unwinding violation reported as a property failure is a wrong answer, so read the counterexample
before concluding the program is at fault: if the failing assertion is the unwinding condition rather
than your own, the rule never reached its property.
