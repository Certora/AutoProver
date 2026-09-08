# The state behind the bytes

> A seventh munge kind, proposed. The six in
> [who-edits-the-program.md](who-edits-the-program.md) §8.4 annotate or split *functions*; this one
> changes how a *type* is serialized, which is the only thing that reaches a class of Prover failure
> the CVLR backend has now hit on every run against SPL stake-pool.
>
> **Status: design, not built — but the technique is now measured.** §10 has the probe: the same
> rule fails with the shipped derives and **verifies, non-vacuously, with them swapped**. That is
> the first rule this backend has verified about stake-pool at all.
>
> §6 is still the part to argue with — the kind is unsound in ways the existing six are not, and
> the checks in §7 are what would have to carry that.

---

## 1. The failure this is for

Runs 7 and 8 formalized the same three authorization properties of stake-pool's admin instructions —
`set_manager`, `set_fee`, `set_staker`. Six prover submissions between them, **zero verified rules**,
three distinct symptoms:

| symptom | seen in |
|---|---|
| `[3005] memcpy with dynamically sized length` | run 7 |
| `[3308] illegal dereference of an absolute address` | run 7 |
| `java.lang.IllegalStateException: Check failed.` at `sbf.domains.SbfType$NumType.castToPtr` | runs 7, 8 (×3) |

None is a rule defect and no rewording clears them. All three come from one place, and it is three
lines long. Every one of these handlers does exactly this and nothing else dynamically sized:

```rust
let mut stake_pool = try_from_slice_unchecked::<StakePool>(&stake_pool_info.data.borrow())?;
// ... the authorization checks the property is actually about ...
borsh::to_writer(&mut stake_pool_info.data.borrow_mut()[..], &stake_pool)?;
```

`StakePool` carries `Option<Pubkey>` and `FutureEpoch<Fee>` fields, so its borsh encoding is
*variable length*. The write is a memcpy whose length the analysis cannot pin down, and the read is
pointer arithmetic over a buffer whose parse it cannot follow. Run 8 constrained
`data_len() == get_packed_len::<StakePool>()` on every rule and it changed nothing, because the
length that defeats the analysis is the encoding's, not the account's.

**This is not the `BigVec` problem**, which is worth stating because I assumed it was and was wrong
(see §8). None of these three handlers touches the validator list at all.

## 2. Why none of the six reaches it

`early_panic`, `mock_fn`, `inline_never`, `hook_on_entry`, `hook_on_exit` and an extraction all
address *a function*: annotate it, replace it, keep its symbol, observe it, split it. The code in the
way here is not a function anybody wrote. It is a **derived trait implementation** — the
`BorshSerialize`/`BorshDeserialize` impls that `#[derive(...)]` generates on the struct — and there
is no function name to name.

`mock_fn` on `try_from_slice_unchecked` is the near miss, and it fails on generics: it is
`try_from_slice_unchecked<T>`, shared by `StakePool`, `ValidatorList` and `StakeStateV2`, so a
stand-in for it is a stand-in for all of them.

## 3. What the normative verification does

`certora-cvlr-kb`'s `stake-pool` project (spec branch `certora-ci/sep-2025`, tier normative) verifies
these handlers. It uses **two different techniques for two different shapes**, and conflating them is
the mistake §8 records:

**A fixed-size struct behind borsh — replace the encoding.**

```rust
// state.rs, the munge:
#[cfg_attr(not(feature = "certora"), derive(BorshDeserialize, BorshSerialize))]
#[cfg_attr(feature = "certora", derive(Copy))]
#[derive(Clone, Debug, Default, PartialEq, BorshSchema)]
pub struct StakePool { ... }
```

```rust
// certora/stake_pool.rs, hand-written:
pub static mut STAKE_POOL: *mut StakePool = std::ptr::null_mut();
pub fn init_global_stake_pool() { unsafe { STAKE_POOL = alloc_havoced::<StakePool>() } }

impl borsh::ser::BorshSerialize for StakePool {
    #[early_panic]
    fn serialize<W: std::io::Write>(&self, _w: &mut W) -> std::io::Result<()> {
        *get_global_stake_pool_mut() = *self;
        Ok(())
    }
}
impl borsh::de::BorshDeserialize for StakePool {
    fn try_from_slice(_v: &[u8]) -> borsh::io::Result<Self> { Ok(*get_global_stake_pool()) }
    // ... deserialize / deserialize_reader / try_from_reader, all the same
}
```

The program's own `try_from_slice_unchecked` and `borsh::to_writer` calls are **untouched**. They now
read and write one havoc'd, fixed-layout value in memory. No byte buffer, so no memcpy, no pointer
arithmetic over a parse, and nothing for the scalar domain to cast — all three symptoms die at the
source rather than being routed around. `derive(Copy)` is what makes `*self` and `*get_global()`
work, and `alloc_havoced` (in our pinned `cvlr-nondet 0.6.1`, `havoc.rs:26`) is what keeps the value
unconstrained.

**A variable-length container — mock its accessors.** `ValidatorList`/`BigVec` is *not* globalized;
it keeps real borsh and its `find`/`find_mut`/`retain` are `mock_fn`'d instead. That is the existing
`mock_fn` kind and needs nothing new.

## 4. What makes the new kind small

The technique looks like three coordinated edits. Only one of them has to be a program edit:

| piece | who |
|---|---|
| swap the derives on the struct | **the editor** — two impls of one trait cannot coexist, so the derive must go |
| the global, its init, the trait impls | **the author**, in its own harness module |
| calling init at the top of each rule | **the author** |

`StakePool` and the author's module are in the same crate, so Rust's orphan rule permits the author
writing `impl BorshDeserialize for StakePool` itself — exactly as it already writes `mock_fn`
stand-ins. The target crate has no `forbid(unsafe_code)`, only `deny(missing_docs)`, which the
scaffold's files already satisfy.

So the seventh kind is **not** "replace serialization". It is one narrow thing:

> **`swap_derive`** — under this unit's feature, remove named derives from a type and optionally add
> others, so the harness can supply the impls instead.

## 5. The record

Shaped after `FunctionExtraction`, which is the precedent for a kind that rewrites rather than
annotates:

```python
@dataclasses.dataclass(frozen=True)
class DeriveSwap:
    path: str                      # the file the type is defined in
    type_name: str                 # "StakePool"
    removed: tuple[str, ...]       # ("BorshDeserialize", "BorshSerialize")
    added: tuple[str, ...]         # ("Copy",)
    original: str                  # the pristine derive attributes, verbatim
```

`original` is content-addressed for the same reason the extraction's is: a replay onto source that
has moved must report `SourceDrifted` rather than rewrite whatever is there now.

Rendered as the gated pair — the derives that are *not* being swapped stay on an ungated `derive`,
so the deployed build is byte-identical:

```rust
#[cfg_attr(not(feature = "unit_x"), derive(BorshDeserialize, BorshSerialize))]
#[cfg_attr(feature = "unit_x", derive(Copy))]
#[derive(Clone, Debug, Default, PartialEq, BorshSchema)]
pub struct StakePool { ... }
```

A `cfg_attr` is sound here where it was not for an extraction: a derive list is a single attribute,
so gating it cannot leave two definitions of one name.

**The swap is transitive, and the record above does not yet say so.** `derive(Copy)` on a struct
requires every field type to be `Copy`, so one request cascades: the probe in §10 could not compile
until `AccountType` was swapped too, which is exactly why the normative verification carries a
second `cfg_attr(feature = "certora", derive(Copy))` on that enum. `Fee` and `FutureEpoch` already
derived `Copy` and needed nothing, so the closure is not the whole field graph — it is the non-`Copy`
subset of it, which rustc computes for free and reports one type at a time.

Two ways to carry that, and it is an open design question rather than a settled one: record a
*set* of `(type, removed, added)` triples as one munge, so the cascade is one reviewable unit; or
have the editor compute the closure and record each type separately, which reads better in a diff
and loses the fact that they stand or fall together. The first is probably right, because reverting
half a cascade leaves a program that does not build.

## 6. What it erases — the part to argue with

The six existing kinds are all defensible in one line. This one is not, and the charter has to say so
rather than leaving the judge to notice.

1. **Encoding properties become unstatable, and may pass vacuously.** "A truncated account is
   rejected", "the account is exactly N bytes", "a field survives a round trip" — after the swap,
   deserialization cannot fail and does not read the buffer. A rule asserting rejection of malformed
   data proves nothing. This is the hazard §7.6.2 already records for a probe that verified while
   doing the wrong thing.
2. **One global per type aliases every account of that type.** A handler reading two `StakePool`
   accounts sees one value. For these three handlers there is exactly one, which is why the
   normative verification gets away with it. **This is the sharpest unsoundness** — and §11 is the
   corpus survey of how three projects actually handle it, which supplies a better check than the
   declaration this section originally proposed.
3. **Error paths from deserialization vanish.** `try_from_slice` returning `Ok` unconditionally
   prunes every execution that would have failed there — the same direction as `early_panic`, so the
   same restriction applies: it cannot make an *acceptance* property statable.
4. **`Copy` changes move semantics.** Adding it is observable to the program: code that relied on a
   move now copies. Behaviour-preserving in practice for a plain-data state struct; not in general.

## 7. Refusals

Modelled on the extraction's five, which each catch something the build would accept or report too
late:

1. **A derive that is not there.** `removed` names something absent → the munge is a silent no-op.
2. **A type not defined in this project.** A foreign type cannot have its derives edited, and the
   author's impl would break the orphan rule.
3. **More than one account of the swapped type reachable in the unit's handlers.** §6.2. Not
   statically decidable, and the corpus says not to try: §11's answer is to model a bounded number
   of instances and make exceeding the bound a *reported violation* rather than a refusal. So this
   is not a record-time refusal at all — it is a shape the author's stand-ins must have, and the
   reviewer's job is to check that the fall-through asserts rather than aliasing silently.
4. **A second swap of the same type for one unit** — last-write-wins on a type is not a thing the
   dep-info check can see.
5. **`added` containing a trait with a blanket impl conflict** — the compile gate catches this, but
   naming it at record time is cheaper than a build round trip.

And one obligation rather than a refusal: the author must supply the impls in the same commit as the
swap. A swap without them does not compile, which the gate catches immediately — so this is a
sequencing note, not a hazard.

## 8. What I got wrong first, recorded because the next person will

Before reading the normative verification closely I told the author, in the property-generation
prompt (`56b2b78e`), to ask the editor to `mock_fn` **the container's accessors** when it saw these
errors — inferring `BigVec` from `[3005]` being a dynamic-length error and from the normative
verification mocking `BigVec`.

That guidance is right for `ValidatorList` and wrong for these handlers, which never touch it. It
also never fired: the trigger it gives is "`[3005]` or `[3308]` on more than one rule", and run 8's
submissions crashed the Prover outright, so no per-rule error codes were ever reported. The author
saw "job failed" and had nothing to match.

Two lessons for the guidance that replaces it: **name the shape, not the container** — a fixed-size
struct behind borsh and a variable-length collection need different kinds — and **trigger on a bare
prover crash**, not only on error codes, since the worst case reports the least.

## 9. Open questions

### 9.1 Is the global necessary, or would a fresh `nondet()` per call do? — *experiment specified*

The global exists so a write is visible to a later read *within one rule*. So the answer should split
by property class, and that is testable rather than arguable:

| property | needs the global? |
|---|---|
| "if `set_fee` succeeded, the manager signed" | **no** — it never reads back what was written |
| "after `set_fee`, the pool's fee equals the argument" | **yes** — without it the post-read is unrelated to the write |

Two rules × two arms (global / fresh `alloc_havoced` per call), four submissions, ~10 minutes on the
pinned path. If it comes out that way, the kind's charter should say the global is required for
*transition* properties and optional for *authorization* ones — and a simpler variant without it is
easier to justify for the second class.

### 9.2 Does the swap clear the `ScalarDomain` crash? — *half answered, finishable*

§10 shows the swap clears the dynamically-sized-memcpy failure. It does **not** show it clears the
crash: the minimal probe never reproduced the crash to begin with. The crash appeared under the
author's richer harnesses, and one of those is preserved (`run8-artifacts/`), so the finishing
experiment is to replay that harness with and without the swap. Two submissions.

### 9.3 Does this generalize past borsh? — *surveyed, and the answer narrows the kind*

It does not generalize the way §9 originally guessed — "name a trait rather than assume borsh" — and
the survey is worth more than the guess. Of six local checkouts with CVLR specs:

| project | framework | the seam it replaces | how |
|---|---|---|---|
| **stake-pool** | native, borsh **derive** | the derived (de)serializer | **swap the derives**, hand-written impls over a global |
| manifest | native, hypertree | accessor *functions* (`get_helper`) | replaced functions dispatching to globals |
| restaking | Anchor | loader *functions* | `mock_fn` |
| fluid | Anchor | `load`/`load_mut` *methods* | a `LoadMock` trait over a global DB |
| smart-account | Anchor | — | `certora_make_pub`, nondet `Vec`; no state indirection |
| klend / kvault | Anchor | — | no derive-level munge at all |

**No Anchor project swaps a derive.** The seam is always "wherever the program turns bytes into a
typed value", but its *form* differs — and in every case except stake-pool that form is **a function
or method**, which `mock_fn` already reaches. restaking and manifest do exactly that.

So the generalization argument runs the other way. `swap_derive` is not a special case of a broader
kind waiting to be found; it is the residue left over when the code has **no function to name**
because a `derive` generated it. That is a narrower justification than §5 claimed, and a better one:
it says precisely when the seventh kind is the only option, and — for Anchor targets, which is most
new Solana development — the answer is that it never is.

Worth stating as a limit on this survey: I could not determine how fluid's `LoadMock` takes
precedence over `BranchAccounts`'s *inherent* `load`, which Rust resolves first and which carries no
`certora` gating in `branch.rs`. The mechanism is either something I missed or the specs call it
explicitly; either way the row above describes what it replaces, not how it wins.

### 9.4 Who writes the impls on an unfamiliar type? — *answered: nobody should*

The impls are boilerplate. Four `BorshDeserialize` methods all returning `*get_global()`, one
`BorshSerialize` method assigning `*self` to it — the only thing that varies is the type name. §10's
arm B was ~30 lines of which one identifier was interesting.

`cvlr` 0.6.1 has no macro for this (`impl_checked_fn`, `impl_rt_fn`, `nondet_impl` are the only ones
it ships), so the answer is to generate it on our side: the scaffold already writes `log.rs`,
`nondet.rs` and `mocks/` into the harness, and a `globals.rs` carrying
`cvlr_global_state!(StakePool)` would reduce the author's share of this munge to one line plus the
init call.

That removes the objection this question was raised for. It does **not** remove §6 — a one-line macro
makes the technique *easy to apply*, which is the opposite of what an unsound-by-default technique
wants, and is exactly why the judge gate in §11.2 has to exist before the macro does.

---

## 10. The probe

One rule, two arms, one variable — `StakePool`'s serialization. The rule drives the extracted
`process_set_fee_inner` over unconstrained accounts and asserts the authorization implication:

```rust
let res = Processor::process_set_fee_inner(&program_id, &accounts[..3], nondet());
cvlr_assert!(res.is_err() || accounts[1].is_signer);
```

| arm | `StakePool` | result |
|---|---|---|
| **A** | derived borsh, as shipped | failed — `Pointer domain: statically unknown length in r3 at call sol_memcpy_` |
| **B** | derives swapped, impls over a havoc'd global | **`Verified`**, and `rule_not_vacuous_cvlr` verified |

Arm A is worth quoting because the Prover diagnoses itself better than this document did:

```
from: program/src/state.rs:44
note:  A memcpy with length that is not determined statically. Common root causes are:
        (2) dynamically sized structure whose size depends on user input
help: Resolve by identifying offending instruction and summarize the code to fix the size
```

`state.rs:44` is the `#[derive(..., BorshDeserialize, BorshSerialize, ...)]` line on `StakePool`.
The tool points at the derive.

Arm B is the whole technique end to end — swapped derives on `StakePool` **and** `AccountType`, a
`static mut` global filled by `alloc_havoced::<StakePool>()`, four-method `BorshDeserialize` and
one-method `BorshSerialize` reading and writing it, and `init_global_stake_pool()` at the top of the
rule. The program's own `try_from_slice_unchecked` and `borsh::to_writer` calls were not touched.

**What arm B does not establish.** That the rule is *true* of the deployed program: it is true of a
program whose `StakePool` round trip is the identity on a havoc'd global, and §6 is the list of what
that costs. The verified sanity rule rules out the cheapest way to be wrong, not the interesting one.


---

## 11. How the corpus models more than one instance

The survey §6.2 needed. Of the local checkouts, four use `alloc_havoced`; three use it for state
indirection and they do not agree:

| project | shape | instances | what happens past the bound |
|---|---|---|---|
| **stake-pool** | one `static mut *mut StakePool`; the borsh impls redirect to it | 1 | nothing — no second instance is representable |
| **manifest** | named globals per instance (`MAIN_SEAT_PK`, `SECOND_SEAT_PK`, …) over one fixed backing array with constant indices; the accessors dispatch on the index | 2 seats, 1 bid, 1 ask | **`cvt_assert!(false)`** |
| **fluid** | `[MaybeUninit<RefCell<T>>; 2]` per type, behind a `LoadMock::load(idx)` trait | 2 allocated | index discarded — see below |

(The fourth, `smart-account`, uses `alloc_havoced` to build a nondet `Vec<T>` rather than to model
state, and is not this pattern at all.)

**Manifest is the one to copy.** It is the only one that makes the bound observable:

```rust
pub fn get_helper_seat(_data: &[u8], index: DataIndex) -> &'static RBNode<ClaimedSeat> {
    if index == main_trader_index()        { get_helper(&*SEAT_DATA, MAIN_SEAT_DATA_IDX) }
    else if index == second_trader_index() { get_helper(&*SEAT_DATA, SECOND_SEAT_DATA_IDX) }
    else { cvt_assert!(false); /* unreachable, protected by the assert */ }
}
```

Aliasing stops being a silent modelling error and becomes a failing rule. That is a much better
answer than requiring the author to declare an instance count and the reviewer to believe it: the
bound is enforced by the same machinery that checks everything else, and a rule that reaches a third
seat *fails*, with a counterexample naming the index.

It also generalises past accounts. The discriminator is whatever the program uses to tell instances
apart — a `DataIndex` here, a `Pubkey` elsewhere — and the stand-in dispatches on it.

**Fluid is the cautionary half, and worth reading before copying anything.** All three of its
`load`/`load_mut` implementations take an index and ignore it:

```rust
fn load<'a>(&'a self, _: u32) -> Result<Ref<'a, Branch>> {
    let branch = unsafe { BRANCH_DB[0].assume_init_ref() };   // always [0]
    ...
}
```

Two elements are allocated and havoc'd; element 1 is never read. Whether that is deliberate — the
properties may only ever touch one branch — or vestigial is not something this survey can settle,
and no assert distinguishes the two. What it does show is that **the array is not the safeguard**:
allocating N instances and then collapsing them in the lookup is indistinguishable, from inside a
verdict, from having modelled one. Manifest's fall-through assert is the difference between a
bounded model and a silent one, and it costs three lines.

### 11.1 It does not transfer to this kind, and that is structural

Manifest mocks `get_helper_seat(data, index)` — **the discriminator is an argument**. The borsh trait
methods are not like that:

```rust
fn serialize<W: std::io::Write>(&self, _writer: &mut W) -> std::io::Result<()>
fn deserialize(buf: &mut &[u8]) -> borsh::io::Result<Self>
```

The read side has *something* — the slice's address, if comparing symbolic pointers is even wise in a
model built to avoid touching them. The **write side has nothing**: `W` is a generic writer wrapping
the account's data and there is no way to recover which account it belongs to. Every write lands in
one global no matter what the read side did.

So §11's table is a survey of two different techniques, not one with a best practice. Manifest's
assert belongs to **accessor mocking**, which is the existing `mock_fn` kind and needs nothing new.
`swap_derive` cannot borrow it, because a derived trait impl is reached without a discriminator.

What is left for `swap_derive` is stake-pool's answer, and it is a **precondition rather than a
check**: use it only where one instance of the type is all the unit's handlers can reach. That is
satisfiable — most Solana handlers take one account per state type, and the account list makes it
visible — but it is the kind of side condition that is quietly violated later, by a handler that
merges or transfers between two of something.

### 11.2 Which reorders §6

With that settled, **§6.1 is the more dangerous item, not §6.2.** Aliasing needs a handler with two
accounts of one type: uncommon, and visible in a signature. "Deserialization can no longer fail and
does not read the buffer" applies to **every rule under the swap**, silently, and makes a whole
property class — malformed input rejected, truncated account rejected, a field survives a round trip
— pass while meaning nothing.

`rule_not_vacuous_cvlr` does not catch this. §10's probe passed its sanity rule and was still only
true of a program whose `StakePool` round trip is the identity on a havoc'd global.

That points at a gate rather than a check — and the existing architecture says exactly where it
goes, which is **the property judge, not the munge reviewer.**

The split is already deliberate on both sides. The reviewer's charter states it:

> **You do not see the properties being proved, and that is deliberate.** Whether these rules still
> mean anything after this change is a different review, done later by a judge that holds the batch.
> Yours is narrower and it is the one that has to happen before the change lands: did the editor
> solve the stated problem, faithfully, within its charter.

And the judge is already the only component handed the munge list —
[`author.py`](../composer/spec/cvlr/author.py)'s `with_assumptions`, "the only callback that sees the
invocation's context". So "does this rule still mean anything given this munge" is the judge's
existing remit, and it already holds both inputs the question needs.

Asking the *reviewer* to gate on property class would break that separation on purpose-built
grounds: it would start refusing faithful edits on an assessment it is structurally not equipped to
make.

So the gate is **a line in the judge's charter**, of the same shape as the one extraction already
carries there ("extraction cannot hollow out a rule, it narrows one"):

> A unit carrying a `swap_derive` cannot return a green verdict on a property about encoding, layout
> or malformed input. The swap made that property's subject unobservable, and a rule asserting it is
> reporting on a program whose round trip is the identity.

Two consequences worth stating rather than discovering later. **The author does not declare
anything** — an earlier draft of this section proposed that, and self-report from the party with an
incentive to ship is both the weakest available version and unnecessary, since the judge holds the
property text and can classify for itself. And **the gate is late by construction**: it fires after
the prover job is spent. That is the price of the reviewer's blindness, and it is the right trade —
catching a meaningless green verdict late beats blocking a faithful edit early on a judgement the
reviewer cannot make.

Until that line exists, this kind is safe to use by hand and unsafe to hand to an agent that applies
it wherever it sees `[3005]` — which is exactly what the guidance in `56b2b78e` would have led to.
