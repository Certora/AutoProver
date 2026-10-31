"""The workspace contracts a Soroban contract imports, built before it so it compiles.

``soroban_sdk::contractimport!(file = "../../target/wasm32-unknown-unknown/release/token.wasm")``
generates a client from another contract's built wasm, at compile time. A project builds the
imported contract first (Phoenix's Makefile does), so a crate that imports one does not compile on
a fresh checkout, and neither the gate's check nor a submission's build would get past it.

:func:`contract_imports` reads, once in preflight, which workspace members the verified package
imports, transitively and in build order. :func:`build_imports` builds them in a cargo session and
copies each artifact to the path its import names. The macro reads only the contract's interface
from the wasm, so the ``wasm32v1-none`` build this backend makes stands in for whatever target the
project's path names. A path no member builds — a wasm checked into the repository — is left alone.

Test sources are not read: an import under ``tests/`` or in a ``test.rs`` is compiled only by
``cargo test``.
"""

import dataclasses
import re
import shutil
from pathlib import Path

from composer.cargo.metadata import CratePackage, Workspace
from composer.cargo.session import CargoSession
from composer.cargo.wasm import WasmBuild, WasmIdentity, wasm_build
from composer.spec.cvlr.soroban_types import is_test_source

_IMPORT = re.compile(r"contractimport!\s*\(\s*file\s*=\s*\"([^\"]+\.wasm)\"")


@dataclasses.dataclass(frozen=True)
class ContractImport:
    """One imported workspace contract. Paths are relative to the workspace root, so the same
    import is built in the project and in a copy of it."""

    member_dir: Path
    artifact_stem: str
    #: Where the importing crate's ``contractimport!`` reads it.
    expected: Path


def _imported_paths(package: CratePackage) -> list[Path]:
    """The wasm files ``package``'s non-test sources import, resolved against its manifest
    directory, which is what ``contractimport!`` resolves them against."""
    src = package.root / "src"
    found: list[Path] = []
    for path in sorted(src.rglob("*.rs")):
        if is_test_source(path, src):
            continue
        for m in _IMPORT.finditer(path.read_text(errors="replace")):
            found.append((package.root / m.group(1)).resolve())
    return list(dict.fromkeys(found))


def contract_imports(workspace: Workspace, package: CratePackage) -> tuple[ContractImport, ...]:
    """The workspace members ``package`` imports, transitively, each after the ones it imports.

    A cycle cannot build in any order, so a member already being visited is not visited again; the
    compiler then reports the missing file, which says more than anything this could.
    """
    by_stem = {
        m.lib.artifact_stem: m for m in workspace.members if m.lib is not None and m is not package
    }
    root = workspace.root.resolve()
    order: list[ContractImport] = []
    done: set[str] = set()
    visiting: set[str] = set()

    def visit(importer: CratePackage) -> None:
        for path in _imported_paths(importer):
            member = by_stem.get(path.stem)
            if member is None or member.lib is None or path.stem in done | visiting:
                continue
            try:
                expected = path.relative_to(root)
            except ValueError:
                continue
            visiting.add(path.stem)
            visit(member)
            visiting.discard(path.stem)
            done.add(path.stem)
            order.append(
                ContractImport(
                    member_dir=member.root.resolve().relative_to(root),
                    artifact_stem=member.lib.artifact_stem,
                    expected=expected,
                )
            )

    visit(package)
    return tuple(order)


async def build_imports(
    session: CargoSession, imports: tuple[ContractImport, ...], *, target_directory: Path
) -> str | None:
    """Build each import in ``session``'s workdir and put it where its importer reads it.

    Returns why the first failing one failed, or ``None``. ``target_directory`` is where cargo puts
    this session's builds.
    """
    workdir = session.workdir
    for imp in imports:
        build = WasmBuild(
            manifest_path=workdir / imp.member_dir / "Cargo.toml",
            identity=WasmIdentity(workdir, imp.member_dir, target_directory, imp.artifact_stem),
        )
        run = await wasm_build(session, build)
        if not run.ok:
            return (
                f"building {imp.artifact_stem}, which the verified contract imports, failed:\n"
                f"{run.verdict.diagnostics}"  # type: ignore[union-attr]
            )
        artifact = run.verdict.manifest.artifact  # type: ignore[union-attr]
        expected = workdir / imp.expected
        expected.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(artifact, expected)
    return None
