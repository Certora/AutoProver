# Who edits the program

> Whether the CVLR backend can move *every* modification of the program under verification behind a
> munge agent — including the property-relative kinds the author applies today — and let the author
> write only its own unit's spec.
>
> An earlier note argued this could not work; §2 quotes its three reasons and answers them. The
> shared working tree (§14–§16) changed one of the premises that argument rested on, and is
> documented here too.
>
> **Built, as a Solana-only editor** ([editor.py](../composer/spec/cvlr/editor.py)). §4's three moves
> all landed; §8 is the design it was built to, and §9 records where the build differed and what was
> deliberately left out. §9.4's first bullet — extraction, the gated pair — has since been built
> too; §10 is what that took.
>
> **Coming from the EVM backend and new to Solana?** Read the appendix (§17) first — three differences
> account for everything below, and the first one causes the other two.

---

## 1. The proposal

One entity edits the program under verification: a munge agent, reviewed by a judge. The author gets
its own unit's harness module and its own tuning file, and nothing else. `munge_function` leaves the
author's tool belt.

The claim that makes it askable is that a munge is now **scoped to one unit** — the attribute is
gated on that unit's cargo feature, so it is dormant in every sibling's build (§14.3). Cross-unit soundness stopped being a
judgement call.

## 2. What changed underneath the old objection

The earlier note gave three reasons the EVM topology (a dedicated editor sub-agent, reviewed by a
second agent) could not transfer to CVLR. They have not aged equally.

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
`certora` feature gates out changes nothing and reports nothing, and nothing checked that a munge
reached the build (since closed, §9.2). Under this proposal it gets worse, not better: the entity that would notice
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
needs — the corpus counts (Project A 43, a vault project 13) are finished projects, not per unit per run.
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
`project_file` and `ProjectSource` (`composer/spec/cvlr/tree.py`) plus the `certora/` subtree; this
turns a check into an ownership rule. Cheap,
and it is most of what "the only entity entrusted with modifying the user's code" asks for.

Note the rule needs stating precisely, because several things already write into the checkout and
should go on writing: the preflight scaffold, `declare_modules`, `declare_unit_features`, and
`write_artifact`. The line is between **the program under verification** and **the verification
scaffolding around it** — which is exactly the line `ProjectSource` and the `certora/` subtree
already draw.

**B — give the judge the diff instead of the description.** `HarnessAssumptions.briefing()` hands the
judge `f"{m.function} ({m.path}): {m.kind.describe()}"` and the author's `why`. `munge_diff` already
computes a real unified diff from state with no working tree (§15). Passing it closes the open half
of the gap "the judge cannot see what the author changed" — *"EVM's reviewer receives a diff and its approval is void the moment anything changes, whereas this judge
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
B gives the judge the artifact it should have had from the start.

~~**Do not do C yet, and collect one number first: munges per unit per run.**~~ **Done, and the
reason to reorder was §8.5's**: the editor's submit gate closes the gap "nothing checks that a munge reached the build", which is a
correctness hole
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
what makes two units' munges of one function two dormant lines rather than a conflict (§14.3). An overlay entry is a whole file's text and
`merge_vfs` is last-writer-wins, so two units editing one file is a collision with no defined answer.
Recovering isolation means one overlay per unit and one materialization per unit — which is the
arrangement the shared tree (§14) exists to have removed. Rejected.

**Option 2 — a typed write surface over the munge records.** The editor's state carries
`list[SourceEdit]`, not `dict[str, str]`. Its write tools are one per kind. The diff is `munge_diff`,
which already computes from records with no working tree. The digest hashes the record list. The gate
is `session.check(features=(certora, unit_x))`. Every property survives, and one of them survives
*because* the model never touches the feature: the tool sets it, from the requesting unit's identity.

Option 2 is the one that works, and it means the editor reuses the **control flow** and almost none of
the **machinery**. The appendix (§17) records that the boundary between the two backends was agent
topology rather than Solidity-versus-Rust; adopt the
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
summary is the editor's account of its own work. That closes the half of "the judge cannot see what
the author changed" that stayed open.

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

That closes the gap "nothing checks that a munge reached the build", which had been open since the
backend was written and is the quietest failure it had — an attribute in a file no enabled
feature reaches changes nothing, reports nothing, and leaves the report claiming a source edit that
did not happen. A build whose dep-info cannot be identified reports `NotChecked` and says so, rather
than passing.

### 9.3 The vocabulary went from two kinds to five — and then to six (§10)

Not scope creep: an editor choosing between two kinds is §2.3's objection at full strength. Added:

* **`inline_never`** — behaviour-preserving outright, and the answer to a function the optimizer
  folded away, which a summary, an inlining directive and a counterexample's stack all need a symbol
  for.
* **`hook_on_entry` / `hook_on_exit`** — `cvlr::hook_on_entry`, in the pinned reference set. The
  vocabulary's only instrument for reaching a point *inside* an execution, which is what two of the
  eight skips in the last gate run actually wanted.

**`certora_make_pub` was dropped, and the reason corrects this document.** §2 described it as a corpus munge kind, which it is — but it is a **project-local proc macro**, defined
in the one project that uses it, with no counterpart in `cvlr`. And Rust cannot make visibility
conditional on a feature without duplicating the item, so even a hand-rolled version would be the
gated-pair shape of §8.4 rather than an attribute. It is not available and should not have been
listed as though it were.

### 9.4 What was deliberately left out

* ~~**Extraction**~~ — §8.4's gated pair. The one kind that is not an attribute, and the one two
  skips wanted. It needed a record alongside `FunctionMunge` and a charter that can describe a
  restructuring, and at the time neither existed. **Built; see §10.**
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
first bullet is the next thing to build. It did, and it was; §10.

---

## 10. Extraction, built

§8.4's gated pair, and §9.4's first bullet. The vocabulary was six kinds at this point, five of
which are one attribute above a signature and one of which is not. It is seven now: the derive swap
(`DeriveSwap` in [munge.py](../composer/spec/cvlr/munge.py)) and §11's module redirect.

### 10.1 The shape, and the two things the tool refuses to delegate

`extract_function` is the editor's sixth tool. The model supplies the enclosing function's name, a
`replacement` for it, an `extracted` function it delegates to, and a justification. Everything else
is the tool's, and that is the design rather than an implementation detail — an extraction has two
properties that would be *claims* if a model wrote them and are *facts* because a record renders
them:

* **The deployed half is captured, not retyped.** `FunctionExtraction.original` is the pristine text
  of the item, read off the developer's copy at the moment the edit is recorded. The `#[cfg(not(..))]`
  half is that string, byte for byte. A model asked to reproduce the original alongside its
  replacement would eventually reproduce it *nearly*, and nothing downstream would notice.
* **Both `cfg` lines come from the record.** §8.4's requirement was that the kind "cannot be inert by
  accident"; the stronger version available here is that it cannot be *active* by accident either.
  The model never writes a `cfg`, so there is no spelling of an extraction in which the feature-off
  build sees something new.

Five checks refuse at record time rather than at the compile gate, and each is a failure a build
would either accept or report far too late to act on. Three are about the pair itself: the enclosing
signature must be unchanged (every caller compiles against whichever half its features select); the
extracted function must be `pub` (the whole point is that the author's rule can call it); and the
replacement must actually call what was extracted (otherwise the two halves do different things and
only one of them is ever built).

The other two are about what one unit already holds against the same function, and the second is the
interesting one:

* **A second split of one function, for one unit, does not build** — two definitions under the same
  feature. The compile gate would say so, in a diagnostic about duplicate symbols that names neither
  of the two edits to take back.
* **An attribute and a split of one function, for one unit, is a silent no-op.** §10.2's ordering
  puts attributes on first, so the `cfg_attr` lands above the `#[cfg(not(..))]` — correct for the
  sibling unit that recorded it, and for the unit that recorded *both* it means the attribute sits
  on the copy its own build discards. It compiles, it reports nothing, it does nothing, and the
  dep-info check that catches the other version of that failure (§9.2) cannot see it, because the
  file was compiled. The refusal points at the remedy: an attribute inside the extracted item's own
  text is gated by the item.

### 10.2 What it cost, and where

About 200 lines, and the surprising half is not the record.

**The scanner.** An attribute needs the signature *line*; a rewrite needs the whole item, which means
knowing which `}` closes it — so `munge.py` grew a small Rust lexer that skips comments (nesting),
strings, raw strings and character literals. It is 40 lines and it is the only place this backend
parses Rust rather than pattern-matching it. `'\u{1F600}'` is why the character-literal pattern
spells its escapes out: a scanner that fell through to counting that brace would mismatch every brace
after it.

**The union.** `FunctionMunge` became `Munge = FunctionMunge | FunctionExtraction`, which touched
seven modules and changed nothing in six of them beyond a type and one call site — `m.kind.describe()`
became `m.describe()`, so the report, the judge briefing and `SourceEditRecord` carry an extraction
without knowing there is a second kind of thing.

**Ordering, in `replay`.** The one place the two kinds genuinely interact. An attribute applied
*after* a split finds two definitions of one name and refuses; applied before, its `cfg_attr` lands
above the `#[cfg(not(..))]` the pair opens with — which is where it belongs, because the unit that
recorded the attribute builds with the extraction's feature off and compiles the original. So
attributes go on first, and the composition is checked against real rustc in both feature states.

**`edit_id` had to grow a digest.** An attribute's whole content is its name, so the id could spell
it out. An extraction's content is two blocks of model-written Rust, and an id blind to them would
let a re-recorded extraction inherit the previous one's review approval and the previous one's prover
stamp — the exact failure `_digest` and `munge_history` exist to prevent.

### 10.3 What the tool cannot check, and who does

**That the code moved is the code that was there.** The tool verifies the signature and preserves the
original; it cannot verify that the feature-on half computes what the feature-off half computes. That
is the reviewer's, and it is now check 4 in its prompt — with the point that the untouched copy
sitting beside it in the diff is what makes a dropped guard easy to miss rather than easy to catch.

**Whether a rule that drives the extracted piece still means the property.** That is the judge's, and
it is the one genuinely new question this kind raises. Extraction is behaviour-preserving, so it
cannot hollow out a rule the way `early_panic` and `mock_fn` can. What it does instead is *narrow*
one: it manufactures a boundary partway through a handler, and everything before that boundary —
account validation, an authority check, the bound that made the transition safe — is outside a rule
that starts at it. Both prompts say so, and the author's says it in the vocabulary of
`rule_subjects`, which is where the narrowing has to be declared.

### 10.4 What is still not there

**Visibility.** The extracted function is `pub`, but the module it lands in may not be reachable from
`crate::certora::specs`, and Rust has no feature-conditional `pub` (§9.3, on `certora_make_pub`). The
charter's answer is a refusal that names it, which is honest and is not a fix. If this turns out to
be common, the remedy is a second gated pair on the module declaration rather than a new kind.

**Two units extracting one function compose, and nothing depends on it.** The second extraction finds
the first's preserved original inside its `#[cfg(not(..))]` half and nests correctly — each feature
selects its own half and the deployed build gets the innermost original. That falls out of content
addressing rather than being designed, so it is recorded here and not advertised in the charter, and
it is only sound *across* units: two for one unit are refused (§10.1).

**The metric is unchanged.** §9.5 still stands: the skip rate, and specifically skips naming a kind
the vocabulary lacks. Extraction was the kind that kept coming back; what the next run says is
whether anything replaces it.

---

---

## 11. The module redirect, built

The eighth kind, and the one that answers a question §9 left open: **how does a munge reach a
method?** `mock_fn` replaces an item with `use <stand-in> as <name>;`, and a `use` inside an `impl`
block is not a method — the attempt fails with `E0599`. So five of the seven kinds address a
function they can name, and none of them reaches an inherent or trait method, which is where a large
share of Solana state logic lives: cross-program-invocation wrappers, checked-arithmetic traits,
zero-copy loaders.

The corpus answer, found by reading a spec branch rather than the documentation, is not to reach the
method at all. It is to redirect the file the method is *in*:

```rust
#[cfg_attr(feature = "unit_x", path = "../certora/mocks/invokes/liquidity_layer.rs")]
pub mod liquidity_layer;
```

Nothing is aliased, so the resolution problem never arises: a different file is compiled in the
module's place, carrying its own types and `impl` blocks. `docs/cvlr-backend-plan.md` §7.12 item 3
has the evidence and the two instances it was read off.

### 11.1 What the tool owns, and why

The model supplies four things: the file holding the `mod` declaration, the module's name, the
substitute's full contents, and a justification. **Where the substitute goes is derived, not
supplied** (`mirror_path`): the mocks tree mirrors the source tree, so a module declared in
`programs/lending/src/invokes/mod.rs` lands at
`programs/lending/src/certora/mocks/invokes/liquidity_layer.rs`. That is the convention the
scaffold's own `certora/mocks/mod.rs` already states and every corpus project follows, and deriving
it removes a whole class of failure — a substitute in the wrong place is a `#[path]` resolving to
nothing, and rustc's message for that names the *module*, not the mistake.

The `#[path]` value itself is computed relative to the declaring file's directory, which is what
Rust resolves it against. Minimal rather than merely correct: climbing to the workspace root and
back down resolves to the same file and breaks the moment the crate moves.

The substitute is reached **only** through the redirect. It is never declared as a module, which is
why the corpus's `certora/mocks/invokes/mod.rs` is a zero-byte placeholder — the directory exists so
git tracks it, and nothing in the module tree names what is inside.

### 11.2 The one kind with no bound on what it changes

Every other kind is constrained by the original. An attribute cannot change what a function
computes. An extraction and a derive swap both keep the original text verbatim for the deployed
build, by construction rather than by the model remembering to. A substitute module is code the
editor invents, and nothing forces it to resemble what it replaces — so **a rule proved against one
is a rule about the substitute** for everything that module was responsible for.

Three places carry that, because no single one of them can:

* The **editor** is told to reproduce the contract rather than the implementation — `nondet()` where
  the real code returns something the caller cannot predict, and any write the program later reads
  back must still happen — and to name the dropped behaviour in `why`.
* The **reviewer** gets the check the tool cannot make: read the substitute against the module it
  replaces, item by item. Is a whole module needed, or would `munge_function` reach it? Does the
  stand-in still carry every item the crate uses? Is the contract reproduced, or just the shape? And
  does `why` name behaviour rather than intent?
* The **judge** is told it cannot reason from the kind at all — for the others it can, which is what
  makes this different — so `why` is its only evidence, and a justification that says what the
  redirect was *for* rather than what it dropped means no rule reaching that module can be accounted
  for.

### 11.3 Cross-crate, and the trade it cost

A redirect inside a **local dependency** is allowed, and it is where the kind earns its keep — the
five math modules the reference project swaps live in `crates/library`, not in the program. It cannot be gated on
the recording unit's feature, because that feature is declared on the program's manifest and means
nothing in another crate, so it is gated on the shared `certora` and the program forwards it:

```toml
# programs/lending/Cargo.toml — written by the scaffold
certora = ["no-entrypoint", "dep:cvlr", "dep:cvlr-solana",
           "library/certora", "liquidity/certora", "lending_reward_rate_model/certora"]
```

`_plan_feature_forwarding` declares a `certora` feature in every local dependency and adds the
forward, for the whole workspace, at scaffold time — not when a munge first needs one, which would
edit a second crate's manifest mid-run after the feature set a build resolved is already fixed.

**The alternative was rejected on build economics.** Forwarding *per-unit* features
(`unit_x = ["library/unit_x"]`) preserves isolation and gives every dependency a distinct feature
set per unit, which reinstates the per-unit dependency build the shared tree exists to remove —
§14.5, where 4 of 515 artifacts vary today. That is not a trade the shared
tree survives.

**What it costs instead is a reporting obligation.** A redirect gated on `certora` is compiled into
**every unit's** build, whoever asked for it. Three things carry that:

* `SharedTree.run_global_munges()` is the set of munges no unit feature gates, and
  `harness_assumptions` folds them into every unit's judge briefing — deduplicated by `edit_id`, so
  the recording unit does not see its own twice.
* The briefing marks them: *IN FORCE FOR EVERY UNIT OF THIS RUN, not only this one*. A judge told
  merely that "the author edited the program" would read a dependency-wide swap as this unit's own
  choice and never think to ask whether its rules touch it.
* The editor's tool says the same thing back when it records one, so `how_to_apply` is written for a
  reader who is verifying something else entirely.

### 11.4 The one thing a scaffold cannot fix

Forwarding is written only when the scaffold creates the `certora` feature. A project that already
had one keeps it untouched — "nothing is overwritten, ever" — so an already-scaffolded project can
have the dependency-side feature and no forward to it. That combination is **silent**: a
`#[cfg_attr(feature = "certora", ..)]` inside a dependency whose feature is never enabled compiles
perfectly and does nothing, so the munge lands, the build succeeds, and the Prover reports exactly
what it reported before — the same shape of failure that cost the run in §11's opening, arrived at
from the other direction.

So the tool checks. `forwards_feature` reads the program's manifest before recording a cross-crate
redirect and refuses one that would not activate, naming the exact line somebody has to add. Failing
closed on an unparseable manifest, because guessing "probably fine" here buys precisely the silent
no-op the check exists to prevent.

---

## 12. Amending a record, and how the gap was found

The eight kinds are what the editor may *do*. This section is about the one thing it could not
**say**, which turned out to matter as much.

### 12.1 The report

An end-to-end run against a real lending program hit the following. A `mock_fn` had been recorded in
an earlier editor session, standing in for a function whose whole body is a cross-program
invocation, and its `why` said the rules would "re-apply the callee's own position update". By the
time the harness converged they did not: executing that function turned out to be blocked outright
by the Prover (`docs/upstream-defects.md` P7), and the author had replaced the call with two
hand-transcribed inequalities. The munge was still correct — the *edit* was exactly right — and its
recorded justification now described a harness that no longer existed.

The author asked the editor to fix the sentence. The editor read the request, read the code, and
called `give_up` with a diagnosis worth quoting for its precision:

> `why` is set only as a side effect of *applying*, and the apply is a no-op-and-refusal once the
> attribute is in the file, so there is no move in the seven kinds that rewrites the recorded prose
> while leaving the edit in place. The kind it would have taken is an amend/re-record operation.

That is the charter working exactly as §8 intended — a change outside the closed list is a refusal
naming what was missing, not an improvisation — and it is the first time the vocabulary-gap report
has named a *real* gap rather than a request that deserved refusing. The property judge then
required the published commentary to carry the correction inline, which is the right fallback and a
bad place for it: the commentary is per-batch prose, and the munge record outlives it.

### 12.2 Why nothing else reached it

Three separate mechanisms each stopped one step short, and they did so consistently rather than by
oversight:

* **The tools guard on identity.** Every munge tool refuses a record whose `edit_id` a unit already
  holds. `edit_id` deliberately excludes `why` — the same choice `munge_history` and `_digest` make,
  so that re-wording costs neither a prover submission nor a review — with the consequence that
  "the same munge with a better sentence" and "the same munge again" are the same call.
* **The reducer discarded it anyway.** `merge_munges` appended, deduplicating by `edit_id`. Even had
  a tool emitted the corrected record, state would have kept the first one.
* **`drop_munge` cannot reach a committed record**, by design: it is for taking back something
  applied *this session*, and dropping a landed munge to re-record it would rebuild the tree and
  invalidate the stamp — an expensive way to fix a sentence, and one that briefly leaves the program
  un-munged.

So the gap was not that anybody forbade the operation. It was that the operation had no name, and
three independent safeguards all read it as its dangerous neighbour.

### 12.3 What was built

`amend_munge`, which is an operation on a record rather than a ninth kind — it changes no code, and
the charter's count is unaffected:

* It reaches both the session's proposals and the committed records inherited from earlier ones,
  which is the case that produced the report.
* It writes the amended record into `proposed`; `merge_munges` now **replaces** a same-`edit_id`
  record rather than dropping it, so the correction survives back to the author's state. That change
  stands on its own: last-write-wins is the only reading of two records that differ solely in prose.
* `_held` and the submit candidate reduce through `munge.latest`, so an amendment of a committed
  record shadows what it corrects instead of replaying twice and reporting the second as drift.
* It voids a standing review. The file is byte-identical and the prover stamp survives — that is the
  whole point of keeping `why` out of `edit_id` — but the reviewer's subject *is* the prose, so an
  amendment slipped in after approval would ship an unreviewed justification.
* It refuses an empty `why`, an unknown id, and a "correction" identical to what is recorded. The
  four "you have already recorded exactly that" refusals now name it, which is the path the run
  needed and did not have.

### 12.4 What the episode says about the closed list

The vocabulary-gap report is cheap to add and easy to dismiss as ceremony, and this is the argument
for keeping it. The editor did not know what an amend operation was; it derived that one must exist
from the shape of what it could not do, and named it accurately enough to implement from. A charter
without that escape hatch would have produced either an improvised edit or a silent mis-report, and
neither leaves evidence.

The list stays closed. What this adds is that a refusal naming a missing *operation* — as distinct
from a missing *edit* — is a design signal and should be read as one.

---

## 13. The import swap, built

The ninth kind, and the one that answers a question the other eight share a blind spot on: **how
does a munge reach a function the program does not own?** Every kind above edits the item it is
about — an attribute on it, a split of it, a redirect of the module holding it — so the item has to
be the program's, and [§8.2](#82-the-decision-that-determines-everything-what-the-editor-edits) is
where that was decided and why it stays decided. A cross-program invocation is the case where it
does not hold. `invoke` belongs to `solana_program`; a munge of a dependency changes its behaviour
for every crate in the graph, including the ones the property is about.

It is not an academic gap. The pinned vault run asked the editor twice for a boundary around
`vault_program::deposit`'s inline `invoke`, was refused both times, and recorded the reason in its
own skip — *"Unstatable without a model of the System Program transfer CPI, and the program cannot
be given one with the munge kinds available."* Two properties were skipped and the handler was given
up. `docs/cvlr-todo.md` U10 is that report.

### 13.1 The seam is the `use` line

What the program *does* own is the declaration that puts the name in scope. So:

```rust
#[cfg(not(feature = "unit_x"))]
use anchor_lang::solana_program::program::invoke;
#[cfg(feature = "unit_x")]
use crate::certora::specs::unit_x::invoke_transfer as invoke;
```

`mock_fn` replaces a definition with a `use`. This replaces a `use` with a `use` — the same
mechanism, reached from the calling side, which is the only side available. The deployed build
compiles the developer's declaration verbatim, because it is still on the page rather than retyped;
that is the property [§10](#10-extraction-built) had to store an `original` to get.

The corpus writes this edit by hand and has for longer than the backend has existed.
`solana-program-stake-pool-audit`'s `processor.rs` opens with exactly this pair for
`solana_program::msg`, and its CPI wrappers get their boundary the same way one layer up — a trait
named for the methods, imported by a `use crate::certora::mocks::*` that is itself gated. So the
kind is not an invention; it is the fifth idiom a corpus survey found, arriving late because the
first four were all about items.

### 13.2 What it is bounded by, and what it is not

Narrower than a module redirect in the way that matters: **it creates no file.** The stand-in is an
item in the harness, written by the author and reviewed as part of the harness, exactly as a
`mock_fn` stand-in is. The editor never authors it. So the one kind where the reviewer has to read
invented code stays the one kind.

Wider than any of them in a different direction, and this is the thing to keep saying: **the swap is
scoped to the file, not to the call.** Every mention of the name below the declaration resolves to
the stand-in, including calls the request said nothing about. A call-site-scoped alternative was
considered and rejected — rewriting one expression and not its neighbour is a change to what the
program does, wearing an edit's clothes, and it would put the editor back in the business of
authoring Rust that the deployed build never sees a counterpart of.

Three refusals carry the rest. A name the file never imports — spelled out in full at the call site,
or arriving through a glob — has no declaration to rewrite, and is reported as such rather than
half-matched. A nested `use` tree cannot have one leaf taken out of it by a split on commas, and
flattening a developer's import to suit a munge is a change to the deployed build. And a file with
two declarations binding the name is refused rather than guessed at, for the reason every ambiguity
in this module is.

The union replay needed one thing the other kinds did not: a declaration gated *on* another unit's
feature is dormant for this build and must not be read as a second binding, or the second unit to
swap an import would report drift against the first unit's output. `_dormant_here` is that, and it
is the only place in this module where one munge has to recognise another's work.

### 13.3 What a CPI stand-in may claim

The kind that made this necessary is also the one where the soundness question is live, so it is
worth stating rather than leaving to each `why`.

The Prover's own replacement for a cross-program call havocs the **caller's** deserialized
`Account<T>`, not merely the account bytes the callee could reach (`docs/upstream-defects.md` P6).
That is what defeats a property about what the program recorded *after* a transfer. A stand-in that
returns `Ok(())` is therefore not a loss of fidelity but a recovery of one: the caller stays
analysable, and the handler's own bookkeeping becomes provable with real account validation and the
real `Context` — which is precisely what the shipped P6 remedy, descending to the accounting core,
gives up.

What it does not do is move anything. So the two halves of a batch separate cleanly, and the
separation is the author's to make:

* a property about **the program's recorded state** is sound under the swap;
* a property about **the lamports or tokens the CPI actually moved** is *false* under it rather than
  merely unproven, which is worse, and is a skip.

Nothing about this is novel to us. Every CPI mock in the corpus drops the callee's effect —
`stake-pool`'s `create_stake_account` assumes the account's shape and returns `Ok(())`, and the reference
project's liquidity-layer stand-in reimplements the accounting it wants and nothing else. The corpus settled
the question of whether a CPI stand-in is publishable before we asked it. What the backend adds is
that the trade is written down in `why` and carried to the judge, which a hand-written harness has
nowhere to put.

### 13.4 What the first real run changed, and the hole it opened

`swap_import` was exercised end to end on 2026-09-21 against the pinned `Deposits` unit of
`test_scenarios/solana_vault_idl` — the component whose 2026-09-18 run gave up the handler with
*"the program cannot be given one with the munge kinds available."* Nine prover jobs, 1h36m, seven
of nine properties verified, three skipped. `deposit_credits_exactly_amount`, the property that skip
was about, verified against the real handler through the real `Context`.

The chain behaved: the author reached the kind from the prompt before its first draft and described
the effect it needed without naming the kind; the editor chose `swap_import` and justified it from
the charter; the reviewer opened `lib.rs` and counted call sites rather than accepting the editor's
scope claim; the judge engaged with the CPI model instead of waving seven green rules through. Two
things the run taught that no amount of prompt-writing would have:

* **The kind redirects a name, so it redirects a module.** The load-bearing second swap was
  `use anchor_lang::solana_program::system_instruction;` — a module, not a function. It was built
  for functions and the module case falls out, which is fortunate rather than designed.
* **A CPI stand-in must neither move lamports nor decode the instruction's payload.**
  `AccountInfo::lamports` on a handler-built info is `[3003]` through the `Rc`; a bincode payload is
  a heap buffer the Prover does not relate to the bytes read back, so every decoded field is
  unconstrained and every rule downstream of one returns a nonsense counterexample. Both are now in
  the author prompt; the run paid two prover jobs to find them.

#### The hole: a stand-in's justification can go stale and nothing says so

The judge blocked the first draft on this, and it is a defect in the design rather than in the run:

> *The `invoke` munge record materially misdescribes the stand-in it redirects to. … The redirect
> points at whatever the harness currently defines, so the record is stale.*

The author rewrote `invoke` twice after the munge was recorded and approved. The `why` went on
describing a stand-in that decoded the payload and moved the lamports — which, read against the
rules shipped beside it, would have made one assertion false and another double-count.

:class:`~composer.spec.cvlr.munge.ModuleRedirect` and
:class:`~composer.spec.cvlr.munge.FunctionExtraction` cannot suffer this: their ``edit_id`` digests
the code they carry, so re-authoring it is a different edit that inherits neither the review nor the
prover stamp. :class:`~composer.spec.cvlr.munge.ImportSwap` and
:class:`~composer.spec.cvlr.munge.MockFn` carry a *path*, and the item at the end of it belongs to
the author's harness — which the author may rewrite freely, and does.

So the two kinds that point at an author-owned stand-in have no mechanical link between the record
and the thing recorded. `mock_fn` has shipped with this since the beginning. It was caught here by a
judge choosing to reconcile the record against the artifact, which is diligence rather than
architecture, and the same judge found a *second* stale record in the same draft — the `early_panic`
one, describing the rule as it was when the munge was requested.

What to do about it is a decision rather than a fix, which is why it is `docs/cvlr-todo.md` U12 and
not a patch. The options are not equivalent: digesting the stand-in's *source* into `edit_id` would
invalidate a review on every unrelated edit to the harness file; digesting only the named item needs
a resolver this module does not have; and simply prompting the author to `amend_munge` after
rewriting a stand-in is what already happened, one judge round late.

---

## 14. One working tree for the run

Every unit of a run builds in one shared copy of the project, at `.cvlr_work/build/`. This replaced
a per-unit workdir, which [cvlr-backend-plan.md](./cvlr-backend-plan.md) §7.5.2 had recorded as
*forced*: its argument was that the compile gate is whole-crate, so in a shared tree unit A's gate
fails whenever unit B's draft is momentarily broken. That is true only while every unit's module is
compiled. Three mechanisms together make it false, and each is inert without the others.

### 14.1 Per-unit cargo features gate the harness modules

`specs/mod.rs` declares each unit's module behind its own feature, and the package manifest gains
one empty feature per unit:

```rust
#[cfg(feature = "unit_deposits")] pub mod deposits;
#[cfg(feature = "unit_admin")]    pub mod admin;
```

```toml
certora = ["no-entrypoint", "dep:cvlr", "dep:cvlr-solana"]
unit_deposits = []
unit_admin = []
```

A unit gates with `cargo check --features certora,unit_deposits` and submits with the same set
through the conf's `cargo_features`. `HarnessTarget.features` ([verify.py](../composer/spec/cvlr/verify.py))
is the one place that pairs `certora` with the unit's feature, for both, so the gate build and the
prover's rerun cannot end up with different feature sets. `declare_modules`
([harness.py](../composer/spec/cvlr/harness.py)) and `declare_unit_features`
([scaffold.py](../composer/spec/cvlr/scaffold.py)) write both declarations once, in the staged
formalizer's `begin`, when every unit is known.

**The features must stay empty.** A unit feature that enabled a dependency feature would change the
dependencies' resolved feature set, give them separate fingerprints, and reinstate the per-unit
dependency build this removes. The same rule holds for the munge gates in §14.3.

The gate also removes a fragility: under a `cfg` gate, a unit whose module file does not exist yet is
invisible to everyone else, where before every declared module had to exist or no unit compiled. And
it fixed a latent `E0119`: two units each writing `impl Nondet for Foo` in their own module no longer
meet, because at most one of the modules is compiled.

### 14.2 rustc's dep-info is what makes it correct

rustc emits, per compilation, the list of source files it actually read, and cargo fingerprints
against that list. A module behind a disabled `cfg` is never read, so it never appears in the
dep-info, so **the `unit_admin` variant's fingerprint does not depend on `deposits.rs`**. That holds
for dirtiness as well as breakage: A's edits do not force B to rebuild.

**Measured:** with `unit_b`'s module made syntactically invalid on disk, `--features
certora,unit_a` finished in 0.00s having compiled nothing, while `--features certora,unit_b` failed
with the expected parse error.

### 14.3 Munges are gated on the unit's feature, so they are additive

`cvlr::mock_fn` takes a `when` parameter naming a cargo feature (default `"certora"`) and expands to
the original function under `#[cfg(not(feature = when))]`. The corpus uses it to keep a menu of mocks
in the source, each dormant until a conf asks for it: a vault project declares eleven such features
under the comment *"Per-conf prover mocks; enabled via `cargo_features`. Inert without `certora`."*

This backend does the same thing by gating the whole `cfg_attr` on the recording unit's feature
rather than using `when`. Both produce the identical result, and the `cfg_attr` form is one mechanism
for every kind, where `when` would not reach `cvlr::early_panic`, which has no such parameter. (The
corpus gates `early_panic` by wrapping the `cfg_attr` condition too, 60 times.) So two units' munges
of one file coexist as two dormant lines, and the union of every unit's munges is the single
well-defined content of that file. Every kind in [munge.py](../composer/spec/cvlr/munge.py) carries
its unit's `feature`, and its `edit_id` includes it.

### 14.4 The build permit

One `asyncio.Semaphore(1)` for the run (`SharedBuild.build_sem`,
[pipeline.py](../composer/spec/cvlr/pipeline.py)), held across staging and the local cargo
invocation, and, for a submission, until the Prover has uploaded what it built.

It is not what makes a `cargo check` correct; §14.1 and §14.2 do that, and concurrent cargo
invocations against one `target/` serialize on cargo's own lock anyway. What the permit buys is a
queue the host can report ("waiting for a build slot" instead of a silent stall), no N sandboxed
processes parked holding grants, and one place for ordering if a unit ever starves.

**For a submission it is what makes the tree correct.** Every SBF build of the crate produces the
same `.so`, and `certoraSolanaProver`'s local phase rebuilds it and then uploads it. A sibling
building for its own feature in between would replace the file this unit's job uploads, so the job
would verify the wrong program (found reviewing #258). A target directory per unit fixes that too,
and was tried and backed out: it costs a cold dependency build and about 1 GB of disk per unit. So
`_stage_and_submit` ([verify.py](../composer/spec/cvlr/verify.py)) holds the permit until the job's
link arrives, which `certoraRun` reports after the upload. What is serialized is each submission's
local phase, a warm rebuild of the program crate plus the upload. The cloud job's minutes stay
concurrent. Every derived write is `os.replace`-atomic, so a concurrent reader sees the old file or
the new one.

### 14.5 What it costs

Measured on the repository's three-unit Solana test scenario (213 crates resolved):

| | Per-unit workdirs (N units) | One tree |
|---|---|---|
| source copy | 104 KB × N | 104 KB |
| `.sandbox_cargo/registry` | 119 MB × N, **re-downloaded per unit** | 119 MB, fetched once |
| `target/` | 955 MB × N, **dependencies recompiled per unit** | ~955 MB + N × the program crate's own artifacts |
| dependency builds | N | 1 |

`target/debug/deps` held 515 artifacts, of which **4** belong to the program's own crate. The rest
are dependencies that cannot differ between units: same lockfile, same `certora` feature, same
toolchain. Feature-varying duplicates the 4, not the 515. **Measured** on a synthetic crate with one
dependency and two empty unit features: the dependency compiled once and kept one `-C metadata`
hash across both feature sets, and returning to `unit_a` after `unit_b` ran zero rustc invocations.

**What gets worse is wall clock in steady state.** Units used to build concurrently: N× the work,
but max-latency rather than sum. Serialized, a gate can queue behind N−1 others. On a cold run the
sum should still beat the old max, because each queued gate is one crate-variant recompile rather
than a dependency build; on a warm run it will not. Only the local build queues (§14.4), so the
queue is seconds deep, not minutes. This has not been measured (§15.4 item 5).

### 14.6 Confinement

The sandbox gives a build a private `CARGO_HOME` because a confined build runs untrusted `build.rs`
and proc-macro code, and *"a malicious build could overwrite an extracted source under
`registry/src` and poison a **later** run that compiles that crate"*
([recipes.py](../composer/sandbox/recipes.py)). One tree means one cargo home shared by every unit of
a run. Within a run, isolation drops from per-unit to per-run, but the untrusted code is the target
program's own build scripts, identical for every unit, so unit A poisoning a cache unit B reads is
the same code either way. Across runs the protection holds because each run starts a fresh tree
(§15.2). [command-sandbox.md](./command-sandbox.md) §11 owns the confinement posture.

### 14.7 What the corpus does, and why the spec cannot leave the crate

Source munging is not one project's habit. Counting `cfg_attr`-with-`certora` outside `certora/`
trees, nine of eleven corpus projects carry it: restaking 73, spl 50, Project A 43, stake-pool 21, a
vault project 13, smart-account 12, manifest 10, vault-tutorial 5, token 3. Only the reference
project and SolanaExamples are clean. The vocabulary across them is six kinds: `early_panic` (152
sites), `mock_fn` (~47), `certora_make_pub` (6), `cvlr_hook_on_entry`/`on_exit` (9),
`inline(never)` (5) and `derive(Copy)` (2). §9.3 records which of these the editor offers.

A fully separate spec crate, the Crucible shape, is **not** available, and the reason is not Rust
versus Solidity. A fuzzing harness can live outside the crate because it is black-box: it drives the
program through transactions and reads accounts back. A CVLR rule is white-box: it calls handlers
directly, which is what `no-entrypoint` is for. Three blocks: 0 of 12 corpus projects use a separate
spec crate (all are `<program-crate>/src/certora/`, and multi-crate workspaces put a `certora/`
subtree in each crate they verify); `mock_fn` and the hooks expand at the program's own definition
site and name `crate::certora::…`, so a separate crate would have to be a dependency of the program;
and `certora_make_pub` shows specs reaching items that are not even `pub(crate)`. What transfers
from the fuzzer is the other half: make the source-side footprint inert and per-unit selectable,
share one crate, and serialize the builds.

### 14.8 What would change this answer

1. **Cargo stops sharing dependency artifacts across the units' feature sets**, or a future resolver
   change makes a leaf feature propagate. The cost case is that one fact.
2. **A munge kind that cannot be made dormant.** Every kind so far is gated on the unit's feature;
   a change that cannot be (a hand-unrolled loop) would make one tree hold two incompatible versions
   of a file.
3. **Unit counts grow past the point where serialization dominates.** At some N the queue is the
   bottleneck, and the answer is a permit count above 1 with per-unit target directories for the
   overflow, which nobody has costed.
4. **Confinement tightens** to require per-unit isolation of untrusted build code. §14.1–§14.3
   survive, but the cargo-home saving does not, and most of §14.5's win goes with it.

---

## 15. The tree is derived, not state

### 15.1 A pure function of checkpointed state

```
tree = the project + each unit's draft + the union of every unit's munges
```

Every term after the first is in the run's checkpointed state: munges and summaries are reduced
state keys ([state.py](../composer/spec/cvlr/state.py)), and the draft is the authoring buffer, of
which state.py says *"The ground truth is the buffer, not the run."* Replay is pure, and the tuning
files are rewritten wholesale from their directives rather than appended to. So the tree is a build
cache: losing it costs a rebuild and never correctness, which is what lets a resumed run, or one
replayed from cache, pick up where it left off.

This is a stronger position than the EVM backend's. EVM must checkpoint its VFS overlay verbatim,
because its edits *are* file text: an agent rewrote a Solidity file and the bytes are the only
description of what it did. CVLR's edits are records, and the text is derived from them, so
dropping a record from state removes the edit, which a text overlay cannot express. That is what
`revert_munge` and `DropMunges` rely on.

### 15.2 A fresh tree per run

`SharedTree.materialize` ([tree.py](../composer/spec/cvlr/tree.py)) deletes any tree a previous run
left and copies the project afresh, once per run, before any unit stages. Within the run the tree is
reconciled in place, so the warm `target/` and the private `CARGO_HOME` persist across units and
gates. It is never reused across runs, because the tree holds the run's private `CARGO_HOME`, whose
unpacked sources a dependency's build script can rewrite, and a tree carried into a later run would
carry that rewrite into its build. The previous run's tree is left until the next run starts, so it
can be inspected.

An earlier version reused an existing tree on resume (`if dest.exists(): return`). That failed for
reasons unrelated to how many trees there are: a crash between a tool call and the checkpoint write
leaves the tree *ahead* of state; resuming a checkpoint that is not the latest leaves it ahead by an
arbitrary amount, with munges its state no longer knows about; and a cache replay has no tree at all.

### 15.3 Three care points

1. **Replay the union of all units' munges, not the current unit's.** Replaying one unit's would
   leave a sibling's munge lines absent until that sibling next stages, and units would fight over
   the file on alternating gates. Feature gating (§14.3) is what makes the union safe, and it is the
   only stable fixed point. Replay is ordered by `edit_id`, because two munges of one function each
   insert a line above its signature, and bytes that depended on which unit staged first would make
   the crate's fingerprint depend on it too.
2. **Content-compare before writing.** Cargo fingerprints on mtime, so rewriting identical bytes
   forces a rebuild. The materializer reads, compares, and writes only on a difference.
3. **Surface drift, do not log it.** Reconstruction reads the developer's project, which can move
   between a cached run and its replay. Replay returns typed refusals (`FunctionNotFound`,
   `FunctionAmbiguous`, `AlreadyMunged`, `SourceDrifted`), so a munge that no longer applies is
   detectable, and the gate puts it in front of the author (`Reconciled.drifted`). A munge that did
   not reach the build is as much a part of why a rule failed as of what a passing rule means.

The run-level declarations (`specs/mod.rs`, the package manifest's unit features, the placeholder
module files) are not derived from any unit's state. They are a function of the job list, and they
are written into the project, as deliverables, before the tree is copied.

### 15.4 The checks, and which are still open

1. **Multi-variant caching** passes on host cargo (§14.5). Still open for `cargo certora-sbf`, which
   is the same cargo with a different triple and ought to behave identically; it belongs in the
   expensive gate.
2. **Dependency fingerprints do not vary with the unit feature.** Passes (§14.5).
3. **Cross-unit independence.** Passes (§14.2).
4. **The disposability invariant** is covered by unit tests
   ([test_cvlr_tree.py](../tests/test_cvlr_tree.py)): deleting the tree and reconciling reproduces
   it byte for byte, and a munge dropped from state leaves the file.
5. **Latency under contention.** Not measured. Time a three-unit run with and without the shared
   tree before believing either half of §14.5.

---

## 16. The tree is a VFS

### 16.1 Why: a run that read its own working tree

The first authoring run against a real program (SPL stake-pool, 2026-09-04, run
`73968efd08574d93b162da1176d3d10f`) died in formalization:

```
FORMALIZATION: Pool Initialization & Admin Configuration (5 properties)
  16.5s   AnthropicContextOverflowError
  prompt is too long: 2,272,575 tokens > 1,000,000 maximum
```

The author's third tool call was `list_files`, and it returned 4,087,756 characters, 28,739 lines of
them under `.cvlr_work`, the run's own working tree. The immediate cause was an exclusion pattern
that missed the sandbox's `CARGO_HOME` one level down. The deeper one was that the author's read
tools and the build were rooted in different places: the reads in the pristine project, the build
in the tree. Four gaps followed from that:

| gap | consequence |
|---|---|
| `get_file` read the pristine project; the build read the tree | an author asking what a function does got an answer about a different text from the one verified |
| `mock_fn` replaces a function body | the author reasoned about a body the build did not use |
| the munge list reached the author once, in a tool result | after summarization nothing restated which functions were munged |
| `cargo check` runs in the tree | diagnostics named `.cvlr_work/build/...` paths the author could not open |

Every one is "the read path and the build path disagree", which is the condition a VFS exists to
prevent. The earlier objection, that materializing for cargo means a cold fetch and a full rebuild
per compile check, was a property of graphcore's `TempDirectoryProvider` (a fresh directory per
materialization), not of materialization. A materializer that writes into a persistent directory
and skips unchanged files leaves `target/` and the `CARGO_HOME` alone, because they are not VFS
content.

### 16.2 What was built

- **The tree** is a graphcore `PersistentMaterializer` (graphcore #39): the project as its base
  (`DirBackend`), and one overlay (`DictBackend`) holding every unit's draft and every munged file's
  replayed content. A path the overlay stops serving is put back to the project's copy, or removed
  if the project has no such file. The base/overlay split is load-bearing: a dropped munge must
  restore the developer's file, and a departed unit's draft must disappear, and only the base can
  tell the two apart. Paths the tree is never filled from (`target/`, the sandbox's directories) are
  excluded by `_NOT_MATERIALIZED`.
- **Every agent reads through a two-layer stack**, the tree above the project, built by
  `build_layered_source_tools` (`fs_tools_layered`) in [entry.py](../composer/spec/cvlr/entry.py).
  Before the tree exists, during analysis and extraction, every read falls through to the project.
  Once it exists, a munged file reads as munged. The listing cache is off on both layers, because
  the build writes the tree while agents read it.
- **Diagnostics paths needed no rewriting.** cargo prints paths relative to its workdir, which is the
  tree, and the read stack serves the same paths: a deliberate error produced `-->
  program/src/lib.rs:2:5`, and `get_file` on that string opens the file.
- **The munge boundary** is `project_file` and `ProjectSource` in
  [tree.py](../composer/spec/cvlr/tree.py), with typed refusals (`NotInProject`, `NotProjectSource`)
  rather than a VFS path check.

What was planned and not built: a three-layer stack per unit (draft, munges, project), with one
`fs_tools_layered` call yielding both a unit's read tools and its materializer. The tree holds every
unit's draft instead, since a sibling's module is inert behind its feature (§14.1), and the read
stack and the materializer are built separately over the same directories.

### 16.3 Risks

1. **Listing a tree that holds a warm `target/` and a 730 MB `CARGO_HOME`.** Answered: the exclusion
   is passed, and a probe over a tree seeded with a 500-file vendored registry listed two entries,
   the project's own sources.
2. **A cached listing over a directory the build is writing.** Answered by turning the cache off
   (§16.2).
3. **The Prover uploads from disk.** `certoraBuildRust.collect_files_from_rust_sources` globs the
   real filesystem, so materialization must be complete before submission. It is, because staging
   runs under the permit before the submission (§14.4). A lazier materializer would break this
   silently.
4. **`get` returns `str`.** Anything non-UTF-8 in the tree is outside the model. Rust sources are
   fine; nothing else has been checked.

---

## 17. Appendix: the EVM and CVLR backends, side by side

For a reader who knows the EVM backend. Three differences account for this document, and the first
causes the other two.

1. **The spec lives inside the program.** A CVL spec is a separate file beside the contracts. A CVLR
   rule is Rust compiled into the program's own crate (§14.7), and it calls handlers directly. So
   the program and its harness are one build, and every edit to the program reaches every unit's
   build unless it is gated (§14.3).
2. **There is no spec-side approximation that computes.** EVM keeps property-relative approximation
   on the spec side, in CVL summaries the author owns, and gives an agent only behaviour-preserving
   rewriting of the source. CVLR's only spec-side instrument, `summarize_for_prover`, havocs rather
   than computes. So `early_panic` and `mock_fn`, which are property-relative, have to be source
   edits, and something that holds the properties has to rule on them: the judge (§2.1).
3. **The build is cargo.** solc's input is a source tree, so EVM materializes its VFS overlay into a
   temporary directory per compile check and pays a file copy. cargo's input is a source tree plus a
   `CARGO_HOME` plus a `target/`, so CVLR materializes into one persistent tree per run (§15, §16).

How each backend gives an agent a modifiable copy:

| | EVM | CVLR |
|---|---|---|
| the edits | file text, in a `dict[str, str]` overlay in graph state (`VFSState`) | records (`Munge`), in graph state; the text is derived |
| what a build sees | a `TemporaryDirectory` written from the overlay and the project, per compile check | `.cvlr_work/build/`, a fresh copy per run, reconciled in place |
| reverting an edit | pick an older snapshot from the edit store | drop the record from state; the tree is re-derived |
| who edits | a munge editor, reviewed behaviourally; it never sees the properties | a munge editor with its own reviewer, and the property judge rules on what the munges did to each rule |
| what may change | Exposure, Refactor, Standardize; behaviour-preserving | a closed set of kinds ([munge.py](../composer/spec/cvlr/munge.py)); property-relative |

What CVLR reuses from the EVM munge machinery: the report's `SourceEditRecord` and
`AppliedEditRecord` (a munge reaches `report.json`'s `source_edits`), the `Formalizer.source_edits()`
hook, and `munge/vfs_diff.py`'s diff. What it does not reuse is everything that follows from EVM's
agent topology and solc: `munge_agent.py`, `compile_check.py` (solc-specific), `edit_store.py`,
`edit_oracle.py`, `erc7201.py`, and the three munge templates.
