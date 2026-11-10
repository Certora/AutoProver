> Where this recipe and the Solana/CVLR manual or other Certora materials diverge, this recipe governs.

### K14. Validating an accounts struct with `try_accounts` `[RULE]`

**Trigger:** a property enforced by the struct's `#[account(...)]` constraints (the signer, `mut`,
`has_one`, the owner) rather than by the handler body.

**A struct built by hand has skipped validation.** Every `#[account(...)]` constraint on it — the
signer, `mut`, `has_one`, the owner — is checked in the struct's generated `try_accounts`, and a
rule that builds the struct field by field and calls the handler never calls it. That is right for a property of the handler body. For a property the
*constraints* enforce, validate first and state it as "success implies":

The names are the example's (a `Deposit` struct with a `depositor` signer); use the program's.

```rust,ignore
use anchor_lang::Accounts as _;          // the trait `try_accounts` is on
use std::collections::BTreeSet;

let accounts = Box::new(cvlr_deserialize_nondet_accounts());
let mut rest: &[AccountInfo] = &accounts[..];
let mut bumps = crate::DepositBumps::default();
let mut reallocs: BTreeSet<Pubkey> = BTreeSet::new();
let mut ix = crate::Deposit::try_accounts(&crate::ID, &mut rest, &[], &mut bumps, &mut reallocs).unwrap();

let signed = ix.depositor.is_signer;   // what the constraint promised, read through the validated struct
clog!(signed as u64);
cvlr_assert!(signed);
core::mem::forget(accounts);
```

The assertion holds on every execution validation accepts, which is the rejection property read the
other way round, and `.unwrap()` keeps the failure path pruned, so the `[3308]` that error
formatting raises does not arise. Declare the rule's subject as
`crate::<AccountsStruct>::try_accounts`, or as the handler if the rule goes on to call it with that
`ix`. One constraint is out of reach this way: a `seeds`
constraint's verdict is unconstrained under the PDA summary (recipe K13).

**The names Anchor generates, and the one that looks right and is not.**

`#[derive(Accounts)]` and `#[program]` emit items no file contains, so searching the source for them
finds nothing and it is easy to conclude they are spelled some other way. They are not guessable and
they are not negotiable — each is a fixed rule over a name you already have:

| generated | rule |
|---|---|
| the bumps type | `<AccountsStruct>Bumps`, in **the struct's own module** |
| client-side accounts | `__client_accounts_<snake>` and `__cpi_client_accounts_<snake>` |
| the instruction discriminant | `instruction::<Handler in PascalCase>::DISCRIMINATOR` |

Two consequences worth having in front of you before you write a `Context::new`.

**The bumps type sits beside the struct, so whatever path reaches the struct reaches it** — append
`Bumps` to that path and nothing else changes. The `__client_accounts_*` modules are the trap: they
hold a struct with the *same field names* and `Pubkey` types, for Rust clients, and reaching for a
bumps type inside one is the specific mistake to avoid. Nothing a rule builds
lives in there.

**You never construct a bumps value.** Anchor writes a `Default` impl covering every field of it, so
`Default::default()` is not a convenience that usually works — it is always valid, and the fields it
fills are the cached PDA bumps, which a rule driving a handler directly has no way to know anyway.
