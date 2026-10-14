> Where this recipe and the Solana/CVLR manual or other Certora materials diverge, this recipe governs.

### K3. Sysvars: let the handler read them, and tie the account to the syscall `[RULE]`

**Trigger:** a handler reads `Clock` or `Rent`, through the syscall (`Clock::get()`,
`Rent::get()`) or from a sysvar account (`Clock::from_account_info(info)?`), and you are about to
work around it: split the handler so the sysvar arrives as a parameter, mock the read, or skip a
property because the read "is external".

**Formula:** none of those. Both forms are analyzed. Drive the handler as it is. When a rule needs
the account and the syscall to agree, say so with one assumption:

```rust,ignore
let accounts = Box::new(cvlr_deserialize_nondet_accounts());
let clock_info = &accounts[2];
cvlr_assume!(*clock_info.key == sysvar::clock::id());
let from_account = Clock::from_account_info(clock_info).unwrap();
cvlr_assume!(from_account.epoch == Clock::get().unwrap().epoch);

crate::processor::Processor::process_withdraw(&accounts, amount).unwrap();
```

`from_account_info` performs the address check itself, so a property about which account is the
sysvar ("the clock must be the canonical Clock sysvar") is a property of the program, stated by
the contrapositive: call the handler with `.unwrap()` and assert the key.

**On the split SDK 3 crates** (`solana-account-info` 3, no `solana-program`) the same holds, spelled
differently:

- `from_account_info` is on `solana_sysvar::SysvarSerialize` (the `bincode` feature of
  `solana-sysvar`), not on `Sysvar`. Import that trait to call it.
- The sysvar's address is `Clock::id()`, from `solana_sysvar_id::SysvarId`.

Current programs usually read sysvars only through the syscalls (`Clock::get()`, `Rent::get()`), and
stake history through `StakeHistorySysvar`, whose generic `sol_get_sysvar` read is modelled too. A
handler that reads them that way needs no account at all.

**What still needs care: `Rent::minimum_balance` computes in `f64`.** The float builtins are
summarized as unconstrained, so two calls with the same length can return different values. Do not
assert that a stored reserve *equals* a fresh `minimum_balance`. Where the program stores the
reserve it computed (a stake account's `meta.rent_exempt_reserve`), state the property over the
stored value, which is what solvency is defined against anyway. If a property genuinely needs the
schedule, an integer stand-in for `minimum_balance` is acceptable: it models the runtime's
configuration, not the program's logic. Say so in the rule's commentary.

A program that recomputes the reserve from `Rent` at each instruction, rather than storing it, has no
stored value to state the property over. Stake on SDK 3 is one: `meta.rent_exempt_reserve` is
deprecated there and ignored. Then the integer stand-in is the way to state it, not a fallback.

**Failure symptom this replaces:** a skip or a munge whose reason says `Sysvar::from_account_info`
is "treated as external" and never returns `Ok`. That was a tuning-file defect, since fixed. If the
Prover's report lists it as external again, record that as the reason and stop. Do not rewrite the
handler around it.

**What this rests on:** Stake Pool's specs (`cvlr_initialize_rent_account`, the epoch props) and
P-Token's (`cvlr_initialize_rent_account`, `cvlr_assume_rent_defaults`) read both sysvars this way.
A Prover probe against the stake program verified that `Clock::from_account_info` and
`Rent::from_account_info` succeed on the sysvar's account, and that a successful
`Clock::from_account_info` implies the canonical key. A second probe, against stake on the SDK 3
crates, verified that `Clock::get()` and `Rent::get()` succeed and agree across two reads, and that
`StakeHistorySysvar` can return an entry.
