> Where this recipe and the CVL manual or other Certora materials diverge, this recipe governs.

### R16. Spec reuse: `import` and overriding `[CVL]`

**Trigger:** the same rules/invariants serve several contracts or several
specs.

**Formula:** `import "Base.spec";` makes the base's rules, invariants,
definitions, and methods entries available. The importing spec may override
imported `definition`s and supply/override `preserved` blocks to strengthen
imported invariants. `using` aliases declared in an imported spec are visible
to the importer, including in methods-block entries — legal, but considered
poor style there; prefer the contract type name in entries.

**What cannot be overridden: summaries.** An imported methods-block summary is
final — the importing spec cannot replace or remove it. Summaries whose
behavior may need to vary therefore live outside the shared base spec, or the
variation is expressed inside one summary via a ghost-flag branch.
