# PR split plan: `eric/crucible` → 3 stacked PRs

> **Status (updated after the command-sandbox merge).** The command sandbox — what
> was the 4th PR of this stack — shipped **independently to `master` as #73**
> ("Command sandbox: confine untrusted native command execution (Landlock +
> seccomp)"). `eric/crucible` has since merged `master`, so the whole sandbox
> (`composer/sandbox/*`, `rust/run-confined`, `docs/command-sandbox.md`, the Docker
> overlay, `test_sandbox_*`) is now an upstream **dependency**, not part of this
> split. The remaining stack is **3 PRs**: ecosystem → rust framework → crucible.
> See [Already landed](#already-landed-command-sandbox-73) below.

The `eric/crucible` branch is large (~124 files, **+18k lines over `origin/master`**,
sandbox excluded — it's upstream now). To make it reviewable it is split into
**3 stacked PRs**, merged in order, each independently compilable and gated.

- **History model:** squash-per-PR — each stacked branch is one clean, self-contained
  commit built from the final file state for that layer (granular phase history is
  not preserved).
- **Execution posture:** build the 3 stacked local branches, verify each compiles +
  passes its gate, then hand off for review before any push.

## Why this order

The import graph is cleanly layered (verified):

- `composer.sandbox` is standalone (imports neither `rustapp` nor `crucible`) — **and
  is already on `master`**, so it anchors the stack from below rather than being a PR.
- `composer.rustapp` imports `composer.sandbox`.
- `composer.crucible` imports `rustapp`, `sandbox`, and the ecosystem.
- `composer.pipeline.ecosystem` / `spec.solana` import none of the above.

So the dependency order is: (sandbox, upstream) → ecosystem → rust framework → crucible.
This mirrors the phases the work was actually built in and front-loads the
behavior-preserving refactor (PR 1). The security-sensitive `run-confined` mechanism
already had its focused review as #73.

## The stack

### PR 1 — Ecosystem abstraction (EVM + Solana front-half)

**Branch:** `eric/ecosystem`

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
  - ⚠️ **Caveat:** the Counter tape (`test_autoprove_integration`) currently **fails on
    `master` itself** — `HarnessFakeLLM: tape lane 'invariant-cvl' exhausted … Completion
    REJECTED: prover validation not satisfied or stale` (CVL_GEN). Verified identical on a
    clean `origin/master` worktree, so it's a **pre-existing stale tape**, not caused by
    this branch. The tape must be re-recorded upstream before it can serve as PR 1's gate.
- **Depends on:** `origin/master`

### PR 2 — Rust application framework (PyO3)

**Branch:** `eric/rust`

The generic wheel host, built on the command-sandbox seam **already upstream**
(consumed via the `none` passthrough provider here — no crucible confinement yet).

- **Code:** `composer/rustapp/*`, `rust/` workspace additions (`Cargo.toml` app-crate
  members + `[workspace.dependencies]`, `autoprover-sdk`, `example-app`/echoprover),
  `pyproject.toml` / `uv.lock` (the `apps` group + wheel path deps),
  `composer/templates/rust/*`
- **Consumes (upstream, not in this PR):** `composer/sandbox/{policy,command,config}.py`
  — the seam + `none` provider from #73. `rustapp` reads `SandboxConfig.backend_spec`,
  whose master shape is `{argv_prefix, timeout_s}` (async; `config.BackendSpec`), and
  the SDK just prepends `argv_prefix` (see `docs/rust-backend-api.md`).
- **Docs:** `rust-applications.md`, `rust-formalization-backends.md`
- **Gate:** `test_rustapp` (echoprover decider round-trip; sandbox is a passthrough here)
- **Depends on:** PR 1 (+ the sandbox seam on `master`)

### PR 3 — Crucible backend (capstone)

**Branch:** `eric/crucible-app`

The Solana verification application, wiring PRs 1–2 and the upstream sandbox together.

- **Code:** `composer/crucible/*`, `rust/crucible-app`, `test_scenarios/solana_vault`,
  crucible RAG (committed manifest `rust/crucible-app/crucible_kb.rag.json` + shared
  `composer/scripts/rag_import.py` + `composer/rag/import_format.py`, `composer/tools/crucible_rag.py`,
  `composer/rag/db.py`), `ReportBackend` "crucible" + render labels + `as_report_backend`,
  **sandbox default → `launcher` for crucible** (fail-closed; the launcher itself is
  upstream). Also carries the two crucible-specific tweaks to the upstream sandbox:
  `composer/sandbox/recipes.py` (`sandbox_rustup_home` + per-run `RUSTUP_HOME` for the
  confined Solana build) and `scripts/docker-compose.sandbox.yml` (un-gated
  `run-confined-build` for the in-container vertical).
- **Docs:** crucible proposal / application / toolchain-versioning
- **Gate:** `test_crucible_gate`, `test_crucible_setup_gate`,
  `test_crucible_formalize_gate`, **`test_crucible_e2e_gate`**
- **Depends on:** PR 1, 2 (+ the sandbox mechanism on `master`)

## Already landed: Command sandbox (#73)

The Landlock + seccomp confinement mechanism — originally the 4th PR of this stack —
was reviewed and merged to `master` on its own as **#73**, and `eric/crucible` has
merged `master`. It is therefore **done**, and everything it shipped is upstream:

- **Code:** `composer/sandbox/{policy,command,config,launcher,recipes}.py`,
  `rust/run-confined`, `scripts/Dockerfile` + Docker sandbox overlay
- **Docs:** `docs/command-sandbox.md`, `docs/rust-backend-api.md`
- **Gate (already green on master):** `test_sandbox_escape` (all vectors denied +
  unconfined control) and the rest of `test_sandbox_*`

The only sandbox lines still in the `eric/crucible` delta are the two crucible-specific
tweaks noted under PR 3 (rustup home + compose un-gate); everything else matches master.

## Cross-cutting files (the one real hazard)

A few files are touched by more than one layer; their *final* form assumes later work
exists, so they can't be assigned wholesale to one PR. Ship an **intermediate form in
the earlier PR, final form in the owning PR**:

| File | Earlier PR gets… | Owning PR finalizes… |
|---|---|---|
| `composer/spec/solana/null_backend.py` | PR 1: `cast(ReportBackend, "crucible")` (report literal still master's `prover`/`foundry`) | PR 3: direct `"crucible"` tag (via the checkout; literal already open from PR 2) |
| `composer/pipeline/ecosystem.py` | PR 1: whole file incl. `RUST_FORBIDDEN_READ` (a regex string, no import dep) | — |
| `composer/spec/source/report/{schema,render,collect}.py` | PR 2: **crucible-aware from here** — `ReportBackend` incl. `"crucible"`, the `outcome_label` vocabulary, and `Verdict.message` (all needed by `rustapp`) | — |
| `composer/rustapp/adapter.py` | PR 2: casts the backend tag (`cast(ReportBackend, tag)`) | PR 3: swap to `as_report_backend` |
| `pyproject.toml` + `uv.lock` | PR 2: `apps` group + `[tool.uv.sources]` omit `crucible_app` (its crate lands in PR 3), so `uv sync`/`uv run` resolve | PR 3: re-add `crucible_app` |
| `composer/sandbox/recipes.py` | — (stays master's) | PR 3: add `sandbox_rustup_home` + per-run `RUSTUP_HOME` |

**Discovered during execution** (the split is more entangled than first sketched): `rustapp`
references the `"crucible"` report backend, its `outcome_label` wording, and `Verdict.message`,
so the report-layer changes move up to **PR 2** (not PR 3); and `null_backend` (PR 1) already
names `"crucible"`, so it takes the earliest cast. Each branch's CI-env pyright is 0 errors.

Everything else is disjoint by directory and maps cleanly. Note `composer/sandbox/*`
and `rust/run-confined` are **frozen upstream** (from #73) — only `recipes.py` carries a
crucible delta, so no sandbox file needs intermediate/final staging beyond that row.

## Invariant held during execution

Each branch must **compile + pass its own gate**, not just the final one. The verified
import layering is what makes that achievable.

## Execution recipe (per PR, when greenlit)

For each PR, branching off the previous (`eric/ecosystem` off `origin/master`):

| PR | Branch | Branches off |
| --- | --- | --- |
| PR 1 | `eric/ecosystem` | `origin/master` |
| PR 2 | `eric/rust` | `eric/ecosystem` |
| PR 3 | `eric/crucible-app` | `eric/rust` |

1. `git checkout <prev-branch> -b <pr-branch>`
2. `git checkout eric/crucible -- <that layer's files>`
3. Adjust cross-cutting files to their intermediate form (see table).
4. Run that PR's gate; fix until green.
5. Squash-commit with a descriptive message.

Result: 3 stacked local branches (`eric/ecosystem` → `eric/rust` →
`eric/crucible-app`) to review before any push.
