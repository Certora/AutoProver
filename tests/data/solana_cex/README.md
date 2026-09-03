# Solana counterexample fixtures

Unedited output from two Certora Solana Prover runs against `test_scenarios/solana_vault_idl`,
kept so that `composer.prover.results` can be tested without submitting a cloud job. See
`tests/test_solana_cex_trace.py`.

- `loop_unwinding/` — a whole run's final treeView (three rules: one VIOLATED, one ERROR, one
  VERIFIED). The violation is a loop bound the prover could not discharge, not a property
  violation. The ERROR is a `[3308]` pointer-analysis failure, which is why this directory also
  serves as the fixture for what that looks like.
- `assertion_failed/rule_output.json` — one rule's output from a different run: a genuine property
  violation, with the rule's logged values in the call trace. Its own run's treeView is not kept
  because every root in it came back ERROR (`docs/upstream-defects.md` P5), so nothing there
  reaches the parser's VIOLATED path.

`/home/runner/...` paths inside these files are the prover's own platform-tools build paths.
