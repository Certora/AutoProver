"""The mechanical steps of the ``certora-autoprover-solana`` skill, for ``certora-cvlr``.

The skill (``docs/cvlr-skill-plan.md``) has the user's own coding agent write the properties and the
rules. What is mechanical, and quietly wrong when done by hand, comes from here: the same preflight,
scaffold and module declarations the pipeline runs, on the user's own working tree rather than a
run's copy.

Builds are unconfined unless ``COMPOSER_SANDBOX_PROVIDER`` names a provider. The pipeline confines
by default because it compiles a project nobody at the keyboard chose to build; here the user's
agent is already building their project with their own permissions.
"""

import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from composer.cargo.features import CargoFeature
from composer.kb.kb_context import (
    CVLR_BUNDLE,
    CVLR_RECIPES,
    recipe_bodies,
    recipe_index_document,
)
from composer.sandbox.config import SandboxConfig
from composer.spec.cvlr.author import render_author_system, render_judge_system
from composer.spec.cvlr.conf import TunableConf, tunable_conf
from composer.spec.cvlr.guidance import SOLANA_CVLR_GUIDANCE
from composer.spec.cvlr.harness import SHARED_INVARIANTS, HarnessModule, declare_modules
from composer.spec.cvlr.preflight import (
    CvlrPreflight,
    gate_workspace,
    prepare_workspace,
    select_package,
)
from composer.spec.cvlr.reference import generations
from composer.spec.cvlr.scaffold import SPECS_DIR, declare_unit_features
from composer.spec.gen_types import CVLR_INTERNAL_DIR
from composer.spec.util import slugify_rust_module
from composer.templates.loader import load_jinja_template

#: Where :func:`write_reference` puts the reference text, relative to the project root. Under the
#: internal directory the scaffold already has git ignore.
REFERENCE_DIR = CVLR_INTERNAL_DIR / "reference"

#: The chain whose reference sets a skill project is checked against.
_CHAIN = "solana"


class ComponentNameRejected(ValueError):
    """A component name the harness cannot use as a module name as given."""


def component_modules(names: Sequence[str]) -> list[HarnessModule]:
    """One harness module per component, named exactly as given.

    A name that is not already a module name is refused with the one it would become, rather than
    renamed: the agent writes the file, so it has to know the name."""
    problems: list[str] = []
    for name in names:
        if (slug := slugify_rust_module(name)) != name:
            problems.append(f"{name!r} is not a Rust module name; use {slug!r}")
        elif name == SHARED_INVARIANTS:
            problems.append(f"{name!r} is the shared invariants module; name the component otherwise")
    if dupes := sorted({n for n in names if names.count(n) > 1}):
        problems.append(f"named more than once: {', '.join(dupes)}")
    if problems:
        raise ComponentNameRejected("; ".join(problems))
    return [HarnessModule(name) for name in names]


@dataclass(frozen=True)
class SetupResult:
    preflight: CvlrPreflight
    #: The package directory, absolute.
    package_root: Path
    modules: list[HarnessModule]
    #: Module files this setup created empty. An existing file is never touched.
    created: tuple[Path, ...]
    added_features: tuple[CargoFeature, ...]
    reference_dir: Path
    #: Whether the scaffolded package was compiled with the shared module in.
    checked: bool

    @property
    def specs_dir(self) -> Path:
        return self.package_root / SPECS_DIR


_STUBS = {
    SHARED_INVARIANTS: "//! Structural invariants, proved once and assumed by every component's rules.\n",
}


def _create_absent(specs_dir: Path, modules: Sequence[HarnessModule]) -> tuple[Path, ...]:
    created: list[Path] = []
    for module in [SHARED_INVARIANTS, *(m.module for m in modules)]:
        path = specs_dir / f"{module}.rs"
        if not path.exists():
            path.write_text(_STUBS.get(module, f"//! The rules for the `{module}` component.\n"))
            created.append(path)
    return tuple(created)


async def setup(
    project_root: Path,
    *,
    package: str | None,
    components: Sequence[str],
    sandbox: SandboxConfig,
    check: bool = True,
) -> SetupResult:
    """Scaffold ``project_root`` for CVLR, declare the shared module and one module per component,
    write the reference text, and compile the result unless ``check`` is off.

    ``components`` replaces the modules an earlier setup declared; a module file already written is
    kept. Raises :class:`~composer.spec.cvlr.preflight.PreflightFailed` when the project is refused,
    and :class:`ComponentNameRejected` before anything is written."""
    modules = component_modules(components)
    with tempfile.TemporaryDirectory(prefix="cvlr-select-") as select_home:
        selected = await select_package(
            project_root,
            package,
            candidates=generations(_CHAIN),
            cargo_home=Path(select_home) if sandbox.enabled else None,
        )
    pre = await prepare_workspace(
        project_root, reference=selected.reference, sandbox=sandbox, package=selected.name
    )
    package_root = pre.workspace_root / pre.package_dir
    specs_dir = package_root / SPECS_DIR
    declare_modules(specs_dir, modules, shared=(SHARED_INVARIANTS,))
    created = _create_absent(specs_dir, modules)
    added = declare_unit_features(package_root / "Cargo.toml", [m.feature for m in modules])
    reference_dir = project_root / REFERENCE_DIR
    write_reference(
        reference_dir,
        package=pre.package,
        cvlr_versions=", ".join(f"{c.name} {c.version}" for c in pre.sources.crates),
        caveats=selected.reference.caveats,
    )
    if check:
        await gate_workspace(pre, sandbox=sandbox)
    return SetupResult(
        preflight=pre,
        package_root=package_root,
        modules=modules,
        created=created,
        added_features=added,
        reference_dir=reference_dir,
        checked=check,
    )


def reference_documents(
    *, package: str, cvlr_versions: str, caveats: Sequence[str]
) -> dict[Path, str]:
    """The reference text for a project on ``cvlr_versions``, by path under the reference
    directory."""
    (facts,) = CVLR_BUNDLE.specs
    docs = {
        Path("README.md"): load_jinja_template(
            "cvlr/skill_reference.j2", package=package, cvlr_versions=cvlr_versions
        ),
        Path("cvlr.md"): facts.loader(),
        Path("properties.md"): SOLANA_CVLR_GUIDANCE,
        Path("author.md"): render_author_system(
            {
                "module": "<module>",
                "cvlr_versions": cvlr_versions,
                "caveats": list(caveats),
                "example": None,
                "conf": tunable_conf(TunableConf()),
            }
        ),
        Path("judge.md"): render_judge_system(
            {"cvlr_versions": cvlr_versions, "caveats": list(caveats)}
        ),
        Path("invariants.md"): load_jinja_template("cvlr_invariant_guidance.j2"),
        Path("recipes") / "index.md": recipe_index_document(CVLR_BUNDLE.label, CVLR_RECIPES),
    }
    docs.update(
        {Path("recipes") / f"{id}.md": text for id, text in recipe_bodies(CVLR_RECIPES).items()}
    )
    return docs


def write_reference(
    directory: Path, *, package: str, cvlr_versions: str, caveats: Sequence[str]
) -> None:
    """Replace ``directory`` with :func:`reference_documents`, so nothing from an older version
    is left beside them."""
    if directory.exists():
        shutil.rmtree(directory)
    documents = reference_documents(package=package, cvlr_versions=cvlr_versions, caveats=caveats)
    for relative, text in documents.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
