> Where this recipe and the Solana/CVLR manual or other Certora materials diverge, this recipe governs.

### K11. State the analysis cannot size: variable-length collections and `swap_derive` `[EDIT]` `[SKIP]`

**Trigger:** `[3005]` memcpy with dynamically sized length; an internal error out of
`sbf.domains.ScalarDomain`; a submission that fails with no per-rule verdicts at all.

Four symptoms, one family, and none of them names its cause:

| symptom | what it is telling you |
|---|---|
| `[3005] memcpy with dynamically sized length` | a copy whose length the analysis cannot pin down |
| `[3308] illegal dereference of an absolute address` | a pointer it could not resolve to an object |
| an internal error out of `sbf.domains.ScalarDomain` | the same analysis giving up rather than reporting |
| **a submission that fails with no per-rule verdicts at all** | the same thing, before any rule was encoded |

That last row is the one to watch for, because it reports the least. A job that comes back failed
with no rule results is not an infrastructure hiccup to retry — retrying it costs a job and returns
the same answer.

None of these is a rule defect and no rewording clears them. They come from the program moving state
whose *size the analysis cannot fix*. `cvlr_assume!` on an account's `data_len()` is still worth
doing and is **not** enough on its own: the length that defeats the analysis is usually inside the
encoding or the accessor, not on the account.

**Two shapes cause this, and only one of them you can do anything about.** Read the handler and find
which:

**A variable-length collection** — a byte-slice-backed vector of entries, a packed list, anything
indexed at a stride computed at runtime. **Ask the editor to `mock_fn` its accessors** — the
find / find_mut / retain / iterate methods, not the handler that calls them. Write stand-ins that
serve a small fixed number of entries, dispatching on whatever index or key the program uses, and
`cvlr_assert!(false)` on the fall-through so that reaching an entry your model does not have is a
*reported failure* rather than a silent aliasing of two entries into one. This is the case where
asking early beats asking last: every rule in the unit needs the same stand-ins, so discovering it
after three prover jobs wastes three.

**A fixed-size state struct behind a derived (de)serializer** — the handler does
`try_from_slice_unchecked::<State>(&info.data.borrow())` and `borsh::to_writer(&mut
info.data.borrow_mut()[..], &state)`, and `State` has `Option` or enum fields, so its *encoding* is
variable length even though the struct is not. **Ask the editor for a `swap_derive`**: it moves the
derived (de)serializer behind your unit's feature so you can supply the impls instead. Name the type
and the two calls.

You then owe the impls, in this module, the way you owe a `mock_fn` stand-in. They read and write a
value held in a `static mut` that each rule initialises with
`cvlr::nondet::havoc::alloc_havoced::<State>()`, so the program's own `try_from_slice_unchecked` and
`to_writer` calls move a fixed-layout value in memory instead of parsing a buffer. `derive(Copy)` is
what lets an impl assign `*self` to it, and the editor adds that.

Two things to get right, because nothing else will catch either:

* **A rule that reads state back after the handler wrote it needs the impls to share one value.** If
  each call mints a fresh one instead, the write is invisible and a transition property is violated
  for a reason that has nothing to do with the program. A rule that only checks *who signed* never
  reads back and does not need the sharing — for those, an impl returning a fresh unconstrained
  value is simpler and safer.
* **One value per type means two accounts of that type are the same account.** If the handler takes
  a source and a destination of the swapped type, this munge cannot express it — `record_skip`
  rather than prove something about a program that cannot tell them apart.

And the cost, which the judge will hold you to: with the deserializer gone, deserialization can no
longer fail and never reads the bytes. Any property about *encoding* — a malformed account rejected,
a truncated one rejected, a field surviving a round trip — is unprovable under a swap and will pass
while meaning nothing. Do not ask for one to serve a property of that shape.

The two are told apart by what the handler touches, not by the error code: the same `[3005]` comes
from both. If the handler never indexes a collection, it is the second shape.
