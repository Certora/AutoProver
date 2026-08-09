# CappedVault

A minimal on-chain vault. Each account may deposit funds into its own balance
and withdraw them later. The vault tracks a running total of all funds held.

## Behavior

- `deposit(amount)` adds `amount` to the caller's balance and to the global
  `totalDeposited` running total.
- `withdraw(amount)` removes `amount` from the caller's balance (and from
  `totalDeposited`), reverting if the caller's balance is insufficient.
- `balance(account)`, `totalDeposited()`, and `CAP()` are read-only views.

## Requirements

1. **Deposits must always succeed.** A user calling `deposit` must never have
   their deposit rejected; the vault must always accept incoming funds.

2. **No account may ever exceed the cap.** Each account's balance must never
   exceed `CAP` (1000). Any deposit that would push the caller's balance above
   the cap must be rejected.
