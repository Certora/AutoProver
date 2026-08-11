# Design options — Crucible toolchain versioning & container packaging

**Status:** options for discussion. No decision yet. Sibling of
[crucible-application.md §6.1](./crucible-application.md) (version compatibility) — this doc
zooms in on *how we package the toolchains and pick versions*, which §6.1 flagged and left open
("a version table replaces the hardcoding later").

---

## 1. The problem

To fuzz a Solana program with Crucible we do two builds, both version-sensitive:

- **Build the program to sBPF** with `cargo-build-sbf` (from the Solana **platform-tools**, which
  bundle their own sBPF `rustc`).
- **Assemble + build a native harness crate** ([composer/crucible/harness.py](../composer/crucible/harness.py))
  that **path-depends on the program crate** (`features = ["no-entrypoint"]`) and also depends on the
  **Crucible crates**, **`anchor-lang`**, the **`solana-*`** crates, and **`libafl`**; then run it via
  `crucible`.

The binding constraint is the harness↔program link: **one version of `anchor-lang` and of the
`solana-*` crates must satisfy both the program and the harness** (two versions of `anchor-lang` in
one dependency graph don't link). So per target program, several axes must line up at once:

| Axis | Set by | Notes |
|---|---|---|
| host `rustc` | `rust-toolchain.toml` / ambient | builds the harness natively + runs `cargo` |
| sBPF `rustc` + Solana SDK | **platform-tools** version | builds the program `.so` |
| `solana-*` crates | program's `Cargo.lock` | harness must match |
| `anchor-lang` | program's `Cargo.lock` / `Anchor.toml` | harness must match |
| `crucible` crates + CLI | our choice | must be compatible with the above |
| `libafl` | `crucible` pins it | rides the crucible version |

So a run needs a **mutually-compatible `(rustc, platform-tools, solana, anchor, crucible)` combo**
that also *matches the program's* solana/anchor — the §6.1 compatibility matrix.

**Where we are today:** the harness pins `anchor-lang 1.0.1` / `solana 3.0` / `libafl 0.15.1` and the
Crucible crates via a **local checkout path-dep** (`CrucibleDep`, hardcoded), and the AutoProver
container ships **none** of this toolchain (no Rust, no platform-tools, no `anchor`, no `crucible`, no
checkout, and the `crucible_app` wheel isn't in `uv sync`). So "package the toolchain" and "pick the
versions" are the same conversation.

---

## 2. What every option must solve (cross-cutting)

Independent of packaging, these are needed by all three options and should be built once, shared:

1. **Version detection** — read the program's `rust-toolchain.toml` (host `rustc`), `Cargo.lock`
   (authoritative `solana-*` / `anchor-lang` versions), and `Anchor.toml` (anchor CLI) to determine
   its required combo. Edge cases: workspaces, unpinned deps, no lockfile.
2. **A compatibility table** (§6.1) — the single source of truth mapping *program versions →
   a compatible `crucible` version + the toolchain set*. Every option consults it; they differ only
   in what they *do* with the result.
3. **Sourcing the Crucible crates** — today a local checkout. Versioning needs a stable story: git
   tags/commits, or published crates.io releases, or vendored-per-combo. (Open: are the crucible
   crates published, or checkout-only?)
4. **Offline + the sandbox** — the confined build is offline ([command-sandbox.md §5](./command-sandbox.md));
   the trusted warm/fetch (network) must have *the resolved combo's* deps available. A bigger version
   matrix means more to vendor/provision.
5. **Two Rust toolchains** — host (harness + `cargo`) and sBPF (platform-tools). Both must be present
   and version-correct; they are installed and selected differently (`rustup` vs `agave`/`solana-install`).

---

## 3. Prior art in AutoProver (how the EVM backends handle this)

AutoProver already ships two EVM toolchain strategies. Neither faces Crucible's *coupled* matrix, but
both are concrete precedents for the packaging axis — and one maps to each end of the options below.

### 3.1 CVL / Certora-Prover path — "bake every version, select by name"

- The image installs **every released `solc`** as `solcX.Y` (`0.8.29` → `solc8.29`), checksum-verified
  against Solidity's official binary index, via [scripts/install_solc.py](../scripts/install_solc.py)
  at build time (a `COPY` + `RUN` in [scripts/Dockerfile](../scripts/Dockerfile)); a bare `solc`
  symlink points at a default (`0.8.29`).
- The compiler is chosen by an explicit **`--solc-version` arg** (default `8.29`) threaded through the
  pipeline ([cli/tui_pipeline.py](../composer/cli/tui_pipeline.py#L85) → [prover/runner.py](../composer/prover/runner.py#L112)
  `--solc <ver>`; the LLM's prover tool also names `solcX.Y`, [tools/prover.py](../composer/tools/prover.py#L71)).
  It is **caller-specified, not auto-detected** from the pragma.
- The verification itself runs in the **Certora cloud** — the Dockerfile explicitly bundles no local
  prover ("Cloud Certora Prover only").
- **Shape:** one version axis (`solc`), all versions baked into one image, per-run selection by name.
  This is essentially the image-baked end of **Options 1–2**, and operational proof that "bake all +
  select" works in this project today.

### 3.2 Foundry / `forge` path — "delegate to the tool's own version manager"

- The backend ([composer/foundry/](../composer/foundry/)) shells out to `forge test --json` on a
  staged draft test in the user's project ([composer/foundry/runner.py](../composer/foundry/runner.py))
  and parses the JSON; publish requires a green *unseeded* run.
- Version management is **forge's**: forge reads `foundry.toml`/pragma and uses its own solc manager
  (`svm`) to fetch the matching compiler, and resolves libraries its own way. AutoProver never picks
  or installs `solc` for this path.
- **`forge` is *not* in the Dockerfile** (no `forge`/`foundryup`/`svm`). So the Foundry backend is
  **not packaged in the container today** — the *same gap* Crucible has; forge just self-manages once
  it's present.
- **Shape:** delegate to the ecosystem's own version manager. This is the **Option-3** pattern —
  except Crucible has no single umbrella manager, so it would compose the ecosystem's own managers
  (`rustup`, `agave`/`solana-install`, `avm`, plus a `crucible` fetch) to play forge's role.

### 3.3 The difference that makes Crucible harder

EVM has **no harness↔contract link coupling**: `forge` (or the prover) compiles the contract and the
test with *one* `solc` — there is no separate test artifact that must *link the contract's compiled
bindings at a matching dependency version*. Crucible's harness **path-depends on the program crate**,
which is exactly what turns one knob (`solc`) into the five-axis coupled matrix of §1. So the `solc`
single-knob model transfers to Crucible only if the compatible matrix turns out small; otherwise
Crucible is closer to the forge/version-manager model.

### 3.4 A fourth angle the prover suggests

The prover keeps the image light (`solc` only) by offloading the heavy tool to the **Certora cloud**.
The Crucible analog — a fuzzing **service** the container calls rather than an in-image toolchain —
is a distinct packaging option worth keeping in view: it sidesteps in-image versioning entirely, at
the cost of standing up and *itself* versioning a service. Not fleshed out here, but noted so it isn't
lost.

---

## 4. The options

### Option 1 — One blessed combo per image (pin-and-bump)

Bake exactly one mutually-compatible toolchain set into the image (host `rustc` + platform-tools +
`anchor` + `crucible` CLI + a crucible checkout at a pinned ref + the pinned harness deps). The image
*is* the version; there's no runtime selection. Programs that don't match the blessed combo are
**rejected fast** with a clear message. Bumping = cutting a new dated/tagged image.

- **Selection:** none at runtime — detection is used only to *fail fast* on mismatch.
- **Crucible crates:** a checkout pinned to a tag/commit, baked in.
- **Offline:** one vendored dep set — the simplest possible.
- **Pros:** smallest image; fully deterministic + reproducible; cleanest offline story; least to build
  and reason about; matches today's hardcoded pins, so it's the shortest path to *runnable-in-container*.
- **Cons:** supports a single combo only; version drift means real programs (on other anchor/solana)
  don't run; we must track upstream and re-bless; it punts the matrix entirely.
- **Best when:** early days, a narrow set of target programs, "get it running" is the priority.

### Option 2 — A matrix of blessed images, selected per program

CI builds **N images**, each a blessed combo (the tag encodes it, e.g.
`autoprover-crucible:sol3.0-anchor1.0-cruX`), enumerated from the compatibility table. A dispatcher
detects the program's versions, maps them to a tag, and runs that image. Each image is internally an
Option-1 image (single combo, reproducible); the *fleet* covers the matrix.

- **Selection:** at dispatch time — pick the image; nothing version-variable inside it.
- **Crucible crates:** each image pins its combo's ref.
- **Offline:** each image vendors its own combo — clean, per image.
- **Pros:** covers the matrix while keeping each image simple + reproducible; no in-container version
  juggling; adding a combo = one table row + one CI build; a bad combo can't affect the others.
- **Cons:** needs a build-matrix + image registry + an image-selection layer; N images to build,
  store, and refresh on every crucible/solana/anchor bump; a program on an un-built combo isn't
  supported until CI adds it; leans hard on reliable version detection.
- **Best when:** several distinct program families/versions, CI + a registry are available, and
  reproducibility matters.

### Option 3 — Thin base + on-demand toolchain provisioning

Ship a thin base image; a **trusted provisioning step** at run/job start installs the exact needed
versions — `rustup` (host toolchain), `agave`/`solana-install` (platform-tools), `avm` (anchor),
and a `crucible` fetch/build at the resolved ref — keyed by the detected versions + the table, cached
on a persistent volume. One image, arbitrary combos, provision-what-you-need.

- **Selection:** at runtime — resolve, then provision.
- **Crucible crates:** fetched at the resolved ref on demand (cached).
- **Offline:** provisioning is the network-on trusted pre-step (like the sandbox's `cargo fetch`
  warm); the confined build still runs offline against the provisioned set.
- **Pros:** one image; supports the full (long-tail) matrix without pre-building every cell; only
  materializes what's used; extends to new versions with no image rebuild.
- **Cons:** per-run (or per-new-combo) provisioning latency + network; a version-manager layer to
  build and maintain; cache/volume lifecycle; the most moving parts at run time; weakest
  reproducibility guarantee (provisioning can drift unless carefully pinned).
- **Best when:** a wide/long-tail of program versions, and run-time flexibility is worth the
  provisioning cost.

---

## 5. Suggested path (not a decision)

- **Start with Option 1.** It's the smallest lift, matches the current hardcoded pins, and gets
  Crucible *runnable in the container* — while forcing us to build the two shared pieces (§2.1
  detection + §2.2 the compatibility table) that 2 and 3 also need. Even Option 1 needs detection, to
  fail fast on a mismatch instead of producing a confusing link error.
- **Evolve to Option 2** as the target-program set widens. Because each matrix cell *is* an Option-1
  image, this is additive (new table rows + CI builds), not a rewrite.
- **Hold Option 3** for when pre-building every matrix cell becomes impractical (a genuine long tail),
  or when a customer's exact combo can't be predicted ahead of time.
- Each end has **direct in-project precedent** (§3): Options 1–2 are the baked-`solc` model, Option 3
  is the `forge`/`svm` delegate-to-a-version-manager model — so neither is a leap into the unknown.
- **The seam that keeps this evolvable:** separate **version resolution** (detect → table → combo)
  from **provisioning** (bake / select-image / install-on-demand). Resolution is shared code; only
  the provisioning backend swaps. This mirrors the ecosystem/backend split the project already uses,
  and the `SandboxProvider` seam pattern from Phase 6.

---

## 6. Open questions

1. **Are the Crucible crates published (crates.io / a private registry), or checkout-only?** Decides
   how a combo sources them (tag vs release vs vendored) — and how heavy Option 3's fetch is.
2. **Exact-match vs compatible-range** — does the harness need the *exact* `solana`/`anchor` the
   program uses, or a semver-compatible range? Fewer, wider combos vs many exact ones — this sizes the
   whole matrix (and Options 2 vs 3).
3. **Detection reliability** — is `Cargo.lock` always present/authoritative? How do we handle
   workspaces, path/git deps, and programs that don't pin a `rust-toolchain`?
4. **Budgets** — image size + registry cost (favors 3) vs run-time provisioning latency (favors 1/2).
5. **Compatibility-table ownership** — who curates it and at what cadence as crucible / solana /
   anchor release? This is the recurring maintenance cost common to all three.
6. **`crucible_app` wheel packaging** — orthogonal but adjacent: the maturin wheel must be built into
   whichever image(s) we ship (needs the host Rust toolchain in the build layer), and the
   `run-confined` sandbox launcher likewise (already a Dockerfile stage). Fold this into whichever
   option is chosen.
