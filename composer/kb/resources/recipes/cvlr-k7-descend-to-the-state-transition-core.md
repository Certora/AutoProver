> Where this recipe and the Solana/CVLR manual or other Certora materials diverge, this recipe governs.

### K7. Descending to the program's state-transition core `[RULE]`

**Trigger:** a handler-level rule is blocked by a CPI, by account validation, or by error formatting
the pointer analysis refuses, and the program has an accounting function below the handler.

Descend to **the program's own state-transition function** — the accounting core the
handler wraps — and drive it with `nondet()` domain structs rather than accounts:

```rust,ignore
// The accounting core, driven directly. Only the fields this transition reads need to be
// nondeterministic — `nondet()` is implemented for scalars, not for `Pubkey`.
let mut vault = VaultState { authority: Pubkey::default(), balance: nondet(), bump: nondet() };
let amount: u64 = nondet();
let before = vault.balance;
let split = crate::vault_accounting::apply_withdrawal(&mut vault, amount, true).unwrap();
clog!(before, amount, split.fee);
cvlr_assert!(before - vault.balance == amount);
```

No `Context`, no accounts struct, no CPI in the way. This is what shipped verification projects do for
exactly this reason.

Three conditions on it, and they are what separate this from the failure it resembles:

* **The function must be the program's.** Descending to `crate::<module>::<fn>` is narrowing scope;
  writing your own version of that function in this module is not descending, it is reimplementing,
  and it produces a verdict about your code. That is never acceptable, at any depth.
* **Declare it.** The rule's declared subject is that function, not the handler, and the report says
  so.
* **Say what it costs, in the commentary.** Account validation, the CPI's effects and Anchor's
  dispatch are outside a rule written this way. A bug in the handler's own glue is out of scope, and
  a reader has to be told that rather than left to assume instruction-level coverage.

If the handler has no such core to descend to — the accounting *is* the handler — then the property
is not reachable today. Skip it and say so.
