# CVLR and the Certora Soroban Prover

What is true of CVLR, of the Soroban Prover, and of the contracts they run against, for any agent
working on a CVLR verification of a Soroban contract — the author who writes rules and the judge who
reviews them.

Nothing here is about one agent's tools or one run's configuration. Those belong to whichever agent
has them, and are in that agent's own instructions.

## What a rule is

A rule is a function carrying **`cvlr_soroban_derive`'s** `#[rule]` attribute, and the import is not
a detail:

```rust
use cvlr_soroban_derive::rule;
```

`cvlr` has a `rule` attribute of its own, in its prelude, and a `cvlr_rules!` macro, and both are the
wrong ones here: they record the rule in a section the Soroban Prover does not read, so a harness
using them compiles and then has nothing to check. The build lists the rules from the section the
prover does read, so such a rule is missing from that list — a rule you wrote that the build does not
list is not a rule.

The rule's name is the function's name, and **its parameters are its arbitrary inputs**: the prover
treats every argument as unconstrained. So `e: Env` is an environment over arbitrary contract
storage, and `amount: i128` is every `i128` at once. A rule body is: constrain the inputs, read the
state the property is about, call the contract, read the state again, assert.

```rust
#[rule]
fn transfer_moves_exactly_the_amount(e: Env, from: Address, to: Address, amount: i128) {
    cvlr_assume!(from != to);
    let from_before = Token::balance(e.clone(), from.clone());
    let to_before = Token::balance(e.clone(), to.clone());
    clog!(from_before, to_before, amount);
    Token::transfer(e.clone(), from.clone(), to.clone(), amount);
    cvlr_assert!(Token::balance(e.clone(), from) == from_before - amount);
    cvlr_assert!(Token::balance(e, to) == to_before + amount);
}
```

`Token` there is the example's; use this contract's own contract struct (the one carrying
`#[contract]`) and its functions, which you call directly as associated functions with the
environment — the way the contract's own unit tests do, minus the test client. There is no
dispatcher to go through. Pass the environment the way each function's signature takes it:
`e.clone()` for `e: Env`, the usual form, and `&e` for `e: &Env`.

**One rule per property and function.** The CVLR release this crate builds against has no form that
instantiates one specification across several functions, so a property that ranges over several
functions is several rules — name them so the family is obvious (`solvency_after_deposit`,
`solvency_after_withdraw`).

**The code under test is the contract's, always.** A rule that drives a function *you* wrote proves
a property of your function. A reader of the report cannot tell that apart from a verdict about the
contract — the two look identical — and if your version and the contract's have drifted by so much
as an operator, the report is wrong in the confident direction. Helpers and `Nondet` implementations
belong in the harness. The state transition the property is *about* does not. Everything the
contract defines is reachable as `crate::…`, since the harness is a module inside the contract's
crate; a path rooted anywhere else names a dependency. If a property is enforced by a dependency
rather than by the contract's own code, that is a skip with that reason, not a rule about the
dependency.

## Storage, and what a rule may say about it

`e.storage()` inside a rule is the same storage the contract reads, and it starts out arbitrary. You
may read it — through the contract's own getters by preference, since those are what a property is
about — and you may shape it before the call:

* `cvlr_assume!(e.storage().persistent().has(&key))` restricts the rule to states where an entry
  exists; `!has` to states where it does not. This is how "a fresh account" or "an initialized
  contract" is stated.
* Writing an entry before the call (`e.storage().instance().set(&key, &value)`) fixes it outright.
  Stronger than an assumption, so use it only for state the property is conditioned on, and say so.

Use the contract's own storage-key type and keys, read from its source. A key spelled differently
from the contract's is a different entry, and the rule then quantifies over state the contract never
reads.

A claim quantified over *every* entry of a kind — "the sum of all balances equals the supply" — has
no rule, because no single read sees them all. Restate it as what each function does to the entries
it touches: a transfer moves one balance down by exactly what it moves the other up.

## Calls into other contracts

The prover does not follow a call into another contract: the call returns an arbitrary value and
changes nothing there. Through a real client, two reads of one balance on another contract are two
unrelated numbers. A property about what another contract did — a token balance after a transfer,
say — therefore cannot be proven against the real client. Skip it and say why, state the property
over this contract's own storage, or — for SEP-41 token clients — set the contract up with the token
model, which replaces the client under the `certora` feature with an in-process model whose balances
a rule can read. A verdict that reads the model assumes each token is a standard SEP-41 token — no
fee on transfer, no hook, no call back into the contract — and the rule's commentary must say so.

A property about *which* contract this contract calls, with what arguments, is this contract's own
code and is checkable without any model.

## Authorization

The host enforces `require_auth`; which address a function requires, and whether it requires one at
all, is the contract's code and is what an access-control property checks. `cvlr_soroban::is_auth`
is the model of "this address authorized the current invocation".

**State a rejection property as a condition a successful call must have satisfied**: record the
condition, make the call, assert the condition held.

```rust
#[rule]
fn only_the_admin_can_mint(e: Env, to: Address, amount: i128) {
    let admin = /* the admin, read through the contract's own getter or storage key */;
    let authorized = is_auth(admin);
    clog!(authorized);
    Token::mint(e, to, amount);
    cvlr_assert!(authorized);
}
```

The prover only ever sees calls that return — a call that panics or errors ends the path — so the
assertion is checked on exactly the successful mints, and it fails if any of them happened without
the admin's authorization.

You will see the other spelling in Certora's own Soroban specifications: assume the condition is
violated, make the call, `cvlr_assert!(false)`. It says the same thing, and it is **the wrong one
here**: those specifications run under a conf with no vacuity check, and this backend's conf runs
one. A rule every one of whose paths ends inside the failing call has no reachable end, which is
precisely what the check reports as vacuous — so the rule comes back failed however right it is.

`cvlr_satisfy!(true)` after a call shows that *some* execution reaches that point — a reachability
check. It does not show the call succeeds for every input you meant, and mapped to an acceptance
property it is a false verification.

## Counterexamples are only as legible as your `clog!`s

A violated rule comes back as a counterexample, and what you can see of it is exactly what you
logged. **Log every value the assertion depends on, before the assertion**, using `clog!`. A rule
that fails with nothing logged tells you that something is wrong and nothing about what; you will
then spend a prover run — minutes — learning what one `clog!` would have told you. This is the
single highest-return habit on this backend.

`clog!` takes the values that implement CVLR's logging trait — integers, booleans, and `Option`s and
`Result`s of them. An `Address` or a `Map` is a host handle, not a value, so log what you compare
instead: the balance, the flag, the result of the equality. If you need a type of your own logged,
implement the logging trait in your own harness module, checking the CVLR crates' source for its
current spelling.

## Assumptions are the sharpest tool and the easiest way to prove nothing

`cvlr_assume!` restricts the states the prover considers. That is how you exclude inputs the
contract genuinely cannot receive — and it is also how a rule becomes vacuous, passing because no
state satisfies its preconditions at all. Two contradicting assumptions make every assertion true.

So: assume the *least* you can get away with. Every conf this backend builds sets `rule_sanity`, so
a rule whose assumptions leave no execution at all is reported `SANITY_FAILED` rather than as a
pass. The check catches only the extreme — assumptions that leave a sliver of states pass it and
still cover almost nothing — and it has a blind spot: it runs after the prover's optimizer, so a
contradiction the optimizer resolves by itself escapes it. If a rule passes immediately and you are
surprised, read what it assumed before believing it, and a `cvlr_satisfy!` reachability rule over
the same assumptions is the check that covers what the sanity pass may miss.

## Nondeterminism is the quantifier

A rule's parameters, and `nondet()` inside its body, produce unconstrained values, and the prover
reasons over *all* of them. That is what makes a rule a proof rather than a test: you do not choose
inputs, you constrain them. So a property of the form "for all amounts" needs no loop and no
sampling — one `amount: i128` parameter is the universal quantifier.

**Never skip a property because it is universally quantified.** That shape is what this tool is for.

**Arbitrary SDK values** come from `crate::certora::soroban_nondet`, which setup wrote for this
contract: `nondet_address()`, `nondet_symbol()`, `nondet_string()`, `nondet_bytes()`,
`nondet_bytesn_32()` (also `_20`, `_64`, `_65`), `nondet_u256()`, `nondet_i256()`,
`nondet_timepoint()`, `nondet_duration()`, `nondet_i128()`, `nondet_u64()`, `nondet_bool()` and the
other integers. A collection takes a thunk for its elements and is at most five long, because the
prover unrolls the loop that fills it: `nondet_vec_internal(|| nondet_address())`,
`nondet_map_internal(|| nondet_address(), || nondet_i128())`.

**The contract's own types already have arbitrary values.** Setup appended an `impl Nondet` for
every `#[contracttype]` and `#[contracterror]` it could build, each reaching every variant and every
field, so `let config = Config::nondet();` (with `use cvlr::nondet::Nondet;`) is a config of any
shape the type allows. Use those rather than writing your own. A type it could not build has none —
a generic type, a type from another crate, a recursive type, or one that contains one of those — and
the compiler says so when you call `nondet()` on it.

**A type without one needs an impl, not a literal.** Implement the `Nondet` trait in your harness
module and dispatch on a nondet discriminant, with a catch-all arm:

```rust
impl cvlr::nondet::Nondet for Role {
    fn nondet() -> Self {
        match cvlr::nondet::nondet::<u64>() {
            0 => Self::Admin,
            1 => Self::Minter,
            _ => Self::Holder,
        }
    }
}
```

The catch-all arm is the part that matters. A `match` whose every arm names a discriminant leaves
the remaining values unmapped, and the rule then quantifies over only the variants you listed —
which reads as a proof about the enum and is a proof about a subset of it.

## Loops are bounded, and the bound is not assumed away

The conf's `loop_iter` unrolls loops that many times, and it does **not** set `optimistic_loop`. So
a loop that can run longer than the bound comes back as a violated assertion reading *"Unwinding
condition in a loop"* — a real result, not a configuration accident: the prover is telling you it
cannot account for the remaining iterations.

1. **Constrain what determines the trip count.** Usually the loop is over a `Vec` or `Map` whose
   length is nondeterministic, so `cvlr_assume!` a bound on that length. This is the honest fix: the
   rule then states the bound it proves under, and the report says so.
2. **Raise `loop_iter`**, when the real trip count is a known small constant the default does not
   reach. The bound is sound while `optimistic_loop` is off: too low shows up as a violation, never
   as a weaker green.
3. **Say the bound is too low, and why.** If the property genuinely needs more iterations than is
   affordable, that is worth reporting as a skip with the count you needed — not worked around.
   `optimistic_loop` assumes every loop finishes within the bound; it is unsound, every verdict in
   the job then depends on it, and it is the last resort, with the reason recorded.

An unwinding violation reported as a property failure is a wrong answer, so read the counterexample
before concluding the contract is at fault: if the failing assertion is the unwinding condition
rather than your own, the rule never reached its property. The SDK's own code loops too — a
`TokenClient` call or an XDR serialization can put a loop under the rule that no assumption about
the contract's data bounds.

## Arithmetic, and why panic-freedom is not on the menu

Arithmetic correctness is a real property class here: the prover checks the contract's arithmetic
exactly, so "the fee is exactly one percent" or "supply is conserved" is stated directly. State it
in the contract's own integer type. `cvlr::mathint::NativeInt` does **not** help with Soroban
amounts on this CVLR line: it converts only from unsigned integers, and amounts are `i128`. Where a
postcondition's own arithmetic could overflow — `to_before + amount` — assume it cannot, in the
contract's type (`cvlr_assume!(to_before.checked_add(amount).is_some())`), and `clog!` the operands.

Multiplying or dividing two values that are *both* symbolic is **nonlinear**, and nonlinear integer
arithmetic is where the solver stops being complete; a rule whose statement multiplies two
unconstrained amounts may never finish. Bound the operands by what the contract itself guarantees,
and `clog!` those bounds so a reviewer sees the domain you proved over. A bound is a soundness step,
not a speed one: without it the prover explores values the contract cannot hold, and the
counterexample it returns is indistinguishable from a real defect.

**"This function cannot be made to panic" is not something you can check, and you should not try.**
A contract that panics — or returns an error — fails the invocation, and the host rolls back every
storage write it made. So a panicking path cannot violate an invariant: there is no surviving state
for the invariant to be false about. The prover prunes those paths to agree with the host, and
treating traps as violations instead is a conf-wide switch, not a per-rule one. If a property asks
for panic-freedom, skip it and say that a panic reverts, so the property is not about state the
contract can be left in.

One thing this makes *more* important rather than less: if an operation can **wrap** rather than
panic, that is a genuine state-integrity bug and it is checkable — assert the post-state equals the
mathematically correct value and the rule fails on the wrapping input. Whether a build wraps or
panics is a profile setting (`overflow-checks`), so read the workspace manifest rather than
assuming.
