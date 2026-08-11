# Why a proposed property turns out uncheckable

> **Status: guidance landed.** Measured on the klend run of 2026-08-10 — 411 properties proposed,
> 378 formalized, **33 declined** as unformalizable. Every one of the 33 was decided when the
> property was *worded*, one phase before anyone tried to check it.

## 1. What was declined, and why

The reasons the authoring sessions recorded are specific and mostly excellent — they name the exact
missing action or the exact field that carries no information. Read as a set, they fall into four
shapes plus one singleton:

| n | % | Shape |
|---|---|---|
| 10 | 30% | A per-call delta: "after X, Y changed" / "X must not lower Y" |
| 9 | 27% | A rejection or liveness claim, with no recorded outcome |
| 8 | 24% | Configuration or accounts the fixture cannot produce |
| 5 | 15% | The deciding quantity is never persisted |
| 1 | 3% | Visible only inside the transaction |

They concentrate: Flash Loans declined **11 of 19** proposed properties, Obligation Orders 8 of 21,
Oracle-Driven Refresh 7 of 33.

### 1.1 The per-call delta (10)

`elevation_group_switch_marks_obligation_stale`, `interest_accrual_idempotent_within_slot`,
`order_activation_requires_market_creation_flag`, `flash_ops_mark_reserve_stale`, …

A Crucible property is a predicate over on-chain accounts, evaluated **between** actions. It has no
pre-state, and — decisively — no knowledge of which instruction just ran. So "after `switch_group`
the obligation is stale" cannot be observed: an obligation is stale after almost every mutating
instruction and fresh after any refresh, and neither value is evidence about `switch_group`.

These are the recoverable ones. Nearly every property in this group protects a *standing relation*
that is checkable — the same run formalized `obligation_fresh_implies_all_reserves_fresh`, which is
the observable content of the stale-marking claim. What was lost was the wording, not the property.

### 1.2 The rejection or liveness claim (9)

`emergency_mode_blocks_set_obligation_order`, `order_cancellation_always_available_to_owner`,
`flash_borrow_gating`, `referrer_token_state_positional_misalignment`, …

A correctly rejected instruction and one that was never sent leave identical state — and a *panic*
and a returned `Err` are also identical, since both roll the transaction back. The fixture's
convention exists for exactly this: a negative action makes the attempt and records the outcome in
`accepted.<attempt>`, which the component's invariant then asserts on.

The causal detail that makes this a *property-authoring* problem: **the fixture is authored from
these properties.** A property that names its attempt concretely gets an action that makes it and
records it; one that says only "must be rejected" gets a generic action whose `bool` is discarded.
klend's reasons say this almost verbatim — *"`action_request_elevation_group` … discards the
outcome, setting no `accepted.*` flag"*.

### 1.3 Unreachable configuration (8)

`twap_and_heuristic_flags_integrity`, `flash_ixs_are_top_level_only`, `pyth_price_data_panic_dos`, …

Partly irreducible — the farms `UserState` account is genuinely not modeled — but several are the
sharper failure: the property constrains values **the harness itself writes**. `pyth_price_data_panic_dos`
would assert bounds on oracle bytes that only `action_set_oracle_price` produces;
`price_refresh_trigger_overflow_dos` would assert an overflow cannot happen, when the harness's own
`1 + v % 1_000_000` is what keeps it from happening. An assertion of that shape holds under every
possible implementation, including a badly broken one.

### 1.4 The unpersisted quantity (5)

`order_execution_bonus_may_exceed_declared_max`, `flash_loan_fee_is_always_charged`,
`flash_borrow_bypasses_borrow_permissioning`, …

The bounded value is computed inside the instruction and stored nowhere, or lands in an accumulator
that four other instructions feed indistinguishably. Detectable at wording time by asking which
account field a checker would compare.

Two of these five are **confirmed bugs**, not gaps — see `rust-applications.md` on the `// FINDING:`
route, which is where they belong.

## 2. What changed

`templates/backend_guidance.j2` — the only prose that reaches property extraction, which runs
*before* the fixture exists — gains the checker's shape and the four rules that follow from it:
prefer the standing relation over the per-call delta; name the account fields that decide it; name
the exact attempt for a rejection or liveness claim; constrain what the program writes rather than
what the harness writes. Plus the two facts worth knowing rather than working around (panic ≡ `Err`
after rollback; an intra-transaction excursion the same transaction restores is invisible).

Pinned by `prompts.rs::the_property_author_is_told_the_shape_of_the_checker_that_will_read_its_output`.

## 3. What this deliberately does not do

**It does not ask for fewer properties.** A declined property with a precise reason is a good
outcome — it tells a reader exactly what this suite does not cover, which is worth more than an
assertion that appears to cover it. The guidance changes how a property is *phrased* and what its
description carries, not whether it is proposed. If it works, the win shows up as properties moving
out of the declined list into the checked one, not as a shorter proposal list.

**It cannot fix §1.3's irreducible half.** No wording makes an unmodeled account observable. That is
a fixture-surface question, and the honest handling is the one klend used: decline it and name what
is missing.

**It is unverified against a live run.** Extraction is an LLM phase; this is prompt prose, and prose
can be ignored. The measurement above is the baseline to compare a rerun against — the number to
watch is the 10 in §1.1 and the 9 in §1.2, which are the ones a rewording can actually move.
