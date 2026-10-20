# Auto-Prove for Solana (CVLR)

`console-solana` and `tui-solana` write [CVLR](https://docs.certora.com/en/latest/docs/solana/index.html) rules for a Solana program and verify them with the Certora Solana Prover in the cloud. This page covers running them from a checkout of this repository. The EVM pipeline, and the parts the two share, are in [README.md](README.md).

```bash
console-solana <cargo_workspace_root> programs/<pkg>/src/lib.rs:<program> [design_doc]
```

The path picks the crate the harness is written into (`--package` overrides it). The identifier is the program the analysis must name. The design document is optional; without one, an agent looks for one in the project. `--help` lists the rest, including the budget flags.

## What the target project must look like

- **A cargo workspace.** The first argument is the directory holding the root `Cargo.toml`. A single-crate project is a workspace of one.
- **A supported Solana platform generation.** Each generation has one CVLR line (`composer/spec/cvlr/reference.py`):

  | Platform generation | `cvlr` | `cvlr-solana`, `cvlr-spl-token`, `cvlr-solana-stake` |
  |---|---|---|
  | `solana-*` 3.x crates | 0.6.1 | 0.6.0 |
  | `solana-program` 2.x | 0.6.1 | 0.5.1 |

  The pipeline reads the generation from the resolved dependency graph and adds the matching CVLR crates itself. A project on `solana-program` 1.x is refused, and so is a project that already depends on a different CVLR line.
- **Anchor, if used, at a version Certora's fork covers.** The Prover cannot analyze crates.io `anchor-lang`: every handler fails with [3006]. The pipeline redirects `anchor-lang` and `anchor-spl` to the matching `certora-v<version>` branch of [Certora/anchor](https://github.com/Certora/anchor) through `[patch.crates-io]`. Covered versions are 0.26.0–0.29.0, 0.30.1, 0.31.1, 0.32.0 and 0.32.1 (`ANCHOR_FORK` in `composer/spec/cvlr/forks.py`). Any other version stops the run with a message naming those versions. The `fixed` crate is handled the same way, at 1.23.1 only.

The program's own `rust-toolchain.toml` is not used. Every cargo command a run makes uses the host toolchain `HOST_RUST_TOOLCHAIN` (1.90.0, in `composer/spec/cvlr/conf.py`), and the program is compiled by the platform tools' rustc whatever the project pins.

## Docker

The Solana image (`scripts/Dockerfile.solana`) adds the Rust toolchain, `cargo certora-sbf`, the platform tools and the CVLR API corpus to the base image. Build the base image first. The header of `scripts/docker-compose.solana.yml` has the full recipe. In short:

```bash
export ANTHROPIC_API_KEY=sk-... CERTORAKEY=...   # compose refuses to load without the first, even to build
docker compose -f scripts/docker-compose.yml --profile autoprove build
COMPOSE="docker compose -f scripts/docker-compose.yml -f scripts/docker-compose.sandbox.yml -f scripts/docker-compose.solana.yml --profile solana --profile sandbox"
$COMPOSE build
$COMPOSE run --rm autoprove-solana setup-db     # once
export HOST_UID=$(id -u) HOST_GID=$(id -g) HOST_WORK_DIR=/path/to/projects
$COMPOSE run --rm autoprove-solana console-solana /work/<proj> programs/<pkg>/src/lib.rs:<program>
```

- **All three compose files, always.** Every cargo build of the target project runs inside the `run-confined` sandbox (Landlock and seccomp; see [docs/command-sandbox.md](docs/command-sandbox.md)), and the sandbox overlay provides the launcher. Without it the project does not load.
- **Run `setup-db` from `autoprove-solana`, not the base image.** It fills every corpus, the EVM ones included. Only the Solana image carries the `cvlr_api_kb` manifest; a `setup-db` from the base image skips that corpus.
- **`HOST_WORK_DIR`** is mounted at `/work`, and project paths are relative to it. It defaults to the current directory.
- **Credentials.** `~/.certora` is mounted into the container, so run `certora-cloud login` on the host first (see [README.md](README.md#certora-prover)).
- `scripts/autoprove`, the EVM quickstart wrapper, does not run the Solana image.

A cold build of both images takes about 35 minutes.

## Host

You need the EVM pipeline's prerequisites from [README.md](README.md#prerequisites): Python, `uv`, `rustup`, the cloud credentials, and PostgreSQL. Solidity compilers are not needed. You also need:

- **The host toolchain:** `rustup toolchain install 1.90.0`.
- **`cargo-certora-sbf` and the platform tools.** Run `cargo install --locked cargo-certora-sbf`, then fetch the platform-tools release the pipeline builds with, `PLATFORM_TOOLS_VERSION` in `composer/spec/cvlr/conf.py` (currently v1.53): `cargo certora-sbf --no-build --tools-version v1.53`. They go under `~/.cache/solana`. If you put them somewhere else, set `CERTORA_PLATFORM_TOOLS_ROOT`.
- **The sandbox launcher.** `uv sync` builds `rust/run-confined` into `.venv/bin`. The sandbox fails closed. For trusted inputs during development, `COMPOSER_SANDBOX_PROVIDER=none` runs builds unconfined, and the run says so on stderr.
- **The CVLR corpora.** Run `./scripts/gen_docs.sh`, then `./scripts/populate_cvlr_rag.sh`. This fills `cvlr_kb` from the Solana manual and `cvlr_api_kb` from rustdoc over the pinned CVLR crates. Generating the API corpus needs a nightly toolchain: `rustup toolchain install nightly`. A run without the corpora still works, without corpus search; `--no-corpus` makes that explicit.

## Output

The harness is a Rust module inside the verified crate, under `<pkg>/src/certora/`, with the rules in `specs/`. It is wired into the crate's `Cargo.toml` (a `certora` feature) and `lib.rs`. Everything else goes under `certora/cvlr/` in the project root: the report, the property-to-rule mapping and the per-rule reports. That includes `certora/cvlr/harness.patch`, the harness and its wiring, named by the report's `deliverable_patch`. `git apply` it on the project as it was before the run.

A component whose proof edited the program also gets `certora/cvlr/components/<module>.patch`. The report's `source_edits[].patch_path` names it. That file is the harness plus that component's edits. `git apply` it on the project as it was before the run, then build with `--features "certora unit_<module>"`. Apply one component's patch to that clean project. Each patch creates the harness files, so it does not apply on top of `harness.patch` or on top of another component's patch. `source_edits[].cumulative_diff` is the edits alone, against the tree the run left behind. It is the record of what changed in the program. The file to apply is `patch_path`. A component the run budget cut short is not in `source_edits`. If its partial edited the program, its `curtailed_components[]` entry carries the same record, and its patch leaves the harness module as the placeholder. To build the partial, rename `<module>.rs.unverified` over `<module>.rs` after applying.

Each delivered component also gets a Prover conf, `certora/cvlr/confs/<module>.conf`, named by the report's `prover_confs`. It has the rules, cargo features, loop settings and summaries that component was verified with. After applying the patch, run it from the package directory: `cd <pkg> && certoraSolanaProver <path to certora/cvlr/confs/<module>.conf>`. The conf has no build script, so the Prover CLI builds with `cargo certora-sbf` in that directory. Its summaries path is relative to the package for the same reason. The summaries file it names, `<pkg>/src/certora/envs/cvlr_summaries_<module>.txt`, is in both patches.

The run builds in a copy of the project in a temporary directory, which is deleted when the run ends. The pipeline adds the Prover's output directories to the project's `.gitignore`.

## Tests

The fast suite covers the CVLR backend without cargo or the cloud. Two expensive tests exercise it end to end. Neither runs in CI, so run them by hand after changing the build, the sandbox or the submission path:

```bash
# A hand-written rule from Certora/SolanaExamples, built and verified; checks its recorded verdicts.
SOLANA_EXAMPLES_REPO=~/src/SolanaExamples \
  env -u CERTORA uv run --no-sync pytest tests/test_cvlr_end_to_end.py -m expensive -s

# The whole pipeline over a Solana program, with the LLM replayed from a tape.
# Needs Postgres with both CVLR corpora loaded.
env -u CERTORA uv run --no-sync pytest tests/test_cvlr_tape.py -m expensive -q -s
```

Each submits real Prover jobs and takes several minutes. `env -u CERTORA` matters: with `$CERTORA` set, submissions go to a local Prover build, and the end-to-end test skips. The end-to-end test also skips, naming what is missing, when there is no SolanaExamples checkout (`$SOLANA_EXAMPLES_REPO`, by default `~/src/SolanaExamples`), no cargo, no platform tools or no sandbox. Re-record the tape with `scripts/record_cvlr_tape.sh` when the pipeline's shape changes on purpose.

## Known limitations

The open work is tracked in [docs/cvlr-todo.md](docs/cvlr-todo.md). The limits a user is most likely to hit:

- **Acceptance properties cannot be stated.** A rule that asserts a handler returns `Ok` comes back vacuous. That includes the checks Anchor's generated account validation enforces. (Blocked on upstream, item 2.)
- **Solana only.** CVLR also targets Soroban, but there is no Soroban entry point yet.
- **One CVLR line per platform generation.** A project pinned to a different CVLR release is refused rather than used as is.

How the backend is built is in [docs/cvlr-backend-plan.md](docs/cvlr-backend-plan.md) and [ARCHITECTURE.md](ARCHITECTURE.md). Defects in the Prover, CVLR and the templates are in [docs/upstream-defects.md](docs/upstream-defects.md).
