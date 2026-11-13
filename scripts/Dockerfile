# syntax=docker/dockerfile:1.7
#
# Container for AutoProver's local execution path — `console-autoprove`
# (or `tui-autoprove`) against a bind-mounted host project, with the Certora
# Prover running in the cloud.
#
# AutoProver bundles both the `composer` pipeline and `certora_autosetup` in a
# single `ai-composer` distribution, so this image installs them directly — no
# external shim. All Python deps resolve from PyPI plus the vendored `graphcore`
# git submodule, and the docs-builder clones the public Certora Documentation
# repo over HTTPS — so the build needs NO SSH/private auth at all.
#
# Build (BuildKit required):
#   docker compose -f scripts/docker-compose.yml --profile autoprove build
#
# Cloud Certora Prover only — the image does not bundle a local prover build.
# CERTORAKEY (and the CERTORA_* cloud-login vars) must be supplied at runtime.

# Hardened forge from Certora's public foundry fork, built from source and
# copied into the final stage. Used by the autoprove path (`forge remappings`
# during compilation setup) and by console-foundry (`forge test`). The fork adds
# opt-in FFI / external-cheatcode guards that the entrypoint enables for
# foundry-mode test execution. Bump FOUNDRY_SHA to move the pin.
# NOTE: the --platform=linux/amd64 pin means this Rust build runs under QEMU
# emulation on Apple Silicon — the first build is slow (~30-60 min); cached after.
ARG FOUNDRY_REPO=https://github.com/Certora/foundry.git
ARG FOUNDRY_SHA=69cdf8ddc3d19ee85ab3b3c389376e0b07e1179b
FROM --platform=linux/amd64 rust:1-bookworm AS foundry-builder
ARG FOUNDRY_REPO
ARG FOUNDRY_SHA
RUN apt-get update \
 && apt-get install -y --no-install-recommends git libssl-dev pkg-config \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git clone "$FOUNDRY_REPO" . && git checkout "$FOUNDRY_SHA" && git rev-parse HEAD
RUN cargo build --release --bin forge && /src/target/release/forge --version

# ---- Stage 1: the documentation sources the RAG corpora are built from ----
FROM --platform=linux/amd64 python:3.12-slim AS docs-builder

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
# Certora/Documentation and Certora/foundry-book are public repos, so clone over HTTPS — no
# SSH/auth needed.
RUN git clone --depth 1 https://github.com/Certora/Documentation.git docs
RUN git clone --depth 1 https://github.com/Certora/foundry-book.git foundry-book

RUN python -m venv /venv \
 && /venv/bin/pip install --no-cache-dir -r /build/docs/requirements.txt

# cvl -> the CVL corpus; cvl + prover + user-guide -> the extended corpus (sanity analyzer).
RUN set -eux; \
    mkdir -p /out; \
    for doc in cvl prover user-guide; do \
      target=/build/docs/docs/$doc; \
      cp /build/docs/conf.py "$target/"; \
      ( cd "$target" && /venv/bin/sphinx-build -M singlehtml . tmp ); \
      cp "$target/tmp/singlehtml/index.html" "/out/$doc.html"; \
    done; \
    cp -r /build/foundry-book/src/pages/reference/cheatcodes /out/foundry-cheatcodes

# ---- Stage 2: system tools and third-party Python deps, shared by the stages below ----
FROM --platform=linux/amd64 python:3.12-slim AS deps

ENV DEBIAN_FRONTEND=noninteractive \
    AUTOPROVE_HOME=/opt/autoprove \
    VIRTUAL_ENV=/opt/autoprove/venv \
    PATH=/opt/autoprove/venv/bin:/usr/local/bin:/usr/bin:/bin \
    HOME=/opt/autoprove/home \
    HF_HOME=/opt/autoprove/home/.cache/huggingface \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/autoprove/venv

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Shared HOME for runtime caches (HF model, certora credentials, etc.).
RUN mkdir -p $HOME/.cache/huggingface \
 && chmod -R go+rX $HOME

# System deps. build-essential/libffi-dev are transiently present so any pip
# dep that needs a build can compile; curl+gnupg for the Adoptium key;
# postgresql-client provides `psql` for the entrypoint's setup-db; git is
# occasionally shelled out to by tooling. No openssh-client / SSH — the
# Python install pulls from PyPI + the vendored graphcore submodule.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git ca-certificates \
      build-essential libffi-dev \
      curl gnupg \
      postgresql-client \
 && rm -rf /var/lib/apt/lists/*

# Java 21 (Temurin JRE) — certora_cli uses it for local CVL spec syntax
# checking before uploading to the cloud. Debian Bookworm ships only
# openjdk-17 and we need >= 19, so install via the Adoptium apt repo.
RUN mkdir -p /etc/apt/keyrings \
 && curl -fsSL https://packages.adoptium.net/artifactory/api/gpg/key/public \
      | gpg --dearmor -o /etc/apt/keyrings/adoptium.gpg \
 && echo "deb [signed-by=/etc/apt/keyrings/adoptium.gpg] https://packages.adoptium.net/artifactory/deb $(. /etc/os-release && echo $VERSION_CODENAME) main" \
      > /etc/apt/sources.list.d/adoptium.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends temurin-21-jre \
 && rm -rf /var/lib/apt/lists/*

# Solidity compilers, installed as `solcX.Y` (the naming the pipeline expects),
# checksum-verified against the official Solidity binary index. Public HTTPS.
COPY scripts/install_solc.py /tmp/install_solc.py
RUN python3 /tmp/install_solc.py && rm /tmp/install_solc.py

# forge (hardened Certora fork), built from source above. Used by the autoprove
# path (`forge remappings`) and by console-foundry (`forge test`). Root-owned
# 0755, executable by the host-UID runtime user; the fork's FFI / external-
# cheatcode guards are enabled per-mode by the entrypoint.
COPY --from=foundry-builder /src/target/release/forge /usr/local/bin/forge
RUN forge --version

WORKDIR $AUTOPROVE_HOME/AutoProver

# Certora CLI package (release channel), selected at build time. certora-cli
# (stable), certora-cli-beta, and certora-cli-beta-mirror install the same
# `certora_cli` module and are mutually exclusive, so the choice is baked into
# the venv — this image is therefore package-specific. The value is the extra
# name, passed straight to `uv sync --extra`.
ARG CERTORA_CLI_PACKAGE=certora-cli

# Dependency layer: lockfile-driven install of all third-party deps WITHOUT the
# local project. Cached across rebuilds whenever pyproject.toml/uv.lock/graphcore
# don't change. `graphcore` is the vendored submodule the [tool.uv.sources] path
# source resolves to; copy it so `uv sync` can build the env offline-of-private.
COPY pyproject.toml uv.lock ./
COPY graphcore/ ./graphcore/
RUN --mount=type=cache,id=uv-cache,target=/root/.cache/uv \
    case "$CERTORA_CLI_PACKAGE" in \
      certora-cli|certora-cli-beta|certora-cli-beta-mirror) ;; \
      *) echo "Unknown CERTORA_CLI_PACKAGE: '$CERTORA_CLI_PACKAGE' (expected certora-cli|certora-cli-beta|certora-cli-beta-mirror)" >&2; exit 1 ;; \
    esac; \
    uv sync --frozen --no-install-project --no-editable \
      --extra cpu --extra ml --extra "$CERTORA_CLI_PACKAGE" --group ragbuild

# Pre-download the sentence-transformers embedding model: the corpus build below embeds with it,
# and a run embeds its queries with it, offline. nomic-embed-text-v1.5 ships custom modeling code,
# hence trust_remote_code. Then make the cache world-writable so the HF library can drop
# `.no_exist` marker files at runtime.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('nomic-ai/nomic-embed-text-v1.5', trust_remote_code=True)" \
 && chmod -R go+rwX $HF_HOME

# ---- Stage 3: the RAG corpora, one SQLite file each ----
# Only the builders' own sources are copied, so this stage (and its minutes of embedding) is
# rebuilt when the docs or the corpus code change, not on every source change.
FROM deps AS rag-builder
WORKDIR /build/src
COPY composer/__init__.py composer/
COPY composer/rag/ composer/rag/
COPY composer/scripts/__init__.py composer/scripts/ragbuild.py composer/scripts/foundry_ragbuild.py \
     composer/scripts/foundry_process.py composer/scripts/text_processors.py composer/scripts/
COPY --from=docs-builder /out/ /build/docs/
ENV PYTHONPATH=/build/src
# foundry_ragbuild gets every page in one invocation: a corpus file is rebuilt from scratch on
# each run, so splitting the pages across runs would keep only the last batch.
RUN set -eux; \
    python -m composer.scripts.ragbuild --output /out/rag/cvl.sqlite /build/docs/cvl.html; \
    python -m composer.scripts.ragbuild --output /out/rag/extended.sqlite \
        /build/docs/cvl.html /build/docs/prover.html /build/docs/user-guide.html; \
    python -m composer.scripts.foundry_process /build/docs/foundry-cheatcodes /build/foundry-html; \
    bash -c 'shopt -s globstar nullglob; pages=(/build/foundry-html/**/*.html); \
             (( ${#pages[@]} > 0 )) || { echo "no foundry cheatcode pages" >&2; exit 1; }; \
             python -m composer.scripts.foundry_ragbuild --output /out/rag/foundry.sqlite "${pages[@]}"'; \
    ls -la /out/rag

# ---- Stage 4: final runtime image ----
FROM deps AS final

# Project layer: copy the rest of the source and install the project itself.
COPY . ./
RUN --mount=type=cache,id=uv-cache,target=/root/.cache/uv \
    uv sync --frozen --no-editable \
      --extra cpu --extra ml --extra "$CERTORA_CLI_PACKAGE" --group ragbuild

# The RAG corpora, opened by default from CERTORA_AI_COMPOSER_RAG_DIR.
COPY --from=rag-builder /out/rag/ $AUTOPROVE_HOME/rag/
ENV CERTORA_AI_COMPOSER_RAG_DIR=$AUTOPROVE_HOME/rag

# The `run-confined` command-sandbox launcher is intentionally NOT baked in here
# (not every AutoProver image needs it). Services that select the `launcher`
# provider opt in via the scripts/docker-compose.sandbox.yml overlay, which
# mounts the binary at $RUN_CONFINED_BIN at runtime — see docs/command-sandbox.md.

# Trim build-only tooling now that wheels are installed.
RUN apt-get purge -y build-essential libffi-dev curl gnupg \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

# Database host/port (checkpoints, store, memory) assume the compose-managed postgres service;
# override per-run if needed.
ENV CERTORA_AI_COMPOSER_PGHOST=postgres \
    CERTORA_AI_COMPOSER_PGPORT=5432

COPY scripts/autoprove-entrypoint.sh /usr/local/bin/autoprove-entrypoint
RUN chmod +x /usr/local/bin/autoprove-entrypoint

# Final perms: make everything under /opt/autoprove world-readable so the
# runtime user (host UID supplied by compose) can read source, venv, and HF
# cache; make $HOME world-writable so certora can persist credentials; and make
# /etc/passwd + /etc/group world-writable so the entrypoint can append a
# synthetic passwd entry for whatever host UID the container runs as — without
# it, libraries that call pwd.getpwuid() (e.g. torch via getpass.getuser())
# crash with `KeyError: 'getpwuid()'`. Done LAST because earlier apt operations
# (notably the Temurin install) rewrite /etc/group and reset its perms.
RUN chmod -R go+rX $AUTOPROVE_HOME \
 && chmod -R go+rwX $HOME \
 && chmod 0666 /etc/passwd /etc/group

WORKDIR /work

ENTRYPOINT ["/usr/local/bin/autoprove-entrypoint"]
CMD ["console-autoprove", "--help"]
