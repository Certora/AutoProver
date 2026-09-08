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
   normative verification gets away with it — but nothing in the kind enforces that, and a unit whose
   handler takes a source and a destination of the same type would be verified against a program
   that cannot distinguish them. **This is the sharpest unsoundness and the one a check must carry.**
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
   statically decidable in general; the tractable version is to require the author to *declare* the
   count and the reviewer to check it against the handler's account list, refusing when it is >1.
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

1. **Is the global actually necessary, or would a `nondet()` value per call do?** The global exists so
   a write is visible to a later read within one rule. A property that only reads might not need it,
   and a simpler kind would be easier to justify.
2. ~~**Does the swap alone clear the `ScalarDomain` crash?**~~ **Half answered — see §10.** The swap
   clears the *dynamically-sized-memcpy* failure outright, measured. It is **not** shown to clear the
   `ScalarDomain` crash: the minimal probe did not reproduce that crash to begin with, so the two
   arms only bracket the memcpy failure. The crash appeared under the author's richer harnesses in
   runs 7 and 8, and whether it shares this cause is still an assertion.
3. **Does this generalize past borsh?** Anchor's `AccountDeserialize`, `bytemuck::Pod` and
   `Pack`/`Sealed` are the same shape. If the kind is worth having it should probably name a trait
   rather than assume borsh, which the record in §5 already allows.
4. **Who writes the impls on a target that is not stake-pool?** The author wrote a four-method
   `BorshDeserialize` here because the normative verification is in front of it in the corpus. On an
   unfamiliar type it is a larger ask than a `mock_fn` stand-in, and it may be the thing that makes
   this kind not worth having.


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
