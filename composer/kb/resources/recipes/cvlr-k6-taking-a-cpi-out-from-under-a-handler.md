> Where this recipe and the Solana/CVLR manual or other Certora materials diverge, this recipe governs.

### K6. Taking a CPI out from under a handler `[EDIT]`

**Trigger:** a handler performs a CPI and the property is about its own bookkeeping; a rule stops on
`[3003]` with `alloc/src/rc.rs` in the trace; a VIOLATED rule over a value decoded from an
instruction's bytes.

Keep the handler and take the CPI out from under it, by asking the editor for a
`swap_import` on the name the call goes through. You write a stand-in with `invoke`'s signature and
return `Ok(())` from it; the editor points the program's `use` at it behind your feature. The call is
gone, and its instruction-building loop with it, so the handler's own bookkeeping is provable — with
the real `Context`, and validation if the struct comes from `try_accounts`, which is what descending
gives up.

**What the stand-in costs.** The Prover's own model of a cross-program call already moves nothing:
every value the call would change keeps its value from before it. So a stand-in returning `Ok(())`
costs no fidelity against that model, and it removes what the call drags in. Under either, a
property about the program's recorded state is sound. A property about the lamports or tokens the
CPI actually moved fails, and one that needs them *not* to have moved, or a total to stay constant
across the call, passes having checked nothing. The same holds for a rule that drives the handler
through its real `invoke` or `invoke_signed`, with no stand-in at all.

For token movements there is a model that does move them: a stand-in whose body calls
`cvlr_solana::token::spl_token_transfer`, `spl_mint_to` or `spl_burn` applies the effect to the
token accounts it is given. A rule asserting over a token account's balance or a mint's supply
across a CPI needs one. For lamports there is none (see below), so such a property is a skip.

**Writing the stand-in: two things that do not analyze.**

Both were measured on a handler that moves lamports with a System Program transfer, and each costs
a prover round to rediscover.

**Move nothing.** `AccountInfo::lamports` is an `Rc<RefCell<&mut u64>>`, and on an info the *handler*
built with `to_account_info()` the pointer analysis cannot establish provenance through the `Rc`:
reading or writing it is `[3308]`'s sibling, `[3003] dereference of a register with unknown
provenance`, with `alloc/src/rc.rs` in the trace. A stand-in that performs the transfer does not
analyze. Record what was asked for instead, and let the rule add the modelled effect in
arithmetically:

```rust,ignore
/// How many lamports the modelled transfer would have moved into `credited`.
fn modelled_credit(credited: &AccountInfo) -> u64 { .. }   // 0 unless the record names it
```

Being inert then buys a second property for free: any lamport change observed after the handler
returns is a **raw** mutation by the handler, which is how "no raw lamport mutation" gets checked.

**Decode nothing.** `system_instruction::transfer` builds its payload with
`Instruction::new_with_bincode`, into a heap `Vec<u8>`. The Prover does not relate the bytes bincode
wrote to the bytes you read back at a fixed offset, so every field you decode is *unconstrained* —
and the symptom is not an error but a VIOLATED verdict with a counterexample no path in the program
can produce (`amount = 0xfffffffffffff87e` against a decoded `0`), on every rule downstream of the
decoded value. Ask for a second `swap_import` on `system_instruction` and record `(from, to,
lamports)` as **values** where the handler passes them; have your `transfer` hand back an
instruction of the same shape, built by hand.

Between them these decide what the pair of stand-ins is for: the constructor's stand-in observes
what the handler *asked for*, and `invoke`'s observes that the instruction really was handed over
and whether the call succeeded — with success under the rule's control, so that "credit only after a
successful transfer" has an antecedent to bind.

Earlier runs reported a CPI changing fields of the caller's deserialized `Account<T>` that the handler
never writes. A direct probe does not reproduce that: the deserialized copy is untouched across an
`invoke`. If a counterexample shows it, report what the counterexample shows rather than assuming that
cause.
