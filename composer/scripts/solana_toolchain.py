"""Install what a CVLR run builds with into a container image.

Every image that runs the Solana AutoProver runs this, from its Dockerfile, one layer per step:
``scripts/Dockerfile.solana`` here, and the cloud image's Solana stage in AIAutoProver. The versions
are read from the code that uses them, so an image built by any Dockerfile agrees with the
``composer`` installed in it, and no Dockerfile states a version of its own.

``toolchain``
    The host Rust toolchain (:data:`~composer.spec.cvlr.conf.HOST_RUST_TOOLCHAIN`), ``cargo
    certora-sbf``, and the platform-tools release every build uses
    (:data:`~composer.spec.cvlr.conf.PLATFORM_TOOLS_VERSION`), unpacked. A confined build has no
    network and a read-only tools cache, so a release the image lacks is a rebuild, not a fetch.
``api-manifest``
    The ``cvlr_api_kb`` manifest, generated with the nightly whose rustdoc JSON
    :mod:`composer.scripts.cvlr_api_docs` parses. The nightly is removed afterwards.
``gitconfig``
    An empty ``~/.gitconfig``. libgit2 reads it before it opens a cached repository, and under
    Landlock an ungranted path fails with EACCES, which cargo reports as a network error for any
    git dependency (:func:`composer.sandbox.recipes.git_config_ro_paths`).

The Dockerfile sets ``RUSTUP_HOME``, ``CARGO_HOME`` and ``CERTORA_PLATFORM_TOOLS_ROOT`` as ``ENV``
before running a step. They are read again at run time by the sandbox policy, so they cannot be set
here. They should be root-owned and outside ``$HOME``: a read-only grant over a tree the confined
build can also write grants nothing.

Each step deletes the shared cargo registry it filled. A confined build fetches into a private
per-run ``CARGO_HOME``, so a warm shared registry would be a gigabyte nothing reads.
"""

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from composer.cargo.sbf import SBF_SUBCOMMAND, SBF_SUBCOMMAND_VERSION
from composer.scripts.cvlr_api_docs import PINNED_NIGHTLY
from composer.spec.cvlr.conf import HOST_RUST_TOOLCHAIN, PLATFORM_TOOLS_VERSION

#: What the Dockerfile must set before a step runs, and why each one matters at run time.
REQUIRED_ENV = ("RUSTUP_HOME", "CARGO_HOME", "CERTORA_PLATFORM_TOOLS_ROOT")


@dataclass(frozen=True)
class Run:
    argv: tuple[str, ...]


@dataclass(frozen=True)
class RequireDir:
    path: Path


@dataclass(frozen=True)
class RemoveTree:
    path: Path


@dataclass(frozen=True)
class Touch:
    path: Path


type Step = Run | RequireDir | RemoveTree | Touch


def _registry(cargo_home: Path) -> list[Step]:
    return [RemoveTree(cargo_home / "registry"), RemoveTree(cargo_home / "git")]


def toolchain_steps(env: Mapping[str, str]) -> list[Step]:
    cargo_home = Path(env["CARGO_HOME"])
    tools_root = Path(env["CERTORA_PLATFORM_TOOLS_ROOT"])
    return [
        Run((
            "sh", "-c",
            "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y "
            f"--no-modify-path --profile minimal --default-toolchain {HOST_RUST_TOOLCHAIN}",
        )),
        Run(("cargo", "--version")),
        Run((
            "cargo", "install", "--locked", f"cargo-{SBF_SUBCOMMAND}",
            "--version", SBF_SUBCOMMAND_VERSION,
        )),
        Run((
            "cargo", SBF_SUBCOMMAND, "--no-build", "--no-rustup",
            "--platform-tools-root", str(tools_root), "--tools-version", PLATFORM_TOOLS_VERSION,
        )),
        RequireDir(tools_root / PLATFORM_TOOLS_VERSION),
        *_registry(cargo_home),
    ]


def api_manifest_steps(env: Mapping[str, str], output: Path) -> list[Step]:
    return [
        Run(("rustup", "toolchain", "install", PINNED_NIGHTLY, "--profile", "minimal")),
        Run((
            sys.executable, "-m", "composer.scripts.cvlr_api_docs", "--chain", "solana",
            "--toolchain", PINNED_NIGHTLY, "--fetch", "--output", str(output),
        )),
        Run(("rustup", "toolchain", "uninstall", PINNED_NIGHTLY)),
        *_registry(Path(env["CARGO_HOME"])),
    ]


def gitconfig_steps(env: Mapping[str, str]) -> list[Step]:
    return [Touch(Path(env["HOME"]) / ".gitconfig")]


def execute(steps: list[Step]) -> None:
    for step in steps:
        match step:
            case Run(argv=argv):
                print("+", " ".join(argv), flush=True)
                subprocess.run(argv, check=True)
            case RequireDir(path=path):
                if not path.is_dir():
                    raise SystemExit(f"expected {path} after the steps above")
            case RemoveTree(path=path):
                shutil.rmtree(path, ignore_errors=True)
            case Touch(path=path):
                path.touch()


def _print_versions() -> None:
    print(f"host Rust toolchain {HOST_RUST_TOOLCHAIN}")
    print(f"cargo-{SBF_SUBCOMMAND} {SBF_SUBCOMMAND_VERSION}")
    print(f"platform tools {PLATFORM_TOOLS_VERSION}")
    print(f"rustdoc nightly {PINNED_NIGHTLY}")


def _image_env() -> Mapping[str, str]:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        raise SystemExit(
            f"set {', '.join(missing)} as ENV in the Dockerfile first: the sandbox policy reads "
            "them at run time"
        )
    return os.environ


def _api_manifest(output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    execute(api_manifest_steps(_image_env(), output))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Install a CVLR run's toolchain into an image, one step per Dockerfile layer."
    )
    steps = parser.add_subparsers(required=True)
    steps.add_parser(
        "toolchain", help="host Rust, cargo certora-sbf, the platform tools"
    ).set_defaults(run=lambda _: execute(toolchain_steps(_image_env())))
    manifest = steps.add_parser("api-manifest", help="generate the cvlr_api_kb manifest")
    manifest.add_argument("--output", type=Path, required=True)
    manifest.set_defaults(run=lambda args: _api_manifest(args.output))
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
