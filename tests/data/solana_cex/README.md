# Solana counterexample fixtures

Output from two Certora Solana Prover runs against the `solana_vault_idl` scenario, kept
so that `composer.prover.results` can be tested without submitting a cloud job. See
`tests/test_solana_cex_trace.py`.

- `loop_unwinding/` — a whole run's final treeView (three rules: one VIOLATED, one ERROR, one
  VERIFIED). The violation is a loop bound the prover could not discharge, not a property
  violation. The ERROR is a `[3308]` pointer-analysis failure, which is why this directory also
  serves as the fixture for what that looks like.
- `assertion_failed/rule_output.json` — one rule's output from a different run: a genuine property
  violation, with the rule's logged values in the call trace. Its own run's treeView is not kept
  because every root in it came back ERROR, so nothing there reaches the parser's VIOLATED path.

Both runs predate the Prover reporting `assertOrigin`. Each rule output has had that one key added
by hand, with the value the Prover now reports for that assertion: `"loopUnwinding"` for the loop
bound, `"user"` for the property violation, `null` for the output that has no violation. Nothing
else is edited.

- `assert_origin/` — unedited rule outputs from a Prover that reports `assertOrigin`
  (`prover_version` `eric/assert-origin`), for rules added to
  SolanaExamples' `cvlr_by_example/first_example`: `user.json` (`rule_faulty_add_performs_addition`),
  `cannot_overflow.json` (a pointer offset by an unbounded `nondet`, under
  `-solanaTACSoundSignedMath`), and `loop_unwinding.json` (a loop with a nondeterministic trip count
  past `loop_iter` 2). The last is from a second run.

`/home/runner/...` paths inside these files are the prover's own platform-tools build paths.
