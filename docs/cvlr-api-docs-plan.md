# CVLR API documentation — two corpora and a research agent

How the CVLR backend's agents learn what CVLR provides. Three pieces:

* **`cvlr_api_kb`** — a corpus generated mechanically from the CVLR crates' **API documentation**
  (rustdoc), at the releases this build pins, with its examples compiled.
* **`cvlr_kb`** — the Solana Prover manual, and nothing else. Prose, hand-written, allowed to lag.
* **`cvlr_research`** — a research sub-agent that answers CVLR questions out of both, in the shape
  `cvl_research.py` has for CVL. The author and the judge call it; they hold no search tools.

**The two corpora are the point, not an implementation detail.** A retrieval hit carries no
provenance, so a stale manual paragraph and a generated signature would arrive indistinguishable
from one corpus. Kept apart, which tool answered says how far to trust the answer, and the
researcher is told that the generated one wins.

This replaced a read-only mount of the CVLR crates' source (`cvlr-backend-plan.md` §5.5), removed in
`5b2afeff` after a corpus-only run showed the corpora could carry the author (§8). Practice
knowledge — how to write a good rule — is a different channel again, the hand-written knowledge
bundle ([cvlr-knowledge-plan.md](./cvlr-knowledge-plan.md)).

---

## 1. What the source mount gave, and what replaces each part

| The mount gave | Now |
|---|---|
| **Negative evidence** — a grep can say a name does not appear | A vector search always returns its nearest rows, so "nothing relevant" is not evidence. Each crate has a synthetic *complete public surface* section (§4.4), read through `cvlr_api_surface` as a closed list, and the researcher may not infer absence any other way (§5.2) |
| **Authority** — source outranks recalled advice | The API corpus outranks the manual (§4.1). rustdoc is the code's account of itself, one step from the code, and a doc comment can be wrong where a signature cannot; the compile gate (§4.7) and the pin (§2) close most of that distance |
| **Tolerance for any CVLR line** — the mount showed whatever the build resolved | Nothing: one CVLR line per build, and other lines are refused (§2) |
| **Macro contracts** — recovered by reading expansions | Doc comments. The published crates have almost none; the unreleased upstream documentation has all of them (§4.2). Until it is released, a corpus built from crates.io lacks them |
| A local grep, at no model cost | A sub-agent call. Measured at about 10% of a run (§8) |

The code explorer never had the mount, and has nothing from this either. If it needs CVLR knowledge,
giving it `cvlr_research` is the direction, not a second mount.

---

## 2. One CVLR line per build

**The run pins the reference set, and a project that pins a different CVLR is refused at
preflight.** That is the CVL arrangement: the Prover ships one CVL, and the language version is not
a thing a project chooses. For CVLR it is a policy rather than a physical constraint, but the
contract is the same. The reference set is [`composer/spec/cvlr/reference.py`](../composer/spec/cvlr/reference.py)
— `cvlr` 0.6.2, `cvlr-solana`, `cvlr-solana-stake` and `cvlr-spl-token` 0.5.1 and 0.6.0; Soroban's
`cvlr-soroban` and `cvlr-soroban-derive` 0.4.0. It is what a fresh project is pinned to, and what the
run compiles is guaranteed equal to it, because anything else was refused. The corpus documents
exactly that set, so there is no version divergence to caveat. Landed in
[#248](https://github.com/Certora/AutoProver/pull/248).

The refusal widens one that already existed — `scaffold` refusing a CVLR pin that does not match the
project's platform generation — to any pin that is not ours, in the same place and before any LLM
spend. It rests on splitting the old `VersionGap` into two types, because only one of them is a
refusal:

```python
type Divergence = Mismatched | Absent

@dataclass(frozen=True)
class Mismatched:   # refuse: the project builds a CVLR release this build does not support
    crate: str; reference: str; resolved: str

@dataclass(frozen=True)
class Absent:       # fine: the project does not depend on it at all
    crate: str; reference: str
```

There is no escape hatch. A flag that lets a project drift would silently degrade every answer the
researcher gives, and there is no such flag for CVL either. Two consequences are open:

* **A bump means a corpus rebuild**, and the two must land together or every answer is about the
  wrong release. `reference.py`'s docstring says so.
* **The scaffold could offer to bump an off-reference project** instead of refusing it. That edits
  the client's dependency graph, so it is a decision rather than a task (U29).

### 2.3 `--withhold-crate` does not reach the researcher

A run can withhold a crate that models a named on-chain program (`cvlr-solana-stake`) when the
target *is* that program, so its model would be an answer key (`docs/stake-benchmark.md`). Only a
`ProgramModel` can be withheld, so the setting is a statement about the target rather than about
what CVLR the run may know. The researcher needs no filter for it, for three reasons in ascending
weight:

1. **The withheld crate is not in the graph.** The scaffold never pins it, so an answer naming it
   costs one failed `cargo_check`, not a wrong verdict.
2. **The corpus does not hold what withholding protects.** The leak would be the crate's *body*; the
   corpus holds the signatures and doc comments of a crate published on crates.io.
3. **A per-run filter would cost the cache.** The researcher's answers are cached across runs under
   one namespace (§5.1), which is only sound because nothing about a run reaches it.

---

## 3. Shape

```
  producers                               corpora
  scripts/gen_docs.sh
    └► solana.html ──► ragbuild ────────► cvlr_kb       (schema cvlr_rag)
  composer/scripts/cvlr_api_docs.py
    └► *.rag.json  ──► rag_import ──────► cvlr_api_kb   (schema cvlr_api_rag)

  search tools
  composer/tools/cvlr_rag.py      ◄────── cvlr_kb
    cvlr_manual_search / cvlr_keyword_search / cvlr_get_section
  composer/tools/cvlr_api_rag.py  ◄────── cvlr_api_kb
    cvlr_api_search / cvlr_api_lookup / cvlr_api_surface
        │
  composer/spec/cvlr_research.py — binds both, applies the ordering
    cvlr_research(question)
        │
    ┌───┴───┐
  author  judge
```

`scripts/populate_cvlr_rag.sh` builds and ingests both on a host. The Solana container generates the
API manifest when the image is built and ingests both at `setup-db`.

---

## 4. The two corpora

### 4.1 Why two, and not one corpus with a `"CVLR API"` header root

1. **Regeneration lifecycle.** Ingest is a plain insert under a uniqueness constraint, so the
   operational model is drop-and-rebuild. The API corpus is rebuilt on a CVLR bump and the manual on
   a documentation revision. Merged, either rebuild drags the other along.
2. **Retrieval interference.** rustdoc rows are short and dense; manual rows are long prose. In one
   vector index a prose query surfaces paragraphs that *discuss* `nondet` above the `nondet()`
   signature. Two indexes let the researcher ask each one deliberately.
3. **A retrieval hit carries no provenance.** This is the deciding one. Search returns headers,
   content and a score, and nothing says how much to trust a row. Under a header-root convention the
   only difference between a generated signature and a stale paragraph is a string the model has to
   notice. **The tool's own name is the affordance**: an answer from `cvlr_api_lookup` is
   authoritative because of which tool returned it.

Each corpus has its own role and schema in `init-db.sql` and its own connection constant in
`composer/rag/db.py`. `cvlr_api_kb` also has a `KNOWLEDGE_BASES` entry, which is how `rag_import`
resolves the tag its manifest carries.

### 4.2 The producer

[`composer/scripts/cvlr_api_docs.py`](../composer/scripts/cvlr_api_docs.py). It builds a probe crate
depending on the whole pinned family (`ChainReference.cargo_dependencies()`) and runs
`cargo +nightly rustdoc --output-format json` over it, offline. The compiler emits the item list,
the signatures, the doc comments and the re-export targets, so the producer is cargo plus a JSON
walk: no model, no API key, and testable by the ordinary suite over trimmed real rustdoc output
(`tests/test_cvlr_api_docs.py`, `tests/data/cvlr_rustdoc/`). It replaced a producer in the private
`certora-cvlr-kb` repository that inferred the surface with a regex scanner and a model.

What it emits is a **contract**: signature, defining crate, feature gate, deprecation, the facade
paths an item is reachable by, and its documentation. Not its implementation. A `macro` item carries
its matchers with the body elided, which is the call signature and nothing more; an expansion would
invite a dependency on binding names nobody promised. The walk has to get three things right:

* **Methods are not path-addressable.** `NativeIntU64::u64_max` lives inside an `impl`, not in
  rustdoc's `paths`, and was the single most-searched name in the recorded run. Inherent impls are
  walked.
* **The facade re-exports almost everything, through private modules and globs.** rustdoc gives the
  *defining* path, and CVLR's house style is a private `mod` plus a public re-export, so an entry
  must name the path a scaffolded project can actually write. Getting the glob cases wrong once made
  a third of the corpus unreachable (§8).
* **Trait-impl methods are mechanical.** Twenty `Add`/`Sub`/`Mul` impls would bury the questions
  people ask. Their methods are not emitted; the type's entry names the traits it implements.

**rustdoc JSON is nightly-only and explicitly unstable.** The parser accepts one `format_version`
and fails loudly on any other, so a format change is a broken rebuild rather than a thin corpus.

**`--crate-source PATH`** documents a local CVLR checkout instead of the published crates: each CVLR
package the checkout defines replaces the release through `[patch.crates-io]`, so the pin still
decides which crates the corpus holds and the checkout decides what is in them. The rows are
byte-identical to what the same crates emit once released; where the build came from is recorded
only in the manifest's `source` line and the log, which no agent reads. That is how documentation is
tried before it is published.

**Every exported item is documented, read from the releases that document it.** The releases
published before October 2026 document 66 of 284 items; `cvlr-asserts`, for one, had 31 items and
no prose. Both repositories have since documented every item and check for it in CI. The core's
documented release, `cvlr` 0.6.2, is published, and so is `cvlr-solana` 0.5.1; 0.6.0 is read from
its release commit until it is. Built from those pins, the corpus has 0 of 354 items without prose
(2026-10-27). The producer's `--undocumented` report is how that is rechecked after a bump.

### 4.3 Header paths

```
["CVLR API", "cvlr-asserts", "0.6.1", "cvlr_assert!"]
["CVLR API", "cvlr-solana", "0.5.0", "cvlr_deserialize_nondet_accounts"]
["CVLR API", "cvlr-asserts", "0.6.1", "Complete public surface"]
```

Level 2 is the *defining* crate, not the facade. The version at level 3 is provenance, not a
retrieval axis — the pin means one version is in the corpus at a time — and it makes a stale corpus
visible in every answer. A leaf is qualified by its kind, and by its defining path, only where the
bare name is not unique: a crate exporting a macro and the function it expands into is ordinary
CVLR, and two identical leaves would violate the importer's uniqueness constraint.

### 4.4 What each item contributes, and the one synthetic section

Per item, both products ([rag-import-format.md](./rag-import-format.md) §2): a `manual_sections`
entry — signature, documentation and examples — which is what an exact lookup returns, and
`embedded_groups` for the prose, which is what a natural-language question lands on. The second is
thin wherever an item is undocumented, and conceptual questions lean on the manual until upstream
documentation lands.

**One synthetic section per crate lists its complete public surface**, marked `atomic` so chunking
never splits it. It is the only row that supports a closed-world read: the researcher fetches it
and can say "this crate exports 41 items and `nondet_option` is not among them" rather than "I found
nothing".

### 4.5 Tools — `composer/tools/cvlr_api_rag.py`

| tool | for |
|---|---|
| `cvlr_api_search(query)` | "what helper does X?" — natural language over the item documentation |
| `cvlr_api_lookup(name)` | "what is the exact signature of X, and how do I import it?" — keyword search and fetch in one call, because the author usually has an identifier |
| `cvlr_api_surface(crate)` | "does X exist?" — the closed-world read of §4.4 |

Every docstring says that this corpus is generated from the CVLR crates and is authoritative, and
that where it disagrees with the manual or with recall, it is right.

### 4.6 `cvlr_kb` is the manual

`cvlr_kb` is the Solana Prover manual, built by `gen_docs.sh` and ingested by
`ragbuild --corpus cvlr`, and nothing else (`26f7b9c9`). It once also held a generated crate
reference, which `cvlr_api_kb` replaces, and project-derived practice entries, which became the
hand-written bundle. A check of the live database before the re-scope found it already held only the
manual. Its tool docstrings say what it is for — methodology, idiom, pitfalls — and send the
question of what exists to `cvlr_api_lookup`.

### 4.7 The compile gate

An author copies the examples it is shown, so every code block the corpus presents as compiled has
compiled. `gate_examples` classifies each block the way rustdoc parses its fence
([`composer/cargo/doctest.py`](../composer/cargo/doctest.py)) and type-checks every block rustdoc
would build — `cargo check`, host target, offline, one module per block in one invocation —
against the whole pinned family, as a doctest would see it. What the corpus then shows:

* **Compiled and passed** — under a label saying it was compiled against the pinned crates.
* **Failed** — removed, with a line saying so, and listed in the log and in the `--undocumented`
  report. Upstream examples may rely on doctest context the gate does not reproduce, so a failure
  drops the block rather than the build; the build fails only when the check itself cannot run.
* **Not built by rustdoc** (`ignore`, `text`, `compile_fail`) — kept, under a label saying it was not
  compiled and should be read as a sketch.

Against the pinned releases, 16 of the 65 blocks are compilable, and all 16 pass. A doctest sees
every family crate as a direct dependency, so an example may import `cvlr_macros::…` where a
scaffolded project must write `cvlr::…`; the researcher's prompt tells it to give the entry's facade
path instead.

The recipes' code blocks go through the same machinery (`tests/test_cvlr_recipe_examples.py`), and a
recipe fence must be either compilable or explicitly `ignore`. Both current recipes are fragments
of a rule that need the program around them, so both are marked `ignore` and the gate compiles
nothing there yet.

Completeness is not a separate gate. With rustdoc there is one extraction, so the thing to assert is
that each surface section lists exactly the items the walk emitted.

---

## 5. The research agent

### 5.1 `composer/spec/cvlr_research.py`

Deliberately close to `composer/spec/cvl_research.py`: the same graph shape, the rough-draft
validator (a draft before delivering keeps the answer grounded), `run_to_completion` on a unique
thread. It gets the CVLR facts bundle (`with_cvlr_facts`) — the facts without the recipe index, since
it cannot retrieve a recipe. What differs:

* **It is indexed from the start.** Its answers are cached across runs under
  `("cvlr_research", "cached")`. The author asks the same API questions across units, rounds and the
  judge, and the pin is what makes an answer a fact about a release rather than about a run.
* **It holds both corpora's tools and nothing else** — no filesystem, no project source. It answers
  about CVLR, not about the program, and holding both is what lets one agent apply the ordering.
* **It takes nothing run-specific** beyond the question (§2.3).

### 5.2 `composer/templates/cvlr_research_system_prompt.j2`

Modelled on the CVL research prompt, with four CVLR-specific rules:

1. **The ordering.** `cvlr_api_*` is generated from the pinned crates with its examples compiled;
   `cvlr_*` is the manual, useful for *how* and *why* and allowed to be out of date about *what
   exists*. When they disagree, the API corpus is right — and the answer says so rather than
   reconciling them silently, because a contradiction is a defect report for the manual.
2. **Existence is settled in one place.** A name not retrieved is a name not had. "Does X exist" is
   answered from `cvlr_api_surface`, read as closed — never from an empty search, never from the
   manual's silence.
3. **Scope.** The corpus is the pinned reference set, whole. There is no version caveat to give and
   no per-run exclusion to honour.
4. **Answer in the shape the caller needs**: the signature, and the import path that compiles in a
   scaffolded project.

### 5.3 The tool contract

`cvlr_research(question)`. The not-found contract is in the tool's own description as well as the
sub-agent's prompt, so the caller knows that "I could not establish that" is an answer and not a
failure to retry around. Its display entry is `CommonTools.cvlr_research` in
`composer/ui/tool_display.py`.

---

## 6. Where it runs

`scripts/populate_cvlr_rag.sh` on a host: it builds the API manifest (`--crate-source` to document a
checkout), ingests it, and ingests the manual if `gen_docs.sh` has built it. In the container,
`scripts/Dockerfile.solana` generates the manifest at image build time, where it has a toolchain and
the network, and keeps only the manifest; `setup-db` ingests it beside the manual. The EVM image
carries no Rust toolchain, so `setup-db` run from it fills the manual alone.

## 7. Tests

`tests/test_cvlr_api_docs.py` drives the producer over five trimmed payloads from a real rustdoc run
— the asserts crate, the mathint crate, the `cvlr` facade, the derive crate and the Solana chain
crate, between them every re-export shape the walk handles. They are trimmed rather than written by
hand, because a hand-written payload encodes the author's belief about rustdoc's schema, and that
belief is what is most likely to be wrong. `tests/test_cvlr_research.py` covers the tools against a
corpus the producer built, and the researcher's prompt and contract. `tests/test_cvlr_rag.py` covers
the manual's tools.

---

## 8. Evidence

**Step 5: the corpus replaced the mount on evidence, not argument.** Two corpus-only runs on the
vault scenario, against the recorded mount-only run as the baseline.

*Run #1, 2026-09-25, stopped at 34 minutes on purpose.* It found the corpus defective: two
`error[E0603]: module 'layout' is private`, which an audit widened to 138 of 200 entries offering no
path a scaffolded project could write. The cause was re-export resolution skipping globs, at two
levels. Fixed, with `cvlr-solana` added as a test fixture because the old fixtures carried none of
those shapes. The qualitative evidence from this run stands: the macro contract reached the author
through a doc comment; the ordering caught the manual claiming an 8-element account array against
the reference's 16 and called it stale (now [upstream-defects.md](./upstream-defects.md) M2); the
not-found contract held on PDA derivation; and the researcher cost about 10% of the run.

*Run #2, 2026-09-28, completed: 2h47m* (`run_id 70e88da52c3841deafd5ee9bde211952`):

| | baseline (mount) | run #1 (broken corpus) | run #2 |
|---|---|---|---|
| CVLR lookups | 178 source calls | 15 sub-agents @ 34m | **51 sub-agents** |
| compile errors | — | 2 × `E0603` @ 25m | **0** |
| rules authored | 19 published | — | 41 in flight, 9 published |

Zero compile errors of any kind across 2,668 lines of authored CVLR, with imports taken from the
corpus — including `use cvlr_solana::cvlr_deserialize_nondet_accounts;`, exactly the path the glob
fix produces. Two of the three units died to an `httpx.ReadTimeout` on a streaming response, not
to the corpus; the client has since been made to retry it.

**Step 7: the tape and the live gate still run without the corpus.** The replay tape was recorded
under `--rag-corpus none` and now replays with the corpus bound, so it needs re-recording, and the
researcher needs its own lane first ([cvlr-todo.md](./cvlr-todo.md) U27). The live gate still passes
`rag_tools=()`, which measures an author with no CVLR reference at all — a configuration nobody ships
(landing plan P8). And the cache's benefit is unmeasured: an indexed tool injects prior findings
into an invocation rather than skipping it, so the saving is in turns and tokens per question, and
it needs a run against a warm index to see.
