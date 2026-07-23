# Running the Crucible demo (solana_vault)

Step-by-step instructions for a human to run the Crucible (Solana fuzzing) backend
end-to-end on the bundled `solana_vault` Anchor scenario. This is the full vertical:
sBPF build → Solana analysis + property extraction → shared-fixture setup →
per-instruction test authoring + fuzzing → report.

Budget ~15–20 minutes of wall-clock for a full run, plus one-time setup (toolchain +
a ~0.5 GB model download on first run). It makes real, paid LLM calls.

There are two ways to run it:

- **On the host** (§1–§7) — you install the toolchain locally and run `console-crucible`
  under `uv`. Fastest iteration for development.
- **Fully in the container** ([§8](#8-running-entirely-in-the-container)) — a prebuilt
  image bundles the whole blessed toolchain (Rust + Solana platform-tools + anchor +
  crucible), so the only prerequisites are Docker and an API key. Best for a clean,
  reproducible run.

---

## 1. Prerequisites (one-time)

### 1a. External toolchain on `PATH`

The pipeline shells out to these; install them first and confirm they resolve:

```bash
which crucible          # the Crucible fuzzer CLI
which cargo-build-sbf   # Solana platform-tools (builds the program to sBPF)
```

- `cargo-build-sbf` comes with the Solana platform tools / Anchor toolchain.
- `crucible` is the fuzzer CLI (installed to `~/.cargo/bin` in a typical setup).

### 1b. A local `crucible` checkout

The generated harness crate path-depends on Crucible's own crates, so you need a
local clone and must point `CRUCIBLE_REPO` at it. It must contain
`crates/crucible-fuzzer`:

```bash
export CRUCIBLE_REPO=/path/to/crucible      # e.g. ~/src/crucible
ls "$CRUCIBLE_REPO/crates/crucible-fuzzer"  # must exist
```

There is **no default** — the run errors clearly if `CRUCIBLE_REPO` is unset.

### 1c. An Anthropic API key

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

### 1d. Python environment (the `ml` extra is required)

The pipeline builds a real sentence-transformers embedder for the indexed store, so
the venv must include the `ml` extra **and** a torch flavor (`cpu` for a GPU-less
box, `cuda` for a GPU box — they are mutually exclusive). Keep your existing
`certora-cli` selection. This mirrors the container's `uv sync` line:

```bash
# GPU-less host:
uv sync --extra cpu --extra ml --extra certora-cli --group apps
# GPU host: swap --extra cpu for --extra cuda
```

The **`apps` group** declares the `crucible_app` and `echoprover` wheels as editable
path dependencies (`[tool.uv.sources]`), so `uv sync --group apps` **builds them via
maturin and keeps them** — it no longer prunes the wheels the way a bare `uv sync`
did with the old out-of-band `maturin develop`. The group is deliberately outside the
default groups, so the container image (no Rust toolchain) never tries to compile it.

### 1e. Auto-rebuild on Rust changes (one-time hook install)

Install the maturin import hook into the venv **once**:

```bash
python -m maturin_import_hook site install
```

After that, with the venv **activated** (so `maturin` is on `PATH`), editing anything
under `rust/crucible-app` transparently recompiles `crucible_app` on the next `import`
— no manual `maturin` step. As a fallback (e.g. running without an activated venv), you
can still force a rebuild explicitly:

```bash
uv run --no-sync maturin develop --release -m rust/crucible-app/Cargo.toml
```

### 1f. Build the sandbox launcher (`run-confined`)

The default provider confines every external command (Landlock + seccomp) and is
**fail-closed** — it refuses to run if the binary is missing. Build it once:

```bash
cd rust && cargo build -p run-confined --release && cd ..
ls rust/target/release/run-confined   # must exist
```

The provider resolves the binary in this order: `$RUN_CONFINED_BIN` → `PATH` →
the dev build at `rust/target/release/run-confined`. This host demo relies on the
last (dev) fallback, so the build above is all you need.

To demo **without** confinement (trusted-input dev), skip this and set
`COMPOSER_SANDBOX_PROVIDER=none` in step 3.

> **Containerized runs (not this demo).** The base image no longer bundles
> `run-confined` — it is provided by the `scripts/docker-compose.sandbox.yml`
> overlay, which builds the launcher and mounts it read-only at `$RUN_CONFINED_BIN`.
> Because Crucible's default provider is fail-closed, add that overlay to every
> compose invocation for a container that runs Crucible, e.g.
> `docker compose -f scripts/docker-compose.yml -f scripts/docker-compose.sandbox.yml …`.
> See [docs/command-sandbox.md](command-sandbox.md).

### 1g. (Optional) Populate the RAG knowledge base

Improves the model's Crucible-specific grounding. The pipeline falls back to a static
cheat-sheet if it's absent, so you can skip this for a first demo. The corpus ships as a
committed manifest (`rust/crucible-app/crucible_kb.rag.json`) — no crucible checkout needed —
imported by the generic importer under the `ragbuild` dependency group:

```bash
uv sync --extra cpu --extra ml --extra certora-cli --group ragbuild --group apps
uv run --group ragbuild python -m composer.scripts.rag_import \
    rust/crucible-app/crucible_kb.rag.json
```

---

## 2. Start Postgres

The CLI stores conversation memory + LangGraph checkpoints in the composer Postgres:

```bash
docker compose -f scripts/docker-compose.yml up -d
```

---

## 3. (Recommended) Clean the scenario directory

Earlier runs leave large generated dirs (`.sandbox_cargo/`, `target/`, `fuzz/`, …) in
the scenario. They no longer break a run (the source tools exclude them), but a clean
dir builds faster:

```bash
rm -rf test_scenarios/solana_vault/{.sandbox_cargo,.sandbox_tmp,target,corpus,output,fuzz,certora,.certora_internal}
```

---

## 4. Run the demo

```bash
env -u CERTORA_DEV_MODE -u CERTORA -u CERTORA_DISABLE_POPUP -u CERTORAKEY \
    -u CERTORA_DISABLE_AUTO_CACHE -u CERTORA_DISABLE_NOTIFICATION \
    COMPOSER_SANDBOX_PROVIDER=launcher \
    CRUCIBLE_REPO="$CRUCIBLE_REPO" \
  uv run --no-sync console-crucible \
    test_scenarios/solana_vault \
    test_scenarios/solana_vault/programs/vault/src/lib.rs:vault \
    test_scenarios/solana_vault/system.md \
    --max-bug-rounds 1 --fuzz-timeout 30
```

Positional arguments:

1. `project_root` — the scenario root.
2. `main_contract` — `path:ProgramName`. Here the program/crate name is `vault` (the
   crate, **not** the `#[program]` module `vault_program`).
3. `system_doc` — the design document (text or PDF) the analysis reads.

Useful flags (`console-crucible --help` lists all):

- `--max-bug-rounds N` — property-extraction rounds per instruction; `1` for a shorter
  demo, default `3`.
- `--fuzz-timeout SECONDS` — per-property fuzzing budget (default 30).
- `--max-concurrent N` — concurrent agents (default 4).
- `--interactive` — pause to refine extracted properties before formalization.
- `--heavy-model` / `--lite-model` — default `claude-opus-4-6` / `claude-sonnet-4-6`.

The `env -u CERTORA_*` unsets are **required** — those variables otherwise interfere
with the run.

> **First run downloads the embedder.** On the first `get_model()` call the pipeline
> fetches `nomic-ai/nomic-embed-text-v1.5` (~0.5 GB, `trust_remote_code=True`) from
> Hugging Face — one-time, needs internet.

---

## 5. What you'll see

At startup it prints the log paths (`.certora_internal/autoProve/<timestamp>.events.jsonl`
and a text log). Then phases stream to the console:

1. **sBPF build** — `cargo-build-sbf` compiles the program; the harness loads the `.so`.
2. **System Analysis** — the model maps the program's instructions/accounts.
3. **Property extraction** — per instruction (`initialize`, `deposit`, `withdraw`, …).
4. **Build Harness** — the shared-fixture setup session authors `main.rs` + fixture and
   validates it with `crucible run … --dry-run` (labeled e.g. `crucible authoring turn`).
5. **Per-component authoring + fuzzing** — a test per property, gated by `crucible run`.
6. **Report** — a property-keyed report is assembled and written.

Deliverables land under `test_scenarios/solana_vault/certora/crucible/` (per-component
tests, `commentary.md`, the property→tests map) plus the report; a run summary
(components / properties / failures) prints at the end.

---

## 6. Faster smoke check (no LLM, ~1 min)

To confirm the toolchain + sandbox + harness-build path works before a full paid run:

```bash
CRUCIBLE_REPO="$CRUCIBLE_REPO" COMPOSER_SANDBOX_PROVIDER=launcher \
  uv run --no-sync python -m pytest tests/test_crucible_sandbox_gate.py -q
```

---

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `NotImplementedError: Sentence transformers not available` | venv synced without the `ml` extra | `uv sync --extra cpu --extra ml --extra certora-cli --group apps` |
| `ModuleNotFoundError: No module named 'crucible_app'` | synced without the `apps` group | re-sync with `--group apps` (1d) |
| `maturin not found` when a Rust edit should have rebuilt | import hook can't see `maturin` on `PATH` | activate the venv (or use `uv run`) so `.venv/bin` is on `PATH`; re-import |
| `FileNotFoundError: crucible checkout not configured` | `CRUCIBLE_REPO` unset / wrong | point it at a clone containing `crates/crucible-fuzzer` |
| sandbox "provider unavailable" / fail-closed | `run-confined` not found | build it (1f), point `RUN_CONFINED_BIN` at a prebuilt binary, or set `COMPOSER_SANDBOX_PROVIDER=none` |
| Postgres connection errors | DB not up | `docker compose -f scripts/docker-compose.yml up -d` |
| `Failed to spawn: pyright` (only when validating) | `uv sync` dropped the `ci` group | add `--group ci --group test` to the sync (keep `--group apps`) |
| (container) `Crucible toolchain missing: …` | running `console-crucible` on the base image | add `-f scripts/docker-compose.crucible.yml` (§8) |
| (container) `run-confined not found` / fail-closed | sandbox overlay not layered in | add `-f scripts/docker-compose.sandbox.yml` (§8) |
| (container) `pull access denied for autoprover-local` | base image not built | build the base first (§8b) |

> After **any** `uv sync`, re-run the wheel build (1e) — this is the most common
> foot-gun.

---

## 8. Running entirely in the container

Instead of installing the toolchain on the host (§1), you can run the whole vertical
inside a container. The **crucible toolchain image** (`scripts/Dockerfile.crucible`)
bakes one blessed, mutually-compatible combo — host Rust `1.89.0`, Solana platform-tools
`3.1.10`, `anchor` `1.1.2`, and `crucible` `v0.2.0` (checkout + CLI) — on top of the lean
base image, following Option 1 in [crucible-toolchain-versioning.md](./crucible-toolchain-versioning.md).
Programs that need a different combo are out of scope for this image; bump the ARGs in
`scripts/Dockerfile.crucible` and rebuild to re-bless.

The image layers three compose files (base + the sandbox launcher overlay + the crucible
toolchain overlay). Because Crucible's sandbox provider is fail-closed, the
`docker-compose.sandbox.yml` overlay is **required**.

### 8a. Prerequisites

Only Docker and an API key — no host toolchain:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Define the three-file stack once for convenience:

```bash
export COMPOSE="-f scripts/docker-compose.yml \
  -f scripts/docker-compose.sandbox.yml \
  -f scripts/docker-compose.crucible.yml"
```

### 8b. Build (one-time; slow — compiles the Solana/Rust/crucible toolchain)

The crucible image does `FROM autoprover-local:latest`, so build the base first:

```bash
docker compose -f scripts/docker-compose.yml --profile autoprove build   # base
docker compose $COMPOSE --profile autoprove build                        # sandbox + crucible
```

### 8c. Start Postgres + initialize the schema

```bash
docker compose $COMPOSE up -d postgres
docker compose $COMPOSE --profile autoprove run --rm autoprove setup-db
```

### 8d. Run the demo

Mount the host directory holding the scenario as `/work` and pass container paths.
Run as your host UID so outputs aren't root-owned:

```bash
export HOST_UID=$(id -u) HOST_GID=$(id -g) HOST_WORK_DIR="$PWD"
docker compose $COMPOSE --profile autoprove run --rm autoprove \
    console-crucible \
    /work/test_scenarios/solana_vault \
    /work/test_scenarios/solana_vault/programs/vault/src/lib.rs:vault \
    /work/test_scenarios/solana_vault/system.md \
    --max-bug-rounds 1 --fuzz-timeout 30
```

`CRUCIBLE_REPO` and `COMPOSER_SANDBOX_PROVIDER=launcher` are baked into the image, and
`run-confined` is mounted by the sandbox overlay — nothing else to set. The entrypoint
derives the RAG connection from the compose Postgres, so no `--rag-db` is needed.

### 8e. Run the e2e gate in the container

The full-vertical gate ([tests/test_crucible_e2e_gate.py](../tests/test_crucible_e2e_gate.py))
runs in-container against the compose Postgres — no docker-in-docker. Point it at that
DB with `COMPOSER_TEST_PG_URL` (a superuser DSN; the test provisions roles/DBs
idempotently) and run pytest from the image's repo dir:

```bash
docker compose $COMPOSE --profile autoprove run --rm \
    -w /opt/autoprove/AutoProver \
    -e COMPOSER_TEST_PG_URL=postgresql://postgres:postgres_admin_password@postgres:5432/postgres \
    autoprove \
    python -m pytest tests/test_crucible_e2e_gate.py -m expensive -q -s
```

It copies the scenario to a writable temp dir first, so it neither needs a writable
image nor pollutes `test_scenarios/`. Same paid-LLM budget as the host run (§4).

> **Toolchain downloads on first run.** `cargo-build-sbf` fetches its sBPF
> platform-tools into `$HOME/.cache/solana` on first use; the container has network at
> run time (the confined step is offline but the trusted warm step is not), so this
> happens automatically on the first build.
