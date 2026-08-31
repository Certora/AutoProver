# Design Doc — A common JSON format for RAG entries + a single importer

> Today every documentation corpus that feeds a search tool ships its **own** RAG builder:
> a bespoke Python module that parses that corpus's native format *and* talks to the RAG
> database, plus a shell wrapper. Adding a new corpus (notably: a new Rust application that
> wants its own `*_kb`) means writing another builder wired to the DB.
>
> This proposes splitting that seam: a **producer** emits a corpus as a common JSON document,
> and one shared **importer** ingests any such document into the RAG DB. The DB coupling,
> chunking, embedding, batching and dual-path ingestion move into the importer — once — and a
> producer shrinks to "parse my docs → emit JSON", with no dependency on the RAG stack.
>
> **Scope:** this layer ships the *mechanism* — the format, the importer, and the tag→connection
> registry — with **no corpus on it**; the first adopter is the Crucible application, which lands
> with the Solana backend. The Foundry and CVL builders stay exactly as they are; they are
> candidate future adopters (§5), not part of this change. The format is nonetheless designed to be
> general, so migrating them later needs no schema change.
>
> Companion to [rust-applications.md](./rust-applications.md) (the descriptor-driven app model and
> the wheel FFI surface). The `knowledge_base`
> tag defined here is the *same* tag a wheel already declares as
> [`rag_db_default`](../composer/rustapp/descriptor.py).

---

## 1. What's actually shared today — and what isn't

Three builders existed, one per corpus:

| Builder | Source format | Ingests |
| --- | --- | --- |
| [`ragbuild.py`](../composer/scripts/ragbuild.py) | CVL-manual HTML (docutils) | vector + manual |
| [`foundry_ragbuild.py`](../composer/scripts/foundry_ragbuild.py) | Foundry cheatcode HTML fragments | vector only |
| `crucible_ragbuild.py` (never landed — replaced by this mechanism) | Crucible markdown | vector + manual |

Each had a shell wrapper ([`populate_rag.sh`](../scripts/populate_rag.sh),
[`populate_foundry_rag.sh`](../scripts/populate_foundry_rag.sh), `populate_crucible_rag.sh`), and
each pins a default connection constant (`DEFAULT_CONNECTION`, `FOUNDRY_DEFAULT_CONNECTION`,
`SANITY_DEFAULT_CONNECTION`) in [`composer/rag/db.py`](../composer/rag/db.py). (This section
describes the state that *motivated* the change: the Crucible builder + wrapper were written that
way first, and this mechanism is what replaced them — the CVL and Foundry ones remain.)

The important observation: **only the first column differs.** Everything downstream is already
common code the three builders call into:

- the chunk model [`BlockChunk`](../composer/rag/types.py) — a header path (`h1..h6`), a `part`
  index, `code_refs`, and a `chunk` body with `<code-ref-N>` placeholders;
- the length-bounded splitter `BlockBuilder` / `BuilderConfig`
  ([`text_processors.py`](../composer/scripts/text_processors.py)), driven by spaCy;
- the **dual-path ingestion** on [`ComposerRAGDB`](../composer/rag/db.py):
  `add_chunks_batch` (embedded chunks → vector search) **and** `add_manual_section` (the full
  section → keyword search / `get_section`);
- embedding, batching, and `part`-numbering of repeated header paths.

So each builder re-implements *source parsing* and then hand-rolls the same orchestration around
the same shared primitives. The bespoke part is small; the boilerplate around it is duplicated
per corpus and, critically, **carries a hard dependency on the RAG DB and the heavy `ragbuild`
uv group** (spaCy + sentence-transformers). A new Rust app that just wants to contribute a corpus
inherits all of that — which is what makes Crucible the natural first case to lift onto a generic
mechanism (the other two builders already exist and work, so they can migrate later or never).

### The right cut: two declared products, chunked by the importer

The seam should fall **between parsing and chunking**, not after chunking. But the two indexes
store **different products**, not one product rendered twice:

- the **manual index** (`manual_sections`) stores *documents* — complete reference units,
  addressable by header path, returned whole by `get_section`, at whatever size they are;
- the **vector index** (`documents`) stores *passages* — length-bounded chunks whose cut points
  matter, and whose quality depends on knowing what each piece of content *is*: a paragraph that
  may be split at sentence boundaries, a table that must stay intact, prose that merely continues
  around a code sample.

The CVL builder keeps two pipelines for exactly this reason: its manual documents are
subtree-inclusive renderings (a section's document contains its child sections inline, with
editorial rules like Example subsections never getting their own document), while its vector
chunks are disjoint bounded passages driven with per-element chunking flags (§5). A single
flattened "section" list feeding both indexes can express neither side faithfully — it erases the
chunking hints the vector side needs, and it forces the manual unit to coincide with the vector
grouping unit.

So the manifest declares the two products **explicitly and independently** (§2): manual sections
as whole documents, embedded groups as kind-annotated block runs. Laying out both is where the
genuinely corpus-specific editorial judgment lives — often two views of the same source, but
never derived one from the other. The importer owns everything mechanical from there down:
running `BlockBuilder` to cut embedded groups into length-bounded chunks, assembling manual
documents, assigning `<code-ref-N>` tags, numbering `part`s, embedding, and batching.

Why this cut and not "emit finished `BlockChunk`s":

- **Chunking is common, tuned, and heavy.** Length-bounding needs spaCy and a shared
  `max_length`. Keeping it in the importer means producers need neither spaCy nor
  sentence-transformers — a Rust app can emit the JSON from a trivial script (or from the wheel
  itself; see §6) with no RAG dependencies.
- **Code-ref tagging is a footgun.** `crucible_ragbuild` manually tracked a `code_refs` list and
  emits `<code-ref-N>` tags in lockstep; getting that wrong orphans a ref. Producers should
  never see the tag scheme — they just say "this block is code."
- **`part` numbering is global.** The manual-section table is unique on
  `(h1..h6, part)`; repeated header paths must bump `part`. That's a whole-corpus concern the
  importer is positioned to own; a producer emitting isolated chunks can't.

---

## 2. The JSON format

A corpus is one **manifest** document: metadata plus the two retrieval products. Schema (v1),
mirrored by a pydantic model the way
[`descriptor.py`](../composer/rustapp/descriptor.py) mirrors the Rust `AppDescriptor`:

```jsonc
{
  "version": 1,
  "knowledge_base": "crucible_kb",         // logical KB tag (== descriptor rag_db_default)
  "source": "crucible@a1b2c3d docs/*.md",  // free-text provenance, for logs only
  "manual_sections": [
    {
      "headers": ["Writing Fuzz Harnesses", "PDA Seed Encoding"],
      "blocks": [
        { "kind": "text", "body": "Seeds are encoded as ..." },
        { "kind": "code", "body": "let (pda, bump) = Pubkey::find_program_address(...);" },
        { "kind": "text", "body": "The bump is then ..." }
      ]
    }
  ],
  "embedded_groups": [
    {
      "headers": ["Writing Fuzz Harnesses", "PDA Seed Encoding"],
      "blocks": [
        { "kind": "paragraph", "body": "Seeds are encoded as ..." },
        { "kind": "code", "body": "let (pda, bump) = Pubkey::find_program_address(...);" },
        { "kind": "continuation", "body": "— the bump is then verified by the runtime." },
        { "kind": "atomic", "body": "| seed | meaning |\n| --- | --- |\n| ... | ... |" }
      ]
    }
  ]
}
```

Field notes:

- **`version`** — schema version; the importer refuses any value it doesn't recognize (an exact
  match against `SCHEMA_VERSION`), before any DB write. Lets the format evolve without silently
  mis-ingesting old files.
- **`knowledge_base`** — the logical corpus tag. This is the *same* string a wheel declares as
  `rag_db_default` and that [`rag_env.py`](../composer/tools/rag_env.py) resolves to search
  tools. Making producer, importer, and runtime agree on one tag is a real simplification: it
  becomes the single key naming a corpus end to end. The importer resolves it to a connection
  string via a registry (§4), overridable by `--output`.
- **`source`** — provenance for logging/traceability only. The RAG schema is header-only
  ([`documents`](../composer/rag/db.py) / `manual_sections` store `content + h1..h6`), so this
  is **not** persisted per row; it just lands in the importer's log line. (If we later want
  per-row provenance we'd extend the DB schema — out of scope for v1.)
- **`headers`** — a header path (on both products), at most 6 entries: `_normalize_head` maps
  entry *i* to column `h(i+1)`, leaving a falsy or absent level as `NULL` in its own column (so a
  gap stays a gap — nothing is left-packed). A path longer than 6 is a producer bug, not something
  the importer trims: it raises rather than silently dropping the deepest level.
- **`manual_sections[].blocks`** — ordered `{ "kind": "text" | "code", "body": "..." }`. A manual
  section is never split, so the only distinction that matters is prose vs. code: code is held
  aside as `code_refs` behind an importer-assigned `<code-ref-N>` placeholder, keeping it out of
  the keyword-searchable text while `get_section` substitutes it back.
- **`embedded_groups[].blocks`** — ordered `{ "kind": ..., "body": "..." }` where the kind
  carries the chunking semantics, mapping 1:1 onto the ways the CVL builder already drives
  `BlockBuilder`:

  | kind | what the block is | when a chunk overflows |
  | --- | --- | --- |
  | `paragraph` | a self-contained prose unit | split at sentence boundaries; cuts prefer the block edge, carrying the previous chunk's last sentence as overlap context |
  | `atomic` | structure that must survive intact (tables, lists) | never sentence-split — emitted as one oversized chunk |
  | `continuation` | prose resuming the stream an earlier block interrupted | no boundary preference; may be cut at any sentence end |
  | `code` | a code sample | never split, never embedded as prose — held as `code_refs` behind a `<code-ref-N>` placeholder |

**Each product feeds exactly its own index.** The two indexes answer different questions — "what
passage *means* this?" vs. "which documents *contain* this term, and give me one in full" — and a
corpus declares what it wants in each. Typically the two lists are parallel views of the same
source, since a corpus's tools module (`composer/tools/<corpus>_rag.py`, bound by
[`rag_env.py`](../composer/tools/rag_env.py)) usually binds all three retrieval styles — but
nothing requires it: a vector-only corpus (what Foundry's builder produces today, §5) is an
`embedded_groups`-only manifest, and manual documents may overlap in content (as CVL's
subtree-inclusive documents do) without affecting the vector side at all.

Deliberately **not** in the schema: `part` (importer-assigned), `code_refs` / `<code-ref-N>`
tags (importer-assigned), `max_length` / chunking knobs (importer flags — a cross-corpus tuning
concern, not corpus data), and any ingest-path selector — what a corpus ingests is exactly what
it declares.

---

## 3. The importer

One module, `composer/scripts/rag_import.py`, that factored the shared orchestration out of the
former `crucible_ragbuild`'s `_async_main` (which was already 90% of this) and generalized it over
the manifest — minus the markdown parser, which stays in the producer:

```
uv run --isolated --group ragbuild python -m composer.scripts.rag_import \
    corpus.rag.json [more.rag.json ...] [--output <conn>] [--max-length N] [--print]
```

Behaviour:

1. **Load + validate** each manifest against the pydantic model (clear errors on a malformed
   file, before any DB write).
2. **Resolve the target** once per manifest: `--output` if given, else the connection registered
   for `knowledge_base` (§4). Refuse to run if neither resolves.
3. Ingest each product into its own index:
   - **vector** → run a `BlockBuilder` over each embedded group, cutting as each block's kind
     dictates (§2); buffer the resulting `BlockChunk`s, flush via `add_chunks_batch` at
     `_BATCH_SIZE`;
   - **manual** → assemble each manual section into one whole-document `BlockChunk` (code as
     `<code-ref-N>` tags), assign its `part` from a per-header-path counter,
     `add_manual_section`.
4. **`--print`** — dry-run: render manual sections and name embedded groups on stdout, no DB
   writes (parity with every builder's existing `--print`).

That is the orchestration the former `crucible_ragbuild` hand-rolled, now reusable by any producer
that emits the manifest. Note it needs the `ragbuild` uv group (spaCy + sentence-transformers) — but
now *only the importer* does; producers don't.

---

## 4. Connection resolution

The importer resolves a manifest's `knowledge_base` tag to a DB connection via a small registry,
overridable by `--output`:

```python
KNOWLEDGE_BASES: dict[str, str] = {
    # "<corpus>_kb": CORPUS_DEFAULT_CONNECTION,
}
```

It is **empty here** — a corpus's entry lands with the application that declares it, together with
the `composer/tools/<corpus>_rag.py` that searches it, because [`rag_env.py`](../composer/tools/rag_env.py)
requires both halves before a tag is usable. This is the same registry idea
as `rag_env.py` (tag → search tools) and the ecosystem registry:
a declarative tag resolved to a concrete resource, not a fork. The existing
`*_DEFAULT_CONNECTION` constants in `db.py` stay put; if CVL/Foundry ever migrate onto this path,
their tags join the registry then — and ideally the two registries share the tag namespace, so a
corpus's *import* target and its *runtime* search tools resolve by one name.

---

## 5. Does the format generalize? (Foundry / CVL — future adopters, not now)

No *existing* corpus moves onto this mechanism now — the first adopter is a new one (Crucible's).
But to be sure we aren't designing a Crucible-shaped format by accident, it's worth checking the
format could absorb the *other* corpora later.

**Foundry** does real editorial grouping, not just parsing: it merges
`signature`/`description`/`parameters`/`returns` into **one** summary chunk keyed by the
cheatcode name, gives `Examples`/`Gotchas` their **own** chunks, and drops `Related Cheatcodes`.
All of that is producer layout, and its per-element decisions map 1:1 onto the block kinds:
parameter tables and lists are `atomic`, descriptions are `paragraph`s, follow-on prose is
`continuation`, samples are `code`. Its builder populates **only the vector index**, which the
format expresses directly as an `embedded_groups`-only manifest. A migration would also be the
moment to decide *deliberately* whether to start emitting `manual_sections`: Foundry's
`foundry_cheatcodes_keyword_search` / `..._get_section` tools currently query a
`manual_sections` table that nothing writes, and the dual declaration makes that gap visible in
the manifest instead of leaving it to ingestion side effects.

**CVL** is the corpus that shows why the products must be independent:
[`ragbuild.py`](../composer/scripts/ragbuild.py) runs two pipelines over the same parsed HTML.
Its vector chunks are disjoint length-bounded passages built with exactly the per-element flags
the block kinds encode (paragraphs splittable, admonitions/lists/tables/asides atomic, stray
inter-tag text as continuation), while its manual documents are **subtree-inclusive** — a
section's document contains all its descendant sections inline, with editorial rules of its own
(an `Example` subsection never gets its own document). Flat sections feeding both indexes could
represent neither the overlap nor the flags; two independent products represent both directly.

The genuinely corpus-specific pieces — Foundry's `.mdx → .html` conversion
([`foundry_process.py`](../composer/scripts/foundry_process.py)) and table-to-parameter-list
translation — stay in producers either way. So the model absorbs both non-trivial corpora cleanly;
the design isn't Crucible-only.

**This has since been tested against CVL's traversal for real.** The `cvlr_kb` docs producer
(§7 item 4) needed the same docutils walk, so the walk was lifted out of `ragbuild.py` into
[`html_manual.py`](../composer/rag/html_manual.py), which emits a tree of blocks tagged with the
*same* `EmbeddedBlockKind` values this format defines. Two findings, both encouraging for a future
CVL migration:

- The per-element mapping above is exactly right: every element the CVL traversal handles has one
  kind, and `ragbuild.py`'s calls into `BlockBuilder` reduced to a five-line match on kind with no
  residue.
- The one thing the flat format cannot express is `push_child`, which links a parent chunk to its
  first child's header path and opening sentence. The tree keeps the nesting, so `ragbuild.py` still
  does it; a manifest-based producer simply omits it. That is the whole gap between the two
  representations, and it is small — but it is real, so a CVL migration should decide about it
  deliberately rather than discovering it as a quality regression.

---

## 6. What this gives Rust applications (the motivating case)

Under the descriptor model a Rust app is a wheel + a declarative `AppDescriptor`; it already
names its corpus via `rag_db_default`. The missing piece is *contributing the corpus content*
without writing composer-resident Python glued to the RAG DB. Two levels:

- **Level 1 (what this layer enables):** the app ships a `<kb>.rag.json` next to its crate (built
  however it likes — a script in the app's own repo, checked-in output, a CI artifact) and the
  generic `rag_import.py` ingests it. No checkout of the upstream doc source at build or run time.
  Composer ships the importer and the schema, nothing corpus-specific. Crucible is the first app to
  do this; see its own docs for that corpus. The CVLR backend does the same from a
  separate private repo (`certora-cvlr-kb`) even though its backend is in-tree Python, so the
  pattern is not wheel-specific.
- **Level 2 (optional, natural follow-on):** add a wheel FFI callout — `rag_entries() -> str`
  returning the manifest JSON — so RAG content becomes part of the app package exactly like
  `descriptor()`. The importer could then ingest straight from a loaded wheel
  (`rag_import --from-wheel <app>`), and a Rust app contributes a corpus with **zero**
  Python. This is out of scope for v1 but is the reason the manifest is self-describing
  (`knowledge_base` inside the document, not a CLI arg): a wheel can emit a complete, resolvable
  corpus with no external metadata.

---

## 7. What's built

The mechanism, corpus-free:

1. Manifest model [`composer/rag/import_format.py`](../composer/rag/import_format.py) — pydantic,
   and deliberately importable with no RAG-stack dependency, so a producer needs only it.
2. The generic importer [`composer/scripts/rag_import.py`](../composer/scripts/rag_import.py) (§3),
   covered by [`tests/test_rag_import.py`](../tests/test_rag_import.py) (each product feeds exactly
   its own index, block kinds cut as declared, `part` numbering across sections *and* across
   manifests sharing a DB, code-ref tagging, version and target-resolution refusals).
3. The `KNOWLEDGE_BASES` registry (§4) in [`composer/rag/db.py`](../composer/rag/db.py). Its first
   entry is `cvlr_kb`, registered alongside [`composer/tools/cvlr_rag.py`](../composer/tools/cvlr_rag.py)
   in [`rag_env.py`](../composer/tools/rag_env.py)'s tools registry — see
   [cvlr-capture-plan.md](./cvlr-capture-plan.md) §8.7. It is also the first corpus fed by *several*
   manifests sharing one tag, which the importer already supported (`part` numbering continues
   across manifests resolving to the same DB).

What an adopting application adds, in one go: its committed `<kb>.rag.json`, both registry halves
(the `KNOWLEDGE_BASES` connection + a `composer/tools/<corpus>_rag.py` in `rag_env._FACTORIES`), the
DB role/schema in [`init-db.sql`](../composer/scripts/init-db.sql), and whatever container wiring
populates it at `setup-db` time.

4. The first real producers, all three of which live **outside this repo**, in the private
   `certora-cvlr-kb` repo, and reach back into it for the pieces they must not restate. That is the
   producer/importer split working as intended, and further than originally planned: a producer
   needs the manifest model and — for the documentation one — the shared parser
   [`html_manual.py`](../composer/rag/html_manual.py), and neither pulls in spaCy, an embedding
   model or a DB. `html_manual.py` stays here because
   [`ragbuild.py`](../composer/scripts/ragbuild.py) is its other consumer;
   [`tests/test_html_manual.py`](../tests/test_html_manual.py) still covers it, and the tests for
   each producer's own layout decisions moved with the producer.

**Untouched:** `foundry_ragbuild.py`, its wrapper, and `refresh_rag.sh`. No runtime code changes —
the search tools, `rag_env.py`, and the DB API are the same.

`ragbuild.py` (CVL) *was* touched, but only to move its HTML traversal into the shared parser: its
output was verified byte-identical before and after on all three manuals it can read (`cvl.html`
232 chunks / 198 manual sections, `prover.html` 182 / 184, unchanged). The one deliberate
behavioural change is confined to the Solana manual, which is not part of the CVL corpus: a
`<blockquote>` was falling through the traversal unhandled and is now kept as prose.

---

## 8. Alternatives considered

- **One shared section list feeding both indexes (an earlier draft of this format).** Rejected
  once measured against what the two indexes actually store (§1): it forced the manual unit to
  coincide with the vector grouping unit (CVL's subtree-inclusive documents are unrepresentable),
  flattened every prose block to one kind so the importer had to guess chunking flags the
  producer knew, and hard-wired "every section feeds both" so a vector-only corpus like Foundry's
  was inexpressible. Replaced before any corpus shipped on it, so the schema version stayed 1.
- **Emit finished `BlockChunk`s in the JSON (cut below chunking).** Rejected: pushes spaCy +
  the `max_length` policy + `<code-ref-N>` tagging + global `part` numbering into every
  producer, re-duplicating the heavy, error-prone parts and re-coupling producers to the RAG
  stack. The whole point is to keep producers dependency-free.
- **A plugin/entry-point registry of builders** (each corpus registers a `build()` callable) —
  removes the shell duplication but keeps every builder coupled to the DB and the `ragbuild`
  group, and gives Rust apps nothing (still composer-resident Python per corpus). A data format
  is a stronger boundary than a code interface here: it's inspectable, diffable, cacheable, and
  producible without the RAG stack.
- **Persist richer per-row metadata** (source URL, doc version, tags). Deferred: the current DB
  schema is header-only, so v1 keeps provenance at the manifest level (logs only). Revisit with
  a schema change if retrieval ever needs to filter on it.
- **One physical DB per corpus vs. one shared DB.** Orthogonal to this proposal — the
  `KNOWLEDGE_BASES` registry expresses whatever the deployment already does (today: shared
  `rag_db`, distinct roles; `extended_rag_db` separate). The format doesn't dictate topology.
