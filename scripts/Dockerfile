# syntax=docker/dockerfile:1.7
#
# Container for AutoProver's local execution path — `console-autoprove`
# (or `tui-autoprove`) against a bind-mounted host project, with the Certora
# Prover running in the cloud. Also `console-foundry` and, since the CVLR
# backend, `console-solana` / `tui-solana` — which need a Rust toolchain and a
# Solana platform-tools release in the image (see the block below `forge
# --version`), and which additionally require the sandbox overlay, because
# their builds are confined by default and this image ships no launcher.
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

# ---- Stage 1: build the documentation HTML for the RAG DBs ----
FROM --platform=linux/amd64 python:3.12-slim AS docs-builder

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
# Certora/Documentation is a public repo, so clone over HTTPS — no SSH/auth needed.
RUN git clone --depth 1 https://github.com/Certora/Documentation.git docs

RUN python -m venv /venv \
 && /venv/bin/pip install --no-cache-dir -r /build/docs/requirements.txt

# cvl -> the base rag_db (scripts/populate_rag.sh). The Solana manual is deliberately NOT built
# here: it feeds the cvlr_kb corpus, and that manifest is produced in the certora-cvlr-kb repo
# (docs/cvlr-capture-plan.md §8.2) rather than at image build time.
# TODO: Include extended_rag for sanity analyzer
RUN set -eux; \
    mkdir -p /out; \
    for name in cvl solana; do \
        target="/build/docs/docs/$name"; \
        cp /build/docs/conf.py "$target/"; \
        ( cd "$target" && /venv/bin/sphinx-build -M singlehtml . tmp ); \
        cp "$target/tmp/singlehtml/index.html" "/out/$name.html"; \
    done; \
    printf 'Certora/Documentation %s (%s)\n' \
        "$(git -C /build/docs rev-parse HEAD)" \
        "$(git -C /build/docs log -1 --format=%cI)" \
        > /out/PROVENANCE

# ---- Stage 2: final runtime image ----
FROM --platform=linux/amd64 python:3.12-slim AS final

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

# ---- Rust + Solana platform tools, for the CVLR backend (console-solana / tui-solana) ----
#
# Three pieces, in the order a CVLR run reaches them:
#
#   * A host Rust toolchain. The fast tier of the two-tier compile gate is a plain `cargo check`
#     (composer/cargo/session.py), run per authored edit. rustup, not a bare cargo: a target
#     project's own rust-toolchain.toml pins the channel its build uses, and rustup resolves that
#     during the *unconfined* warm step — under confinement RUSTUP_HOME is read-only and the
#     download cannot happen.
#   * `cargo certora-sbf` — Certora's build subcommand, which is what the slow tier runs and what
#     the prover's build_script reruns (composer/cargo/sbf.py).
#   * The platform-tools release that subcommand compiles with, unpacked at build time. This one is
#     not an optimization: docs/cvlr-backend-plan.md §3 item 3 runs every build confined, and a
#     confined build has no network and a read-only tools cache — so a version this image lacks
#     cannot be fetched at run time, it is a rebuild. A project whose base conf pins a different
#     `cargo_tools_version` is that case, and composer.cargo.sbf.PlatformToolsMissing names it.
#
# All three live under $AUTOPROVE_HOME, root-owned and world-readable — deliberately not under
# $HOME, which the final layer makes world-*writable*: a read-only Landlock grant over a tree the
# confined build can also write is not a grant of anything.
ARG SOLANA_TOOLCHAIN=1
ARG RUST_VERSION=1.90.0
ARG CARGO_CERTORA_SBF_VERSION=0.3.5
# Must agree with `cargo_tools_version` in composer/spec/cvlr/conf.py's TEMPLATE_BASE, which is the
# version every run asks for unless the project pins its own. tests/test_cvlr_image.py fails the
# routine gate if the two drift.
ARG SOLANA_TOOLS_VERSION=v1.43
ENV RUSTUP_HOME=/opt/autoprove/rustup \
    CARGO_HOME=/opt/autoprove/cargo \
    CERTORA_PLATFORM_TOOLS_ROOT=/opt/autoprove/solana \
    PATH=/opt/autoprove/venv/bin:/opt/autoprove/cargo/bin:/usr/local/bin:/usr/bin:/bin
# The shared cargo registry is deleted at the end: every confined build fetches into a private,
# per-run CARGO_HOME under its own workdir (composer.sandbox.recipes.sandbox_cargo_home), and only
# $CARGO_HOME/bin is granted read-only — so a warm shared registry would be a gigabyte nothing reads.
RUN set -eux; \
    if [ "$SOLANA_TOOLCHAIN" != "1" ]; then \
      echo "[build] SOLANA_TOOLCHAIN=$SOLANA_TOOLCHAIN: no Rust, no platform tools, no console-solana"; \
      exit 0; \
    fi; \
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
      | sh -s -- -y --no-modify-path --profile minimal --default-toolchain "$RUST_VERSION"; \
    cargo --version; \
    cargo install --locked cargo-certora-sbf --version "$CARGO_CERTORA_SBF_VERSION"; \
    cargo certora-sbf --no-build --no-rustup \
        --platform-tools-root "$CERTORA_PLATFORM_TOOLS_ROOT" \
        --tools-version "$SOLANA_TOOLS_VERSION"; \
    test -d "$CERTORA_PLATFORM_TOOLS_ROOT/$SOLANA_TOOLS_VERSION"; \
    rm -rf "$CARGO_HOME/registry" "$CARGO_HOME/git"

# An empty global git config, which a confined build with a git dependency cannot start without.
# libgit2 reads it before it will open a cached repository at all, and under Landlock the open of a
# path that was never granted fails with EACCES rather than ENOENT — so libgit2 does not degrade to
# "no user config", it reports the repository as unopenable and cargo blames the network
# (composer.sandbox.recipes.git_config_ro_paths). The policy grants this file only if it exists, and
# every Anchor project needs it: composer.spec.cvlr.munge.ANCHOR_FORK patches anchor-lang to a git
# repo.
RUN touch $HOME/.gitconfig

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

# Project layer: copy the rest of the source and install the project itself.
COPY . ./
RUN --mount=type=cache,id=uv-cache,target=/root/.cache/uv \
    uv sync --frozen --no-editable \
      --extra cpu --extra ml --extra "$CERTORA_CLI_PACKAGE" --group ragbuild

# Pre-built documentation HTML — the entrypoint's setup-db reads cvl.html to populate rag_db and
# solana.html to populate the methodology half of cvlr_kb. PROVENANCE names the docs revision both
# came from.
COPY --from=docs-builder /out/cvl.html /out/solana.html /out/PROVENANCE $AUTOPROVE_HOME/prover-docs/

# The rest of cvlr_kb is not baked in. The generated CVLR crate reference and the project-derived
# practice entries are produced in the private certora-cvlr-kb repo and ship in its package, so the
# entrypoint's setup-db finds them through CVLR_KB_REPO or an installed `certora_cvlr_kb`. An image
# without them still has the manual; an image with neither half has no CVLR corpus, which the
# backend reports rather than crashes on.

# The `run-confined` command-sandbox launcher is intentionally NOT baked in here
# (not every AutoProver image needs it). Services that select the `launcher`
# provider opt in via the scripts/docker-compose.sandbox.yml overlay, which
# mounts the binary at $RUN_CONFINED_BIN at runtime — see docs/command-sandbox.md.

# Pre-download the sentence-transformers embedding model so first runs are
# offline-fast. nomic-embed-text-v1.5 ships custom modeling code, hence
# trust_remote_code. Then make the cache world-writable so the HF library can
# drop `.no_exist` marker files at runtime.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('nomic-ai/nomic-embed-text-v1.5', trust_remote_code=True)" \
 && chmod -R go+rwX $HF_HOME

# Trim build-only tooling now that wheels are installed.
#
# `build-essential` survives in a Solana image and is not an oversight: `cargo check` — the fast
# tier, run per authored edit — compiles and *links* the project's build scripts for the host, and
# the SBF build does the same for every proc-macro in the graph. Only the target code goes through
# platform-tools' own LLVM. Without `cc` the first thing a CVLR run does is fail with a linker
# error about a crate the author never wrote.
RUN set -eux; \
    purge="libffi-dev curl gnupg"; \
    [ "$SOLANA_TOOLCHAIN" = "1" ] || purge="$purge build-essential"; \
    apt-get purge -y $purge; \
    apt-get autoremove -y; \
    rm -rf /var/lib/apt/lists/*

# Database host/port assume the compose-managed postgres service; override
# per-run if needed.
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
#
# The three toolchain trees are skipped, and the reason is image size rather than permissions:
# `chmod` makes overlayfs copy a file up into this layer whether or not the mode actually changes,
# so recursing over ~3 GB of Rust and platform tools would store a second copy of all of it.
# rustup and `cargo install` write under umask 022, so those trees are already world-readable.
RUN set -eux; \
    chmod go+rX "$AUTOPROVE_HOME"; \
    for entry in "$AUTOPROVE_HOME"/*; do \
      case "$entry" in \
        "$RUSTUP_HOME"|"$CARGO_HOME"|"$CERTORA_PLATFORM_TOOLS_ROOT") ;; \
        *) chmod -R go+rX "$entry" ;; \
      esac; \
    done; \
    chmod -R go+rwX "$HOME"; \
    chmod 0666 /etc/passwd /etc/group

WORKDIR /work

ENTRYPOINT ["/usr/local/bin/autoprove-entrypoint"]
CMD ["console-autoprove", "--help"]
