# Plan — retire the CVLR source mount, answer API questions from documentation

Today the CVLR authoring agent and its judge read the **source** of the CVLR crates the build
resolved. This plan replaces that channel with three pieces:

* **`cvlr_api_kb`** — a new corpus extracted mechanically from the CVLR crates' **API
  documentation** (rustdoc), compile-gated, and regenerated per release.
* **`cvlr_kb`** — the existing corpus, re-scoped to what it actually is: the *manual* and
  verification practice. Prose, hand-curated, and allowed to lag.
* **`cvlr_research`** — a research sub-agent that answers CVLR questions out of both, in the shape
  `cvl_research.py` already has for CVL.

**The two corpora are the point, not an implementation detail.** What the source mount really
provided was not "source" — it was an *authority ordering*: a channel derived from the code, which
outranks a channel written by hand. Splitting the corpora preserves that ordering with the new
channel in the top slot. One merged corpus would not: a retrieval hit carries no provenance, so a
stale manual paragraph and a generated signature would arrive indistinguishable.

This reverses a decision that `cvlr-backend-plan.md` §5.5 argued for and §7.5.5 measured, so §1
states what that decision bought and what giving it up costs. §2 settles the version question by
pinning — the CVLR analogue of CVL being whatever the Prover ships. §3–§8 are the migration.

---

## 1. What is there now, and what the change forfeits

### 1.1 The pieces

| piece | role |
|---|---|
| [`composer/spec/cvlr/crates.py`](../composer/spec/cvlr/crates.py) | resolves the CVLR family from `cargo metadata`; reports gaps against the reference set |
| [`composer/spec/cvlr/crate_mount.py`](../composer/spec/cvlr/crate_mount.py) | presents those crate trees under one version-stamped namespace (`cvlr-asserts-0.6.1/src/lib.rs`) |
| [`composer/spec/cvlr/source_tools.py`](../composer/spec/cvlr/source_tools.py) | `cvlr_source_files` / `cvlr_source_read` / `cvlr_source_search` over the mount |
| [`cvlr_source_tools.j2`](../composer/templates/cvlr_source_tools.j2) | the prompt fragment that says the mount is **the authority** |
| [`cvlr_rag_tools.j2`](../composer/templates/cvlr_rag_tools.j2) | the fragment for the existing `cvlr_kb` corpus tools, which says the corpus is *not* the authority |
| [`pipeline.py:484`](../composer/spec/cvlr/pipeline.py#L484) | mounts, builds the tools, carries them on `CvlrDeps.crate_tools` |
| [`author.py:645`](../composer/spec/cvlr/author.py#L645) | routes them to the author **and** the judge |

Two asymmetries matter for the migration and are easy to miss:

* **The judge has the crate source and no corpus.** `cvlr_property_judge_system_prompt.j2:205`
  includes `cvlr_source_tools.j2`; nothing includes `cvlr_rag_tools.j2` there, and
  `build_feedback_thunk` passes `extra_tools=crate_tools` and nothing else. Deleting the mount
  without giving the judge the research tool leaves it with **no** CVLR knowledge channel at all.
* **The code explorer never got this.** §5.5 proposed teaching it about CVLR source; that half was
  never built. There is nothing to migrate there, and the research tool is the natural home for it.

### 1.2 What we give up — stated plainly, because each of these needs a mitigation

1. **Negative evidence.** `cvlr_source_search` can say *"`nondet_option` does not appear in the CVLR
   sources this project builds against … do not use it."* A vector search cannot: it always returns
   its nearest rows, and "nothing relevant came back" is indistinguishable from "the query was
   phrased oddly". Existence is the single most common question the author asks, and answering it
   wrong is a compile error at best and a plausible-looking wrong helper at worst.
   → Mitigation: §4.3 (complete per-crate symbol index sections) and §5.3 (the researcher's
   not-found contract).
2. **The authority ordering — preserved, not lost.** Both prompt fragments are built around
   *source outranks corpus*. That ordering is real and worth keeping: one channel is derived from
   the code and one is written by hand. It survives as *API corpus outranks manual corpus* (§4),
   which is why the corpora are split rather than merged. What is genuinely given up is the
   *strength* of the top channel: rustdoc is the code's own account of itself, one build step away
   from the code, where the mount was the code. A doc comment can be wrong in a way a signature
   cannot. → Mitigation: §4.7's compile gate, plus the version pin (§2.4), which closes the other
   half of the distance.
3. **Tolerance for a project on another CVLR line.** The mount was what made a floating version
   safe. Nothing replaces it, so the version gets pinned instead and off-pin projects are refused
   (§2). That is a deliberate narrowing of what we accept, not a mitigation.
4. **Cost shape.** `cvlr_source_search` is a local grep. The research agent is an LLM call with its
   own tool loop. §7.5.5's census recorded **50 combined `cvlr_source_read`/`cvlr_source_search`
   calls in one run**; the replay tape has 514 mentions of `cvlr_source`. Converting that traffic to
   sub-agent invocations is a real budget change, and the `AgentIndex` cache is not optional here.

### 1.3 What the change buys

* The author stops spending context on Rust it has to *read as a compiler would*. Doc text answers
  "how do I model a token account" directly; source only ever answers it by inference.
* rustdoc resolves re-exports. The mount's standing hazard — `cvlr` re-exports almost everything, so
  the definition of `cvlr_assert!` is in `cvlr-asserts` — is a fact rustdoc records rather than a
  habit the prompt has to teach.
* The precedence rule gets *simpler*, not more complex. Today it is "source outranks the corpus",
  where the corpus is one bag holding the manual, a crate reference and abstracted practice entries
  of varying confidence — so the rule sorts one thing above a heap. After the split it sorts two
  named corpora, each with a stated provenance and a stated regeneration lifecycle.
* One place (the API producer) where feature gating, deprecation and `#[doc(hidden)]` are decided,
  rather than a habit each agent has to learn from reading `lib.rs`.
* The research sub-agent returns a short synthesized answer instead of raw files, which is the same
  context economy the code explorer exists for.

---

## 2. The version question — settled by pinning

**Policy: the run pins the reference set. A project that already pins a different CVLR is refused
at preflight.** This is the CVL arrangement: the Prover ships one CVL and the language version is
not a thing a project chooses. CVLR's version *is* a thing a project can choose, so ours is a
policy where CVL's is a physical constraint — but the resulting contract is the same one, and it is
the contract the rest of this plan is written against.

That is not a hedge that was available before. With the source mount, a floating version was safe
because the mount made the *resolved* crates authoritative and said so in every prompt. Removing
the mount removes that defence, so the version has to be nailed down somewhere; pinning is the
cheapest place.

### 2.1 Three versions become one

| # | version | where it comes from | under this policy |
|---|---|---|---|
| 1 | **the reference set** | [`composer/spec/cvlr_reference.py`](../composer/spec/cvlr_reference.py) — `cvlr` 0.6.1, `cvlr-solana` 0.5.0, `cvlr-solana-stake` 0.5.0, `cvlr-spl-token` 0.5.0; Soroban: `cvlr-soroban` / `cvlr-soroban-derive` 0.4.0 | the single source of truth; a bump is a code edit, a corpus rebuild and a PR |
| 2 | **what a fresh project is pinned to** | `ChainReference.scaffold_crates()` via `scaffold._scaffold_pins` | unchanged — it *is* (1), minus anything `--withhold-crate` removed |
| 3 | **what the run compiles** | `crates.resolve(workspace)` after the scaffold | **guaranteed equal to (1)**, because anything else was refused |

The corpus documents exactly (1). The author is writing against exactly (1). There is no divergence
to caveat, and `cvlr_api_kb` never needs a second version's content.

### 2.2 The gate — a widening of a refusal that already exists

This is not a new *kind* of behaviour. `scaffold._check_platform` already refuses a project whose
CVLR pin does not match its platform generation, and `tests/test_cvlr_gate.py`'s docstring records
the consequence: `test_scenarios/solana_vault` pins Anchor 1.x, lands on the Solana v3 split, and
**the scaffold refuses it**, with witness tests in `test_cvlr_scaffold`. The new gate is the same
refusal widened from "wrong platform generation" to "any CVLR pin that is not ours", in the same
place, with the same `Blocked` type, before any LLM spend. That is what `_check_pins` is (§2.6).

The branch that changed is the one that existed to *tolerate* a foreign pin. `_scaffold_pins` said:
if the project already declares the chain crate, it has chosen its CVLR line and the scaffold keeps
it. Its docstring's reasoning — that mixing lines puts two generations of `AccountInfo` in one graph
— is now the refusal's message, and the function is gone, along with `_introduced` and `_declared`,
which existed only to serve it.

**`VersionGap` had to split into two types first**, because it conflated two different facts behind
one `resolved: str | None`, and only one of them is a refusal:

```python
type Divergence = Mismatched | Absent

@dataclass(frozen=True)
class Mismatched:   # refuse: the project builds a CVLR release this build does not support
    crate: str; reference: str; resolved: str

@dataclass(frozen=True)
class Absent:       # fine: the project does not depend on it at all
    crate: str; reference: str
```

`VersionGap.describe()` already spelled out that the two mean different things. `Absent` is the
*expected* state under `--withhold-crate` (§2.3) and must never be a refusal — a run that withholds
`cvlr-solana-stake` would otherwise refuse itself.

### 2.3 The one version-shaped leak the pin does not close: `--withhold-crate`

`entry.py:441` lets a run withhold a specialization — the case is a target that *is* the stake
program, where `cvlr-solana-stake` would hand the author the answers
(`docs/stake-benchmark.md`). Today this works for free: a withheld crate is never pinned, so it is
never resolved, so it is never mounted. **A corpus has the stake rows regardless of what this run
pinned**, and the researcher would serve them. Pinning does not help here: the corpus is correct
about the reference set and the run has deliberately departed from it.

So withholding has to become a corpus-side filter — the researcher is told which crates are out of
scope, and `cvlr_api_surface` (§4.5) must not list them. This is the only version-adjacent work the
pin policy leaves on the table, and it is small.

### 2.4 What the pin deletes from the rest of this plan

Worth listing, because it is most of the complexity the first draft was carrying:

* No three-way gap disposition, no per-run confidence level, no measurement programme to decide
  whether multi-version content is needed. One gate, two outcomes.
* No version divergence for the researcher to reason about: `{{ documented_versions }}` and
  `{{ cvlr_versions }}` are the same string, so the caveat rule leaves the system prompt (§5.2).
* **The authority ordering gets its full strength back.** §1.2(2) worried that rustdoc is one step
  further from the code than the mount was. With a guaranteed version match and a compile gate
  (§4.7), `cvlr_api_kb` is as authoritative about *this run* as the mount was — the gap between
  "the code" and "the code's account of itself" is the only remaining distance, and it is small.
* The version level in the API corpus's header path (§4.3) stops being a multi-version axis and
  becomes pure provenance. Keep it anyway: it is free, it makes a stale corpus visible in every
  answer the researcher gives, and it makes a 0.6→0.7 rebuild legible while it is half-done.

### 2.5 Costs, and what was decided

* **A bump gets heavier.** A `cvlr_reference.py` edit was a pin change. It is now a pin change
  *plus* an API-corpus rebuild, and the two must land together or every answer is about the wrong
  release. **Done in spirit, not in letter:** that docstring now says a bump "moves every project
  this build sets up" and names the one-supported-line rule, but it does not mention the corpus,
  because no corpus exists on `eric/cvlr-preflight` and a forward reference there is unreviewable.
  The coupling has to be written in when §8 step 2 lands the producer.
* **No escape hatch. Decided.** An `--allow-cvlr-version-drift` flag was considered and rejected:
  there is no flag for using an old CVL, and a flag that silently degrades every answer the
  researcher gives is worse than a refusal. Nothing was shipped, and a real need is an argument for
  the next bullet rather than for drift.
* **`cvlr-backend-plan.md` §5.5 still needs correcting.** It cites the three gaps the public
  examples produce — `cvlr` 0.4.1, `cvlr-solana` 0.4.4, `cvlr-solana-stake` absent — as evidence
  for the mount. Under this policy the first two describe targets we now refuse. Open.
* **Open: should the scaffold offer to bump an off-reference project?** It would turn a refusal
  into a fix and is the natural follow-on. It edits the project under verification, so it is
  `who-edits-the-program.md` territory and needs a maintainer's call. Not filed yet.

### 2.6 Landed, in PR #248

**Done.** [#248 "Add the Solana Prover preflight step"](https://github.com/Certora/AutoProver/pull/248)
adds every file the gate touches — `crates.py`, `scaffold.py`, `preflight.py` and
`cvlr_reference.py` are all *new* in it — so the pin work went in as an update to that PR rather
than a follow-up, which kept `VersionGap` from ever landing in the shape we had already decided was
wrong.

Two commits on `eric/cvlr-preflight` (`a396088a`, `3a5bf37f`), pushed, and cherry-picked here as
`ff1a6030` and `b2efa926`:

| file | what changed |
|---|---|
| `composer/spec/cvlr/scaffold.py` | `_check_pins`, beside `_check_platform`, refusing before anything is written. `_scaffold_pins`, `_introduced` and `_declared` existed only to express the deference and are gone; both gates now run unconditionally. |
| `composer/spec/cvlr/crates.py` | `VersionGap` → `Mismatched \| Absent` (§2.2), plus `CvlrSources.mismatched()`. |
| `composer/spec/cvlr/preflight.py` | the post-scaffold backstop: `PreflightFailed` on a `Mismatched`, which the gate cannot produce and a `[patch]` table can. |
| `composer/spec/cvlr_reference.py` | the one-supported-line rule in the module docstring, and four docstrings rewritten to stop explaining themselves by naming a corpus that does not exist there (`3a5bf37f`). |
| `tests/` | five scaffold tests replacing the three that encoded the deference, and the plumbing tests moved to the new types. |
| the PR description | three refusals, not two, with the narrowing argued rather than listed. |

Two things the work settled that the plan had not:

* **The gate needs two readings, not one.** The resolved graph is exact for a crate some member
  already depends on, but a crate pinned in `[workspace.dependencies]` and depended on by nobody
  yet is absent from the graph and about to be inherited by the member being scaffolded. Only the
  manifests see it. A git or path dependency is refused outright: a gate cannot pass a version it
  cannot read.
* **`_check_pins` iterates `crates()`, not `scaffold_crates()`** — deliberately, and it matters
  only on this branch, where `withholding` makes the two differ. A withheld crate the project
  declares *for itself* at a foreign version still collides; a withheld crate that is simply not
  there is `Absent` and never refuses.

Deliberately kept: `UnpublishedCapability` and `ChainReference.cargo_dependencies()` have no
callers in #248 and were left in place rather than dropped and re-added with their consumers.

---

## 3. Shape of the replacement

```
certora-cvlr-kb (private)                          AutoProver
──────────────────────────                         ─────────────────────────────────────
rustdoc producer ─► cvlr_api_kb.rag.json ─┐
practice producer ─► cvlr_kb.rag.json ────┤─► populate_cvlr_rag.sh ─┬─► cvlr_api_kb  (schema cvlr_api_rag)
gen_docs.sh ─► solana.html ───────────────┘                         └─► cvlr_kb      (schema cvlr_rag)
                                                                            │
                                    composer/tools/cvlr_api_rag.py ◄────────┤
                                      cvlr_api_search / _lookup / _surface  │
                                    composer/tools/cvlr_rag.py ◄────────────┘
                                      cvlr_manual_search / _keyword / _get_section
                                                    │
                                    composer/spec/cvlr_research.py
                                      cvlr_research(question)  ── sub-agent, binds both
                                                    │
                                          ┌─────────┴─────────┐
                                       author               judge
```

**The ingest path needs almost nothing.** `composer/scripts/rag_import.py` already groups manifests
by the connection each one's own `knowledge_base` tag resolves to (`_resolve_output`, then
`groups[...]` at :157), and `populate_cvlr_rag.sh` does **not** pass `--output` to it — the
`$conn` it computes is used only for the manual HTML's `ragbuild` call. So a manifest declaring
`"knowledge_base": "cvlr_api_kb"` routes itself to the new corpus with **zero importer changes**,
as soon as the tag has a `KNOWLEDGE_BASES` entry. The script's error text and its comment header
need updating; its logic does not.

---

## 4. The two corpora

### 4.1 Why two, and not one corpus with a `"CVLR API"` header root

Header-root separation inside `cvlr_kb` would be cheaper by a few dozen lines. It is the wrong
call on four counts, and the fourth is the one that decides it.

1. **Regeneration lifecycle.** Ingest is a plain `INSERT` — no `ON CONFLICT`, no truncate — and
   `manual_sections` carries `CONSTRAINT parts_unique UNIQUE (h1..h6, part)`. Re-ingesting a
   corpus therefore either duplicates rows or raises, so the operational model is *drop the schema
   and rebuild*. Merged, bumping CVLR means rebuilding the hand-curated practice entries and the
   manual as collateral. Split, "rebuild the API corpus" is scoped to its own schema and the
   manual is untouched.
2. **Retrieval interference.** rustdoc rows are short, dense and uniform; manual rows are long
   prose. In one vector index they compete badly in exactly the direction that hurts — a query for
   `nondet` surfaces manual paragraphs that *discuss* nondeterminism above the `nondet()` signature,
   because the prose is a better match for a prose query. Two indexes let the researcher ask each
   one deliberately and compare.
3. **The version axis is one corpus's problem** (§2.4c). Merged, one header-path convention has to
   carry both a crate release and a docs revision.
4. **A retrieval hit carries no provenance.** This is the deciding one. `find_refs` returns
   headers, content and a similarity score; `search_manual_keywords` returns headers and a rank.
   Nothing says where a row came from or how much to trust it. Under a header-root convention the
   only thing distinguishing a generated signature from a stale paragraph is a string at `h1` that
   the model has to notice and remember what it means. **The tool's own name is the affordance** —
   the same argument `source_tools.py` makes for why its tools are not called `get_file`. An answer
   from `cvlr_api_lookup` is authoritative because of which tool returned it, and that is a fact
   about the call, not a convention the model has to keep in mind.

Registration cost, for the record, is small and well-trodden — four corpora already exist this way:
a role and schema in `composer/scripts/init-db.sql`, a connection constant and `KNOWLEDGE_BASES`
entry in `composer/rag/db.py`, a `_FACTORIES` entry in `composer/tools/rag_env.py`, and a
`composer/tools/cvlr_api_rag.py`. See §6.

### 4.2 `cvlr_api_kb` — source: rustdoc JSON, not rendered HTML

Build a probe crate from `ChainReference.cargo_dependencies()` (which already emits exactly the
reference set plus the platform crates) and run `cargo +nightly rustdoc -Z unstable-options
--output-format json` over each CVLR crate. JSON rather than scraped HTML because it gives, per
item: the resolved path, the full signature, the doc comment, `#[doc(hidden)]`, deprecation, the
`cfg`/feature gate, and — the one that matters most — **where a re-export actually resolves to**.

`crate_mount.py` stays. It is not agent-facing any more, but it is how the kb repo reads crate
trees as a plain script, and doc comments are not the whole story: **macro bodies are not in
rustdoc JSON in any useful form**, and `cvlr_assert!`/`cvlr_assume!`/`clog!` are the surface the
author touches most. Expect the producer to read those from source and emit the expansion alongside
the doc comment. Correct `crate_mount.py`'s docstring to say producer-only.

### 4.3 Header paths

```
["CVLR API", "cvlr 0.6.1", "cvlr-asserts", "cvlr_assert!"]
["CVLR API", "cvlr-solana 0.5.0", "cvlr-spl-token", "nondet_token_account"]
```

Version at level 2 is provenance, not a retrieval axis: the pin (§2) means one version is in the
corpus at a time. It stays because it is free, it makes a stale corpus visible in every answer, and
it makes a half-finished 0.6→0.7 rebuild legible. Crate at level 3 is the *defining* crate, not the
facade — that is the re-export trap, recorded rather than taught.

### 4.4 What each item contributes, and the one synthetic section

Per public item, two products (`rag-import-format.md` §2):

* `manual_sections` — signature as a `code` block, doc comment as `text`, examples as `code`. This
  is what an exact lookup returns in full, and it is the definitive answer.
* `embedded_groups` — doc prose as `paragraph`, signature as `code`, tables as `atomic`. This is
  what "how do I give an account field a nondeterministic value?" lands on.

Plus, **one synthetic section per crate: the complete public surface, as a list.** This is the
mitigation for §1.2(1). It is the only row that supports a *closed-world* read — the researcher
fetches it and can then say "this crate exports 41 items and `nondet_option` is not among them"
rather than "I found nothing". Mark it `atomic` so chunking never splits it, and give it a stable
heading (`["CVLR API", "<ver>", "<crate>", "Complete public surface"]`) that a dedicated tool
fetches by name.

### 4.5 Tools — `composer/tools/cvlr_api_rag.py`

Three, deliberately not named like `cvlr_rag.py`'s, because the names are where the ordering is
stated:

| tool | backed by | for |
|---|---|---|
| `cvlr_api_search(query)` | `find_refs` | "what helper does X?" — natural language over the item docs |
| `cvlr_api_lookup(name)` | `search_manual_keywords` + `get_manual_section` | "what is the exact signature of X, and which crate defines it?" — the one-shot path for the question the author asks most |
| `cvlr_api_surface(crate)` | `get_manual_section` on the §4.4 heading | "does X exist?" — the closed-world read |

`cvlr_api_lookup` collapsing keyword-search-then-fetch into one call is a deliberate departure from
CVL's three-tool shape. The two-step is right when you are exploring a manual and wrong when you
have an identifier and want its signature, which by the §7.5.5 census is the dominant case.

Every docstring says the same thing in one sentence: **this corpus is generated from the CVLR
crates and is authoritative; where it disagrees with the manual or with recall, it is right.**

### 4.6 `cvlr_kb` — re-scoped, and it loses a product

`cvlr_kb` today holds three things (`db.py`'s comment on `KNOWLEDGE_BASES`): the Solana manual, a
**CVLR crate reference** manifest, and project-derived practice entries. The crate reference is
what `cvlr_api_kb` replaces and does better; it should be retired from the kb repo's producers
rather than left to contradict the generated corpus. That is the one migration step that deletes
existing corpus content, and it needs a maintainer's confirmation that nothing hand-written of
value is only in there.

What stays in `cvlr_kb` is what the API docs cannot say: the manual's methodology, and the
abstracted practice entries (still `UNREVIEWED`-marked, per `CvlrVectorSearch`'s docstring). Its
tool docstrings should be edited to state its new position in the ordering — advice and practice,
not authority on what exists.

### 4.7 The compile gate stays, and now covers more

The kb repo already compile-gates corpus content against the reference set (`cargo check`, a few
seconds on the host — no SBF toolchain needed). Examples extracted from doc comments go through it,
and rustdoc's own doctests are the cheap version of the same check and should be run. This is what
keeps §1.2(2) honest: the top channel is only authoritative because something compiles it.

---

## 5. The research agent (work here)

### 5.1 `composer/spec/cvlr_research.py`

Mirror `composer/spec/cvl_research.py` closely; it is a good design and divergence costs review.
What carries over unchanged: the `_build_research_graph` shape, the `RoughDraftState` +
`_wrote_draft` validator (a draft before delivering is what keeps the answer grounded), the
`FlowInput`/`MessagesState` pair, `run_to_completion` with a `uniq_thread_id`, and
`with_cvlr_context` (the CVLR bundle, `kb_context.py:211`) in place of `with_cvl_context`.

What differs:

* **Use the indexed variant from the start.** `indexed_cvl_research_tool` / `AgentIndex` is optional
  for CVL and is not optional here — §1.2(4). The author asks the same API questions across units,
  rounds and the judge. Namespace: `("cvlr_research", "cached")`, matching
  `DEFAULT_CVL_AGENT_INDEX_NS`.
* **Tools bound: both corpora.** The three `cvlr_api_kb` tools (§4.5) and the three `cvlr_kb` tools
  (`composer/tools/cvlr_rag.py`), and nothing else. No filesystem, no project source — this agent
  answers about CVLR, not about the program. Holding both is what lets one agent apply the ordering
  instead of pushing that judgement onto every caller.
* **No version reasoning.** The pin (§2) means the corpus and the build agree by construction, so
  the researcher states the version as a fact and never has to hedge on it. What it does need from
  `CvlrPreflight` is the withheld-crate list (§2.3).

### 5.2 `composer/templates/cvlr_research_system_prompt.j2`

Modelled on `cvl_research_system_prompt.j2`, with four CVLR-specific rules. The first carries the
source mount's job across; the rest are narrower:

1. **The ordering.** `cvlr_api_*` is generated from the CVLR crates and compile-gated. `cvlr_*` is
   the manual and abstracted practice: useful for *how* and *why*, and allowed to be out of date
   about *what exists*. When the two disagree about a name, a signature or a behaviour, the API
   corpus is right and the manual is stale — say so in the answer rather than reconciling them
   silently, because a contradiction found here is a defect report for the manual.
2. **Existence is settled in one place.** A name you did not retrieve is a name you do not have. To
   answer "does X exist", call `cvlr_api_surface` for the crate and read it as closed — do not infer
   absence from an empty search, and never from the manual's silence.
3. **Scope.** Crates this run withheld (§2.3) are out of scope; do not recommend them or list them
   as available. There is no version caveat to give — the pin guarantees the corpus describes the
   crates this project builds.
4. **Answer in the shape the caller needs**: the signature, and the crate that *defines* the item
   rather than the facade path — the caller is about to write it into Rust that has to compile.

### 5.3 The tool contract

`cvlr_research(question)`, with a `CVLR_RESEARCH_BASE_DOC` in the shape of `CVL_RESEARCH_BASE_DOC`.
The one addition over CVL's: the not-found contract has to be in the *tool's* doc as well as the
sub-agent's prompt, so the caller knows an "I could not establish that" is a real answer and not a
failure to retry around.

Display: a `CommonTools.cvlr_research` entry in `composer/ui/tool_display.py` beside
`CommonTools.cvl_research`.

---

## 6. Wiring changes, file by file

**Deleted**
* `composer/spec/cvlr/source_tools.py`
* `composer/templates/cvlr_source_tools.j2`

**New**
* `composer/tools/cvlr_api_rag.py` — the three tools of §4.5
* `composer/spec/cvlr_research.py`, `composer/templates/cvlr_research_system_prompt.j2`,
  `composer/templates/cvlr_research.j2` (the prompt fragment that replaces `cvlr_source_tools.j2`)

**Kept, re-purposed**
* `composer/spec/cvlr/crate_mount.py` — producer-only; docstring corrected (§4.2)
* `composer/spec/cvlr/crates.py` — `gaps()` has a real consumer as of §2.6: the scaffold gate and
  the preflight backstop, where until then it only reached a log line
* `composer/tools/cvlr_rag.py` — docstrings restated for its new position in the ordering (§4.6)

**Corpus registration** (all four together, or the tag validates and silently produces no tools —
`rag_env.py`'s module docstring is explicit about this)
* `composer/scripts/init-db.sql` — `cvlr_api_rag_user` role, `cvlr_api_rag` schema, `search_path`
* `composer/rag/db.py` — `CVLR_API_DEFAULT_CONNECTION`, a `KNOWLEDGE_BASES` entry, and the
  `KNOWLEDGE_BASES` comment corrected: it currently describes `cvlr_kb` as holding the crate
  reference, which §4.6 retires
* `composer/tools/rag_env.py` — a `_FACTORIES` entry and a second line in its module docstring
* `scripts/populate_cvlr_rag.sh` — header comment and the no-manifest error text only; the ingest
  logic already routes per manifest (§3)

**Edited**
* `pipeline.py` — drop `mount` / `cvlr_source_tools` and the "no CVLR sources" warning; build the
  research tool instead. `CvlrDeps.crate_tools` → `research_tool`. The gap loop at :495 goes away
  entirely: under the pin there is nothing left to report by the time `prepare_system` runs.
* ~~`crates.py`, `scaffold.py`, `preflight.py`, `cvlr_reference.py` — the pin gate.~~ **Landed**
  (§2.6). What is left is the corpus-rebuild coupling in `cvlr_reference.py`'s docstring, which
  waits on the producer.
* `author.py` — `CvlrMountParams` is no longer about a mount; rename to `CvlrVersionParams` and keep
  `cvlr_versions` (the author still needs to know what it is writing against). `crate_tools` /
  `extra_tools` become the research tool, still routed to author **and** judge (§1.1).
* `cvlr_property_generation_system_prompt.j2:26` — replace the `cvlr_source_tools.j2` include with a
  `cvlr_research.j2` fragment. `:30`'s `cvlr_rag_tools.j2` include: decide whether the author keeps
  direct corpus tools alongside the researcher, or only the researcher (**recommend: researcher
  only**, so there is one channel and one place the version caveat is applied).
* `cvlr_property_judge_system_prompt.j2:205` — same replacement; this is where the judge gains a
  knowledge channel it did not have.
* `cvlr_rag_tools.j2` — its closing paragraph defers to the crate source. Rewrite or delete with the
  include decision above.
* `entry.py` — build the research tool alongside `rag_tools` (:420), which means a second
  `build_rag_tools("cvlr_api_kb", staged.embed_model)` call. Worth naming: `rag_db_default` on an
  `AppDescriptor` is a *single* tag, so a wheel cannot declare two corpora. The CVLR entry point
  already composes its own tool set by hand there, so this costs one line — but if a second
  descriptor-driven corpus is ever wanted, `rag_env` needs a real answer rather than this.
  `--withhold-crate` (:441) now also has to reach the researcher as a corpus filter (§2.3), and
  `cvlr_api_surface` is the tool it most obviously has to hide a crate from.
* `template_manifest.json` — the new template, minus the deleted fragment.

---

## 7. Tests, the tape, and what must be re-recorded

* `tests/test_cvlr_plumbing.py`, `tests/test_cvlr_knowledge.py` — reference the mount directly; both
  need rewriting against the research tool.
* New `tests/test_cvlr_research.py` — the graph compiles, the draft validator fires, the not-found
  contract holds against a stubbed corpus, and the version caveat renders when a gap is present.
* `tests/test_cvlr_rag.py` — extend for the new section shape, including the complete-surface
  section, and add a `cvlr_api_kb` registration test: the tag resolves in **both** `KNOWLEDGE_BASES`
  and `rag_env._FACTORIES`, which is the half-registration failure `validate_rag_db` exists to
  catch.
* A test that the two corpora do not share a connection — `CVLR_DEFAULT_CONNECTION !=
  CVLR_API_DEFAULT_CONNECTION` — so a copy-paste of the constant cannot silently merge them again.
* ~~`tests/test_cvlr_scaffold.py` — the pin gate, beside the platform-generation witness tests.~~
  **Landed** (§2.6): five tests covering a foreign line, the refusal arriving before `apply` writes
  anything, a `[workspace.dependencies]`-only pin, an unreadable git dependency, and both spellings
  of the supported release passing. The `Absent` test stands in for the real withheld-stake case and
  is worth strengthening now that `withholding` is in scope here.
* **`composer/testing/ui_harness_cvlr_vault.py` must be re-recorded.** 514 `cvlr_source` mentions; the
  tape is keyed on the tool calls the author actually made, and none of those calls will exist. This
  is an expensive run (`scripts/record_cvlr_tape.sh`, the `generate-tape` skill) plus a hand-clean
  pass, and it is the largest single cost in the migration. Budget it explicitly rather than
  discovering it.
* `tests/test_cvlr_gate.py` — the live gate is where §7.5.5's census gets re-taken. **The success
  criterion for the whole migration is that census**: the author's CVLR questions should still be
  ~50 per run in *volume of questions asked* while collapsing to far fewer sub-agent invocations
  through the `AgentIndex` cache, and the compile-failure rate on helper names must not rise.

---

## 8. Landing order

**0. The version pin, in PR #248 (§2.6). ✅ Done** — `a396088a` and `3a5bf37f` on
`eric/cvlr-preflight`, cherry-picked here as `ff1a6030` and `b2efa926`. It was separable from
everything below, correct on its own terms, and it is what makes the single-version corpus a
guarantee rather than an assumption. Step 5 now has one precondition left instead of two.

1. **Register `cvlr_api_kb`** — the four-file corpus registration of §6, with an empty schema.
   Reviewable on its own and it unblocks everyone: the kb repo can ingest against it immediately.
2. **The rustdoc producer** in `certora-cvlr-kb`. Additive; nothing here changes, because
   `rag_import` already routes by tag. This is where §4.4's complete-surface section gets proven out
   by hand before any agent depends on it.
3. **`cvlr_api_rag.py` + `cvlr_research.py` + templates + display + tests**, wired *alongside* the
   source mount. Both channels live. This is the only point at which the two can be compared on the
   same run.
4. **The `--withhold-crate` corpus filter** (§2.3) — the one version-adjacent item (0) does not
   close. Independent of (3).
5. **Remove the mount**, rewrite the two system prompts, delete `source_tools.py` and its fragment.
6. **Retire the crate-reference manifest** from `cvlr_kb` (§4.6) and restate `cvlr_rag.py`'s
   docstrings. Deliberately *after* (5): while both channels are live, a duplicated crate reference
   is harmless, and retiring it early would leave a window where neither corpus answers an API
   question well.
7. **Re-record the tape**, re-run the gate, re-take the census.

(1)–(4) are additive and reviewable on their own. (5) is the only irreversible step. Its second
precondition — the pin, without which the mount is the only thing guaranteeing the author reads the
right CVLR — is met; what remains is (3)'s comparison run, which has to show the researcher
answering the questions the §7.5.5 census says the author actually asks.
