# Who edits the program

> Whether the CVLR backend can move *every* modification of the program under verification behind a
> munge agent — including the property-relative kinds the author applies today — and let the author
> write only its own unit's spec.
>
> Companion to [munge-and-working-copies.md](munge-and-working-copies.md) §5, which argued this could
> not work, and to [single-working-tree.md](single-working-tree.md), which changed one of the three
> premises that argument rested on.
>
> **Built, as a Solana-only editor** ([editor.py](../composer/spec/cvlr/editor.py)). §4's three moves
> all landed; §8 is the design it was built to, and §9 records where the build differed and what was
> deliberately left out.
>
> **Coming from the EVM backend and new to Solana?** Read the appendix first — three differences
> account for everything below, and the first one causes the other two.

---

## 1. The proposal

One entity edits the program under verification: a munge agent, reviewed by a judge. The author gets
its own unit's harness module and its own tuning file, and nothing else. `munge_function` leaves the
author's tool belt.

The claim that makes it askable is that a munge is now **scoped to one unit** — the attribute is
gated on that unit's cargo feature, so it is dormant in every sibling's build
([single-working-tree.md](single-working-tree.md) §2.3). Cross-unit soundness stopped being a
judgement call.

## 2. What changed underneath the old objection

[munge-and-working-copies.md](munge-and-working-copies.md) §5 gave three reasons the EVM topology
could not transfer. They have not aged equally.

### 2.1 "The soundness argument would be written by the wrong party" — dissolved

This is the one the proposal is really about, and the answer is better than the proposal assumes.

§5's version: *"`why_sound` for a munge is 'sound for the properties in this batch', and only the
author holds the batch. A sub-agent could report faithfully what it changed and could not argue the
change was acceptable."*

Every clause is true and the conclusion does not follow, because **the sub-agent does not have to
argue it.** It has to report faithfully; something else has to rule. In EVM that something is the
munge reviewer, whose *"entire seven-item checklist is behavioural"* and whose verdict is explicitly
scoped — *"Approval is not an endorsement that the edit was wise."* So in EVM there is genuinely
nobody to rule on property-relative acceptability, which is why EVM routes such changes to the spec
side instead.

**CVLR already has that somebody.** The contextual property judge holds the batch — it is handed the
properties and the draft — and its system prompt already rules on exactly these two kinds:

> `early_panic` rewrites every `?` in a function to `.unwrap()` … sound for a property about what a
> *successful* call does, and fatal to any property about rejection, because the path that rejects is
> the path it deleted. A rule asserting that some input is refused, driving a function that carries
> `early_panic`, is vacuous — say so and name the rule.
>
> `mock_fn` is the harness-mirror problem wearing an attribute … A stand-in that reproduces the
> arithmetic a rule asserts over means the rule verifies the stand-in.

That is the ruling §5 said no reviewer could make. It is written, it is shipped, and it is already
fed the munge list through `HarnessAssumptions`. The reason CVLR can delegate what EVM cannot is not
the feature gating — it is that CVLR's reviewer is property-aware where EVM's is deliberately not.

So the division of responsibility is available today:

| | holds | answers |
|---|---|---|
| author | the property, the prover's failure | *that* something in the program is in the way |
| munge agent | the program's source | *what* changed, faithfully |
| judge | the properties and the draft | whether the change leaves the rules meaning anything |

### 2.2 The cross-unit half is now mechanical, and needs no judge at all

The proposal offers "the judge could ensure" that a munge is soundness-preserving for other units.
It does not have to. Under the current arrangement that is true **by construction**, given three
invariants:

1. every munge's feature is the recording unit's own — enforced in `MungeFunction`;
2. unit features are empty — enforced in `declare_unit_features`, and load-bearing for the cost
   argument as well;
3. every build names `certora` plus exactly one unit feature — enforced by `HarnessTarget.features`,
   which is the single place that pairs them.

A judge asked to check this would be checking a property of the build system using an LLM. Better to
leave it where it is and spend the judge's attention on §2.1.

**One invariant is not enforced and is the real exposure.** A munge inserted into a file the
`certora` feature gates out changes nothing and reports nothing — [munge-and-working-copies.md](munge-and-working-copies.md)
§8 gap 2, still open. Under this proposal it gets worse, not better: the entity that would notice
(the author, from an unchanged prover result) is now two hops from the edit. A munge that reached the
build has to become a checked fact before the agent is worth having.

### 2.3 "The request would have to be a prescription" — survives intact

§5: *"The editor prompt's central discipline is 'Requests describe problems, not edits'. With a
two-element vocabulary, describing the problem precisely enough for an editor to choose *is*
choosing."*

Nothing has changed here. Compare EVM's own examples, which are genuinely open-ended —

> *"The contract keeps its state in a struct at a hand-derived constant storage slot, which the
> prover's storage analysis cannot resolve. Can you move it to standard, annotated ERC-7201
> namespaced storage?"*

— with the CVLR request that would produce a munge: *"the `?` in `calculate_fees` is making my rule
unanalyzable."* There is exactly one move. The agent adds a round trip and no decision.

This does not sink the proposal. It relocates its justification: the agent is not there to **choose**,
it is there to be the **single writer** and the place a review attaches. That is a real thing to want
from a tool that edits somebody's repository. But it should be argued on those grounds, not by
borrowing EVM's, and it invites the obvious question §4 is about — whether a writer that does not
choose needs to be an agent.

### 2.4 "The cost is inverted" — survives, and is now measurable

An editor conversation plus a judge pass per one-line attribute insert, inside a loop whose latency
is [cvlr-backend-plan.md](cvlr-backend-plan.md) §9's top risk. Batching helps: one request carrying
several munges, one cumulative diff, one review. Nobody has measured how many munges a unit actually
needs — the corpus counts (klend-audit 43, kamino 13) are finished projects, not per unit per run.
**That number decides §2.3 and §2.4 together, and it is cheap to collect from the runs we already
make.**

---

## 3. The kinds do not split the way the topology needs

A clean "only the munge agent touches the program" boundary assumes a munge is entirely inside the
program. Two of the five kinds in the corpus are not: they are a program-side attribute *plus* a
spec-side body.

| kind | soundness | spec-side half |
|---|---|---|
| `certora_make_pub` | universal — visibility only | none (but see §9.3: not available) |
| `inline(never)` | universal — behaviour-preserving | none |
| `early_panic` | property-relative | none |
| `mock_fn` | property-relative | the stand-in |
| `cvlr_hook_on_entry` / `on_exit` | universal when observation-only | the hook body |

Note that the split cuts *across* the soundness axis, which is why it is easy to miss: it is not the
property-relative kinds that straddle the boundary, and it is not the universal ones either.

For the three with no spec-side half, the ownership rule is exact. For the other two it needs a
handshake, and there are only two ways to write it:

* **The author writes the stand-in first, then requests the attribute.** Clean ownership, at the cost
  of a window in which the harness contains a `pub fn` nothing points at — harmless, and the compile
  gate catches the reverse ordering. This is the one to take.
* **The munge agent writes both**, putting the stand-in in `certora/mocks/`. Simpler for the
  requester and it breaks the rule the proposal is for: the agent is now writing spec-side code
  whose *content* is a property-relative approximation, which is the author's judgement wearing the
  agent's hands.

## 4. What is actually being proposed, in three separable moves

They have very different costs and only the third needs an agent.

**A — make the boundary structural.** Remove `munge_function` from the author's belt; give the
program-source write to one place. The boundary already exists in code as
`SharedTree.resolve` plus the `certora/` subtree; this turns a check into an ownership rule. Cheap,
and it is most of what "the only entity entrusted with modifying the user's code" asks for.

Note the rule needs stating precisely, because several things already write into the checkout and
should go on writing: the preflight scaffold, `declare_modules`, `declare_unit_features`, and
`write_artifact`. The line is between **the program under verification** and **the verification
scaffolding around it** — which is exactly the line `is_project_source` and the `certora/` subtree
already draw.

**B — give the judge the diff instead of the description.** `HarnessAssumptions.briefing()` hands the
judge `f"{m.function} ({m.path}): {m.kind.describe()}"` and the author's `why`. `munge_diff` already
computes a real unified diff from state with no working tree
([single-working-tree.md](single-working-tree.md) §4). Passing it closes the half of
[munge-and-working-copies.md](munge-and-working-copies.md) §8 gap 1 that is still open — *"EVM's
reviewer receives a diff and its approval is void the moment anything changes, whereas this judge
receives a description"* — and it is a few lines. **This is the whole of "the judge could ensure
this", and it does not need A or C.**

**C — interpose an LLM munge agent between the author and the edit.** The expensive one, justified
by §2.3's relocated argument rather than by EVM's.

A and B are worth doing on their own evidence. C is the question.

## 5. The shape C would have to take

If it is built, three things follow from §2 and they are not optional.

**The record has to stop conflating two justifications.** `FunctionMunge.why` is today one field
meaning both "why the prover could not analyze this" and "why this is sound for my properties". Split
by author:

* `problem` — the author's, because only the author saw the prover fail;
* `changed` — the agent's, factual, the EVM editor's competence and the only thing it is qualified to
  assert;
* the judge's ruling — separate, and invalidated by `tuning_history` the way a stamp already is.

**Refusal has to be a first-class return.** EVM has `MungeRefusal` and the requester reads the
explanation. Here it also has to compose with the give-up boundary: an agent that refuses leaves the
author holding a `record_skip` naming the kind it would have needed, which is
[cvlr-backend-plan.md](cvlr-backend-plan.md) §7.6.4's boundary reached from a new direction. **Skip
rate is the metric that says whether C helped or hurt**, and it is the one to watch.

**The dry-run feedback has to survive the extra hop.** `munge_function` today runs `apply_munge`
against disk before recording, so *"this file defines no function named X; it does define …"* comes
back in the same turn. Behind an agent that becomes a round trip. Keep the check at the tool boundary
even if the decision moves.

## 6. Verdict

**The proposal works, and for a better reason than it gives.** Feature gating is not what unblocks
it — that only makes the cross-unit question mechanical, which removes a judge's job rather than
creating one. What unblocks it is that CVLR's judge is property-aware and already rules on exactly
these two kinds, which is the thing §5 said did not exist and which EVM's munge reviewer is
deliberately built *not* to be. §5's conclusion was right about EVM and wrong about CVLR, and the
sentence that carried the error is *"a sub-agent could report faithfully what it changed and could
not argue the change was acceptable"* — true, and the wrong entity was being asked to argue.

**Do A and B now; they stand on their own and are small.** A gives one owner for the program source.
B gives the judge the artifact it should have had since §8 gap 1 was written.

~~**Do not do C yet, and collect one number first: munges per unit per run.**~~ **Done, and the
reason to reorder was §8.5's**: the editor's submit gate closes
[munge-and-working-copies.md](munge-and-working-copies.md) §8 gap 2, which is a correctness hole
rather than an ergonomic one, and it comes with the topology rather than before it. The number is
still worth collecting — it decides whether the round trip pays for itself — but it is no longer what
the decision waits on.
 §2.3 and §2.4 both turn
on it. If a unit needs one or two munges, an agent is a round trip and a judge pass for an edit with
no decision in it, and A+B has all of the benefit. If a unit needs ten, batching makes the agent
cheap per edit and the single-writer discipline starts paying for itself.

**Both experiments ran together, which was not the plan and was the right call.** The vocabulary went
from two kinds to five in the same change, because an editor choosing between two kinds is §2.3's
objection at full strength and an editor choosing between five is not. `inline_never` and the
`cvlr_hook_*` pair came in; `certora_make_pub` did not, and §9 says why.

## 7. What would change this answer

1. **Munges per unit per run turns out to be high.** §2.4's arithmetic inverts and C gets cheap per
   edit.
2. **A munge lands that the judge should have caught and did not.** B was necessary and insufficient,
   and a dedicated reviewer with a narrower remit starts to look different from a property judge with
   a long checklist.
3. **§8 gap 2 bites** — a munge that silently reached no build. That has to be fixed before C
   regardless, because C puts two hops between the edit and the person who would notice — though
   §8.5 finds the fix may come *with* C rather than before it, since the editor's submit gate is
   where EVM catches the same failure.
4. **The vocabulary widens past six kinds.** An exposure-style extraction is a real edit with real
   choices in it, and §2.3's "the request *is* the edit" stops holding the moment there is more than
   one way to do what was asked. This is the trigger most likely to fire, and it fires on the
   experiment §6 recommends first.

---

## 8. If we extend the existing editor agent

Concretely: what [munge_agent.py](../composer/spec/source/munge/munge_agent.py) would have to become
to serve every munge Solana needs, not just the two kinds implemented today.

### 8.1 What the editor is made of

501 lines, and they do not divide the way the file's name suggests.

| part | | |
|---|---|---|
| `MungerStateExtra`, `MungeRefusal`, `CommonMungeDescription`, `GiveUpTool` | **chain-neutral** | `executive_summary` / `how_to_apply` / `why_sound` is already the right shape |
| `EditMungeTool` — request in, summary + diff + a commit key out | **chain-neutral** except the `added_files` note | the NL-request discipline and the refusal path transfer whole |
| `RequestReviewTool` — diff, review, stamp `reviewed_digest`; any later edit voids the approval | **chain-neutral in shape**, VFS-bound in mechanism | the digest gate is the good idea and it is three lines of hashing |
| `munge_feedback_judge` | **chain-neutral in shape**, EVM in prompt | |
| `SubmitEditTool` | **half and half** | the approval check is neutral; the conf-map extension and `check_edits_compile` are solc |
| `AddedFile` (`SolidityIdentifier`, `CompilerSettings`), `ComputeErc7201Slot`, `ComputeKeccakString`, `compiler_map_semantics.j2` | **EVM** | drop for Solana |
| `EditToolsHost` | **already the seam** | a `Protocol` with `write_tools` / `read_tools` / `mat` |

So the topology — request, edit, earn a review, submit behind a digest, or refuse — is portable. What
is not portable is everything below it, and that is one decision rather than many.

### 8.2 The decision that determines everything: what the editor edits

Every mechanism in the file is bound to `VFSState`. The write tools are graphcore's
`edit_file`/`put_file`; `summarize_changes` diffs two overlays; `reviewed_digest` is
`EditStore._deterministic_hash(vfs)`; `EditStore.commit` snapshots one. There are two ways to give
Solana an editor and they differ here.

**Option 1 — hand Solana a VFS.** Reuses the machinery almost verbatim. It also forces the per-unit
workdir back, and the reason is not the VFS: it is that **a free-form text edit has no `cfg_attr`.**
A munge is scoped to one unit because it is an attribute gated on that unit's cargo feature, which is
what makes two units' munges of one function two dormant lines rather than a conflict
([single-working-tree.md](single-working-tree.md) §2.3). An overlay entry is a whole file's text and
`merge_vfs` is last-writer-wins, so two units editing one file is a collision with no defined answer.
Recovering isolation means one overlay per unit and one materialization per unit — which is the
arrangement [single-working-tree.md](single-working-tree.md) exists to have removed. Rejected.

**Option 2 — a typed write surface over the munge records.** The editor's state carries
`list[SourceEdit]`, not `dict[str, str]`. Its write tools are one per kind. The diff is `munge_diff`,
which already computes from records with no working tree. The digest hashes the record list. The gate
is `session.check(features=(certora, unit_x))`. Every property survives, and one of them survives
*because* the model never touches the feature: the tool sets it, from the requesting unit's identity.

Option 2 is the one that works, and it means the editor reuses the **control flow** and almost none of
the **machinery**. [munge-and-working-copies.md](munge-and-working-copies.md) §6 found that the
boundary between the two backends was agent topology rather than Solidity-versus-Rust; adopt the
topology and the boundary moves to the edit representation. That is the same seam seen from the other
side, and it is where the shared code has to be cut.

### 8.3 The seam

`EditToolsHost` already exists as a `Protocol` with three members. It grows to about seven, and the
editor core becomes generic over the edit representation:

| member | EVM | Solana |
|---|---|---|
| `write_tools` | `edit_file`, `put_file` over the VFS | one typed tool per munge kind, plus the mock-body writer (§8.4) |
| `read_tools` | VFS reads | `env.source_tools` — the same shared project reads the author already gets |
| `diff(state)` | `summarize_changes` | `munge_diff` |
| `digest(state)` | hash of the overlay | hash of the record list |
| `gate(state)` | `check_edits_compile` + conf-map extension | `cargo check --features certora,unit_x` |
| `completion_model` | `MungeDescription` (adds `added_files`) | `CommonMungeDescription` unchanged — Solana adds no files to a build's file set |
| `commit(state)` | `EditStore.commit` → an application key | append the records to the author's `munges` |

`compile_check.py`, `edit_store.py`, `erc7201.py` and `conf_maps.py` stay behind the EVM
implementation of that protocol and are not touched.

### 8.4 What Solana has to add

**Three more munge kinds, and one of them carries an expression.** `certora_make_pub` and
`inline(never)` look like new `MungeKind` variants with nothing but an attribute string — though
building it found the first is not available at all (§9.3).
`cvlr_hook_on_entry` / `on_exit` take the call to insert, so the kind carries a Rust expression the
compile gate is the only check on — which is a wider surface than the two kinds today and wants
saying in the charter.

**The spec-side half has to be a recorded artifact too.** `mock_fn` and the hooks name a body the
editor must also write, and that body is Rust in `certora/mocks/`. Two constraints follow, and
missing either is a quiet failure. It has to be **feature-gated per unit** — `mocks/mod.rs` declares
its submodules unconditionally, so an unguarded mock compiles for every unit and a broken one breaks
all of them, which is the coupling the spec modules already avoid. And it has to be **in state, not
just on disk**, or the tree stops being derivable from the checkpoint and §4's whole resume story
lapses for exactly the units that used a mock. So: `MockBody(unit, module, source)` alongside
`FunctionMunge`, written by the same reconcile.

**Extraction, if it is wanted, is the only kind that is not an attribute.** It is also the kind two
of the last gate run's eight skips actually wanted. It can stay declarative — a content-addressed
`TextRewrite(path, before, after, feature)` whose replay fails loudly when `before` no longer matches,
which is the drift detection `apply_munge` already has. What it cannot do is be inert by accident, so
in Rust it has to be written as a gated pair:

```rust
#[cfg(not(feature = "unit_x"))]
fn settle(...) { /* original, inline */ }
#[cfg(feature = "unit_x")]
fn settle(...) { settle_transition(...) }
#[cfg(feature = "unit_x")]
pub fn settle_transition(...) { /* what the rule wants to descend to */ }
```

Ugly, and it keeps every property: the deployed build is untouched, siblings compile the original,
and the record replays onto pristine.

**What stays out of the agent's hands.** The `[patch.crates-io]` fork redirects
([munge.py](../composer/spec/cvlr/munge.py)) are read from a table of maintained forks and applied by
the scaffold. They are a munge in the Certora sense and emphatically not an agent's decision; a
version the fork does not cover is a `Blocked` with a sentence a human acts on.

### 8.5 What Solana gets for free — including one gap that has been open since the start

**The author's edit-management surface maps onto the record list.** `commit_edit` appends,
`edit_history_log` lists, `revert_to_edit` drops. That last one is the `revert_munge` §5 notes is
missing today, arriving as a side effect of the topology rather than as its own feature.

**`EditsNotCompiled` has a cheap Solana analogue, and it closes §8 gap 2.** EVM's submit gate refuses
when *"the build succeeded but these edited files were never parsed by the compiler."* The identical
failure here — a munge inserted into a file the `certora` feature gates out, which changes nothing and
reports nothing — has been open since the backend was written. `cargo` emits a `.d` file per target
listing every source rustc actually read, so after the gate build the check is "is the munged path in
it". Worth confirming against `cargo certora-sbf` before relying on it, but if it holds, adopting the
submit gate buys the fix. **That is a better argument for the topology than §6 reached**, because it
is a correctness gap rather than an ergonomic one.

**And a refinement to §2.1.** That section argued CVLR's property judge is the reviewer §5 said did
not exist. It is — but it is the *publish* gate, and the editor's `request_review` is a *submit* gate
asking a different question: did you do what was asked, faithfully, within charter. Solana wants both,
and the editor's own reviewer can stay behavioural exactly as EVM's is. §2.1 should be read as "the
property-relative ruling has a home", not as "one review suffices".

### 8.6 Cost, and the order

Roughly: about 250 of the 501 lines are the portable control flow, about 180 stay behind the EVM
adapter, and Solana writes about 260 new (the typed write tools, the record digest and diff adapters,
the cargo gate, the mock-body writer) plus prompts. Against that, a Solana-only editor built directly
on the pieces already here is perhaps 300 lines and refactors nothing that currently works.

So the reuse is real but modest, and it is bought with a refactor of a production EVM path. **Build
Solana's editor against a locally-declared protocol first, and lift the shared core only once there
are two implementations to generalise from.** That is [cvlr-backend-plan.md](cvlr-backend-plan.md)
§4.1's standing rule — *share piecemeal; there is no per-chain bundle* — and it is the order that
keeps a working backend working.

The prompts are less work than they look. `munge_charter.j2` is already an `{% include %}`, so the
charter is a template swap: EVM's three prose categories and behavioural-equivalence criterion out,
Solana's typed vocabulary and its lines-not-crossed in. `compiler_map_semantics.j2` comes out
entirely. `prover_quick_background.j2` needs a Solana sibling. The body of
`munge_editor_system.j2` — requests describe problems, refuse rather than comply, `why_sound` names
the charter category — transfers unchanged, and is the part worth having.

---

## 9. What was built, and what it cost

[editor.py](../composer/spec/cvlr/editor.py), [depinfo.py](../composer/cargo/depinfo.py), two
prompts, and the three moves of §4. The shape is §8's Option 2 throughout: the editor edits
**records**, not text.

### 9.1 The moves

**A — one owner for the program source.** `munge_function` is gone from the author's belt and is now
the editor's. The author's two program tools are `code_editor(request)` and `revert_munge(edit_id)`;
the boundary is enforced by which agent holds which tool rather than by a rule in a prompt.

**B — the judge gets the diff.** `HarnessAssumptions` carries a `diff` computed by `munge_diff` from
the records, and the briefing tells the judge to read it *rather than* the summary, because the
summary is the editor's account of its own work. That closes the half of
[munge-and-working-copies.md](munge-and-working-copies.md) §8 gap 1 that stayed open.

**C — the editor.** `code_editor` runs one conversation: read the program, apply munges,
`request_review`, `submit_edits`, or `give_up`. The approval is a hash of the record list, so
applying or dropping a munge afterwards voids it. Submit re-checks the approval, compiles, and
verifies the edits reached the compiler.

### 9.2 The gate that made C worth doing now

§8.5 predicted an `EditsNotCompiled` analogue and it works. Cargo writes a `.d` beside each artifact
listing every source rustc read; `compiled_sources` finds the one belonging to *this* feature
variant by looking for a file only this build compiles — the unit's own harness module — and answers
whether the munged paths are in it. Measured on a real cargo build before anything was written: with
`unit_b` disabled, `src/b.rs` is simply absent from the dep-info.

That closes [munge-and-working-copies.md](munge-and-working-copies.md) §8 gap 2, which had been open
since the backend was written and is the quietest failure it had — an attribute in a file no enabled
feature reaches changes nothing, reports nothing, and leaves the report claiming a source edit that
did not happen. A build whose dep-info cannot be identified reports `NotChecked` and says so, rather
than passing.

### 9.3 The vocabulary went from two kinds to five

Not scope creep: an editor choosing between two kinds is §2.3's objection at full strength. Added:

* **`inline_never`** — behaviour-preserving outright, and the answer to a function the optimizer
  folded away, which a summary, an inlining directive and a counterexample's stack all need a symbol
  for.
* **`hook_on_entry` / `hook_on_exit`** — `cvlr::hook_on_entry`, in the pinned reference set. The
  vocabulary's only instrument for reaching a point *inside* an execution, which is what two of the
  eight skips in the last gate run actually wanted.

**`certora_make_pub` was dropped, and the reason corrects this document.** §2 and the appendix
described it as a corpus munge kind, which it is — but it is a **project-local proc macro**, defined
in the one project that uses it, with no counterpart in `cvlr`. And Rust cannot make visibility
conditional on a feature without duplicating the item, so even a hand-rolled version would be the
gated-pair shape of §8.4 rather than an attribute. It is not available and should not have been
listed as though it were.

### 9.4 What was deliberately left out

* **Extraction** — §8.4's gated pair. The one kind that is not an attribute, and the one two skips
  wanted. It needs `TextRewrite` alongside `FunctionMunge` and a charter that can describe a
  restructuring; neither exists.
* **`MockBody`** — §8.4 argued the editor might write mock bodies into `certora/mocks/`. It does not:
  §3's recommendation stands, the author writes the stand-in in its own harness module and names it
  to the editor, and the editor refuses a `mock_fn` whose target does not exist. That keeps the
  ownership rule exact and needs no second write surface.
* **Staging.** EVM's `commit_edit` lets an author read a diff and decide. Here the editor's output is
  already compiled and reviewed, so it lands in the author's munge list directly and `revert_munge`
  is the author's say. One tool instead of two, and the undo is the thing §5 said was missing.

### 9.5 What to watch

The skip rate. §5 said it: an editor that refuses leaves the author holding a `record_skip`, so
**skips naming a kind the vocabulary lacks are the metric that says whether this helped or hurt**.
Three of the five kinds are new, so the first run under this arrangement is also the first test of
whether the vocabulary or the topology was the limit — and if extraction keeps coming back, §9.4's
first bullet is the next thing to build.

---

## Appendix — for a reader who knows the EVM backend and not Solana

Enough to read the rest of this note. The differences that matter are three, and the first one causes
the other two.

### A.1 There is no spec file, so there is nowhere else to put an approximation

A CVL spec is a standalone `.spec` the prover is pointed at. **A CVLR "spec" is Rust: a module inside
the crate under verification**, compiled with it, reached through the build rather than through a
path. There is no second artifact.

That matters because of what the spec side can do in each ecosystem. CVL summaries can *compute* — a
ghost function, an internal function, `ALWAYS(x)`. The Solana Prover's equivalent is a **tuning
file**: a list of regexes over demangled symbols, and the prover replaces a matched symbol with an
unconstrained value. It can delete a function; it cannot replace one. `summarize_for_prover` writes
into that file.

So the EVM instinct — *"when 'pretend this function does not exist' is genuinely right, CVL has a
deleting summary for it, declared in the spec"* ([munge_charter.j2](../composer/templates/munge_charter.j2))
— works for the *deleting* case on Solana too, and has nowhere to go for the *computing* case. That
is the whole reason the two backends' munge topologies diverged: property-relative approximation has
to happen in the program's source, which puts it on the author's side of EVM's own dividing line.

### A.2 A munge is a one-line attribute, and it is compile-time conditional

EVM's editor rewrites Solidity — Exposure, Refactor, Standardize, under a behavioural-equivalence
criterion (*"Representation may change; logical behavior may not"*). The Solana equivalent is
narrower and stranger: five of the six kinds in the corpus are a **single attribute inserted above a
function signature**, from a library:

```rust
#[cfg_attr(feature = "unit_deposits", cvlr::early_panic)]
pub fn redeem_fees(reserve: &mut Reserve) -> Result<u64> { … }
```

Two things here have no Solidity analogue.

**Verification builds are feature-gated.** A Solana verification project builds under a cargo feature
(conventionally `certora`) that pulls in the harness and suppresses the on-chain entrypoint. So an
edit can *ship in the file and be inert*: with the named feature off, `cfg_attr` contributes no
attribute at all and the compiled function is the one the project deployed. There is no Solidity
construct that is present in the source and absent from the artifact unless asked for.

**That is what scopes a munge to one unit.** We gate each munge on the *requesting unit's own*
feature, so one function can carry several munges — one per unit — each dormant in every other unit's
build. EVM gets the same isolation for free and by a different route: each unit's edits live in that
unit's VFS overlay in graph state, so there is no shared file to collide in. Ours is weaker (one
physical file, N dormant lines) and cheaper (one crate, one `target/`, see
[single-working-tree.md](single-working-tree.md)).

The attributes themselves come from the `cvlr` crate: `early_panic` rewrites every `?` in a function
to `.unwrap()`; `mock_fn(with = path)` replaces the function with a named stand-in.

### A.3 There are two reviewers on the EVM side and one on ours — and ours sees the properties

This is the difference §2.1 turns on, and it inverts the usual expectation.

| | EVM | CVLR |
|---|---|---|
| who edits the program | a dedicated editor agent, commissioned by the author with a natural-language *request* | the author, via a typed `munge_function(path, function, munge, why)` — the change this note proposes |
| what the editor sees | not the spec, deliberately | — |
| who reviews the edit | the munge reviewer: a seven-item **behavioural** checklist (lost writes, added constraints, interface drift, …), verdict scoped to *"not an endorsement that the edit was wise"* | the **contextual property judge** — the same one that reviews the harness — which holds the properties and already rules on both munge kinds by name |
| where property-relative approximation goes | the spec, as a CVL summary | the program's source, because A.1 leaves nowhere else |
| revert | `edit_store.py` content-hash VFS snapshots | none; the file is rebuilt from pristine and replayed from the munge list, so removal is a property of the representation |
| what the report says | `SourceEditRecord` / `AppliedEditRecord` | the same types — this is the one piece shared verbatim |

So if you come from EVM expecting "the editor must not see the spec, therefore it cannot judge a
property-relative change", the CVLR answer is that **the editor still does not judge it — the judge
does, and unlike EVM's munge reviewer, ours is already property-aware.** That is the entire argument
of §2.1, and it is why a delegation EVM cannot make is available here.

### A.4 Three things that will trip you up

* **`cargo check` is whole-crate.** The compile gate compiles the package, not one spec file, so
  "another unit's draft is broken" used to be a real failure mode. Cargo features fixed it; see
  [single-working-tree.md](single-working-tree.md) §2.2.
* **The working copy is a real directory, not a VFS.** Cargo's input is a source tree *plus* a
  `CARGO_HOME` *plus* a `target/`, so materializing a snapshot per compile check would mean a cold
  dependency fetch and a full rebuild. Edits are real writes — but derived from checkpointed state,
  so the tree stays disposable.
* **`mock_fn` straddles the author/agent boundary.** The attribute goes in the program; the stand-in
  it names is Rust the author writes in the harness. §3 is about that seam. There is no EVM analogue
  because a CVL summary's target lives in the spec, on one side of the line.
