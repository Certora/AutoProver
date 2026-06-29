# Auto-Prove Pipeline

Auto-prove is a multi-agent pipeline that automatically generates and verifies CVL specifications for Solidity smart contracts. Given a project root, a main contract, and a design document, it analyzes the system's components, formulates properties, and generates CVL specs — running the Certora Prover in a loop to verify them.

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
```

The prover runs in the Certora cloud. Add `--tui` for the interactive UI, `--rebuild` to force an image rebuild. Pass extra autoprove options after `--` (e.g. `-- --max-concurrent 2`). Under the hood this is just `scripts/docker-compose.yml` (a profile-gated `autoprove` service next to `postgres`); see the header of that file to drive it with raw `docker compose` commands instead.

> **Apple Silicon:** the image is `linux/amd64` (the Solidity compiler binaries are only published for amd64). Enable **Docker Desktop → Settings → General → "Use Rosetta for x86/amd64 emulation"** — it runs the amd64 image ~near-native; without it, qemu makes the build and the one-time RAG embedding *dramatically* slower.

The rest of this document covers the **host `uv` flow** for development.

## Prerequisites

You need everything from the [AIComposer infrastructure setup](AICOMPOSER_INFRA.md):

- Python 3.12+, `uv`, Docker with compose
- `ANTHROPIC_API_KEY` in your environment
- PostgreSQL databases running (see below)
- RAG database populated
- Solidity compiler(s) on `$PATH` (naming convention `solcX.Y`, e.g. `solc8.29`)

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
tui-autoprove <project_root> <path/to/Contract.sol:ContractName> <design_doc>
```

### Console Mode

```bash
console-autoprove <project_root> <path/to/Contract.sol:ContractName> <design_doc>
```

Console mode prints the same pipeline output to stdout without the interactive TUI. Useful for CI or logging. NB if you need to do `print` debugging, the `console_autoprove.py` is your best
bet. Debugging in the tui_autoprove.py workflow *mandates* the use of a python logger.

### Dev Setup
You will likely want to install the tool using the `--editable` flag. You'll also want to run `uv sync --group test` to pull in the testing utilities we use.

### Arguments

| Argument | Description |
|---|---|
| `project_root` | Root directory of the Solidity project |
| `main_contract` | Path to the contract file and contract name, separated by `:`. The path must be relative to or within `project_root`. Example: `src/Token.sol:Token` |
| `system_doc` | Path to a design document (plain text or PDF) describing the system |

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

### Phase 3: Structural Invariants

Formulates and generates CVL for system-wide structural invariants (e.g. total supply consistency, balance accounting). The resulting `certora/specs/invariants.spec` is made available as a resource that later phases can import and use as preconditions.

### Phase 4: Per-Component Property Extraction (parallel)

For each component identified in Phase 0, an agent analyzes the code and formulates properties to verify. Runs in parallel, bounded by `--max-concurrent`. Produces a list of property formulations per component.

### Phase 5: Per-Component CVL Generation (parallel)

For each component's properties, an agent generates CVL specs and runs the prover to verify them. Failed specs are revised in a feedback loop. Results are written to `certora/specs/autospec_{component}.spec` with accompanying commentary files. Also bounded by `--max-concurrent`.

### Output

Auto-prove writes its output into the `certora/` directory within the project root. Generated specs live under `certora/specs/` (the prover resolves CVL `import`s relative to that directory), while their run configs go to `certora/confs/`:

- `certora/specs/invariants.spec` — structural invariants (if any were formulated)
- `certora/specs/autospec_{component}.spec` (e.g. `autospec_Core_Logic.spec`) — per-component specs
- `certora/specs/summaries/*.spec` — AutoSetup-generated and protocol-specific summaries
- `certora/confs/*.conf` — per-spec prover configs (each `verify` points at the spec's path relative to the project root)

Each spec (`invariants` and every `autospec_{component}`) is accompanied by metadata under `certora/properties/`, keyed by the spec's stem:

- `certora/properties/{stem}.properties.json` — the analysis-phase property formulations (title, sort, methods, description); `title` is the cross-reference key
- `certora/properties/{stem}.property_rules.json` — the property→rules mapping (`{property title: [rule names]}`)
- `certora/properties/{stem}.commentary.md` — LLM commentary explaining the generated spec (per-component specs only)

The pipeline returns an `AutoProveResult` with counts of components analyzed, properties generated, and any failures.

## Caching

When `--cache-ns` is provided, auto-prove caches the results of expensive phases (system analysis, property extraction, invariant CVL generation) in the LangGraph store. On subsequent runs with the same `--cache-ns`, cached results are reused if the inputs (project root, contract path, design doc content) haven't changed.

The cache key is derived from a SHA-256 hash of the project root, design document content, contract path, and contract name. Changing any of these invalidates the cache.

### Exploring the cache/memory

You can view the cache/memory of the various phases by running `scripts/autoprove_cache_explorer.py`. This script takes the same positional arguments as the `*_autoprove.py` entrypoints, as well as the `--memory-ns` and `--cache-ns`.
This should load up a TUI frame that lets you inspect the currently cached values. You can toggle to memory mode to view (and potentially edit) the memories of the various sub agents.

### Debugging Agents

In headless mode, the CVL Generation agents will produce a mnemonic name. You can pass this mnemonic into the `snapshot_viewer.py` to view the conversation history of that agent.
