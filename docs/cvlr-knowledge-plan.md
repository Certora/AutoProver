# CVLR knowledge: where it belongs

> Part I, **done**, records where CVLR *practice* knowledge lives — a knowledge bundle and a recipe
> set, written by hand — and the rules for drawing on verification engagements when writing it. It
> replaces the capture pipeline that was going to generate that knowledge into the `cvlr_kb` corpus,
> and the private repository that pipeline ran in, `certora-cvlr-kb`, which is being retired. Built:
> the bundle (`2ffc1243`), the factored prompt (`f4c953fd`), the other CVLR agents wired to it, and
> the recipes (`e04f2dea`, and K3 in `d129bacf`). It lands as the landing plan's K1.
>
> This document had a Part II, a Solana property corpus in certorag. It was removed on 2026-10-14:
> nobody owns that work, and it depended on `certora-cvlr-kb`.
>
> Related: [cvlr-api-docs-plan.md](./cvlr-api-docs-plan.md) (the two CVLR corpora, which hold
> reference material rather than practice), [cvlr-backend-plan.md](./cvlr-backend-plan.md) (the consumer),
> [cvlr-todo.md](./cvlr-todo.md) (the leads the capture survey left, and U6). Paths below are
> relative to the repository root unless marked `certora-cvlr-kb:`.

---

## 0. The one-paragraph version

On the CVL/Solidity side, exactly two knowledge assets are machine-derived: the CVL manual (sphinx →
`ragbuild`) and certorag's property corpus. **Every piece of CVL *practice* knowledge is
hand-authored**, and it is delivered as an always-in-context bundle plus a trigger-indexed recipe
set, not as retrievable corpus prose. CVLR now has the same shape: `cvlr_baseline_facts.md` in a
CVLR bundle, read by every CVLR agent that can act on it, and three recipes. Corpus search holds only
reference material — the manual and the generated API — because a corpus is allowed to be absent
at run time, and anything load-bearing must be in the prompt or the bundle. What CVLR still lacks
is a property corpus, and none is planned.

---

# PART I — Where CVLR practice knowledge belongs (done)

## 1. The CVL knowledge stack

`composer/kb/kb_context.py` assembles the CVL context documents and injects them into an agent's
*initial prompt* via `with_cvl_context`, behind a `CacheMarker`:

| Layer | Artifact | Delivery |
|---|---|---|
| Baseline facts | `composer/kb/resources/cvl_baseline_facts.md` | always in context |
| Topic KBs | `cvl_summarization_rag_draft.md`, `cvl_invariants_quantifiers.md` | always in context |
| Situation recipes | `cvl_recipes_index.yaml` + bodies under `recipes/` | **index** always in context; body on demand via `get_cvl_recipe` |
| Backend guidance | `CERTORA_BACKEND_GUIDANCE` in `composer/spec/prop_inference.py` | cached system prefix of property inference |
| Reference manual | sphinx → `composer/scripts/ragbuild.py` → RAG db | searched |
| Property corpus | `certorag` | analogy probe at property inference |

Two facts about it decided Part I. The practice layers were written by an expert in one commit
(`b6320237`); there is no extraction pipeline behind them, no ranking and no ledger. And the
bundle is shared: `with_cvl_context` has seven call sites in this repo and an eighth in certorag.
The knowledge is a *bundle*, deliberately separable from any one agent's prompt.

## 2. The CVLR side, as built

| Artifact | What it covers |
|---|---|
| `composer/kb/resources/cvlr_baseline_facts.md` (398 lines) | What a rule is; assumptions and vacuity; nondeterminism as the quantifier; loop bounds and the `optimistic_loop` ladder; arithmetic and why panic-freedom is off the menu; the nonlinear ladder; `clog!` legibility; when the Prover cannot follow the program's own data. Factored out of the author's system prompt (§4.2). |
| `composer/kb/resources/cvlr_recipes_index.yaml` + `recipes/cvlr-k*.md` | Three recipes, K1–K3 (§4.3) |
| `composer/templates/cvlr_property_generation_system_prompt.j2` (517 lines) | The author's own contract: output shape, its tools, the publish stamps, the workflow, Anchor entry points and the worked example |
| `composer/spec/cvlr/guidance.py` — `SOLANA_CVLR_GUIDANCE` | `backend_guidance` for property inference, written from the manual (§4.4) |
| `composer/spec/cvlr/envs/` | The starting layers of the two tuning files, maintained in AutoProver since `596ec0de` |
| `composer/templates/cvlr_munge_editor_system.j2` / `_review_system.j2` | The munge editor's charter and its reviewer |
| `composer/tools/cvlr_rag.py`, `composer/tools/cvlr_api_rag.py`, `composer/spec/cvlr_research.py` | Reference, not practice: the manual (`cvlr_kb`), the API generated from the crates (`cvlr_api_kb`), and the research sub-agent the author and the judge reach both through ([cvlr-api-docs-plan.md](./cvlr-api-docs-plan.md)) |

## 3. Why the capture pipeline's entries did not become corpus content

The capture survey of the Solana engagements produced 83 abstracted entries: 27 `rule_shape`, 18
`prover_flag`, 17 `conf_key`, 11 `env_inlining`, 6 `munge_form`, 3 `env_summaries`. They were
headed for `cvlr_kb`. Against the CVLR side as it already stood:

- **`prover_flag`, `conf_key` (35)** — outside the author's action space. `adjust_prover_config`
  moves the loop bound and `optimistic_loop`, nothing else, and the run's actual conf is injected
  into the prompt verbatim.
- **`env_inlining`, `env_summaries` (14)** — restating the starting layers. The real problem there is
  path spelling across platform generations, which `env_paths.py` owns. One exception became lead L1
  in `cvlr-todo.md`.
- **`munge_form` (6)** — owned by the munge editor's charter.
- **`rule_shape` (27)** — the only candidates, triaged into recipes (§4.3).

It is what happens when a machine pass and an expert work the same seam in parallel and the expert
finishes first. The evidence was good; the entries were the wrong output shape.

## 4. Decision

**CVLR practice knowledge is written by hand and delivered through the bundle and the recipes, not
through corpus search.** Corpus search holds reference material only: `cvlr_kb` is the Solana
manual and nothing else (`26f7b9c9`), and `cvlr_api_kb` is the API generated from the crates. The
capture pipeline's lasting output is evidence for whoever writes the knowledge, and what survives of
it is the list of leads in `cvlr-todo.md`.

### 4.1 The bundle (`with_cvlr_context`)

`kb_context.py` was generalized into a `KnowledgeBundle[C]` record (`2ffc1243`): a label, its
`ContextSpec`s, and an optional `RecipeSet[C]` (tool name, index resource, parser, index template).
The channel vocabulary is a type parameter, so a CVLR recipe cannot name a CVL channel. Both
families share one index template, `kb_index.j2`, and `kb_tools(bundle)` builds either family's
recipe tool. The one change CVL agents see is that the channel legend now renders with the index.

Each CVLR reader gets what it can act on, which is why there are two bundles:

- **`with_cvlr_context`** (`CVLR_BUNDLE`, the facts plus the recipe index) — the author and the
  judge, the two agents holding `get_cvlr_recipe`. The recipe tool is added in
  `composer/spec/cvlr/entry.py`.
- **`with_cvlr_facts`** (`CVLR_FACTS_BUNDLE`, the facts alone) — the munge reviewer and
  `cvlr_research`, which cannot retrieve a recipe.
- **Nothing** — the munge editor. Its charter forbids reasoning about the Prover's internals, which
  are the bundle's subject.

The counterexample analysis needs no wiring of its own: it runs over the author's message list, and
the bundle is at its head. The bundle is a cached prefix, so the per-run material — the injected
conf, the worked example and the Anchor section it runs through — stays in the author's prompt.

### 4.2 Populating it: the author's prompt, factored

The bundle was not written from scratch or from the entries. The author's 754-line system prompt was
split along one line (`f4c953fd`): this agent's contract stays in the prompt, and CVLR and Prover
facts true for any CVLR agent move to the bundle. 388 lines moved. The judge now cites the bundle's
sections instead of restating them, and a test checks each cited heading against the bundle
(`tests/test_cvlr_knowledge.py`, `tests/test_cvlr_bundle.py`).

### 4.3 Recipes, with the channels taken from the action space

A channel tells a stuck agent whether the fix is in its action space, so the channels are the
author's tools (`e04f2dea`), as `CvlrChannel` in `kb_context.py`:

| Channel | Tool | Note |
|---|---|---|
| `RULE` | `put_harness` | the default |
| `MOCK` | `summarize_for_prover` | unsound by construction; invalidates the prover stamp |
| `EDIT` | `code_editor` | the agent describes the problem, not the edit |
| `CONF` | `adjust_prover_config` | **only** the loop bound and `optimistic_loop`; anything else is a `record_skip` |
| `SKIP` | `record_skip` | the honest terminal channel, with no CVL peer |

There is no channel for env files or scaffolding: the starting env files are maintained here and the
composites are generated, so a recipe whose action is "add an inlining line" names something the
author cannot do. A trigger may be a symptom, a code situation or a verification goal, as the CVL
recipes' are.

The 27 `rule_shape` entries were triaged against the factored bundle, and two survived:

- **K1** — satisfy rules for reachability and witnesses, and the acceptance property they must not
  stand in for. The prompt and the judge mentioned `cvlr_satisfy!` only as a thing not to do.
- **K2** — `clog!` before an assertion over an enum or a bitfield. Logging gives the value a use
  site, which stops the comparison chain being folded into a bitmask: a solver workaround, not a
  legibility habit.

The rest restated the bundle. One was dropped for a reason worth keeping: "end every rule with a
vacuity check" is attested but does not transfer, because `cvlr_vacuity_check!` is gated on a cargo
feature AutoProver never enables.

More recipes come from what runs show the author missing; there is no pipeline behind them. K3 is
the first that came that way:

- **K3** — sysvars, read directly. A handler that reads `Clock` or `Rent`, by syscall or from the
  sysvar account, is driven as it is, and a rule that needs the account and the syscall to agree
  says so with one assumption. The stake benchmark's third run split handlers, mocked reads and
  skipped properties because a tuning-file defect made `Sysvar::from_account_info` external
  ([stake-benchmark.md](./stake-benchmark.md)). With the defect fixed, Prover probes on SDK 2 and
  SDK 3 stake verified that both forms are analyzed, and the recipe gives the SDK 3 spelling. It
  also names the limit that remains: `Rent::minimum_balance` computes in `f64`, so a reserve
  property is stated over the stored reserve, or over an integer stand-in where the program stores
  none. Its evidence is Stake Pool's and P-Token's specs, paraphrased per §4.4, and
  `tests/test_cvlr_knowledge.py` checks its index entry and body.

### 4.4 Rules for drawing on engagements

Whoever writes the next fact or recipe will be reading verification engagements for evidence. The
capture survey paid for these rules, and they outlive it.

- **The manual is the reference; the projects are the evidence.** Where the published manual and a
  project instance disagree on *form*, the manual wins: it is maintained, version-scoped and public,
  and a project instance is one team's choice frozen at one date. Projects remain the only source
  for what actually goes wrong — which SDK boundary needed mocking, which property was false, what a
  timeout hunt ended in — and no manual reports that. `guidance.py` is written to this rule.
- **"Quotable" means quotable after it compiles.** The manual's own examples carried two defects that
  fail against every release (recorded in [upstream-defects.md](./upstream-defects.md)), so an
  example taken from it goes through the same compile check as any other.
- **Never quote engagement code.** Paraphrase or generalize it, whether or not the repository is
  public. The reason is overfitting, not confidentiality: a quoted block teaches one project's
  house layout, helper names and era. A synthetic minimal example retrieves and generalizes better
  than an excerpt from someone's protocol. Tutorials may be quoted if they are current.
- **Rank on two axes, because frequency alone points the wrong way.** Older projects outnumber
  current ones, so a raw count promotes a superseded idiom precisely *because* it is old.
  *Recurrence* — in how many projects does the underlying problem appear — decides whether a
  problem is worth knowledge. *Currency* — does a current-practice instance of the solution exist —
  decides whether there is a solution to state. A recurring problem with only legacy solutions is a
  gap to record, not advice to ship.
- **Count clients, not repositories.** Two repositories from one client share tooling, layout and
  house style, so a team's convention can appear twice and read as an industry idiom. Two repos from
  one client are one vote.
- **Judge currency per chain.** The Soroban projects run cvlr 0.3–0.4 while the Solana ones run
  0.4–0.6, so a 0.4 crate is current on one chain and a generation behind on the other. And
  vintage belongs to an instance rather than a project: a recent project can carry copied legacy
  patterns.
- **An unreviewed how-to and an unreviewed soundness claim fail differently.** A wrong how-to wastes
  an iteration, and the compiler catches it. A wrong soundness claim produces a proof of something
  false, and nothing downstream catches it. The second kind never enters the bundle unreviewed.
- **Gate the shape of generated content, not only its properties.** A capture manifest once passed
  schema validation, sanitization and the compile gate while 97% of its blocks were single
  characters: an array field arrived as a string and the renderer iterated it. Every gate asked "is
  this well-formed?" and none asked "is this anything at all?". `cvlr_api_docs.py` is the producer
  this now applies to.

Three facts about project files the survey found the hard way, because they silently destroyed
data before they were known:

- **Certora confs are JSON5-shaped**: `//` comments and trailing commas. A strict parser rejected
  152 of 486 surveyed confs.
- **Env files comment with `;` only.** `#` opens a directive (`#[inline(never)] ^core::.*$`), so
  treating it as a comment marker discards every entry while leaving the file count healthy.
- **Read versions from `Cargo.lock`, per crate, as (name, version, source).** The CVLR family does
  not version in lockstep, one lockfile can resolve two versions of one crate, manifest dependency
  names need not match resolved package names, and a stray `Cargo.lock.backup` will match a careless
  glob. A chain crate also implies a platform generation (`cvlr-solana` 0.5 is `solana-program` 2.2,
  and 0.6 is the split `solana-*` 3.x crates), which no CVLR version string shows.
