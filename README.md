# Auto-Prove Pipeline

Auto-prove is a multi-agent pipeline that automatically generates and verifies CVL specifications for Solidity smart contracts. Given a project root, a main contract, and a design document, it analyzes the system's components, formulates properties, and generates CVL specs — running the Certora Prover in a loop to verify them.

The same pipeline also writes [CVLR](https://docs.certora.com/en/latest/docs/solana/index.html) rules for Solana programs and verifies them with the Certora Solana Prover; see [Solana (CVLR)](#solana-cvlr) below.

## Quickstart (Docker)

The fastest way from a clean clone to a working run. Everything — Python deps, the JRE, all `solcX.Y` compilers, the RAG knowledge base, and Postgres — is provisioned inside containers; the only host requirement is Docker (with Compose + BuildKit).

```bash
git clone --recurse-submodules git@github.com:Certora/AutoProver.git
cd AutoProver

# Cloud Certora Prover credentials + Claude key (see the table below).
export ANTHROPIC_API_KEY=sk-... CERTORAKEY=...

# One-time Certora cloud login
uv tool install certora-cloud
certora-cloud login

# A ready-to-run example project (public).
git clone https://github.com/Certora/AutoProverExamples.git

# Builds the image (first time), starts Postgres, runs one-time DB/RAG setup,
# then runs the pipeline. The project root is mounted into the container; the
# contract and design-doc paths are relative to it.
scripts/autoprove AutoProverExamples/SmokeTest src/Answer.sol:Answer design.md

# The design doc is optional — drop it and an agent discovers one from the project:
scripts/autoprove AutoProverExamples/SmokeTest src/Answer.sol:Answer
```

The prover runs in the Certora cloud. Add `--tui` for the interactive UI, `--rebuild` to force an image rebuild. Pass extra autoprove options after `--` (e.g. `-- --max-concurrent 2`). Under the hood this is just `scripts/docker-compose.yml` (a profile-gated `autoprove` service next to `postgres`); see the header of that file to drive it with raw `docker compose` commands instead.

> **Apple Silicon:** the image is `linux/amd64` (the Solidity compiler binaries are only published for amd64). Enable **Docker Desktop → Settings → General → "Use Rosetta for x86/amd64 emulation"** — it runs the amd64 image ~near-native; without it, qemu makes the build and the one-time RAG embedding *dramatically* slower.

The rest of this document covers the **host `uv` flow** for development.

## Prerequisites

- Python 3.12+, `uv`, Docker with compose
- `rustup` — a default `uv sync` builds the `rust/` crates (see below)
- `ANTHROPIC_API_KEY` in your environment
- PostgreSQL databases running (see below)
- RAG database populated
- Solidity compiler(s) on `$PATH` (naming convention `solcX.Y`, e.g. `solc8.29`)

### Rust toolchain

A `uv sync`, or any `uv run`, which revalidates path dependencies, builds the maturin crates under `rust/`. Without a
toolchain the sync fails outright rather than skipping. Install [rustup](https://rustup.rs) (a distro `cargo` package is
not enough: `rust-toolchain.toml` pins the toolchain and rustup is what reads it), then once, from the repo root:

```bash
rustup toolchain install --no-self-update
```

Re-run that after any bump to `rust-toolchain.toml`'s `channel` — rustup would otherwise install the new toolchain from
inside the build, where the concurrent cargo calls a sync makes race each other and one dies mid-download.

Not working on the Rust side? `uv sync --no-dev` skips the crates entirely — the Rust tests then skip themselves instead
of failing, and the CI pyright job runs this way.

### Certora Prover

**Cloud mode (the default):** Two credentials are needed. `CERTORAKEY` (set in your environment) authenticates `certoraRun` when it submits prover jobs. The job *results* are fetched with the Certora cloud credentials produced by `certora-cloud login` — install the public CLI (`uv tool install certora-cloud`) and run it once (`certora-cloud login`); it writes `~/.certora/credentials.json`, which is read automatically. Exporting `CERTORA_USER`/`CERTORA_TOKEN`/`CERTORA_REFRESH_TOKEN` instead is an optional override.

**Local mode:** You need a full local Prover build from https://github.com/Certora/CertoraProver. From the Prover repo root, run `./gradlew copy-assets` and set `CERTORA` to point to `CertoraProver/target`. (N.B. Local mode is not yet wired through the `scripts/autoprove` Docker wrapper.)

## (One Time) Database Setup

### 1. Start PostgreSQL

From the `scripts/` directory:

```bash
cd scripts/
docker compose create && docker compose start
```

This launches a `pgvector/pgvector:pg16` container with all databases pre-initialized via `init-db.sql`. The databases created are:

| Database | Purpose |
|---|---|
| `rag_db` | CVL manual search (pgvector embeddings) |
| `langgraph_store_db` | LangGraph document/index store |
| `langgraph_checkpoint_db` | Workflow checkpoints for resumption |
| `memory_tool_db` | LLM context memory (hierarchical filesystem) |
| `audit_db` | Execution history and prover results |

By default the databases are on `localhost:5432`. Override with environment variables if needed:

```bash
export CERTORA_AI_COMPOSER_PGHOST=myhost
export CERTORA_AI_COMPOSER_PGPORT=5433
```

Note: the RAG database has its own connection string (default `postgresql://rag_user:rag_password@localhost:5432/rag_db`), overridable via `--rag-db`.

### 2. Populate the RAG Knowledge Base

The RAG database must be populated before running auto-prove. This is a two-step process:

```bash
# Build the Certora documentation HTML
./scripts/gen_docs.sh

# Populate the RAG DB from the CVL documentation
./scripts/populate_rag.sh
```

`gen_docs.sh` clones the [Certora Documentation](https://github.com/Certora/Documentation) repo and builds single-page HTML files via Sphinx. `populate_rag.sh` then chunks and indexes the CVL documentation into `rag_db`.

The sanity analyzer (`sanity_analyzer/`, run by AutoSetup) defaults to a second, extended corpus: CVL plus the Prover docs. Populate it with `./scripts/populate_extended_rag.sh` after `gen_docs.sh`.

The RAG is read-only at runtime and derived entirely from the documentation HTML, so when the docs change, rebuild it. `./scripts/refresh_rag.sh` regenerates the docs, wipes the database and repopulates it (`--all` also rebuilds the extended corpus; `--skip-gen-docs` reuses the HTML already built). Run it when nothing is querying the RAG: search returns nothing while it re-embeds.

### 3. Populate the CVL Knowledge Base

Run the knowledge base population script to load common CVL pitfall articles into the LangGraph store:

```bash
uv run --extra ml python -m composer.scripts.kb_populate
```

This inserts ~30 curated articles (summary misapplication, vacuity traps, ghost semantics, etc.) that agents consult during spec generation.

## Installation

To install the scripts for execution simply run:

```bash
uv tool install '.[ml,certora-cli]'
```

The `certora-cli` package is selected via one of three mutually-exclusive extras (pick exactly one): `certora-cli` (stable/main release), `certora-cli-beta`, or `certora-cli-beta-mirror`. The `prover` extra is an alias for `certora-cli` (the main release), so `'.[ml,prover]'` is equivalent to the command above. These extras include all of the dependencies for running the prover scripts (in local mode) and the certoraRun scripts themselves (cloud mode).

The `ml` group includes `sentence-transformers` and `einops`, required for the embedding model (`nomic-embed-text-v1.5`) used by RAG and the indexed store.

## Usage

Auto-prove has two entry points: a Textual-based TUI and a headless console mode.

### TUI Mode

```bash
tui-autoprove <project_root> <path/to/Contract.sol:ContractName> [design_doc]
```

### Console Mode

```bash
console-autoprove <project_root> <path/to/Contract.sol:ContractName> [design_doc]
```

The `design_doc` is optional. When omitted, a "Design Doc Discovery" phase runs an
agent that searches the project for the best design/specification document and uses
it automatically; the run fails fast with guidance if it finds nothing suitable.

Console mode prints the same pipeline output to stdout without the interactive TUI. Useful for CI or logging. NB if you need to do `print` debugging, the `console_autoprove.py` is your best
bet. Debugging in the tui_autoprove.py workflow *mandates* the use of a python logger.

### Dev Setup
You will likely want to install the tool using the `--editable` flag. You'll also want to run `uv sync --group test` to pull in the testing utilities we use.

### Arguments

| Argument | Description |
|---|---|
| `project_root` | Root directory of the Solidity project |
| `main_contract` | Path to the contract file and contract name, separated by `:`. The path must be relative to or within `project_root`. Example: `src/Token.sol:Token` |
| `system_doc` | Path to a design document (plain text or PDF) describing the system. **Optional** — auto-discovered from the project when omitted |

### Options

| Option | Default | Description |
|---|---|---|
| `--max-concurrent` | 4 | Maximum number of parallel agents for property extraction and CVL generation |
| `--cache-ns` | None | Cache namespace string. Enables cross-run caching so repeated runs skip completed phases |
| `--memory-ns` | None | Memory namespace. Defaults to the auto-generated thread ID |
| `--cloud` | off | Run prover jobs in the Certora cloud instead of locally |
| `--model` | `claude-opus-4-6` | Anthropic model to use |
| `--tokens` | 10000 | Token budget for LLM responses |
| `--thinking-tokens` | 2048 | Thinking token budget |
| `--rag-db` | `postgresql://rag_user:rag_password@localhost:5432/rag_db` | RAG database connection string |

### Example

```bash
tui-autoprove \
    AutoProverExamples/SmokeTest \
    src/Answer.sol:Answer \
    design.md \
    --cloud \
    --max-concurrent 2 \
    --cache-ns my-answer-run
```

## Pipeline Phases

The pipeline executes the following phases in order:

### Phase 0: System Analysis

Analyzes the source code to identify the system's components, contracts, and external actors. Uses filesystem exploration tools to read and understand the codebase. The result is cached and feeds into all subsequent phases.

### Phase 1: Harness Setup

Runs AutoSetup to analyze compilation and classify external contracts (ERC20s, interfaces, etc.). Generates harness contracts and a prover configuration (`compilation_config.conf`). Also produces summaries for known external contracts.

### Phase 2: Custom Summaries

Generates CVL summaries for ERC20 contracts and external interfaces discovered in Phase 1. Only runs if the system has external contracts that need summarizing.

### Phase 3: Per-Component Property Extraction (parallel)

For each component identified in Phase 0, an agent analyzes the code and formulates properties to verify. Runs in parallel, bounded by `--max-concurrent`. Produces a list of property formulations per component.

### Phase 4: Per-Component CVL Generation (parallel)

For each component's properties, an agent generates CVL specs and runs the prover to verify them. Failed specs are revised in a feedback loop. Where a counterexample starts from a state the contract cannot reach, the agent states that relationship as an invariant, proves it in the same spec, and cites it with `requireInvariant`. Results are written to `certora/specs/autospec_{component}.spec` with accompanying commentary files. Also bounded by `--max-concurrent`.

### Output

Auto-prove writes its output into the `certora/` directory within the project root. Generated specs live under `certora/specs/` (the prover resolves CVL `import`s relative to that directory), while their run configs go to `certora/confs/`:

- `certora/specs/autospec_{component}.spec` (e.g. `autospec_Core_Logic.spec`) — per-component specs
- `certora/specs/summaries/*.spec` — AutoSetup-generated and protocol-specific summaries
- `certora/confs/*.conf` — per-spec prover configs (each `verify` points at the spec's path relative to the project root)

Each `autospec_{component}` spec is accompanied by metadata under `certora/properties/`, keyed by the spec's stem:

- `certora/properties/{stem}.properties.json` — the analysis-phase property formulations (title, sort, methods, description); `title` is the cross-reference key
- `certora/properties/{stem}.property_rules.json` — the property→rules mapping (`{property title: [rule names]}`)
- `certora/properties/{stem}.commentary.md` — LLM commentary explaining the generated spec

The pipeline returns an `AutoProveResult` with counts of components analyzed, properties generated, and any failures.

## Caching

When `--cache-ns` is provided, auto-prove caches the results of expensive phases (system analysis, property extraction, per-component CVL generation) in the LangGraph store. On subsequent runs with the same `--cache-ns`, cached results are reused if the inputs (project root, contract path, design doc content) haven't changed.

The cache key is derived from a SHA-256 hash of the project root, design document content, contract path, and contract name. Changing any of these invalidates the cache.

When the design doc is auto-discovered, the discovery step is itself cached under a **doc-independent** key (project root, contract path, contract name) — so a repeat run on the same project reuses the previously discovered document and skips the discovery agent. Like the main cache, it only invalidates when the cache namespace rotates, not when project files change.

### Exploring the cache/memory

You can view the cache/memory of the various phases by running `scripts/autoprove_cache_explorer.py`. This script takes the same positional arguments as the `*_autoprove.py` entrypoints, as well as the `--memory-ns` and `--cache-ns`.
This should load up a TUI frame that lets you inspect the currently cached values. You can toggle to memory mode to view (and potentially edit) the memories of the various sub agents.

### Debugging Agents

In headless mode, the CVL Generation agents will produce a mnemonic name. You can pass this mnemonic into the `snapshot_viewer.py` to view the conversation history of that agent.

## Solana (CVLR)

`console-solana` and `tui-solana` write CVLR rules for a Solana program and verify them with the Certora Solana Prover in the cloud. They take the same shape of arguments as the EVM entry points:

```bash
console-solana <cargo_workspace_root> programs/<pkg>/src/lib.rs:<program> [design_doc]
```

The path picks the crate the harness is written into (`--package` overrides it); the identifier is the program the analysis must name. `--help` lists the rest.

### Docker

The Solana image (`scripts/Dockerfile.solana`) layers the Rust toolchain, `cargo certora-sbf` and the Solana platform-tools onto the base image, so build the base image first. The header of `scripts/docker-compose.solana.yml` has the full recipe; in short:

```bash
docker compose -f scripts/docker-compose.yml --profile autoprove build
COMPOSE="docker compose -f scripts/docker-compose.yml -f scripts/docker-compose.sandbox.yml -f scripts/docker-compose.solana.yml --profile solana --profile sandbox"
$COMPOSE build
$COMPOSE run --rm autoprove-solana setup-db     # once; populates the CVLR corpora too
export HOST_UID=$(id -u) HOST_GID=$(id -g) HOST_WORK_DIR=/path/to/projects
$COMPOSE run --rm autoprove-solana console-solana /work/<proj> programs/<pkg>/src/lib.rs:<program>
```

All three compose files are needed: every cargo build of the target project runs inside the `run-confined` sandbox (Landlock + seccomp; see [docs/command-sandbox.md](docs/command-sandbox.md)), and the sandbox overlay is what provides the launcher.

### Host

On top of the EVM prerequisites:

- `cargo install --locked cargo-certora-sbf`, then fetch the platform-tools release the pipeline builds with (`PLATFORM_TOOLS_VERSION` in `composer/spec/cvlr/conf.py`): `cargo certora-sbf --no-build --tools-version <version>`. They land under `~/.cache/solana`; set `CERTORA_PLATFORM_TOOLS_ROOT` if you put them elsewhere.
- The CVLR corpora: `./scripts/populate_cvlr_rag.sh` after `gen_docs.sh`. Generating the API corpus needs a nightly toolchain (`rustup toolchain install nightly`).
- The sandbox launcher: `uv sync` builds `rust/run-confined` into `.venv/bin`. The sandbox fails closed. For trusted inputs during development, `COMPOSER_SANDBOX_PROVIDER=none` runs builds unconfined, and the run says so on stderr.

### Output

The harness is a Rust module inside the verified crate, under `<pkg>/src/certora/` (rules in `specs/`), wired into the crate's `Cargo.toml` and `lib.rs`. Everything else — the report, the property→rule mapping, per-rule reports — goes under `certora/cvlr/` in the project root. Edits the pipeline made to the program itself (munges) are recorded in the report.

## AI Composer (code generation)

AI Composer generates a Solidity implementation that satisfies a CVL spec. It shares this repo's setup (databases, RAG, Prover credentials); the Prover runs in the cloud unless `--local-prover` is given.

```bash
console-codegen spec.spec Interface.sol system_doc.txt   # or tui-codegen
```

`spec.spec` is the CVL the implementation must satisfy, `Interface.sol` holds the `interface` it must implement, and `system_doc.txt` describes the system. The run is semi-automatic: it can ask for help, propose spec changes, or ask to relax a requirement, so watch it.

`--thread-id` (logged at start) and `--checkpoint-id` (printed as the run goes) resume from any checkpoint, not only the latest. `--debug-fs DIR` with both dumps the generated files at that checkpoint and exits. `--help` lists the rest.
