"""Install what a Soroban CVLR run builds with into a container image.

The Soroban counterpart of :mod:`composer.scripts.solana_toolchain`, run the same way, one layer per
step: by ``scripts/Dockerfile.soroban`` here, and by the cloud image's Soroban stage in AIAutoProver.
The versions are read from the code that uses them, so an image built by either Dockerfile agrees
with the ``composer`` installed in it, and no Dockerfile states a version of its own.

``toolchain``
    Two rustup toolchains, and nothing of Solana's. The host toolchain
    (:data:`~composer.spec.cvlr.conf.HOST_RUST_TOOLCHAIN`) is the one the run reads the project's
    cargo metadata with, before any build. The Soroban toolchain
    (:data:`~composer.spec.cvlr.conf.SOROBAN_RUST_TOOLCHAIN`), with the
    :data:`~composer.cargo.wasm.WASM_TARGET` standard library, is the one every check and build
    of the contract uses (:func:`composer.spec.cvlr.preflight.session_settings`), the contracts it
    imports included. A confined build has no network and a read-only ``RUSTUP_HOME``, so a
    toolchain or target the image lacks is a rebuild, not a fetch.
``gitconfig``
    An empty ``~/.gitconfig``, for the reason :mod:`composer.scripts.solana_toolchain` gives.

No ``rustfilt``: it serves the summary directives ``verify_rules`` checks, and a Soroban unit binds
none. No ``cvlr_api_kb`` manifest either: the corpus is one table shared with Solana, so a Soroban
manifest would replace the Solana one wherever both images seed the same database.

The Dockerfile sets ``RUSTUP_HOME`` and ``CARGO_HOME`` as ``ENV`` before running a step, root-owned
and outside ``$HOME``, for the reasons :mod:`composer.scripts.solana_toolchain` gives. Each step
deletes the shared cargo registry it filled.
"""

import argparse
import os
from collections.abc import Mapping
from pathlib import Path

from composer.cargo.wasm import WASM_TARGET
from composer.scripts.solana_toolchain import (
    RemoveTree,
    Run,
    Step,
    execute,
    gitconfig_steps,
)
from composer.spec.cvlr.conf import HOST_RUST_TOOLCHAIN, SOROBAN_RUST_TOOLCHAIN

#: What the Dockerfile must set before a step runs. The sandbox policy reads both again at run time.
REQUIRED_ENV = ("RUSTUP_HOME", "CARGO_HOME")


def toolchain_steps(env: Mapping[str, str]) -> list[Step]:
    cargo_home = Path(env["CARGO_HOME"])
    return [
        Run((
            "sh", "-c",
            "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y "
            f"--no-modify-path --profile minimal --default-toolchain {HOST_RUST_TOOLCHAIN}",
        )),
        Run((
            "rustup", "toolchain", "install", SOROBAN_RUST_TOOLCHAIN,
            "--profile", "minimal", "--target", WASM_TARGET,
        )),
        Run(("cargo", "--version")),
        Run(("cargo", f"+{SOROBAN_RUST_TOOLCHAIN}", "--version")),
        RemoveTree(cargo_home / "registry"),
        RemoveTree(cargo_home / "git"),
    ]


def _print_versions() -> None:
    print(f"host Rust toolchain {HOST_RUST_TOOLCHAIN}")
    print(f"Soroban Rust toolchain {SOROBAN_RUST_TOOLCHAIN}, target {WASM_TARGET}")


def _image_env() -> Mapping[str, str]:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        raise SystemExit(
            f"set {', '.join(missing)} as ENV in the Dockerfile first: the sandbox policy reads "
            "them at run time"
        )
    return os.environ


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Install a Soroban CVLR run's toolchain into an image, one step per Dockerfile "
        "layer."
    )
    steps = parser.add_subparsers(required=True)
    steps.add_parser(
        "toolchain", help=f"the host Rust toolchain, and the Soroban one with {WASM_TARGET}"
    ).set_defaults(run=lambda _: execute(toolchain_steps(_image_env())))
    steps.add_parser(
        "gitconfig", help="an empty ~/.gitconfig for confined git dependencies"
    ).set_defaults(run=lambda _: execute(gitconfig_steps(_image_env())))
    steps.add_parser(
        "versions", help="print the versions the steps install"
    ).set_defaults(run=lambda _: _print_versions())
    args = parser.parse_args(argv)
    args.run(args)


if __name__ == "__main__":
    main()
