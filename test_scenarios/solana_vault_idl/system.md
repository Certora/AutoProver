# Lamports Vault (fee-sharing)

A minimal Solana program (Anchor) that lets a user custody SOL in a program-derived vault, with an
optional fee share on withdrawal.

## Overview

Each user owns a **vault**, a PDA derived from `["vault", authority]`. The vault records its
`authority` (the owner) and a `balance`. There is one program, `vault`.

## Instructions

- **initialize** — creates the caller's vault PDA, sets `authority` to the caller, and `balance`
  to 0. A given authority has exactly one vault; initializing twice must fail.
- **deposit(amount)** — transfers `amount` lamports from the depositor into the vault (via a
  System Program transfer) and increases `balance` by `amount`. Anyone may deposit into any vault.
- **withdraw(amount)** — moves `amount` lamports out of the vault and decreases `balance` by
  `amount`. Only the vault's `authority`, who must sign, may withdraw, and only up to the recorded
  `balance`. A **fee collector account may optionally be supplied**: when it is, 1% (100 bps) of the
  withdrawal is paid to it and the remainder to the authority; when it is not, the authority
  receives the whole amount. The vault gives up `amount` either way.

## Requirements

- Only the account recorded in `vault.authority` (and it must sign) can withdraw from that vault.
- A withdrawal never removes more than the vault's recorded `balance`.
- The vault's recorded `balance` tracks the net of deposits minus withdrawals and never
  underflows or overflows.
- A withdrawal conserves lamports: what leaves the vault equals what the authority and the fee
  collector together receive.
- Supplying a fee collector never lets the authority receive *more* than it would without one.
- The vault PDA is always the canonical PDA of `["vault", authority]` for its recorded authority.

## Actors

- **Vault authority** — a user keypair; the owner of a vault and the only withdrawer.
- **Fee collector** — an optional account credited a share of each withdrawal.
- **System Program** — the standard Solana program used to move lamports on deposit.
