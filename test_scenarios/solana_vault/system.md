# Lamports Vault

A minimal Solana program (Anchor) that lets a user custody SOL in a program-derived vault.

## Overview

Each user owns a **vault**, a PDA derived from `["vault", authority]`. The vault records its
`authority` (the owner) and a `balance`. There is one program, `vault`.

## Instructions

- **initialize** — creates the caller's vault PDA, sets `authority` to the caller, and `balance`
  to 0. A given authority has exactly one vault; initializing twice must fail.
- **deposit(amount)** — transfers `amount` lamports from the depositor into the vault (via a
  System Program transfer) and increases `balance` by `amount`. Anyone may deposit into any vault.
- **withdraw(amount)** — transfers `amount` lamports from the vault back to the authority and
  decreases `balance`. Only the vault's `authority`, who must sign, may withdraw, and only up to
  the recorded `balance`.

## Requirements

- Only the account recorded in `vault.authority` (and it must sign) can withdraw from that vault.
- A withdrawal never removes more than the vault's recorded `balance`.
- The vault's recorded `balance` tracks the net of deposits minus withdrawals and never
  underflows or overflows.
- The vault PDA is always the canonical PDA of `["vault", authority]` for its recorded authority.

## Actors

- **Vault authority** — a user keypair; the owner of a vault and the only withdrawer.
- **System Program** — the standard Solana program used to move lamports on deposit.
