# CVLR and the Certora Solana Prover

What is true of CVLR, of the Solana Prover, and of the programs they run against, for any agent
working on a CVLR verification. It is written to the author, who writes the rules: "you" is the
author, and "the editor" is the agent that changes the program under them. A judge reviewing the
author's rules, or the editor itself, reads it as what the author was told. The recipes it names
(K4, K11, ...) are the author's; what each says is summarized here where it is named.

What it says about accounts, CPIs and account validation is for programs on the Solana SDK's
`AccountInfo`, Anchor included. A program on another framework, such as Pinocchio or jiminy, has
its own account type and no token model, and where your instructions give caveats for the
program's framework, those govern.

## What a rule is

Two forms, and the choice matters:

* `#[rule] fn rule_<name>() { ... }` — one rule. The attribute makes the function a prover entry
  point; the rule's name is the function's name.
* `cvlr_rules! { name: "<name>", spec: <spec>, bases: [base_a, base_b] }` — one *specification*
  (a `cvlr_spec!` of `requires` and `ensures`) checked against several base functions, which
  generates one rule per base (`<name>_a`, `<name>_b`, with a `base_` prefix stripped). Reach for
  this when one property genuinely applies across several handlers; each generated rule gets its own
  verdict and its own counterexample. Two things the macro does not do for you:
  - **A base is a harness function, not a handler.** It takes the accounts and the spec, assumes
    the spec's `requires`, calls one handler, and asserts its `ensures`. Write one per handler.
  - **The module must define `cvlr_impl_rule!`.** `cvlr_rules!` expands to one
    `cvlr_impl_rule!{rule_name, spec, base}` per base and leaves that macro to the calling module,
    so without it nothing compiles. It is what writes each `#[rule]`: build the nondet accounts and
    call `base(&accounts, spec)`. The spec expression is pasted into every rule, so it cannot name
    a local variable.

A rule body is: create nondeterministic inputs, assume the preconditions, call the program, assert
the postcondition.

**The code under test is the program's, always.** A rule that drives a function *you* wrote proves a
property of your function. A reader of the report cannot tell that apart from a verdict about the
program — the two look identical — and if your version and the program's have drifted by so much as
an operator, the report is wrong in the confident direction. Helpers, mocks, `Nondet` and `CvlrLog`
implementations all belong in the harness. The state transition the property is *about* does not.

The harness is a module *inside* the program's crate, so everything the program defines is reachable
as `crate::…` — `crate::<program module>::<handler>` — and a path rooted anywhere else names a
dependency.

A rule can drive a *library* function, verify cleanly, and read like
a verification of the program. `cvlr_assume!(!accounts[1].is_signer); cvlr_assert!(Signer::try_from(&accounts[1]).is_err());`
demonstrates that Anchor rejects a non-signer — true, and nothing whatever about the program you were
given. If a property is enforced by a dependency rather than by the program's own code, that is a skip
with that reason, not a rule about the dependency.

The program's own account validation is not a dependency. `#[derive(Accounts)]` generates
`try_accounts` for each accounts struct *inside the program's crate*, from the program's own
`#[account(...)]` declarations, and it is reachable as `crate::<AccountsStruct>::try_accounts`. A
property enforced by those declarations — the signer, `mut`, `has_one`, owner — is a property of the
program, and the rule for it drives that function. A rule that builds the accounts struct by hand
never runs validation at all, so it cannot demonstrate one.

If you cannot reach the program's own code, report that: name the program function and what stopped
the rule reaching it. It is never a reason to move the rule onto something you can reach.

## Counterexamples are only as legible as your `clog!`s

A violated rule comes back as a counterexample: the call trace, the failing assertion's message, and
its source location. The message says *which* assertion failed; the values it failed on are in the
trace only where a `clog!` put them there. **Log every value the assertion depends on, before the
assertion**, using `clog!`. A rule that fails with nothing logged tells you that something is wrong
and nothing about what; you will then spend a prover run — minutes — learning what one `clog!` would
have told you.

The harness the scaffold writes is `src/certora/mod.rs` and `src/certora/specs/mod.rs`, inside the
program's crate, and rules live in modules under `specs/`. If a type you assert over has no
`CvlrLog` implementation, write one in the module that holds the rules rather than formatting it by
hand. `CvlrLog` is CVLR's trait, so the orphan rule allows an impl only for a type the program's
crate defines. For a type from another crate — `Clock`, an SDK or Anchor type — that has none, log
its fields one by one.

## Assumptions are the sharpest tool and the easiest way to prove nothing

`cvlr_assume!` restricts the states the prover considers. That is how you exclude inputs the program
genuinely cannot receive — and it is also how a rule becomes vacuous, passing because no state
satisfies its preconditions at all. Two contradicting assumptions make every assertion true.

So: assume the *least* you can get away with. Every conf this backend builds sets `rule_sanity`,
so a rule whose assumptions leave no state at all is reported as vacuous rather than as a pass. That
check catches only the extreme. Assumptions that leave a sliver of states pass it and still cover
almost nothing, so if a rule passes immediately and you are surprised, read what it assumed before
believing it.

## Nondeterminism is the quantifier

`nondet()` produces an unconstrained value, and the prover reasons over *all* of them. That is what
makes a rule a proof rather than a test: you do not choose inputs, you constrain them. So a property
of the form "for all amounts" needs no loop and no sampling — one `let amount: u64 = nondet();` is
the universal quantifier.

**Never skip a property because it is universally quantified.** That shape is what this tool is for.

For account inputs, `cvlr_solana::cvlr_deserialize_nondet_accounts()` returns an array of sixteen
nondet `AccountInfo`s.

**Keep the account array alive to the end of the rule.** Build it in a `Box` and end the rule with
`core::mem::forget(accounts);`. Without that, the array's `AccountInfo`s are dropped after your last
assertion, the drop loop runs ahead of the vacuity check, and the rule is reported vacuous for code
that is not the program's. It is the most common cause of `SANITY_FAILED` on this backend. When
something borrows the array for the rest of the rule, as an Anchor accounts struct from
`try_accounts` does, `forget` cannot move it; leak it instead,
`Box::leak(Box::new(cvlr_deserialize_nondet_accounts()))`, which is never dropped either.

**A nondet account has a nondet data length, and that is what breaks rules.** The account machinery
gives you unconstrained bytes *and* an unconstrained `data_len()`. Every deserializer the program
runs on that account then has to cope with a buffer of unknown size, which is where the Prover stops
being able to help you. Three separate failures all come from it, and none of them names the cause:

| what you see | where it comes from |
|---|---|
| `Pointer domain: statically unknown length ... at sol_memcpy_` | serializing into `data.borrow_mut()[..]` |
| `[3308] illegal dereference of an absolute address` | unpacking a token account or mint |
| `unexpected pointer arithmetic` (an internal Prover error) | deserializing a state enum |

So **constrain the shape of every account your property touches, before anything reads it**:

```rust
// A mint with no Token-2022 extensions. One assumption, and the extension parser
// inside the program now has a length it can reason about.
cvlr_assume!(mint_info.data_len() == spl_token_2022::state::Mint::SIZE_OF);
```

**Constrain the shape; let the program do the parsing.** The rule says how big the account is, and
the handler unpacks it as it normally would. Do not deserialize account data *inside the rule* to
build up preconditions — `unpack`, `try_from_slice_unchecked` and `borsh::to_writer` in a rule body
are where these errors come from, and a precondition you established by parsing is one the program is
about to parse again anyway. Reach for a `data_len()` assumption first, every time.

This is also the honest reading of a success path that will not go through. If no execution of a
handler succeeds — every success rule comes back `SANITY_FAILED` while the rejection rules verify —
the usual cause is not that success is impossible but that nothing has pinned the accounts down
enough for it to be *reachable*. Add the shape assumptions before concluding anything about the
program.

**An enum input needs an impl, not a literal.** `nondet()` covers the scalars, and a struct you can
build field by field — but there is no way to write "some variant of this enum" inline. Implement
the trait in your harness module and dispatch on a nondet discriminant:

```rust
impl cvlr::nondet::Nondet for FeeType {
    fn nondet() -> Self {
        match nondet::<u64>() {
            0 => Self::SolReferral(nondet()),
            1 => Self::Epoch(nondet()),
            _ => Self::SolWithdrawal(nondet()),
        }
    }
}
```

The catch-all arm is the part that matters. A `match` whose every arm names a discriminant leaves
the remaining values unmapped, and the rule then quantifies over only the variants you listed —
which reads as a proof about the enum and is a proof about a subset of it.

## Loops are bounded, and the bound is not assumed away

The conf's `loop_iter` unrolls loops that many times, and it does **not** set `optimistic_loop`. So a
loop that can run longer than the bound comes back as a violated assertion reading *"Unwinding
condition in a loop"* — a real result, not a configuration accident: the prover is telling you it
cannot account for the remaining iterations.

`optimistic_loop` hides that by assuming every loop finishes within the bound, so it is the last
remedy, not the first. The remedies, in order (recipe K4): bound in the rule what sets the trip count;
summarize or munge the function holding a loop the property is not about; raise `loop_iter` to the
smallest count the property needs; and only then `optimistic_loop`, for a trip count no bound
discharges.

## Asking for a munge

When the program's own data is what the analysis cannot follow (`[3005]`, a `[3308]` on account
state that the `data_len()` assumption above did not clear, an internal error out of `ScalarDomain`,
or a job that fails with no per-rule verdicts), the remedy is usually a munge (recipe K11): a
`mock_fn` of a variable-length collection's accessors, or a `swap_derive` of a fixed-size state type
whose encoding is variable-length. A `[3308]` whose trace runs through error formatting — the
program's `#[error_code]` enum building its message — is not about the data: the remedy is a
summary of the formatting path, or a skip (recipe K10).

**A munge is the most consequential thing in this run, so treat asking for one that way.** It changes
the program the verdicts are about. Prefer, in order: a different rule; a summary, if the code in the
way is not what the property depends on; then the editor. Read the diff it hands back before
accepting it.

The editor can refuse, and a refusal is information. If what a property needs is none of its nine
kinds — a hand-unrolled loop, a restructured state machine, a shrunk collection — the editor will
say so and name the kind it would have taken. Skip the property and pass that on. That is a decision for a
person, and naming it is what makes the skip actionable.

A munge is scoped to your unit. It is in your copy of the program and no other unit's, so it applies
to your rules and to nothing else, and a sibling unit editing the same function is a separate edit
rather than a conflict. You never have to coordinate with another unit about one. It is also gated on
your unit's cargo feature, so the user can apply it to their own tree and still build what they
shipped.

## Arithmetic, and why panic-freedom is not on the menu

Arithmetic correctness is a real property class here, and a stronger one than on EVM: rules reason
over exact integers, so "the fee is exactly one percent" or "supply is conserved" is stated directly.
Use `NativeInt` (`cvlr::mathint`, an unsigned 256-bit integer) when you need arithmetic without a
`u64` or `u128` overflow in the way. Two things about it are easy to get wrong:

* **Where a bound is lost.** `NativeInt::from(x)` of a `u64` keeps `x`'s value, and with it the
  bound. A `NativeInt` the rule creates itself — from `nondet()`, or as a lemma's free context —
  ranges up to 2^256, so a quantity the program stores as a `u64` needs `cvlr_assume!(x.is_u64())`
  where it is created. The section on nonlinear arithmetic below says what leaving it out costs.
* **Converting back assumes the fit.** `u64::from` and `.into()` to `u128` *assume* the value fits,
  so an execution that would overflow is removed, not reported. Where overflow is the property,
  check `is_u64()` or `is_u128()` yourself before converting.

**"This handler cannot be made to panic" is not something you can check, and you should not try.**
A panic reverts: on Solana a panic — like a returned `Err` — aborts the instruction and the runtime
rolls back every account mutation. So a panicking path cannot violate an invariant: there is no
surviving state for the invariant to be false about. Pruning those paths is not a gap in the
analysis, it is the analysis agreeing with the runtime. A reachable panic is an *availability*
concern, not a state-integrity one, and it is not a finding you can produce from a rule. The
Prover's switch for reporting panics as violations is off in this backend's conf, and the author's
config tool cannot turn it on.

So do not write a rule asserting a handler never panics, and do not report a panicking path as an
attack. If a property in your batch asks for panic-freedom, skip it and say that a panic reverts, so
the property is not about state the program can be left in.

One thing this makes *more* important rather than less: if an unchecked operation can **wrap** rather
than panic, that is a genuine state-integrity bug and it is checkable — assert the post-state equals
the mathematically correct value and the rule fails on the wrapping input. Whether a build wraps or
panics is a profile setting, so read it rather than assuming. The Prover's build is a release build,
so the setting is `overflow-checks` under `[profile.release]` in the *workspace root's*
`Cargo.toml` (Cargo ignores a profile in a member crate's manifest), and it is off unless set.

## When the Prover cannot finish: nonlinear arithmetic

A rule can be correct, compile, and still never produce a verdict: the job reports it at a low
completion percentage — 1%, 4% — after splitting it into many sub-problems, and HALTs on its global
timeout. Waiting will not change that. The usual cause is **nonlinear** arithmetic: a product or
quotient of two values that are *both* symbolic, such as `price * amount` where the Prover chose
both. One symbolic operand against a constant is fine. `NativeInt` takes the arithmetic off
bitvectors but does not make a product linear.

There are two sources, and they take different answers (recipe K15):

* **The program's arithmetic helpers** (a `SafeMath` trait, a `u256` module, fixed-point scaling):
  ask the editor to `redirect_module` them onto `NativeInt`. Overflow is then unreachable in the
  substitute, so a "cannot overflow" property is gone rather than proved.
* **The property's own products** (solvency, exchange rates): bound every operand the rule creates
  as a `NativeInt` by what the program enforces, then, if it still does not finish, prove the algebra once in a `cvlr_lemma!` and
  `apply` it in the heavy rule. An unbounded operand is a soundness problem, not a speed one: it
  yields counterexamples on states the program cannot reach.

A rule that still does not finish is skipped, naming the timeout and what was bounded and lifted. A
rule with no verdict is not a verification.
