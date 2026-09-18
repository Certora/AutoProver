# Plan — a CVLR knowledge bundle, and how it lands

Implements Part I of `certora-cvlr-kb:certorag/docs/cvlr-knowledge-plan.md` — W1, W2 and W3, which
are AutoProver work. W4 is disposition inside `certora-cvlr-kb` and is not planned here; the only
thing this side needs from it is the triage that feeds W3.

The source plan's argument is taken as settled: CVL's practice knowledge is a *bundle* with eight
consumers, CVLR's is trapped inside one agent's system prompt, and the 83 abstracted entries are
evidence for a document rather than rows in a corpus.

---

## 1. The constraint that shapes all of it

`composer/kb/kb_context.py` builds context documents behind a `CacheMarker`, and both
`cvl_context_raw()` and every `ContextSpec.loader` are zero-argument and `@cache`d. **The bundle is
a process-global, run-invariant, cacheable prefix.** That is not incidental — it is what makes four
documents affordable across eight agents.

So the split of the 754-line author prompt is not the source plan's two-way *contract vs. facts*.
It is three-way, and the third arm is the one that decides the hard cases:

| arm | goes | why |
|---|---|---|
| this agent's contract | stays | output shape, the tool list, the publish stamps, the workflow |
| run-invariant CVLR and prover facts | **bundle** | true for any CVLR agent, identical every run |
| per-run rendered material | stays | `{{ conf }}`, `{{ example }}`, `{{ module }}` — a cached prefix cannot carry it |

Anything else — giving `ContextSpec` parameters, rendering the bundle per run — buys the move at the
cost of the cache, which is the only reason the bundle is cheap.

---

## 2. W1a — generalize `kb_context.py` (shared code, master)

No CVLR dependency. By the landing plan's fourth rule this goes to master directly, and it can be
written today.

Four things in that module are CVL-specific: the `RecipeChannel` literal, `KB_TOOL_NAME`, the
`_INDEX` list of `ContextSpec`s, and two resource filenames. Introduce a frozen
`KnowledgeBundle` carrying exactly those, with `CVL_BUNDLE` as the only instance this PR adds.

Two decisions worth making at the seam rather than later:

* **`KBRecipe` becomes generic over its channel vocabulary** — `class KBRecipe[C: str]`, with
  `CvlChannel = Literal["CVL", "CONF", "EDIT"]`. A union of both vocabularies in one model would
  typecheck a CVL recipe carrying `SKIP`. Two instantiations of one generic, not one model with a
  wider literal.
* **The `@cache`s key on the bundle.** They are currently zero-arg; they become
  `@cache def _kb_model(b: KnowledgeBundle)`, which is why the dataclass is frozen.

Also parameterize: `kb_tools(bundle)` in `knowledge_base.py`, `CommonTools.kb_displays()` (one entry
per bundle's tool name), and `cvl_kb_index.j2`, which names "the CVL manual" in prose the other
bundle would be wrong to inherit.

`with_cvl_context` keeps its signature and its behavior. Seven call sites, none edited.

**Gate for this PR:** the CVL suite passes unchanged, and one new test renders a second bundle with
its own channels and resources — the executable spec for adding one.

## 3. W1b — `CVLR_BUNDLE`

`CVLR_BUNDLE`, `with_cvlr_context(prompt)`, `cvlr_kb_tools()` (tool `get_cvlr_recipe`), and the
resources W2 and W3 fill. **This imports nothing from `composer/spec/cvlr/`** — it is markdown, a
yaml index and a record — so it can land before any of the backend.

## 4. W2 — factor the 754-line prompt

Section by section, with the third arm applied. Line ranges are against the current template.

| § | lines | disposition |
|---|---|---|
| persona, Output shape | 1–17 | stays — this agent's artifact |
| What a rule is | 18–52 | **bundle** (one `{% if example %}` line at 40 stays behind) |
| `clog!` legibility | 53–66 | **bundle** |
| Tools | 67–114 | stays — the action space, and three `{% include %}`s |
| Assumptions and vacuity | 115–125 | **bundle** |
| Nondeterminism is the quantifier | 126–188 | **bundle** |
| The prover configuration | 189–215 | stays — renders `{{ conf }}` |
| Loops and the `optimistic_loop` ladder | 216–242 | **bundle** |
| Ask the editor | 243–304 | stays — the `code_editor` contract |
| When the Prover cannot follow the program's data | 305–383 | **bundle** — prover behavior, not a tool contract |
| Reaching a handler on an Anchor program | 384–497 | **bundle** |
| — the worked example on this program | 498–551 | stays — `{{ example }}` throughout |
| The names Anchor generates | 552–586 | **split**, see below |
| Arithmetic and panic-freedom | 587–619 | **bundle** |
| The nonlinear ladder | 620–745 | **bundle** |
| When a rule should fail | 746–754 | stays — `expect_rule_failure`'s contract |

Roughly 530 lines move and 220 stay.

**The Anchor-names table is the one real casualty.** Its rows are invariant (`<AccountsStruct>Bumps`
in the struct's own module, `__client_accounts_<snake>`, `instruction::<Pascal>::DISCRIMINATOR`) but
its third column is filled from `{{ example }}`, and that column is the payoff: it is the difference
between a rule and an instance of it. Recommendation: the bundle carries the rules and the trap they
exist to prevent; the author's prompt keeps a short rendered block naming this program's three. One
topic in two places is the cost, and it is smaller than either alternative.

**A test that the split loses nothing.** Every `##`/`###` heading that leaves the template must
appear in `cvlr_baseline_facts.md`. Cheap, and it catches the failure mode that matters — text
deleted from the prompt and never pasted into the document.

**What `certora-cvlr-kb` contributes here is review, not prose.** Per entry against the factored
document: *already said* (drop), *silent and N projects agree* (a ledger question for the document's
author), or *the document takes a position the field contradicts* — which `guidance.py`'s rule says
to surface rather than act on.

## 5. W3 — recipes

Channels come from the author's tool list, not from the capture taxonomy:
`RULE` (`put_harness`), `MOCK` (`summarize_for_prover`), `EDIT` (`code_editor`), `CONF`
(`adjust_prover_config` — the loop bound and the solver portfolio, nothing else), `SKIP`
(`record_skip`). `ENVFILE` and `SCAFFOLD` are not channels: env files are vendored data rendered by
`env_paths.py`, and scaffolding is a different phase. A recipe naming an action this agent cannot
take is a recipe that cannot be followed.

Triggers widen to *symptom, code situation, or verification goal* — which is what the CVL recipes
already do (R1 is a code situation, R18 a verification goal) despite `cvlr-capture-plan.md` §7.2
requiring an observable symptom. §7.2.1's separate rejection of reference prose stays; it was right
and it is about something else. **`cvlr-capture-plan.md` §7.2 needs both corrections written into
it**, or the next person to read it writes the wrong triggers.

W3 lands last because its content is the 27 `rule_shape` entries surviving triage against W2's
document, and that triage is W4, in the other repo. It can land with a hand-written starter set.

---

## 6. What this does to the landing plan

**S6 (wave 1, master-direct).** W1a. Shared code, no CVLR dependency, no new behavior — the shape
wave 1 is for. Reviewable today, and it unblocks everything below.

**K1 (wave 2, after S6).** W1b plus W2's `cvlr_baseline_facts.md`. Its only reader is the author
prompt, which does not exist on master until C4a2 — the same land-ahead-of-your-consumer shape C7a
already has, and acceptable for the same reason. Landing it in wave 2 is what lets C4a2 and C3b
*create* their prompts already factored, rather than landing 754 lines and moving 530 of them out a
wave later.

**C3b and C4a2 absorb the consumer half.** Each wires `with_cvlr_context` into the agent it lands —
the munge editor and reviewer at C3b, the author and judge at C4a2 — and C4a2's prompt file is
created in its factored form. C4a2 shrinks by roughly the 530 lines K1 carries.

**K2 (wave 4).** W3, plus the `cvlr-capture-plan.md` §7.2 corrections, after W4's triage.

Ordering, then: **S6 → K1 → (wave 3 as planned, with C3b and C4a2 consuming the bundle) → K2**.

---

## 7. What was checked, and where the source plan overstates

* **The judge does duplicate the author.** `cvlr_property_judge_system_prompt.j2` states vacuity and
  over-assumption policy independently — 11 lines mentioning assumptions, plus its own takes on
  nondeterminism, loop bounds, `clog!` and Anchor. This is the bundle's first real consumer.
* **The munge editor does not, yet.** `cvlr_munge_editor_system.j2` and `_review_system.j2` mention
  one of the moved topics once each. The source plan's test — "the munge editor and the judge stop
  restating what the author knows" — is passed by the judge and not yet by the editor. The editor's
  benefit is prospective: it stops going *without*, rather than stops repeating. Worth saying so
  before the work is justified by a duplication that is not there.
* **`build_basic_rag_tools` is not in the way.** It bundles `kb_tools()` with the CVL manual tools,
  but nothing on the CVLR path calls it; the CVLR agents assemble their own. `cvlr_kb_tools()` is
  added where they are built, not inherited.
