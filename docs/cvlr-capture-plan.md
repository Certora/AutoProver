# Plan — The Capture Phase

> How we turn ~10–12 completed CVLR/Solana verification projects into the knowledge assets the
> CVLR backend needs: a RAG corpus, trigger-indexed KB recipes, `backend_guidance`, and test
> fixtures.
>
> Split into **two phases**: Phase A is purely machine-driven and ends with a *working prototype
> corpus plus a ranked list of questions only humans can answer*; Phase B spends expert time
> answering that list and upgrading the corpus. Phase A does not wait on anybody.
>
> This is §7.1 of [cvlr-backend-plan.md](./cvlr-backend-plan.md) in detail. Companions:
> [rag-import-format.md](./rag-import-format.md) (the corpus manifest schema the output must
> match) and [ecosystem-abstraction.md](./ecosystem-abstraction.md).
>
> **The project inventory — repository names, paths, branches, and which are client-confidential
> — is deliberately not in this document.** It is tracked separately. This document is the
> procedure.

---

## 1. The problem this phase actually solves

A completed verification project is **not documentation**, and nothing useful comes from
importing one as prose. What a project contains is *paired evidence*:

```
   a situation in the program  →  the spec/munge that handled it  →  the verdict it produced
```

The agent needs the general form of that triple. The project holds ~1 instance of it, wrapped in
several thousand lines of code specific to one protocol. So capture is fundamentally an
**abstraction** pipeline: N project-specific instances in, M source-free generalized entries out.

Having 10–12 projects instead of one changes the character of the work qualitatively. With one
project you cannot tell an idiom from a quirk. With ten you can count — subject to §3.1, because
the sample is not uniform.

---

## 2. The two phases

The split is **not** "automated versus manual". It is:

| | Phase A | Phase B |
|---|---|---|
| Sources | The repositories, the crates, the published docs | People with firsthand experience |
| Answers questions of the form | *What is in the artifacts?* | *Why, and is it still right?* |
| Needs expert time | No | Yes, and only this |
| Outputs | **A working prototype corpus** + **a ranked question ledger** | Upgraded entries; the ledger burnt down |
| Blocked by | Nothing but Stage 0 | Phase A's ledger |

The reason to draw the line here is that everything derivable from the artifacts is derivable
*now*, at machine speed, without occupying the few people who have done these projects. And the
act of deriving it is what tells us **precisely** what to ask them — which is a far better use of
their time than an open-ended interview.

So Phase A has two deliverables of equal importance:

1. **A prototype corpus that ingests and retrieves.** Imperfect on purpose. Good enough to wire
   into the backend and start measuring whether retrieval helps at all.
2. **The question ledger** (§6) — every point where the machine pass hit a wall it cannot climb,
   recorded with its evidence, what it blocks, and how much it blocks. This is Phase B's backlog,
   and it is *generated*, not composed.

Phase A is also **re-runnable**. When a project's spec branch advances, or a Phase B answer
lands, re-running the mechanical stages is cheap and re-ranks everything.

---

## 3. Stage 0 — Inventory, disclosure, tiering (prerequisite)

**Do this first.** The RAG corpus is embedded into a database that ships with the product and is
read back verbatim by `get_section`. Anything that lands in it is effectively published. This is
the one piece of human time in Phase A, and it is administrative rather than expert.

For each project, record:

| Field | Why |
|---|---|
| Public or client-confidential | Determines whether *anything* may be quoted verbatim |
| Spec branch and code branch | The extraction inputs (§4.1) |
| Resolved CVLR version | Idioms age; see §4.3.6 |
| Prover version, or the spec branch's date as a proxy | Vintage tier (§3.1) |
| **Vintage tier: normative or evidence** | Decides what the project is allowed to teach |
| Chain (Solana / Soroban) | Corpus section tagging |
| Whether the spec verifies today | A non-verifying spec is still evidence, but not a fixture |

**The rule for the corpus: no client code verbatim, ever.** Every entry that carries code
carries a *synthetic* example — rewritten against invented types, minimal, and compiled (§9).
Public projects may be cited by name and linked; confidential ones contribute the *idiom* and are
cited only as "observed in N projects".

### 3.0.1 Quoting: never from an engagement, encouraged from a tutorial

**Every code block derived from a verification engagement is paraphrased or generalized — never
quoted — whether or not that repo is public.** The reason is not confidentiality; it is
**overfitting**. A corpus that quotes how eleven particular projects happened to do things trains
the agent on those projects' accidents: their house layout, their helper names, their era's
idioms. A generalized entry states the idiom; a quoted one states one instance of it.

So the rule keys on `kind`, not on `disclosure`:

| Source `kind` | Code blocks |
|---|---|
| `engagement` | **always** paraphrased / generalized, public or not |
| `tutorial`, `examples` | quoting **encouraged** — *provided it agrees with current best practice* |

`disclosure` still matters, but for a narrower thing than before: whether a project may be **named
and linked**, and whether its identifiers need to be on the sanitization denylist. It no longer
governs whether code can be reused, because nothing from an engagement is reused verbatim either
way. Note what follows for the four confidential engagements whose upstream program is public —
relaxing their disclosure would buy attribution, not quotable code.

**The tutorial proviso is a live gate, not a formality**, but it bites the example *repos*, not
the published documentation. The two public example repos sit on the 0.4 line with no
`cvlr_rules!`/`cvlr_spec!`, so quoting them needs a currency check that some of their content
fails. The published Solana manual does not have that problem: it states that it targets
`cvlr >= 0.6` / `cvlr-solana >= 0.5`, marks features "since 0.5"/"since 0.6", and carries whole
sections on the constructs the example repos lack (§4.7). It is quotable *and* current.

So the corpus has a normative, citable backbone from day one, and the authoring burden falls where
the docs are thin rather than across the board. Two consequences worth keeping straight:

- **The docs are the reference; the projects are the evidence.** Where a doc section and a
  project instance disagree on form, the doc wins — it is maintained, versioned and public.
- **The docs describe the API and the method; they do not report what breaks.** No published page
  says which SDK boundary needed mocking on a real protocol, which property turned out to be
  false, or what a two-day timeout hunt ended in. That is what the projects and §6.3 are for, and
  it is still the part that cannot be quoted.

**Paraphrasing prevents copying, not bias.** Which idioms get an entry at all is still decided by
what these projects happened to do. The actual defenses against overfitting are the two ranking
axes — recurrence across *clients* (§4.5), which suppresses one team's house style, and currency,
which suppresses one era's — plus the modernization pass for gaps neither axis can fill. Treat
paraphrasing as necessary and not sufficient.

This is not merely a legal formality. A synthetic minimal example retrieves and generalizes
better than a 200-line excerpt from someone's lending protocol, which mostly teaches the agent
about that protocol.

### 3.1 Two tiers, because the sample is not uniform

A small number of recent projects ran against current CVLR and a current Prover with today's best
practices in mind; many older ones use methods that have since been superseded. Both are worth
reading, but *for different things*:

| Tier | Which projects | Authoritative for |
|---|---|---|
| **Normative** | Recent: current CVLR, current Prover, written to current practice | **How** to do things — the canonical form of every idiom, prompt examples, `backend_guidance` |
| **Evidence** | Everything older | **What** situations exist — which problems arise, which SDK boundaries need mocking, which failure modes occur, how often |

> **Older projects vote on the problem set. Recent projects decide the solution form.**

With one addition the survey forced: **the published manual is normative by construction**, and on
questions of *form* it outranks every project instance — it is maintained, explicitly
version-scoped, and public, where a project instance is one team's choice frozen at one date
(§3.0.1). Projects remain the only source for what actually goes wrong, which no manual reports.

**Compute the tiers per chain, against that chain's own frontier.** The surveyed Soroban projects
run cvlr 0.3.x–0.4.x while the Solana ones run 0.4.x–0.6.x, so a 0.4.1 core is *current* on Soroban
and a generation behind on Solana. Ranking both chains on one version scale would mark the entire
Soroban corpus as legacy and leave that chain with no normative tier at all — which is not a
finding about Soroban practice, only about release cadence.

This keeps the value of a dozen projects — breadth of situations, which no single project can
give — while refusing to let the older majority set the idiom.

Tier assignment is a **prior, not a verdict**: vintage is really a property of each *instance*, not
of a project. A recent project can carry copy-pasted legacy patterns; an old project can contain a
pattern that is still canonical. So classify at the instance level (§4.4) and use the project's
tier only as the default when no instance-level signal is available.

---

# PHASE A — Machine-driven

## 4. Extraction

### 4.1 Locate the artifact set: use the merge-base, not the branch tips

Verification lives on a branch; the code lives on another. The obvious move — diff the two tips —
is wrong, and badly so. On the project we calibrated this procedure against, the tip-to-tip diff
was **549 files, +41k/−146k lines**, almost all of it unrelated upstream development that happened
after the branches diverged. The merge-base diff isolated the actual verification work to **~600
added lines of source edits across ~50 files**, plus the added spec tree.

```bash
MB=$(git merge-base <code-branch> <spec-branch>)

# The spec surface — everything verification ADDED:
git diff --name-status --diff-filter=A $MB <spec-branch>

# The munge — every edit verification made to production code:
git diff --diff-filter=M $MB <spec-branch> -- programs/ crates/ src/
```

**The merge-base isolates the branch's divergence, which is not the same as the verification
work.** Where a spec branch also carried ordinary feature development, the added set contains it:
one project's `A` set is 100 files of TypeScript UI code and dev utilities, another's includes
committed `.so` binaries. So the diff narrows the problem and the *classifier* (§4.2) finishes it —
which is why a large "unclassified" bucket is a signal about the branch, not a tool failure. Judge a
project by whether its rules/confs/env files were found, never by the size of the added set.

**Also check the dates.** On the calibration project the spec branch head was *older* than the
code branch head — the spec was written against code that has since moved. Pair the spec with the
code it was written against (the spec branch's own tree), never with today's mainline, or both the
munge diff and the rule↔code correspondence come out wrong.

### 4.2 Normalize into a per-project artifact manifest

The layout is a convention, not a standard, so record where each kind actually lives rather than
assuming. The kinds seen in practice, all of which recur:

| Kind | Typical location | Worth |
|---|---|---|
| **Rules** | `<program>/src/certora/specs/*.rs` | The gold standard — what a good rule looks like |
| **Mocks** | `<program>/src/certora/mocks/**` | The most reusable munge material |
| **Env files** | `<program>/src/certora/envs/cvlr_{inlining,summaries}.txt` | Which functions get inlined/summarized |
| **Per-rule confs** | `<program>/src/certora/confs/*.conf` | The empirical answer to "what conf do we generate" |
| **Base confs + build scripts** | a repo-level `certora*/` directory | Project-level invocation shape |
| **CI expectations** | `expected.json` next to each rule group | **Rule → SUCCESS/FAIL ground truth** (§4.3.5) |
| **Task automation** | `just/`, make, or shell recipes | How a human actually drives a run |
| **Munge diff** | the `--diff-filter=M` set | The munge charter's raw material |
| **Cargo wiring** | `Cargo.toml` features + `[package.metadata.certora]` | Scaffold ground truth |

Output: one small JSON/YAML manifest per project naming these paths. Everything downstream reads
the manifest, so a project with an unusual layout is handled once, here.

**Built and run over all 16 projects** — `tools/locate.py` in the private repo, with the manifests
committed beside the inventory. The corpus is therefore counted rather than estimated:

| | rules | confs | expected verdicts | env files | mocks | harness | build scripts | munge |
|---|---|---|---|---|---|---|---|---|
| files, 16 projects | **264** | 486 | 287 | 106 | 90 | 181 | 28 | **527** |

Three classifier facts worth carrying into any layout work, each of which mis-scored a project
before it was fixed: rules nest arbitrarily deep under a spec directory *and* appear as a single
`spec.rs` in smaller projects (matching one directory level found 1 of 24 rule files in one
project); `mod.rs` under a spec tree is glue, not a rule; and **cargo wiring must be read from the
tree, not the diff**, because verification usually *modifies* an existing manifest, which lands the
scaffold's ground truth in the munge set conflated with source edits.

**Do not assume one cargo workspace per project.** One surveyed repo has *no root workspace at
all*: four independent sibling workspaces at the root, each with its own `Cargo.lock` and no root
`Cargo.toml`, with the verified program consuming two of its siblings as path dependencies *across
workspace boundaries* — and the munge edits files in both. Three things follow, and the last one
reaches past capture into the backend:

- The per-project manifest must name the **build unit** (which workspace) explicitly, not derive it.
- Version stamping must pick the *relevant* lockfile; sibling workspaces can disagree (one resolves
  `cvlr` 0.4.0 while a sibling manifest declares 0.4.1).
- A **compile gate scoped to one workspace would silently miss edits in the other** — precisely the
  condition `EditsNotCompiled` exists to catch ([main plan](./cvlr-backend-plan.md) §5.2). Whatever
  the CVLR munge check ends up being, its notion of "was this file compiled" has to span every
  workspace the munge touches.

### 4.3 The extractors (a script, no LLM)

Cheap, exhaustive, re-runnable. Nothing here needs judgment, so nothing here should cost tokens.
Each extractor emits rows tagged with project, tier, and CVLR version.

#### 4.3.1 Rules

Every `#[rule]`: name, file, body, and the CVLR calls inside it (`cvlr_assert!`, `cvlr_assume!`,
`cvlr_satisfy!`, `nondet*`, `clog!`, `cvlr_lemma!`, the `Nondet`/`CvlrLog` derives). The
calibration project alone has **63 rules across 27 spec files**; ten projects put this in the high
hundreds, which is easily enough to rank by frequency.

Derived metrics worth having immediately, because they answer live design questions in the main
plan: assertions per rule, `clog!` calls per assertion (§5.3 there), how often a rule is
parametric versus one-per-instruction (open question #5 there).

#### 4.3.2 Conf surface

Parse every conf and every `[package.metadata.certora]` block into a key → value-frequency table.
This *settles* main-plan open question #2 empirically rather than by reading the docs — on the
calibration project the from-sources style with `solana_inlining` / `solana_summaries` pointing at
`src/certora/envs/*.txt` is what is actually used.

#### 4.3.3 Env files

Inlining and summaries entries, bucketed by symbol shape (SDK function, math helper, CPI boundary,
…). Ten projects' worth is a direct answer to "what does the scaffold's starting env file contain"
— currently a guess.

Two gotchas, both observed: the files carry **three** naming conventions, not two — bare
(`envs/inlining.txt`), `cvt_*` in older projects, and `cvlr_*` in current ones — so match all
three, and note that a prefixed-only pattern scored a project with six env files as having none; and **"summaries" names two different mechanisms.** One project has both an
env-file summary list *and* a tree of Rust replacement implementations under
`src/certora/summaries/`. They are not variants of one thing — the first is an `ENV`-channel
entry, the second is closer to `MOCK` (§7.2) — and conflating them would produce recipes that
recommend the wrong action.

#### 4.3.4 Munge-diff taxonomy

Classify every hunk in the `M` set by form. The forms observed, to be treated as a starting
taxonomy rather than an exhaustive one:

| Form | Shape | What it does |
|---|---|---|
| Implementation swap | paired `#[cfg(feature = "certora")]` / `#[cfg(not(...))]` | Replace a definition under verification |
| **Module redirect** | `#[cfg_attr(feature = "certora", path = "../certora/mocks/<x>.rs")]` | Point a `mod` at a mock **without touching any call site**. The highest-*leverage* idiom found and also the **rarest**: 10 occurrences in 2 projects, against 1029 implementation swaps in 13 (§4.3.7) |
| Attribute injection | `#[cfg_attr(feature = "certora", cvlr::early_panic)]` | Add a CVLR attribute only under verification |
| Expression swap | `if cfg!(feature = "certora") { … }` | Replace one hard subexpression (e.g. an interest computation) inline |
| Module hook | `pub mod certora;` | The single entry point into the spec tree |
| Feature wiring | `certora = [...]`, `certora = ["library/certora"]` | The feature and its propagation across workspace crates |
| Sub-gates | a second feature composed with the first | Enable/disable specific functions within a verification build |

Note what the counts say: on the calibration project the munge was ~600 lines against a spec
surface of tens of thousands. **Munging is broad but shallow.** That is a load-bearing input to
the main plan's give-up boundary (§5.2 there) — the charter should expect many small mechanical
edits, not a rewrite.

#### 4.3.7 What the first extraction run found

`tools/extract.py` + `tools/tables.py` over all 16 projects: **3704 rows in 1.3 s**, re-runnable.
The numbers below are the point of the stage — several were open questions in
[cvlr-backend-plan.md](./cvlr-backend-plan.md) §8 answered by reading documentation and hoping.

**Three parsing facts, each of which silently destroyed data before it was fixed.** They matter
beyond this stage because the backend has to *write* these files:

- **Conf files are JSON5-shaped, not JSON**: `//` comments *and* trailing commas. A strict parser
  rejected **152 of 486** (31%). Whatever emits a conf may therefore emit trailing commas safely,
  and whatever reads one must tolerate them.
- **Env files comment with `;` only.** `#` opens a *directive* — an entry is
  `#[inline(never)] ^core::.*$` — so treating `#` as a comment marker discarded **every entry in
  all 52 inlining files** while leaving the file count looking healthy.
- **Some env files carry a `DO NOT EDIT — AUTOMATICALLY GENERATED` banner and others do not** (6
  files in one project, 4 in another, 0 in a third). Recorded per file, not concluded: whether the
  backend should *author* these or *run their generator* is a ledger question, and it changes the
  scaffold.

**The scaffold's starting env file is now evidence rather than a guess.** These entries appear in
**14 of 14** projects that have env files: `^core::.*$`, `^std::.*$`, `^<?alloc::.*$`,
`^solana_program::.*$`, `^([^:]+::)*CVT_.*$`, `__rust_alloc` / `__rust_dealloc` /
`__rust_alloc_zeroed` / `__rg_alloc` / `__rg_dealloc` / `__rg_oom`, `memcpy`. 5208 entries total,
bucketed: 3395 inlining (622 `solana_sdk`, 462 `anchor`) and 1813 summaries (191 `solana_sdk`, 29
CPI).

**The conf surface, settled empirically.** `[package.metadata.certora]` has exactly three keys
across the 9 projects that use it — `solana_inlining`, `solana_summaries`, `sources` — which
settles main-plan open question #2 in favour of the from-sources style. Top-level conf keys by
project count: `rule` (16), `build_script` (15), `loop_iter` (15), `rule_sanity` (13), `java_args`
(13), `msg` (13), `optimistic_loop` (12), `smt_timeout` (10), `override_base_config` (8). And a
`prover_args` baseline that a top-level key count cannot see: **five `-solanaOptimistic*` flags set
`true` in 13 of 13 projects** (`Join`, `Memcmp`, `MemcpyPromotion`, `NoMemmove`, `Overlaps`), then
`-solanaTACOptimize` (12), `-solanaAggressiveGlobalDetection` (11), `-solanaTACMathInt` (10),
`-solanaStackSize 8192` (9).

**1303 rule instances.** The two-axis rank is no longer a hypothesis: `cvt_assert` has **1130
occurrences but in only 6 projects**, while `cvlr_assert` has **931 in 14**. A frequency-ranked
corpus would have taught the superseded spelling. Two sharper cases:

- **`cvlr_vacuity_check` has zero uses anywhere; `cvt_vacuity_check` has 113 across 4 projects** —
  a §4.5 bottom-left cell exactly as predicted: the problem recurs and *every* solution we hold is
  legacy. The manual names neither macro (§4.7).
- **`acc_infos_with_mem_layout`: 211 calls in 5 projects** — undocumented, *and* one of the nine
  symbols the unreleased 0.6 chain crate removes (§4.7.3). A widely-used idiom facing removal is
  the highest-value thing a ledger can carry.
- **130 macros appear in exactly one project** — the KB-recipe quadrant, and a map of what the
  library does not provide (mint extensions, anchor contexts, signer assertions, stack-height
  control).

**Munge: 527 modified files across 13 projects, 1393 marker-carrying lines.** Implementation swap
dominates (1029 occurrences, 13 projects), then attribute injection (203, 12), dependency wiring
(71, 12), feature wiring (33, 13), expression swap (24, 5), module hook (23, 11), module redirect
(10, 2). Note the raw added-line count (22309) is *not* munge volume — where a spec branch also
carried feature work its additions land in the same set (§4.1), so the marker count is the honest
measure.

**Verdicts: 1199, of which 45 `FAIL` and 5 `SANITY_FAIL`.** That is **50 ledger questions**
generated mechanically, and it surfaced a verdict value the plan had not anticipated: `SANITY_FAIL`
is its own outcome, not a flavour of `FAIL`.

#### 4.3.5 Expected-verdict files

`expected.json` maps rule name → `SUCCESS` | `FAIL`. Two uses:

- **Fixtures** (§7.4): exactly the verdict ground truth an integration test needs.
- **Negative knowledge**: a rule *expected to fail* encodes either a known bug or a known tool
  limitation. "Properties that do not hold, and why" is what teaches an agent when to stop pushing
  versus keep trying — and it exists nowhere in published documentation. The *why* is not in the
  file, so every `FAIL` becomes a ledger question (§6.1).

#### 4.3.6 Version stamping

Record the CVLR family versions each project resolved, from its own lockfile:

```bash
git show <spec-branch>:Cargo.lock | grep -A2 'name = "cvlr'
```

Glob lockfiles carefully: one surveyed repo keeps a `Cargo.lock.backup` beside the real one, and a
pattern like `*Cargo.lock*` would stamp versions from a stale file.

CVLR is a **family**, and it is wider than the core crate — nineteen members observed across the
surveyed projects (sixteen below plus the Soroban line `cvlr-soroban`, `cvlr-soroban-derive`,
`cvlr-soroban-macros`): `cvlr`, `cvlr-asserts`, `cvlr-decimal`, `cvlr-derive`, `cvlr-early-panic`,
`cvlr-fixed`, `cvlr-hook`, `cvlr-log`, `cvlr-macros`, `cvlr-mathint`, `cvlr-nondet`, `cvlr-spec`,
`cvlr-vectors`, plus the Solana line `cvlr-solana`, `cvlr-solana-stake`, `cvlr-solana-token`.
Treat the list as open: it grew by eleven over twelve projects, so **discover the family from the
lockfile** rather than matching a hardcoded set.

The projects also span **three generations** — 0.4.x, 0.5.x and 0.6.x — so "current CVLR" is a
moving target *within* the corpus, not just relative to it. Sources seen: crates.io, git pinned to
a commit, git pinned to a tag, git pinned to a branch, a **local path override**, and a **personal
fork pinned to a platform-SDK release branch** (one Soroban project takes `cvlr-soroban` from a
fork on a `soroban-22.0.8` branch rather than from the Certora org repo).

Worse for any single "version" notion: **the core line and the Solana line version
independently.** One project resolves the whole `cvlr*` core at 0.4.1 from crates.io while every
`cvlr-solana*` crate is at 0.5.0 from a git branch. And **manifest dependency names do not
correspond to resolved package names** — that same project declares `cvlr-spl-token` in its
manifest while the lock resolves `cvlr-solana-token`, which no manifest mentions. Match on lock
names, never manifest names. **The family is not versioned in lockstep** — one project resolves `cvlr`
0.4.2, `cvlr-solana` 0.4.1 and `cvlr-vectors` 0.4.0 together — so record a version *per crate*,
never one number for "the CVLR version". Worse, **one project can resolve two versions of the same
crate simultaneously**: a surveyed lockfile carries `cvlr-asserts` at both 0.4.0 (a git branch) and
0.4.1 (crates.io), because part of the dependency tree takes each. So the stamp is a *set* per
crate, and an instance's effective version depends on which dependency path reaches it — a single
scalar "version" field cannot represent what is actually there. The set's elements are
**(name, version, source)** triples, not (name, version) pairs: another lockfile lists
`cvlr-asserts` 0.4.0 from crates.io *and* `cvlr-asserts` 0.4.0 from a git commit, so keying on the
version string alone silently merges two distinct resolutions. Stamp every extracted instance. Shipping a dated idiom
unmarked is the corpus-side version of the "reading the wrong CVLR" risk in the main plan's §5.5.

**Stamp the platform generation alongside the CVLR versions.** A CVLR chain crate is bound to one
chain-platform line — `cvlr-solana` 0.4.x to `solana-program` 1.18, 0.5.0 to 2.2, the unreleased
0.6 line to the split `solana-*` v3 crates — and each generation has its own `AccountInfo` *type*.
Two instances can therefore carry identical `cvlr` versions and still be mutually incompatible,
and no CVLR version string explains why (§4.7.3).

Also expect more than one **dependency source**: crates.io, git pinned to a commit, git pinned to
a tag, and git pinned to a *branch* all appear across the surveyed projects. A stamp that assumes a
registry version silently loses the git cases. Read `Cargo.lock`, never `Cargo.toml` — a
branch-pinned manifest names no version at all, and the lock is the only place the resolved commit
appears.

### 4.4 Instance-level vintage classification

Per §3.1 the project's tier is only a prior. Classify each extracted *instance* as
current / legacy / unknown, using signals in this order of reliability:

1. **Crate-provided-versus-hand-rolled.** The strongest signal, and mechanically checkable: does
   the instance hand-roll something current CVLR now provides? Conversely, does it *use* a
   construct that only exists in recent CVLR (e.g. the lemma/parametric-rule machinery —
   `cvlr_lemma!`, `cvlr_spec!`, `cvlr_rules!`)? A recent-only construct dates the instance from
   the inside, which a commit date cannot do.

   The parametric-rule machinery is the clearest positive marker, and it is *rare*: exactly one
   surveyed project uses it (`cvlr::spec::cvlr_rules! { … spec: cvlr_spec! { … } }`, from the
   `cvlr-spec` crate on the 0.6 line). So the currency axis will often rest on a single project —
   which is why it is a separate axis from recurrence rather than folded into it.

   **Both of the first two signals are chain-relative, and reading them across chains inverts
   them.** No surveyed Soroban project uses the parametric machinery — but it appears only on the
   0.6 line, which Soroban has not reached, so its *absence there is evidence of nothing*. Likewise
   a version comparison is only meaningful within a chain (§3.1). Evaluate signals 1 and 2 against
   the project's own chain, and treat "construct absent" as informative only when that construct
   exists for that chain.

   On the negative side this resolves to a **three-rung ladder**, and the rungs are distinguished
   by the *import path*, not by the macro name — so classify on imports and dependencies, never on a
   grep for `cvt_` alone:

   | Rung | What it is | How to detect |
   |---|---|---|
   | 1 — superseded *library* | `cvt` / `cvt-macros` / `nondet` from `Certora/solana-cvt` | a `solana-cvt` git dependency; bare `use cvt_assert` |
   | 2 — current library, legacy *surface* | CVLR's own compatibility module, often with local shim macros | `use cvlr::asserts::cvt::{…}`; `macro_rules! cvt_*` |
   | 3 — current | native `cvlr_assert!` etc. | `cvlr` imports, no `cvt` path |

   Rung 2 is the one a name-based check gets wrong: it looks identical to rung 1 at the call site
   and is a completely different situation.

   **A fifth state the data forced: `mixed`.** Five of sixteen projects carry rung-1 *and*
   rung-2/3 evidence at once — importing `cvt::cvt_assume` (the superseded crate) alongside
   `cvlr::cvt::*` (the compat module) or native calls. That is not "unknown": it says a migration
   is in progress, which is a different fact with a different follow-up, and collapsing the two
   would hide the most common real state in the corpus after `current`. It is also the easiest deprecation mapping to write,
   because the compat module *is* the migration path — the entry can name the exact swap.

   **The ladder is per-concern, not per-project.** A project can sit on different rungs for
   assertions, for logging, and for file naming at once — one surveyed project pairs 692
   `cvt_assert` with the pre-`clog!` logging API (`cvlr::log::cvt_cex_print_tag`) *and* env files
   named `cvt_inlining.txt` / `cvt_summaries.txt` where a current project uses
   `cvlr_inlining.txt` / `cvlr_summaries.txt`. So classify each concern separately, and note that
   **a vocabulary check restricted to `*.rs` misses the filename axis entirely** — which is the
   one that reaches the scaffold (§6 there), since the templated project shape has to emit the
   current names.
2. **Resolved CVLR version** (§4.3.6) — bounds what the author *could* have used.
3. **Does it still compile against current CVLR?** Cheap and decisive for the worst cases. Worth
   running over every extracted instance: it costs one build per project and finds the idioms that
   are not merely dated but dead.
4. **Project tier / branch date** — the fallback when nothing above discriminates.

Signal 1 is what makes instance granularity worth the effort: it catches the copy-pasted legacy
pattern inside a recent project, which a date-based classification gets exactly backwards.

Every instance that reaches "unknown" after all four signals becomes a ledger question.

### 4.5 Rank on two axes, because frequency alone points the wrong way

The obvious filter is frequency: an idiom in ≥2 projects generalizes, an idiom in 1 is a quirk.
That is right about *problems* and **actively wrong about solutions**, because of §3.1's
distribution. The older projects are the more numerous, so a raw count promotes the obsolete
precisely *because* it is obsolete — the legacy spelling outvotes the current one every time.

So rank on two axes, kept separate:

- **Recurrence** — in how many *distinct projects*, of any tier, does the underlying *problem*
  appear? **Count distinct clients, not just distinct repos.** The surveyed set contains two
  same-client pairs, and same-client projects share tooling, layout and house style — so a
  convention local to one team can appear in two repos and read as an industry idiom. Two repos
  from one client is one vote plus a note, not two votes; the honest ≥2 bar is two *teams*.
- **Currency** — does a **normative-tier** instance of the solution exist?

| | Normative solution exists | Only legacy solutions |
|---|---|---|
| **Recurs (≥2 projects)** | **Ship it.** Full corpus entry; `backend_guidance` candidate | **The single most valuable ledger item.** The problem is common and we have *no* demonstration of the current answer → §6.2 |
| **One-off** | KB recipe with a narrow trigger | Drop, or one line in a "seen once" appendix |

The bottom-left quadrant is the point of doing this. Under a single-axis rank those idioms look
like well-attested best practice; under two axes they are correctly identified as *gaps* — gaps we
would otherwise have shipped as advice.

### 4.5.1 What the first ranked run found

`tools/rank.py` over the extracted rows. Three results changed how the rest of Phase A should be
run.

**Sixteen repositories are eleven clients.** Grouped by codebase owner, as evidenced by package
names: the three SPL-upstream repos are one client, Kamino is two repos, Squads two, Certora's own
teaching material two. So **"appears in ≥2 projects" is a much weaker statement than it looks**, and
the honest bar — two *teams* — eliminates several idioms that a repo count would have promoted. One
project's owner is not yet established, which is recorded in the inventory as `~` and makes every
recurrence count an **upper bound** until it is named.

**Vintage, per project per concern** (rung from the import path, never the macro name):

| | assertions | logging | vacuity | env naming |
|---|---|---|---|---|
| current (rung 3) | 9 | 13 | 0 | 6 |
| legacy surface (rung 2) | 0 | 0 | 0 | 5 |
| superseded library (rung 1) | 1 | 0 | 1 | 0 |
| mixed | 5 | 1 | 4 | 0 |
| absent / unknown | 1 | 2 | 11 | 5 |

Three things to read off it:

- **The per-concern claim is confirmed by a same-client pair.** Kamino's two repos are rung 3 on
  assertions and rung 2 on env-file naming — the same team, current in one concern and legacy in
  another. A project-level vintage would have been wrong for both.
- **Tier really is a prior, not a verdict.** One *normative*-tier project is `mixed` on assertions
  and `unknown` on env naming (it uses the unprefixed third convention), exactly the case §3.1
  predicted.
- **Vacuity is current nowhere.** No project on any rung uses `cvlr_vacuity_check`; the four that
  check vacuity all use the legacy spelling.

**The quadrants: 221 ship, 8 ledger gaps, 197 recipes, 112 appendix.** And the valuable cell needed
splitting, because *recurs with no current solution* turned out to mean two different things:

- **4 are mechanical renames** — `cvt_assert` → `cvlr_assert` and friends, where the current name
  both exists in the pinned crates *and* is attested in normative projects. These cost no expert
  time; they are a mapping table.
- **4 are real questions.** `cvt_vacuity_check` maps to a name that **exists but has zero uses
  anywhere and no mention in the manual** — so the rename is not the answer, and the question is
  whether that is still how vacuity is checked (the `rule_sanity` conf key, present in 13 projects,
  is the obvious hypothesis). The other three are env-file entries that recur across clients with
  no normative-tier instance, which asks why current projects stopped inlining them.

That distinction is worth its own state (`unattested_replacement`): without it, the single most
interesting gap in the corpus would have been filed as a spelling change.

**One false question caught, which is a warning about the ledger's own quality.** `cvlr_assert_eq`
is produced by a macro-generating macro — `impl_bin_assert!(cvlr_assert_eq, ==, $)` — and is
therefore invisible to a `macro_rules!` scan. The first run reported `cvt_assert_eq` as having *no*
current counterpart, i.e. as a question for a human, when the replacement is used 46 times across 6
projects. **A ledger that sends an expert to answer something the crates already answer spends the
one resource Phase B has.** Scan for generated names, and treat every "no replacement found" as
suspect until the symbol list is known to be complete.

### 4.6 The abstraction pass (LLM, still Phase A)

One idiom at a time. Input: every instance across projects, with surrounding code and version
stamps. Output: a source-free entry with a fixed shape:

1. **Trigger** — how you recognize you are in this situation, in observable terms.
2. **Pattern** — the general form, with a **synthetic** minimal example.
3. **Why it is sound** — what the transformation preserves. Mandatory for anything munge-shaped;
   a munge idiom without a soundness argument is a way to prove something false.
4. **Cost** — what it gives up (over-approximation, lost coverage, runtime).
5. **Provenance** — recurrence count, CVLR version range, and which tier the canonical form came
   from. No client identifiers.

Doing this per-idiom rather than per-project is what forces generalization: the pass sees five
instances at once and cannot simply summarize one.

**Tier discipline inside the pass.** When instances disagree, the normative one wins on *form* —
this is not a majority vote, and the pass must be prompted accordingly or it will average one
current idiom together with four legacy ones and emit something that resembles neither.

**Field 3 is the honest limit of Phase A.** The pass can *propose* a soundness argument; it cannot
certify one. Every proposed argument is emitted as a ledger question and the entry is
quarantined (§5.1) until a human signs it off.

### 4.7 The bulk layers — free corpus content with no project involved

Two more Phase-A sources, both machine-driven, that make the prototype useful on day one rather
than sparse:

- **Published docs** — the Solana Prover manual, imported through
  [rag-import-format.md](./rag-import-format.md). **Built: see §4.7.1.** It is much more than an
  API listing: fourteen top-level sections including *Methodology*, *Parametric Rules & Macros*,
  *Specifications and Lemmas*, *Mocks & Feature Gates*, *Nondet & Havoc*, *Solana Accounts*,
  *Anchor*, *Rule Sanity Checks*, *Understanding Prover Output* and *Troubleshooting* — and it
  targets current CVLR (§3.0.1). Several of the main plan's open questions are answered here
  rather than by extraction.
- **Generated crate reference** — an agent reads the CVLR crate family and emits a reference entry
  per macro, derive, and helper. Cheap and *self-verifying*: the emitted examples must compile
  against the crate they document. Most directly enabled by the main plan's §5.5 (the crate source
  is on disk in the resolved version).

  **Measured, so the scope is known rather than assumed.** The reference set (§4.7.3) resolves to
  14 crates with **282 public symbols, of which the manual names 74 — 26%.** The layer is therefore
  worth building, and the priority order falls out of where the gaps are rather than out of taste:

  | Gap | Coverage | Why it ranks here |
  |---|---|---|
  | `cvlr-solana` | 4/28 fns, 0/7 macros, 0/2 types | The chain API — CPI (`invoke`, `invoke_signed`), key comparison (`require_keys_eq`), account logging (`clog_acc_info`), mint modelling (`impl_nondet_mint`) — is almost entirely unnamed |
  | `cvlr-solana-stake` | 0/15 | Undocumented in full; the word "stake" does not appear in the manual |
  | `cvlr-log` | 5/35 fns, 0/8 macros | Counterexample readability is a named hard part of the backend plan (§5.3) |
  | `cvlr-nondet` | 5/21 fns | The other half of every rule's setup |
  | `cvlr-fixed`, `cvlr-decimal`, `cvlr-mathint` | 14/71 | Numeric modelling; many are mechanical operators, so count overstates the real gap |
  | `cvlr-spec` types | 0/5 | The parametric layer's prose is good but its type surface is unnamed |

  Two gaps are sharper than a count can show, because **the projects use what the manual does not
  document**: `cvlr::early_panic` (0 mentions) appears in the munge taxonomy (§4.3.4) as a whole
  category of edit, and `invoke`/`invoke_signed` (0 mentions) are how CPIs are reached at all.
  `cvlr_vacuity_check` is the inverse and just as instructive: the manual teaches vacuity as a
  *method* ("Catch vacuity early") without ever naming the macro that performs it. This is exactly
  the corpus's value-add over the docs, and it is measurable rather than asserted.

- **The public examples repo.** Two complete minimal CVLR projects, canonical `*_core` inlining
  and summaries files, and a conf variant pair with expected-verdict files. Public, tiny, and
  authoritative about the *scaffold* rather than about practice — see the backend plan's Phase 3.
- **Public tutorial prose** (quotable, subject to §3.0.1's currency proviso). One surveyed
  project is a public tutorial whose README is already
  pedagogical — organized by property category, with further READMEs documenting the `certora/`
  scaffold itself. It is the only project material that needs *import* rather than abstraction, and
  being public it can be quoted verbatim. Cheapest real content in the whole plan; do it first.

These four layers plus §4.6's idioms are the prototype corpus. Note the division of labor: the
bulk layers give **coverage of the API**, the extracted idioms give **coverage of practice**, and
only the latter needs the confidential projects at all.

A caution that came out of the survey, now narrowed by what the docs turned out to contain: **the
public example repos lag the newest observed practice by a full generation** (0.4 line, no
parametric rules; the newest project is on 0.6 with them) — but the published manual does not
(§3.0.1). So the example repos are the best source of *scaffold* material and a poor source of
*current rule-writing* material, while the manual is good for both. The recency/citability
trade-off is real but local, which is why §4.5 keeps the two axes separate rather than collapsing
them into one "is this good practice" score.

### 4.7.1 Status: the public docs layer is built

The first bulk layer is done end to end, which makes `cvlr_kb` a real corpus rather than a
registration:

| Step | Artifact |
|---|---|
| Build the manuals | [gen_docs.sh](../scripts/gen_docs.sh) → `scripts/prover-docs/solana.html`, now also writing a `PROVENANCE` file naming the docs revision |
| Parse | [composer/rag/html_manual.py](../composer/rag/html_manual.py) — sphinx HTML → a tree of typed blocks, bs4 only, no RAG dependencies |
| Produce | [composer/scripts/cvlr_docs_manifest.py](../composer/scripts/cvlr_docs_manifest.py) → `scripts/cvlr-docs/cvlr-docs.rag.json` (147 embedded groups, 156 manual sections, 121 code blocks) |
| Ingest | `composer.scripts.rag_import` → 148 chunks + 156 sections in the `cvlr_rag` schema |
| Spot-check | 20 authoring-agent questions (§4.9): 20/20 returned a relevant section, `get_section` round-trips, keyword search resolves `cvlr_rules` at 0.998 |

Two notes for whoever extends this:

- The parser was extracted *from* `ragbuild.py` rather than written beside it, and the CVL corpus
  it feeds was verified byte-identical before and after (232 chunks / 198 sections unchanged, same
  for `prover.html`). A second HTML parser would have drifted from the first within a release.
- Retrieval is good but not uniformly: three of the twenty questions returned a plausible-but-not-
  best section, and in one case (*"one rule covering every instruction"*) keyword search found the
  right page — *Parametric Rules & Macros* — while vector search did not. Worth revisiting once
  the practice half lands, since more content changes the ranking.

Still open in this layer: the generated crate reference, and whether to include `prover.html`
(diagnostics and timeouts are on-topic; its EVM CLI surface is not).

### 4.7.2 The reference set: what "current CVLR" resolves to

§4.4's third vintage signal and §9's first acceptance gate both say "compiles against *current*
CVLR", and there is no single coordinate that means it. What the survey and the registry show:

| Line | Newest published | Date | Newest *observed in use* |
|---|---|---|---|
| `cvlr`, `cvlr-spec` | 0.6.1 | 2026-03-28 | 0.6.1 (crates.io) |
| `cvlr-solana`, `cvlr-solana-stake` | 0.5.0 | 2026-01-16 | 0.6.0-dev (git branch / path override) |
| `cvlr-solana-token` / `cvlr-spl-token` | **never published** | — | 0.5.0–0.6.0-dev (path override) |
| `cvlr-soroban*` | 0.4.0 | 2025-03-17 | 0.4.0 from a **personal fork** on a Soroban-SDK branch |

The projects cannot settle it either: fifteen lockfiles resolve **ten distinct (version, source)
combinations**, and the sources include crates.io, Certora git by commit, Certora git by branch,
two different *personal forks*, and local path overrides. Nor can "whatever the newest project
does" be the rule — that project's manifest requires `cvlr-solana = "0.6.0-dev"`, a version that
exists on no registry; its workspace reaches it through `[patch.crates-io]` pointed at a **moving
branch**; and its committed lockfile records the crate with *no source and no checksum* (a path
resolution), so the build is not reproducible from the repository. **That is a ledger question in
its own right**, not just an inconvenience for us.

Three things narrow the decision, all mechanically checked:

1. **The unpublished Solana branch is not a feature jump.** Against published 0.5.0 it adds *no*
   new public symbols and removes nine (the `mem_layout_*` helpers and the `AccountInfo`
   re-export), because SPL-token support was factored into a separate crate; it also moves from
   monolithic `solana-program` 2.2 to the split `solana-*` crates. Its changelog's `[Unreleased]`
   section is empty. `0.6.0-dev` is a version bump in flight.
2. **Current rule *form* lives in the core line, which is published.** `cvlr_rules!`,
   `cvlr_spec!`, `cvlr_lemma!`, `cvlr::derive::*` and the predicate machinery are all in
   `cvlr`/`cvlr-spec` 0.6.1.
3. **A rule in the documented current form compiles against published-only dependencies** — core
   0.6.1 plus `cvlr-solana` 0.5.0, host target, no git pin and no patch. Verified.

### 4.7.3 Decided: the reference set

**Published releases only, pinned exactly**, and now settled — recorded as data in
[composer/spec/cvlr_reference.py](../composer/spec/cvlr_reference.py), the single answer the
compile gate, the crate reference and the generated scaffold all read:

| Chain | CVLR | Platform generation it implies |
|---|---|---|
| Solana | `cvlr` 0.6.1, `cvlr-solana` 0.5.0, `cvlr-solana-stake` 0.5.0 | `solana-program` 2.x (the last monolithic line) |
| Soroban | `cvlr` 0.6.1, `cvlr-soroban` 0.4.0, `cvlr-soroban-derive` 0.4.0 | `soroban-sdk` 22.x |

Verified by compiling a probe per chain against exactly these pins — the Solana one exercising
`cvlr_rules!` / `cvlr_spec!` / predicates / derives and `cvlr_deserialize_nondet_accounts`, the
Soroban one a `#[contract]` impl with nondet and `clog!`. That probe *is* §9's gate in miniature.

**Choosing a chain crate chooses a platform generation, and that is the load-bearing consequence.**
`cvlr-solana` tracks the Solana platform line, and each generation has its own `AccountInfo`
*type*, so a mismatch is a hard type error rather than a warning:

| `cvlr-solana` | requires | surveyed projects there |
|---|---|---|
| 0.4.4 / 0.4.5 | `solana-program` 1.18 | 9 — klend, kvault, restaking, texture, manifest, stake-deposit, smart-account, and both public example repos |
| **0.5.0 (the reference)** | `solana-program` 2.2 | 2 — fluid (2.3.0), stake-pool (2.2.1) |
| 0.6.0-dev (unreleased) | split `solana-*` v3 | 1 — spl-token-pinocchio |

Two things follow, both deliberate:

- **The reference band is the two most recent conventional projects**, which is the right target:
  the newest normative work that is not on an unreleased crate. The nine 1.18-era projects stay
  *evidence* — their situations count, their `AccountInfo`-typed code does not compile here.
- **Pinocchio / split-crate programs are out of scope until 0.6.0 ships.** Worth stating plainly,
  because the newest engagement is also the only source of some current practice (§3.0.1). Its rule
  *form* still applies — that lives in the published core line — only its account handling does not.

Accepted consequences, recorded so they are not rediscovered later as bugs:

- **SPL Token support is a known hole.** The token model was factored out of `cvlr-solana` on the
  unreleased 0.6 line and published under neither `cvlr-spl-token` nor `cvlr-solana-token`. Entries
  needing it model the token account themselves. The gap is recorded in the reference data under
  *both* names, so searching either finds it; if the crate is ever published the corpus is revised
  then, rather than pre-emptively.
- **The scaffold never emits a git-branch or `[patch.crates-io]` pin**, however current the project
  that does. That is precisely what makes the newest engagement's build unreproducible from its own
  repository, and it is not a practice to propagate to users.
- **A stamp needs the platform generation, not just the CVLR version** (§4.3.6).

**And the docs need the compile gate too.** Building the probe surfaced two defects in the manual's
own examples, both of which fail against every available version:

- `cvlr_rules! { … bases: [a, b, c], }` — the macro arm is `bases: [ $($base:ident),* $(,)? ]`,
  which does not accept a trailing comma *after* the bracket.
- `let acc_infos: [AccountInfo; 8] = cvlr_deserialize_nondet_accounts();` — both 0.5.0 and the dev
  branch return a hard-coded `[AccountInfo<'a>; 16]`.

Neither is version skew; both are wrong as written. This qualifies §3.0.1: the manual is the
normative and quotable source, but "quotable" still means *quotable after it compiles*. Fixing
these upstream is cheap and is the first concrete payback from this work.

### 4.8 Entry status — the prototype must be honest about confidence

Every entry carries a status, and the status governs where it may be used:

| Status | Meaning | May be retrieved? | May enter `backend_guidance`? |
|---|---|---|---|
| `verified` | Machine-checkable and checked: compiles, and its claim is about API shape rather than methodology | Yes | Yes |
| `proposed` | LLM-abstracted from instances, unreviewed | Yes, **marked as unreviewed** | No |
| `quarantined` | Carries an uncertified soundness claim, or a form we cannot date | **Not as advice** | No |

The distinction between `proposed` and `quarantined` is the one that matters, and it is not about
confidence level — it is about failure mode. An unreviewed *how-to* entry that is wrong wastes a
loop iteration and the compile gate catches it. An unreviewed *soundness* claim that is wrong
produces a proof of something false, and nothing downstream catches that. So unreviewed how-to
content ships with a marker; unreviewed soundness content does not ship as advice at all.

### 4.9 Phase A exit criteria

- [ ] A `cvlr_kb` manifest ingests cleanly via `composer.scripts.rag_import`, dry-run reviewed.
- [ ] Every code-bearing entry compiles against current CVLR.
- [ ] Retrieval spot-check: ~20 questions an authoring agent would actually ask return a sensible
      entry. (Judging "sensible" here is machine-adjacent — the questions can be written from the
      extracted rule corpus, and the check is "did anything relevant come back", not "is the
      advice right", which is Phase B's job.)
- [ ] Every entry carries a status (§4.8) and provenance.
- [ ] **The ledger exists, is ranked, and every quarantined entry is represented in it.**
- [ ] The whole pipeline re-runs from the project manifests with one command.

At that point the backend can be wired to a real corpus and Phase B can start — and, importantly,
so can measurement: we find out whether retrieval helps before spending expert time improving it.

---

## 5. Phase A's second output — the question ledger

### 5.1 Questions are *generated*, not composed

Every mechanical stage knows the shape of its own ignorance. The ledger is what it emits when it
hits one, so the list is derived from actual gaps and is complete with respect to them:

| Question source | Trigger in Phase A | Question shape |
|---|---|---|
| **Currency gap** | §4.5 bottom-left quadrant | "This problem recurs in N projects and every solution we have is legacy. What is the current way?" |
| **Uncertified soundness** | §4.6 field 3 | "Is this argument correct — does the transformation preserve what it claims?" |
| **Unexplained FAIL** | §4.3.5 | "Known bug, tool limitation, or is the property genuinely false?" |
| **Divergent solutions** | Same problem, N materially different solutions, no tier signal to break the tie | "Which of these is right, and why the difference?" |
| **Undatable instance** | §4.4 reaches *unknown* | "Is this still how we do it?" |
| **Unexplained conf/env entry** | An option or summary entry with no discernible pattern across projects | "Why is this needed? What breaks without it?" |
| **Orphan mock** | A mock with no obvious counterpart in the program | "What is this standing in for, and why?" |

The **divergent-solutions** detector deserves its own note: it is purely mechanical (cluster
instances by problem, diff their forms), it is invisible to any single-project review, and its
output is exactly the disagreement that a practitioner can resolve in thirty seconds and nobody
else can resolve at all.

### 5.2 What a ledger entry contains

- The question, in one sentence, answerable without reading this document.
- **The evidence**: the instances, with project and version, so the expert is reacting to real
  code rather than recalling.
- **What it blocks**: the corpus/KB entries that stay `proposed` or `quarantined` until it is
  answered.
- **Weight**: recurrence of the blocked entries, so the list is orderable.
- The **write-back**: which entry this upgrades, and to what status.

That last field is what keeps Phase B from turning into a conversation with no artifact. Every
answer has a predetermined destination.

### 5.3 Prioritization

Rank by blocked weight, not by curiosity. A question blocking one high-recurrence idiom outranks
five questions blocking one-offs. Publish the ranked list with a suggested time budget per band,
so the experts can stop at any point and know that what is left is the least consequential.

---

# PHASE B — Expert refinement

## 6. Answering the ledger

### 6.1 Question-driven, not project-driven

Phase B is **not** a set of interviews about projects. It is a ranked list of specific questions
with evidence attached, answerable in minutes each. This is the entire payoff of the split: expert
time goes to the ~40 things only they know, not to narrating work they finished months ago.

Practical form: a working session per band of the ledger, with the evidence on screen. Most
answers are one or two sentences. Batch by *kind* rather than by project — twenty soundness
sign-offs in a row are much faster than twenty context switches.

### 6.2 The modernization pass (the currency gaps)

For each recurring problem with no normative-tier solution, someone who knows current practice
**authors** the current answer, rather than extracting one. Then it is verified like everything
else: it compiles against current CVLR, and ideally it is exercised on the reference project.

This is the one place where capture stops being extraction and becomes authorship, and it is easy
to let it quietly degrade into "ship the old pattern, it is what we have". Two rules:

- **An unmodernized legacy idiom never enters `backend_guidance` or the corpus as advice.** It may
  appear in the KB as a deprecation mapping (§7.2), which frames it as something to recognize
  rather than something to write.
- **The pass has an explicit "we don't know" outcome.** If nobody can say what the current answer
  is, that is a finding about a gap in our own practice and belongs on the work list in that form
  — not smoothed over with the legacy pattern.

### 6.3 The residue that is not question-shaped

Some knowledge cannot be reached by a generated question, because Phase A does not know it is
missing. A short, time-boxed conversation per project (~30 minutes) covers it:

1. What did you try first that **did not work**, and what did the prover say?
2. Which rule was hardest to get to verify, and what finally did it?
3. *(normative-tier authors)* What did you deliberately do **differently** from the older
   projects, and why?
4. What would you tell someone starting this project on day one?

Answers 1 and 2 are the material for KB recipes keyed by *symptom* — the retrieval mode an
authoring agent actually needs when a run is stuck. Answer 3 is the rationale behind the normative
tier: without it we can copy the current form but cannot tell an agent *why* it is preferred,
which is what it needs in order to generalize.

Do this **after** the ledger, not before. The ledger will already have surfaced much of it, and
what remains is genuinely open-ended and better asked once the specifics are out of the way.

### 6.4 Write-back and re-ranking

Each answer upgrades entries along its recorded write-back path: `quarantined` → `proposed` →
`verified`, or *deleted*, which is a perfectly good outcome. A newly-authored modernization
becomes a normative instance, which changes §4.5's currency axis — so **re-run Phase A** after a
Phase B batch. It is cheap by construction, and it means the second pass ranks against better
information than the first.

---

## 7. Destinations

Shared by both phases; what differs is which statuses (§4.8) are allowed through.

### 7.1 RAG corpus — reference prose

Emit a `cvlr_kb` manifest per [rag-import-format.md](./rag-import-format.md) and ingest with
`composer.scripts.rag_import`. The producer needs no RAG dependencies, so this can be a plain
script.

- `embedded_groups` — the retrieval passages. Block kinds carry the chunking semantics:
  `paragraph` for prose, `code` for the synthetic example (never split, never embedded as prose),
  `atomic` for tables, `continuation` for prose resuming around a code block.
- `manual_sections` — whole reference units returned intact by `get_section`.
- `headers` — a path of ≤6 levels; a taxonomy **we choose**, and it should be chosen once before
  bulk authoring. A workable first cut: *Primitives / Accounts & State / Mocking & Munging /
  Nondeterminism / Logging & Diagnosis / Conf & Environment / Methodology*.

**Where the chain marker goes is not uniform, and getting it wrong costs retrieval.** Content
splits three ways, and each wants a different granularity:

| Category | Example | Chain marker |
|---|---|---|
| **Chain-independent** | the `cvlr` core — `cvlr_assert!`, `nondet`, `clog!`, mathint, vacuity checks, and all methodology | none |
| **Analogous** — same concept, different spelling | nondet a platform value: Solana `cvlr_nondet_pubkey` ↔ Soroban `nondet_address`; declare a rule: core `cvlr` ↔ `cvlr_soroban_derive::rule`; summarize: a `cvlr_summaries.txt` entry ↔ `cvlr_soroban_macros::apply_summary` | **on the code block**, section keyed by *concept* |
| **Chain-specific** | accounts / PDAs / signers vs. storage durability / TTL / `require_auth` | **on the section** |

The middle row is the one a uniform chain tag damages. If those sections are chain-scoped, an
agent asking "how do I nondet the entry state" while working on Soroban gets *nothing* from an
entry tagged `solana` — even though the concept transfers and the answer is one substitution away.
Key such sections by concept and carry per-chain spellings inside them, so retrieval hits from
either chain and the reader sees the correspondence.

The bottom row inverts: a Solana account-model entry retrieved during Soroban work is worse than
no hit, so those stay chain-scoped at the section level.

This also confirms the one-corpus decision empirically (main plan §5.4): of the nineteen crates
observed, only six are chain-specific — three per chain — and the analogous category is
substantial. Two corpora would duplicate the shared majority *and* hide the correspondences.

Accepts `verified` and `proposed` (marked). Answers *"how do I express X in CVLR?"*

### 7.2 KB recipes — situation → action

Follow the existing CVL recipe format exactly: one `index.yaml` of
`{id, name, triggers[], channel, file, search_terms[]}` plus one markdown file per recipe under
`resources/recipes/` ([kb/resources/](../composer/kb/resources/)). Recipes are short — the CVL set
averages ~25 lines.

**The `channel` vocabulary must be redefined for CVLR.** CVL's channels are `CVL` / `CONF` /
`EDIT`, reflecting the CVL action space. CVLR's is different, and §4.3.4's taxonomy hands us the
vocabulary:

| Channel | Meaning |
|---|---|
| `RULE` | Fix it in the rule — assertions, assumptions, nondet |
| `MOCK` | Redirect a module to a mock implementation |
| `GATE` | Feature-gate a swap in the program source |
| `ENV` | An `cvlr_inlining.txt` / `cvlr_summaries.txt` entry |
| `CONF` | A prover option — diagnose and surface, outside the agent's action space |
| `SCAFFOLD` | Project structure or Cargo wiring |

Defining this *from* the capture data rather than guessing is a concrete payoff of capturing before
authoring the KB, and the channel is what tells a stuck agent whether the fix is even in its
action space.

**Part of this vocabulary is chain-dependent, so validate it per chain before fixing it.** `ENV`
is derived from Solana's `cvlr_inlining.txt` / `cvlr_summaries.txt` files; the surveyed Soroban
projects summarize with an attribute macro (`cvlr_soroban_macros::apply_summary`) instead, which is
a different action in a different place. So `ENV` may not apply to Soroban at all while that chain
may need a channel the Solana taxonomy has no name for. A recipe whose channel does not exist on
the reader's chain is worse than no recipe, because the channel is exactly the part the agent acts
on.

**Deprecation mappings belong here, and they are not waste.** A legacy idiom that lost the
currency test still has a job: the agent *will* meet it in existing client projects it is asked to
extend, and may carry it in its own priors. So emit a recipe whose trigger is *recognizing the
legacy form* and whose body is the current spelling. This is the one destination where legacy
material is authoritative, precisely because it is framed as something to recognize rather than
something to write.

Answers *"I am seeing X — what do I do?"* and *"this project does X — is that still how we do it?"*

### 7.3 `backend_guidance` and prompt fragments

The top idioms only, by §4.5 rank, `verified` status only. This text is in the cached system
prefix of every property and authoring agent — the most expensive real estate we own. Set a hard
line budget before writing it and defend it; everything that does not fit goes to §7.1 or §7.2,
both retrieved on demand.

**Phase A contributes little here on purpose.** Guidance is methodology, methodology is what needs
certifying, and certifying is Phase B. Phase A's contribution is the API-shape subset that is
machine-verifiable.

### 7.4 Fixtures — not knowledge

The reference project plus its `expected.json` files become the smoke scenario, the replay tape,
and the integration-test verdict fixtures (main plan §6 and §7.8). Track separately from the
knowledge work: different consumers, different lifetime, and the only destination that must stay
byte-exact rather than abstracted.

---

---

## 8. Where this lives

### 8.1 The two precedents already in the repo

**Yes — AutoProver has two working models for an externally-sourced corpus, and they differ.**

**Precedent 1 — the CVL manual (the older way).** The source of truth is a *separate* repo,
never vendored and not a submodule: [scripts/gen_docs.sh](../scripts/gen_docs.sh) shallow-clones
`Certora/Documentation`, sphinx-builds it, and drops singlehtml files into
`scripts/prover-docs/` — which is **untracked**, pure local scratch. Then
[scripts/populate_rag.sh](../scripts/populate_rag.sh) runs the corpus-specific builder
[composer/scripts/ragbuild.py](../composer/scripts/ragbuild.py), which parses that HTML and
talks to the RAG DB directly. The curated CVL *KB* is different again: it is committed **in**
AutoProver as package data under [composer/kb/resources/](../composer/kb/resources/).

Two things to take from it. First, the pattern of "external source of truth, cloned on demand,
intermediate artifacts untracked" is established and works. Second — and this is free money —
`gen_docs.sh` already loops over `docs/{cvl,solana,prover,user-guide}/`, so **it already builds
`solana.html` today**; the Solana Prover docs are being generated and simply never ingested.
§4.7's bulk-docs layer is mostly wiring that already exists.

**Precedent 2 — Crucible (the current way, and the one to model).** Per
[rag-import-format.md](./rag-import-format.md) §6, the application's own repo builds a
`<kb>.rag.json` manifest "however it likes" and commits it; AutoProver ships only the schema
([composer/rag/import_format.py](../composer/rag/import_format.py), deliberately importable with
**no** RAG-stack dependency) and the generic importer. Nothing corpus-specific enters AutoProver
except registrations.

**Model the new repo on precedent 2.** Precedent 1 requires AutoProver to host a
corpus-specific builder and a doc checkout, which is exactly what the import-format work was
written to eliminate; precedent 2 is the documented, tested path
([tests/test_rag_import.py](../tests/test_rag_import.py)) with both registries sitting empty
awaiting a first entry. It also keeps every project-derived byte out of the main repo.

### 8.2 Two manifests, one tag

Do **not** put the whole corpus behind the private repo. Split it by provenance:

| Manifest | Built in | Content | Needs project access? |
|---|---|---|---|
| `cvlr-docs.rag.json` | AutoProver | Published docs (§4.7) + generated crate reference | No |
| `cvlr-practice.rag.json` | The private repo | Project-derived idioms (§4.6) | Yes |

Both declare `knowledge_base: "cvlr_kb"`. Ingesting several manifests into one tag is supported
and tested — `part` numbering continues across manifests sharing a DB
(`test_part_numbering_continues_across_manifests_sharing_a_db`).

The payoff is **graceful degradation**: someone with a plain AutoProver checkout and no access to
the private repo still gets a working CVLR corpus — API coverage without practice coverage. Given
that §4.7 is also the fastest path to a non-empty DB, this makes the public half both the first
thing built and the thing that never blocks anyone.

### 8.3 The private repo's layout, and its internal boundary

```
certora-cvlr-kb/                          (private; created at ~/src/certora-cvlr-kb)
  projects/            inventory (§3) + per-project artifact manifests (§4.2)
  extract/             raw extractor output (§4.3) — CLIENT-DERIVED, gitignored
  ledger/
    open/              generated questions — machine-owned, rewritten every run
    answers/           human-owned, keyed by question id
  entries/             abstracted entries with status + provenance (§4.6, §4.8)
  recipes/             KB recipes, incl. deprecation mappings (§7.2)
  tools/               extractors, abstraction driver, ledger generator, publisher, publish gate
  src/certora_cvlr_kb/
    data/              THE published artifacts: cvlr-practice.rag.json + the recipe set
```

Two refinements the skeleton settled:

- **`extract/` is gitignored, not merely unpublished.** It is fully regenerable from the project
  checkouts, so committing it buys nothing but permanence — and permanence is the risk, since
  client excerpts in git history cannot be un-shipped. This is affordable precisely because
  ledger entries cite evidence **by reference** (project id + rev + path + symbol) rather than by
  value, so a question resolves against a checkout instead of a stored copy.
- **There is no separate `dist/`.** The installable package's data directory *is* the dist, so
  there is exactly one published location and no authoritative-copy question.

The important line is not the repo boundary, it is the one **inside** the repo: `extract/` holds
client-derived material and never publishes; `entries/`, `recipes/` and `dist/` are sanitized.
Private-repo status is not a substitute for sanitization, because everything in `dist/` ends up
embedded in a database that ships.

Note that the no-verbatim rule is now *two* rules with one enforcement point: confidentiality (no
client identifiers or code leaving the boundary) **and** methodology (no engagement code quoted at
all, §3.0.1). The second is the binding one, since it applies even to public engagement repos that
the first would permit.

**Enforce that boundary in CI, not in review.** A publish job that re-runs §9's cleanliness gate
over `dist/` — no client identifiers, no verbatim excerpts, every code block compiles — is the
difference between a rule and a hope. It is also the only gate that cannot be recovered from
after the fact.

### 8.4 Publishing: a small installable package

The private repo should ship its `dist/` as a **thin installable package** (`certora-cvlr-kb`,
manifest + recipes as package data) that AutoProver takes as an *optional* dependency, rather
than having its output committed into AutoProver.

Why not just commit it into AutoProver:

- It keeps generated content out of the main repo, so there is no "which copy is authoritative"
  question and no stale-copy failure mode.
- The corpus gets its **own version number**. Given how much §3.1/§4.4 machinery exists to track
  which practice is current, a run being able to record *which corpus version it used* is worth
  the packaging cost.
- Optional-dependency semantics express §8.2's degradation exactly: absent package → docs-only
  corpus, which is a supported state rather than a broken one.
- It is the natural on-ramp to import-format §6's Level 2 (`rag_import --from-wheel`), which
  already anticipates a corpus arriving inside a package.

Fallback if packaging friction is high: commit `dist/` into AutoProver under a clearly-marked
generated path, with a check that it matches the private repo's `dist/` at a recorded commit.
Ordinary, but it reintroduces the two-copies problem.

### 8.5 The KB recipes are the awkward case

CVL's recipes are loaded from package data via `importlib.resources`
([composer/kb/kb_context.py](../composer/kb/kb_context.py)), so CVLR's must also be reachable as
package data. But they are *authored* in the private repo, next to the evidence and the ledger
answers that produced them.

So they follow the same publish path as the manifest: **authored in the private repo, shipped in
the package, never hand-edited downstream.** If a recipe needs fixing, it is fixed upstream and
republished. Whichever mechanism §8.4 lands on, the recipe loader must be able to find recipes
from the installed corpus package as well as from AutoProver's own resources — a small
generalization of the loader, and the one code change on the AutoProver side that is not a pure
registration.

### 8.6 Storing the ledger: the regeneration problem

The ledger is **generated** (Phase A re-runs, §4.9) *and* **annotated** (Phase B answers). Those
two facts fight, and the storage layout is what settles it:

- `ledger/open/` is **machine-owned**. A Phase A re-run rewrites it wholesale. Never hand-edit.
- `ledger/answers/` is **human-owned**. A re-run never touches it.
- A question is *resolved* iff an answer file matches its id — so re-running Phase A after Phase B
  regenerates only the questions that are still open, and closing the loop needs no state machine.

That requires question ids that are **stable across re-runs**, which means content-derived rather
than sequential: an id is the hash of a declared identity tuple (question kind + the identity of
its subject — the idiom key, the rule name, the conf option). Two rules keep this honest: the
identity tuple is stored as **fields alongside** the id, and nothing ever parses information back
out of the id string. The id is an address; the data lives in the record.

**The ledger never publishes.** Answers quote client specifics freely — that is the point of
having a private repo — and only the sanitized entries derived from them cross into `dist/`.

### 8.7 The AutoProver-side registrations

Small, and enumerated by [rag-import-format.md](./rag-import-format.md) §7. Four things, in one
change, because a half-registration validates and then silently produces no tools. **All four are
done:**

1. **`KNOWLEDGE_BASES["cvlr_kb"]`** plus `CVLR_DEFAULT_CONNECTION` in
   [composer/rag/db.py](../composer/rag/db.py) — the registry's first entry.
2. **[composer/tools/cvlr_rag.py](../composer/tools/cvlr_rag.py)** — `cvlr_get_section` /
   `cvlr_manual_search` / `cvlr_keyword_search`, registered in
   [rag_env._FACTORIES](../composer/tools/rag_env.py) behind a deferred import. The Python backend
   binds these directly rather than through a wheel descriptor, but the registry is the same one.
3. **DB role and schema** in [init-db.sql](../composer/scripts/init-db.sql): `cvlr_rag_user` owning
   a `cvlr_rag` schema in the shared `rag_db`, `search_path` set per role — exactly as `rag`,
   `extended_rag` and `foundry_rag` already are.
4. **[scripts/populate_cvlr_rag.sh](../scripts/populate_cvlr_rag.sh)** plus a `setup-db` step in
   [autoprove-entrypoint.sh](../scripts/autoprove-entrypoint.sh). Both ingest whichever of §8.2's
   manifests they find and treat "none" differently by context: the script errors (an explicit
   populate that would leave the corpus empty is a mistake), while `setup-db` reports a skip (no
   CVLR corpus is a supported degradation, and failing image setup over it would be wrong).

Both of the pieces this section used to list as *not* done are now done, and the shape they took
is worth recording:

5. **The public docs manifest producer** (§4.7.1) —
   [cvlr_docs_manifest.py](../composer/scripts/cvlr_docs_manifest.py) over
   [html_manual.py](../composer/rag/html_manual.py). The parser was extracted from `ragbuild.py`
   instead of written alongside it, with the CVL corpus verified unchanged; a second HTML parser
   would have drifted within a release.
6. **The corpus is baked into the image.** `docs-builder` now builds `solana.html` as well as
   `cvl.html` and records the docs revision in a `PROVENANCE` file; the final stage runs the
   producer, so the public manifest is part of the image. `setup-db` discovers the two halves
   independently — the baked public one and, if installed, the private practice package — rather
   than treating the second as a fallback for the first. An install with neither still degrades to
   static guidance, which is why finding nothing is a logged skip and not a setup failure.

Not verified by a real image build: the Dockerfile and entrypoint changes are checked by shell
parse and by a simulation of the discovery block against all three cases (neither half, public
only, both), but no container has been built from them here.

## 9. Acceptance gates

**Per entry, both phases:**

- [ ] **Compiles.** Every code example builds against *current* CVLR — the same compile check that
      validates the generated crate reference (main plan §5.4/§5.5). The corpus is not allowed to
      be the one place with unverified code in it.
- [ ] **Clean.** No client identifiers, no verbatim confidential code; provenance by count.
- [ ] **Not quoted from an engagement.** Engagement-derived code is paraphrased or generalized,
      public repo or not (§3.0.1). A verbatim block is admissible only from a `tutorial` or
      `examples` source **and** only after passing the currency check.
- [ ] **Version-stamped.** Known CVLR version range.
- [ ] **Status-labeled** per §4.8, with the ledger question named if not `verified`.

**Additionally, to reach `verified`:**

- [ ] **Generalized.** Cites ≥2 projects, or is explicitly project-specific and lives in the KB
      rather than the corpus.
- [ ] **Sound.** Any munge-shaped entry states what the transformation preserves, **signed off by
      a human**.
- [ ] **Current.** The canonical form is normative-tier, or came through §6.2, or the entry is
      explicitly a deprecation mapping. "Extracted from the projects" is not by itself a claim
      that it is how we do things now.

**Per corpus, before ingest:**

- [ ] `rag_import --print` dry-run reviewed by a human — the format is easy to get subtly wrong
      (header paths, block kinds) and the failure mode is silent bad retrieval, not an error.
- [ ] Header taxonomy fixed and applied consistently.
- [ ] Retrieval spot-check (§4.9).

---

## 10. What not to do

- **Do not wait for expert availability to start.** Phase A is gated on nothing but Stage 0, and
  its whole purpose is to convert "we need to talk to people" into a ranked list of forty specific
  questions.
- **Do not let the prototype's `proposed` entries harden into truth by inertia.** They are marked
  for a reason; the ledger is what un-marks them. A prototype that quietly becomes the product is
  the main risk of shipping early.
- **Do not dump spec files into the corpus.** Embedding whole project specs produces retrieval
  that answers "what did project P do" instead of "how do I do X". The most tempting shortcut
  here, and it degrades the corpus rather than filling it.
- **Do not import the munge diff raw.** Thousands of lines of protocol-specific edits; the value is
  entirely in the classified taxonomy, not the hunks.
- **Do not rank solutions by popularity.** The older projects outnumber the current ones, so a
  single-axis count systematically promotes superseded practice and reads as well-attested while
  doing it. The failure mode most likely to survive review, because the output looks
  well-evidenced (§4.5).
- **Do not quietly ship a legacy idiom as guidance** because no modern instance exists. Either
  modernize it (§6.2) or record the gap; a corpus that teaches yesterday's practice is worse than
  one with a hole in it, because the hole is visible.
- **Do not diff branch tips** (§4.1).
- **Do not let extraction volume substitute for §6.3.** The failure knowledge is the
  differentiator and it is not in the repositories.

---

## 11. Sequencing and effort

| Stage | Phase | Nature | Blocking? |
|---|---|---|---|
| 0 — Inventory, disclosure, tiering | pre-A | Admin judgment, ~1 day | **Blocks everything** |
| 4.1–4.2 — Locate + manifest | A | Script | Blocks 4.3 |
| 4.3 — Extractors | A | Script; hours, re-runnable | Blocks 4.4 |
| 4.4–4.5 — Classify + rank | A | Script | Blocks 4.6 |
| 4.6 — Abstraction | A | LLM; the main token cost, bounded by recurrence order | Blocks 4.8 |
| 4.7 — Docs + crate reference | A | LLM; **parallel with everything above** | — |
| 8.1–8.7 — Private repo + AutoProver registrations | A | Mechanical; **do early**, everything else lands in it | Blocks 4.8 |
| 4.8–4.9 — Assemble + ingest | A | Mechanical | **Ships the prototype** |
| 5 — Ledger | A | Emitted by the stages above | Blocks B |
| 6.1–6.2 — Ledger answers + modernization | B | Expert time; the scarce resource | Upgrades entries |
| 6.3 — Open-ended interviews | B | Human time, after the ledger | KB symptom recipes |
| 6.4 — Write-back + re-run A | B | Mechanical | — |

**Within Phase A, do the normative tier first and finish it** before working the older projects.
It establishes the canonical vocabulary that everything else is classified *against* — the other
order makes current idioms arrive as exceptions to a legacy baseline rather than the baseline
itself. And if time runs short, what got done is the part that decides how the agent writes rules;
the older projects contribute breadth, which degrades gracefully.

§4.7 is the fastest path to a non-empty DB and depends on no project at all, so it should run
first in wall-clock terms even though it is listed after extraction. If everything else slipped,
the docs-plus-crate-reference corpus alone is still a usable prototype — and per §8.2 it is
already the manifest that needs no private-repo access, built from `solana.html`, which
`gen_docs.sh` produces today.

Stand up the private repo and the four AutoProver registrations (§8) **before** the extraction
work, not after. They are small and mechanical, they are where every later artifact has to land,
and doing them first means the prototype ships the day the first manifest validates instead of
waiting on plumbing.

The output of §4.3 and §4.5 is *immediately* useful before any corpus exists: the conf frequency
table, the env-file inventory, and the munge taxonomy each settle an open question in
[cvlr-backend-plan.md](./cvlr-backend-plan.md) §8 that is currently answered by reading the
documentation and hoping.
