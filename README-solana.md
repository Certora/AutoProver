# Auto-Prove for Solana (CVLR)

`console-solana` and `tui-solana` write [CVLR](https://docs.certora.com/en/latest/docs/solana/index.html) rules for a Solana program and verify them with the Certora Solana Prover in the cloud. This page covers running them from a checkout of this repository. The EVM pipeline, and the parts the two share, are in [README.md](README.md).

```bash
console-solana <cargo_workspace_root> programs/<pkg>/src/lib.rs:<program> [design_doc]
```

The path picks the crate the harness is written into (`--package` overrides it). The identifier is the program the analysis must name. The design document is optional; without one, an agent looks for one in the project. `--help` lists the rest, including the budget flags.

## What the target project must look like

- **A cargo workspace.** The first argument is the directory holding the root `Cargo.toml`. A single-crate project is a workspace of one.
- **A supported Solana platform generation.** Each generation has one CVLR line, pinned exactly in `composer/spec/cvlr/reference.py`. The core is `cvlr` 0.6.2 on every row below. The chain crates of one line are released together:

  | Platform generation | `cvlr-solana` | Models |
  |---|---|---|
  | `solana-*` 3.x | 0.6.1 | `cvlr-spl-token` and `cvlr-solana-stake`, both 0.6.1 |
  | `solana-program` 2.x (2.2 and later) | 0.5.2 | the same two crates at 0.5.2 |
  | `solana-program` 1.18 | 0.4.7 | none. Token helpers live in `cvlr-solana`. There is no stake model. |

  Those three chain-crate pins read an account's flags without a branch per flag. An older crate on the same line adds eight paths per nondeterministic account, and a handler-level rule times out.

  The pipeline reads the generation from the crates the verified package itself links, newest match first, and adds that line. Any other `solana-program` 1.x line, including 1.17, is refused: 1.17 is a different `AccountInfo`, and its `solana-frozen-abi` pin does not build with the platform tools this pipeline uses. A project that already depends on a different CVLR line is refused too.

  On 1.18 the published manual is written for 0.5 and later, so it names `cvlr_spl_token` and `cvlr_solana_stake`, which this line does not have. A token CPI reaches `invoke_signed_unchecked`, which the prover treats as opaque. When a property needs the transfer itself, the stand-in for the program's token call invokes the matching summary in `cvlr_solana::token`: `spl_token_transfer`, `spl_token_2022_transfer`, `spl_mint_to`, `spl_burn`, or `spl_close_account`.
- **Or a program written on [jiminy](https://github.com/igneous-labs/jiminy).** A program whose package depends on `jiminy-entrypoint` (0.1.x) is on the jiminy generation, whatever SDK crates its dependencies also link. It gets the `cvlr` core and no chain crate: `cvlr-solana`'s helpers build the SDK's `AccountInfo`, which a jiminy handler cannot take. The scaffold writes `src/certora/jiminy.rs` in their place, with `cvlr_jiminy_abr`, `cvlr_jiminy_account` and `cvlr_jiminy_accounts`, compiled against the program's own jiminy through `jiminy-entrypoint`'s re-export. The package must declare `jiminy-entrypoint` itself. No token or stake model applies, and a CPI through `jiminy_cpi` is not modelled.
- **Or a program written on [Pinocchio](https://github.com/anza-xyz/pinocchio), 0.8 through 0.12.** A package that depends on `pinocchio` is on one of its four generations: 0.11 and 0.12 (`AccountView`, with the original data length recorded in the account header under `account-resize`), 0.10 (`AccountView` with a resize delta), 0.9 (`pinocchio::account_info::AccountInfo` with a resize delta), and 0.8 (the same `AccountInfo` with its borrow state inverted and the original data length in the header). Each gets the `cvlr` core and a `src/certora/pinocchio.rs` with `cvlr_pinocchio_account` and `cvlr_pinocchio_accounts`, written for its generation's account header and compiled against the program's own Pinocchio. The package must declare `pinocchio` itself. Most Pinocchio programs are `no_std` and install their own panic handler, which collides with the one `std` brings through cvlr, so the scaffold gates `no_std` and the handler off under the `certora` feature. No token or stake model applies, and a CPI through `pinocchio::cpi` (`pinocchio-token`, `pinocchio-system`) is not modelled.
- **Anchor, if used, at a version Certora's fork covers.** The Prover cannot analyze crates.io `anchor-lang`: every handler fails with [3006]. The pipeline redirects `anchor-lang` and `anchor-spl` to the matching `certora-v<version>` branch of [Certora/anchor](https://github.com/Certora/anchor) through `[patch.crates-io]`. Covered versions are every stable release from 0.26.0 through 1.2.1 (`ANCHOR_FORK` in `composer/spec/cvlr/forks.py`). Any other version stops the run with a message naming those versions. The `fixed` crate is handled the same way, at 1.23.1 only.

The program's own `rust-toolchain.toml` is not used. Every cargo command a run makes uses the host toolchain `HOST_RUST_TOOLCHAIN` (1.90.0, in `composer/spec/cvlr/conf.py`), and the program is compiled by the platform tools' rustc whatever the project pins.

## Docker

The Solana image (`scripts/Dockerfile.solana`) adds the Rust toolchain, `cargo certora-sbf`, `rustfilt`, the platform tools and the CVLR API corpus to the base image. Build the base image first. The header of `scripts/docker-compose.solana.yml` has the full recipe. In short:

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
- **`HOST_WORK_DIR`** is mounted at `/work`, and project paths are relative to it. It defaults to the current directory. Point it at the project under test. If it is this repository, the repo's `rust-toolchain.toml` is what cargo sees inside the container.
- **Host port 5432 belongs to the development database** (`docker compose -p composer_db -f scripts/docker-compose.yml`). `scripts/verify-solana-image.sh` starts its own compose project without publishing that port. Reusing that port override on the development project leaves nothing listening on 5432, and `tests/test_cvlr_tape.py` then skips as "corpus unreachable" while the data volume is still full. Recreate only that postgres, from `scripts/`, with `docker compose -p composer_db -f docker-compose.yml up -d --no-deps postgres`.
- **Credentials.** `~/.certora` is mounted into the container, so log in on the host first (see [README.md](README.md#certora-prover)).
- `scripts/autoprove`, the EVM quickstart wrapper, does not run the Solana image.

A cold build of both images takes about 35 minutes.

After a build, `scripts/verify-solana-image.sh` checks the images without an LLM: the toolchain in the Solana image, the two entrypoint guards, and one Prover job from `tests/test_cvlr_end_to_end.py` run inside the container against a SolanaExamples checkout (`--examples DIR`, or `$SOLANA_EXAMPLES_REPO`). It needs `CERTORAKEY` or a `certora-cloud login`, and takes about two minutes. `--no-prover` skips the job.

## Host

You need the EVM pipeline's prerequisites from [README.md](README.md#prerequisites): Python, `uv`, `rustup`, the cloud credentials, and PostgreSQL. Solidity compilers are not needed. You also need:

- **The host toolchain:** `rustup toolchain install 1.90.0`.
- **`cargo-certora-sbf` and the platform tools.** Run `cargo install --locked cargo-certora-sbf`, then fetch the platform-tools release the pipeline builds with, `PLATFORM_TOOLS_VERSION` in `composer/spec/cvlr/conf.py` (currently v1.53): `cargo certora-sbf --no-build --tools-version v1.53`. They go under `~/.cache/solana`. If you put them somewhere else, set `CERTORA_PLATFORM_TOOLS_ROOT`.
- **`rustfilt` on `$PATH`:** `cargo install rustfilt`. It is the demangler the Solana Prover names functions with. Without it, the check that each summary directive matches a symbol in the build is skipped, with a warning.
- **The sandbox launcher.** `uv sync` builds `rust/run-confined` into `.venv/bin`. The sandbox fails closed. For trusted inputs during development, `COMPOSER_SANDBOX_PROVIDER=none` runs builds unconfined, and the run says so on stderr.
- **The CVLR corpora.** Run `./scripts/gen_docs.sh`, then `./scripts/populate_cvlr_rag.sh`. This fills `cvlr_kb` from the Solana manual and `cvlr_api_kb` from rustdoc over the pinned CVLR crates. Generating the API corpus needs a nightly toolchain: `rustup toolchain install nightly`. A run without the corpora still works, without corpus search; `--no-corpus` makes that explicit.

## Output

The harness is a Rust module inside the verified crate, under `<pkg>/src/certora/`, with the rules in `specs/`. It is wired into the crate's `Cargo.toml` (a `certora` feature, and one `certora_unit_<module>` feature per component) and `lib.rs`. Everything else goes under `certora/cvlr/` in the project root: the report, the property-to-rule mapping and the per-rule reports.

Each component with a harness gets a directory, `certora/cvlr/components/<module>/`, and each delivered component a conf, `certora/cvlr/confs/<module>.conf`. The report's `unit_deliverables` names both for each component:

- `<module>.patch` is that component's tree: the scaffold, its harness module, its cargo feature and summaries file, and its edits to the program. `git apply` it on the project as it was before the run, then build with `--features "certora certora_unit_<module>"`. Apply one component's patch to that clean project. Each patch creates the scaffold, so it does not apply on top of another component's patch.
- `files/` has a copy of each file the patch adds, at its project-relative path, for reading the harness without applying anything. Edits to files the project already has are in the patch only.
- `confs/<module>.conf` reruns the component's rules, with the cargo features, loop settings and summaries it was verified with. After applying the patch, run it from the package directory: `cd <pkg> && certoraSolanaProver <path to the conf>`. The conf has no build script, so the Prover CLI builds with `cargo certora-sbf` in that directory. Its summaries path is relative to the package for the same reason.

`source_edits[].cumulative_diff` is a component's edits to the program alone, against the tree the run left behind. It is the record of what changed in the program; the file to apply is the patch, which `source_edits[].patch_path` also names. A component the run budget cut short is not in `source_edits`. If its partial edited the program, its `curtailed_components[]` entry carries the same record. A curtailed component's directory rebuilds its unvalidated draft and has no conf.

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

Each submits real Prover jobs and takes several minutes. `env -u CERTORA` matters: with `$CERTORA` set, `certora_cli` targets a local Prover build and refuses the cloud submission before upload, in a message that names neither the variable nor the tool. The record script checks for this and stops. The end-to-end test also skips, naming what is missing, when there is no SolanaExamples checkout (`$SOLANA_EXAMPLES_REPO`, by default `~/src/SolanaExamples`), no cargo, no platform tools or no sandbox. A skip there is a missing prerequisite. `scripts/verify-solana-image.sh` treats a skipped in-container end-to-end test as a failure for that reason.

Re-record the tape with `scripts/record_cvlr_tape.sh` when the pipeline's shape changes on purpose. The script checks the prerequisites that otherwise fail after the run has started: `$CERTORA`, `CERTORAKEY`, the platform tools, postgres, both CVLR corpora, and a Certora login that can still refresh. Extra arguments are passed through. `--cache-ns NAME --workdir DIR` stages the scenario in a stable directory so a second invocation resumes. The cache key includes the project path, so the default fresh temporary directory is never a cache hit. A resumed invocation records only the calls it makes and writes them over the tape, so point `COMPOSER_RECORD_OUT` at another file for that invocation.

## Dev cloud

A dev-cloud Solana job runs the AutoProver commit that AIAutoProver's `feature/solana` pins, published on push as `autoprover-solana:latest-solana`. It does not run this checkout, and it does not follow `feature/solana` here until that pin moves. Staging and production use a separate image tag built from AIAutoProver's master. `scripts/verify-solana-image.sh` is the local check before a pin bump. The header of that script says what it checks.

## Known limitations

The open work is tracked in [docs/cvlr-todo.md](docs/cvlr-todo.md). The limits a user is most likely to hit:

- **Acceptance properties cannot be stated.** A rule that asserts a handler returns `Ok` comes back vacuous. That includes the checks Anchor's generated account validation enforces. (Blocked on upstream, item 2.)
- **Solana only.** CVLR also targets Soroban, but there is no Soroban entry point yet.
- **One CVLR line per platform generation.** A project pinned to a different CVLR release is refused rather than used as is.

How the backend is built is in [docs/cvlr-backend-plan.md](docs/cvlr-backend-plan.md) and [ARCHITECTURE.md](ARCHITECTURE.md). Defects in the Prover, CVLR and the templates are in [docs/upstream-defects.md](docs/upstream-defects.md).
