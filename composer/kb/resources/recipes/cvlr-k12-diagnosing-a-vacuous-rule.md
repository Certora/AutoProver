> Where this recipe and the Solana/CVLR manual or other Certora materials diverge, this recipe governs.

### K12. Diagnosing a vacuous rule `[RULE]`

**Trigger:** a rule reported `SANITY_FAILED`: no execution reaches its end, so it proves nothing.
First check that it ends with `core::mem::forget(accounts);`; the dropped account array is the most
common cause on this backend.

**Start with `explain_vacuity(rule)`.** It reruns that one rule with the vacuity check off, about
as fast as an ordinary run, and reads the unsat core of the proof. Its first line settles the
question above: if the rule's own assertion is not in the core, the core's constraints contradict
each other and the analysis names them; if it is, your assumptions are not the cause and weakening
them will not help. What a core cannot show is anything after the assertion: it is the core of the
rule's proof, and the vacuity check's `satisfy` comes later, after the drops.

If you need to go further, **diagnose with `cvlr_assert!(false)`, not `cvlr_satisfy!`.** A
satisfy rule also turns every assertion into an assumption, so it assumes away exactly the one
causing the vacuity and reports the path reachable while the real rule stays vacuous. Measured:
three submissions of satisfy diagnostics, every one VERIFIED, on a rule whose every path hit the unwinding assertion inside a
CPI's instruction encoder; the first diagnostic ending in `cvlr_assert!(false)` came back
VIOLATED with the loop in its trace. Copy the real rule, replace its final assertion with
`cvlr_assert!(false)`, and `clog!` the values the assertion reads:
- VIOLATED on `false` → the path is live, and the trace shows a concrete execution. If a
  generated assertion fired first, that is the cause; if the logged values satisfy your
  postcondition, the vacuity comes from code after the assertion, not from the program.
- Still vacuous → add the rule's assumptions back into the copy one at a time; the one that makes
  it vacuous is the contradiction.
