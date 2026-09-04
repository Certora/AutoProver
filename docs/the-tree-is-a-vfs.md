# The working tree is a VFS

> [munge-and-working-copies.md](munge-and-working-copies.md) §3 steps (5) and (6) argued that a
> virtual filesystem is the wrong instrument for this backend. Steps (1)–(4) were superseded by
> [single-working-tree.md](single-working-tree.md); this document retires the other two. (5) rests
> on an assumption about how a VFS materializes rather than on any property of cargo, and (6) has
> since become false.
>
> The recommendation is to replace `SharedTree`'s bespoke half with graphcore's
> `fs_tools_layered`, keeping the materialization strategy that makes cargo cheap.
>
> **Built.** §7's steps 1–3 are done — graphcore's `eric/persistent-materializer` branch and
> `cvlr: the tree materializes through the VFS`. Step 4 turned out to need no code; §7 says why.
> Of §6's five risks, one was answered by building it and four are still open.

---

## 1. Why this is being reopened, and what settled it

The question was raised twice and answered from cost both times. What was missing both times was a
run: the arrangement was defended on the price of the alternative, never on what the arrangement
itself cost, because nothing had yet gone wrong with it.

Something has now. The first authoring run against a real program — SPL stake-pool, 2026-09-04, run
`73968efd08574d93b162da1176d3d10f` — reached formalization and died there:

```
FORMALIZATION: Pool Initialization & Admin Configuration (5 properties)
  16.5s   AnthropicContextOverflowError
  prompt is too long: 2,272,575 tokens > 1,000,000 maximum
```

The author's system and user prompts were 33 KB and 27 KB. Its third tool call was `list_files`,
and that returned **4,087,756 characters — 28,904 lines, 28,739 of them under `.cvlr_work`**, the
run's own working tree. The immediate cause was an anchored exclusion pattern that missed the
sandbox's private `CARGO_HOME` one level down, and that is fixed. The reason the run could see the
tree at all is this document's subject.

Cost of that run: ~$100 of LLM spend, 56 minutes, zero rules authored.

---

## 2. Step (5) does not survive

> **(5)** *Therefore a VFS would have been expensive here in a way it is not for solc. solc's input
> is a source tree, so materializing one is a file copy. `cargo`'s input is a source tree plus a
> `CARGO_HOME` plus a `target/`. A materialized cargo snapshot is either a cold 119 MB fetch and a
> full rebuild per compile check, or it is not isolated at all.*

Every clause about cargo is true. The conclusion does not follow, because "materialized snapshot"
is doing work the premise never establishes: it assumes materialization means *a fresh directory per
compile*.

That is true of the EVM path, which is where the intuition came from —
[compile_check.py:125](../composer/spec/source/munge/compile_check.py#L125) does
`with accessor.materialize(state) as tmp`, a temp dir per check. It is a property of
`TempDirectoryProvider`, not of `Materializer`, whose contract is only:

```python
class Materializer(Protocol):
    async def dump_to(self, target: pathlib.Path) -> None: ...
    def get(self, path: str) -> str | None: ...
```

`target` is a caller-supplied directory. Nothing requires it to be new, empty, or discarded. A
materializer that writes into a *persistent* directory and skips files whose content is unchanged
leaves `target/` and the private `CARGO_HOME` exactly where they were, because it never touches
them — they are not VFS content, they are things that happen to live in the same directory.

This is not hypothetical. It is what [tree.py](../composer/spec/cvlr/tree.py)'s `_write_if_changed`
already does, and §2 of [single-working-tree.md](single-working-tree.md) measured why it has to:
rustc's fingerprint is content plus mtime, so an unconditional rewrite of an unchanged file costs a
rebuild. The requirement step (5) treats as disqualifying is one this backend already implements —
just on the wrong side of the abstraction.

**What (5) is actually right about** is that `TempDirectoryProvider` would be ruinous here, and that
a VFS whose only materializer is a temp dir is the wrong instrument. That is a statement about the
provider, and the fix is a second provider.

---

## 3. Step (6) is now false

> **(6)** *Therefore edits are real writes, and there is no revert. Once the working copy is a
> private directory that a build already sees, the overlay has nothing left to buy.*

Both halves have been overtaken.

**"There is no revert"** — `DropMunges` and the author's `revert_munge` tool exist, and the tree is
rebuilt from the surviving munge list on every build
([single-working-tree.md](single-working-tree.md) §4). The doc's own §8 gap 3 was updated to "half
closed" when the representation gained removal, and the editor supplied the other half. Nobody
followed the thread back to (6).

**"The overlay has nothing left to buy"** — this is the load-bearing clause, and it is wrong in a way
the stake-pool run made concrete. The overlay buys *the author reading what the build compiles*.
Without it, four gaps follow mechanically, and they are not cosmetic:

| gap | consequence |
|---|---|
| `get_file` reads the pristine project; the build reads the tree | an author asking "what does this function do" gets an answer about a different text than the one verified |
| `mock_fn` replaces a function body | the divergence is semantic, not cosmetic: the author reasons about a body the build does not use |
| the munge list reaches the author once, in the `code_editor` tool result | after summarization nothing re-states which functions are munged; `HarnessAssumptions` renders exactly this and is wired to the **judge** only (`input_lift=with_assumptions`) |
| `cargo check` runs in the tree | diagnostics name `.cvlr_work/build/...` paths, which after the exclusion fix the author cannot open at all |

Every one of these is "the read path and the build path disagree". That is the condition a VFS
exists to make impossible.

---

## 4. The observation that should have come first

`SharedTree` is a VFS. Not "like" one — the same design, built separately:

| `SharedTree` | graphcore VFS |
|---|---|
| edits are checkpointed state; the tree is derived from them | edits are `VFSState`; disk is derived |
| pristine + union of munges + per-unit draft, in that precedence | layered backends, first hit wins |
| `_write_if_changed`, content-compared, `os.replace`-atomic | `Materializer.dump_to` |
| `resolve()` returning `NotInWorkdir` / `NotProjectSource` | path normalization and `handle_path_errors` |
| `DERIVED_MANIFEST` tracking what the last materialization wrote | the state the accessor materializes from |

The signature that matters is the one this reimplementation lost:

```python
def fs_tools_layered(backends, forbidden_read=None, global_exclude=None)
        -> tuple[list[BaseTool], Materializer]
```

**The read tools and the materializer come out of one call over one backend stack**, precisely so
they cannot disagree about what the composite view contains. Building them as two separate things —
source tools rooted at the pristine project, materialization rooted at the tree — is what allowed
them to disagree, and every gap in §3's table is that disagreement showing up somewhere.

---

## 5. The design

Per unit, a three-layer stack in precedence order:

```
[ this unit's draft ]   (the harness module it is authoring)
[ the munges        ]   (run-wide: the union every unit shares)
[ the pristine project ] DirBackend over the real checkout
```

One `fs_tools_layered` call per unit yields that unit's read tools *and* the materializer that feeds
its build. The author's `get_file` then returns munged content for a munged file, and pristine
content for everything else, with no further wiring.

Three properties carry over unchanged from the current design and must not be lost:

- **Materialization is incremental into a persistent directory.** This is the new provider, and it
  is the whole of §2's answer.
- **The scopes differ**: munges are run-wide, drafts are per-unit. The stack expresses this; a
  single flat overlay would not.
- **Builds stay serialized behind the existing permit.** Concurrent materialization into one tree
  needs exactly the mutual exclusion the permit already provides.

---

## 6. What could still sink this

Written before implementation, so these are open questions rather than answered ones.

1. **`DirBackend.list` is `rglob("*")` over the root.** Pointed at a tree containing a warm
   `target/` and a 730 MB `CARGO_HOME` that is the 28,904-line listing again, from the other side.
   `fs_tools_layered` takes `forbidden_read`, so the exclusion is expressible — but it must be
   passed, and the listing is enumerated before filtering. Whether that enumeration is fast enough
   over ~20k files, per unit, is a measurement nobody has taken.
2. **`cache_listing` defaults to `True`.** A cached listing over a directory the build is actively
   writing is a staleness question this design has not answered.
3. **The prover uploads from disk.** `certoraBuildRust.collect_files_from_rust_sources` globs the
   real filesystem, so materialization must have happened before submission and must be complete.
   That is true today; a lazier materializer would break it silently, which is the failure mode
   `WORK_DIR`'s own docstring already records for a different cause.
4. **`get` returns `str`.** Anything non-UTF-8 in the tree is outside the model. Rust sources are
   fine; whether anything else needs to be there is unchecked.
5. **This is a graphcore change**, and graphcore is pinned by commit in `pyproject.toml`. A
   persistent-directory materializer belongs upstream rather than as a local subclass, which makes
   the change cross-repo and slower than the diff suggests.

---

## 7. Migration

Ordered so that each step is independently revertible and none is a flag day.

1. **Land the persistent materializer in graphcore**, alongside `TempDirectoryProvider`. `SharedTree`
   is the reference implementation; `_write_if_changed` is the body.
2. **Build the stack per unit** and take the read tools from it, leaving the existing materialization
   in place. At this point the author reads the composite view — which closes §3's table — while
   nothing about the build changes.
3. **Retire `SharedTree`'s own materialization** in favour of the stack's, keeping `resolve()`'s
   typed refusals as the munge-boundary check they also serve
   ([munge.py](../composer/spec/cvlr/munge.py)'s `NOT_PROJECT_SOURCE`).
4. ~~**Rewrite diagnostics paths** back to project-relative before they reach the author.~~
   **Not needed — step 2 subsumed it.** cargo emits diagnostic paths relative to its workdir, and
   its workdir is the tree; the author's read stack is now rooted at the same place, so the path
   the compiler prints is already a key the composite view serves. Measured, not assumed: a
   deliberate error produced `--> program/src/lib.rs:2:5`, and `get_file` on that exact string
   opens the file. The gap was never in the paths — it was that the reads were rooted somewhere
   else, and re-rooting them closed both halves at once. A rewriter would have been code that does
   nothing, and that would start corrupting paths the moment either root moved.

Step 2 is where the value is. Steps 1 and 3 are what make it not a second reimplementation.

### What building it settled

§6's risk 1 — that `DirBackend.list`'s `rglob("*")` over a tree holding a warm `target/` and a
730 MB `CARGO_HOME` would re-create the listing problem from the other side — is answered: the
exclusion is passed, and a probe over a tree seeded with a 500-file vendored registry listed **two**
entries, the project's own sources. Risks 2 through 5 (`cache_listing` staleness, the prover's
eager-materialization requirement, non-UTF-8 content, and landing the materializer upstream) are
untouched by this work and stand as written.

---

## 8. What would change this answer

If the persistent materializer turns out to be unlandable upstream and a local subclass is refused,
the honest fallback is not the status quo — it is to keep `SharedTree` and wire the author's read
tools to it directly, accepting the duplication in exchange for closing §3's table. The gaps are
what cost a run; the abstraction is what stops them recurring. They are worth separating if forced.
