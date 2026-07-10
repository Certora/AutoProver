# PR split plan: `eric/crucible` → 4 stacked PRs

The `eric/crucible` branch is large (62 commits, ~94 files, +13.5k lines over
`origin/master`). To make it reviewable it is split into **4 stacked PRs**, merged
in order, each independently compilable and gated.

- **History model:** squash-per-PR — each stacked branch is one clean, self-contained
  commit built from the final file state for that layer (granular phase history is
  not preserved).
- **Execution posture:** build the 4 stacked local branches, verify each compiles +
  passes its gate, then hand off for review before any push.

## Why this order

The import graph is cleanly layered (verified):

- `composer.sandbox` is standalone (imports neither `rustapp` nor `crucible`).
- `composer.rustapp` imports `composer.sandbox`.
- `composer.crucible` imports `rustapp`, `sandbox`, and the ecosystem.
- `composer.pipeline.ecosystem` / `spec.solana` import none of the above.

So the dependency order is: ecosystem → rust framework → sandbox mechanism → crucible.
This also mirrors the phases the work was actually built in, and front-loads the
behavior-preserving refactor (PR 1) while isolating the security-sensitive
`run-confined` mechanism (PR 3) for focused review.

## The stack

### PR 1 — Ecosystem abstraction (EVM + Solana front-half)

Runtime `Ecosystem`/`Language` seam; the driver generalized over `Unit`/`Main`; EVM
reproduces today's behavior exactly; Solana added as a second ecosystem, proven by
analysis + property extraction against a null (no-verifier) backend.

- **Code:** `composer/pipeline/ecosystem.py`, `composer/pipeline/core.py` +
  `ptypes.py`, `composer/spec/system_model.py` (`FeatureUnit`),
  `composer/spec/prop_inference.py`, `composer/spec/system_analysis.py`,
  `composer/spec/solana/model.py`, `composer/spec/solana/null_backend.py`,
  `composer/templates/solana/*`
- **Docs:** `ARCHITECTURE.md`, ecosystem proposal
- **Gate:** existing EVM autoprove tape (Counter) still green **+** `test_solana_gate`
  (front-half)
- **Depends on:** `origin/master`

### PR 2 — Rust application framework (PyO3)

The generic wheel host + the command-runner / sandbox **seam** (unconfined `none`
provider only — no confinement yet).

- **Code:** `composer/rustapp/*`, `rust/` workspace (`Cargo.toml`, `autoprover-sdk`,
  `example-app`/echoprover), the `RunCommand` effect,
  `composer/sandbox/{policy,command,config}.py` (seam + `none` provider),
  `pyproject.toml` / `uv.lock`, `composer/templates/rust/*`
- **Docs:** `rust-applications.md`, `rust-formalization-backends.md`
- **Gate:** `test_rustapp` (echoprover decider round-trip; sandbox is a passthrough here)
- **Depends on:** PR 1

### PR 3 — Command sandbox (Landlock + seccomp confinement)

The security-sensitive confinement mechanism, isolated for focused review.

- **Code:** `composer/sandbox/launcher.py` + `recipes.py` (offline resolution, private
  CARGO_HOME / TMPDIR), `rust/run-confined`, `scripts/Dockerfile` + `.dockerignore`
- **Docs:** `docs/command-sandbox.md`
- **Gate:** `test_sandbox_escape` (all vectors denied + unconfined control) +
  `test_crucible_sandbox_gate` (legit build under launcher)
- **Depends on:** PR 2

### PR 4 — Crucible backend (capstone)

The Solana verification application, wiring PRs 1–3 together.

- **Code:** `composer/crucible/*`, `rust/crucible-app`, `test_scenarios/solana_vault`,
  crucible RAG (`composer/scripts/crucible_ragbuild.py`, `composer/tools/crucible_rag.py`,
  `scripts/populate_crucible_rag.sh`, `composer/rag/db.py`), `ReportBackend` "crucible"
  + render labels + `as_report_backend`, sandbox default → launcher for crucible
- **Docs:** crucible proposal / application / toolchain-versioning
- **Gate:** `test_crucible_gate`, `test_crucible_setup_gate`,
  `test_crucible_formalize_gate`, **`test_crucible_e2e_gate`**
- **Depends on:** PR 1, 2, 3

## Cross-cutting files (the one real hazard)

A few files are touched by more than one layer; their *final* form assumes later work
exists, so they can't be assigned wholesale to one PR. Ship an **intermediate form in
the earlier PR, final form in the owning PR**:

| File | Earlier PR gets… | Owning PR finalizes… |
|---|---|---|
| `composer/rustapp/adapter.py` | PR 2: thin generic adapter using `cast(ReportBackend, tag)` | PR 4: swap to `as_report_backend` |
| `composer/spec/source/report/schema.py` + `render.py` | — (stays master's `prover`/`foundry`) | PR 4: close to `{…, crucible}` + crucible labels |
| `composer/pipeline/ecosystem.py` | PR 1: whole file incl. `RUST_FORBIDDEN_READ` (a regex string, no import dep) | — |

Everything else is disjoint by directory and maps cleanly.

## Invariant held during execution

Each branch must **compile + pass its own gate**, not just the final one. The verified
import layering is what makes that achievable.

## Execution recipe (per PR, when greenlit)

For each PR, branching off the previous (PR 1 off `origin/master`):

1. `git checkout <prev-branch> -b <pr-branch>`
2. `git checkout eric/crucible -- <that layer's files>`
3. Adjust cross-cutting files to their intermediate form (see table).
4. Run that PR's gate; fix until green.
5. Squash-commit with a descriptive message.

Result: 4 stacked local branches to review before any push.
