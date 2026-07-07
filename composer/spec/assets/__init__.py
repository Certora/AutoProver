"""Static CVL assets shipped into generated projects.

The ``.spec`` files in this directory are packaged data (declared under
``[tool.setuptools.package-data]`` in pyproject.toml) resolved through
``importlib.resources``, so they work both from a source checkout and from an
installed wheel. The pipeline copies them into the project's ``certora/specs``
tree so generated specs can import them like any other summary file.

The CVL math library material comes in two tiers with exactly one source each:

* ``CVLMathAbstract.spec`` (packaged here) — the relational ``*Abstract`` tier
  plus WAD/RAY definitions. Standalone; always installed.
* ``Math.spec`` (packaged by certora_autosetup, NOT here) — the exact
  ``*Summary`` tier. AutoSetup's curated summary closure copies it into
  ``certora/specs/summaries/`` when a matched summary needs it; when it did
  not, the install helper copies the same canonical file to the same path.
  Either way the exact tier exists under one name at one path, so a generated
  spec's ``import "summaries/Math.spec"`` keeps typechecking across runs
  (same-path diamond imports dedupe) and there is no second copy to drift.
"""

from importlib.resources import files
from pathlib import Path

from certora_autosetup.utils.constants import SUMMARIES_SUBDIR

from composer.spec.gen_types import CVLResource, SUMMARIES_DIR, under_project
from composer.spec.util import ensure_dir

CVLMATH_ABSTRACT_SPEC_NAME = "CVLMathAbstract.spec"
MATH_SPEC_NAME = "Math.spec"
#: Canonical (project-root-relative) install locations, next to the AutoSetup /
#: custom summaries so imports look uniform to the spec author. The Math.spec
#: path is the SAME one AutoSetup's copy_summaries_folder uses (it preserves
#: the bundled layout), which is what makes the single-name scheme work: its
#: presence signals "the exact tier is already installed".
MATH_SPEC_PROJECT_PATH = SUMMARIES_DIR / MATH_SPEC_NAME
CVLMATH_ABSTRACT_PROJECT_PATH = SUMMARIES_DIR / CVLMATH_ABSTRACT_SPEC_NAME


def cvlmath_abstract_spec_text() -> str:
    """The packaged standalone relational-abstraction library source."""
    return (files(__package__) / CVLMATH_ABSTRACT_SPEC_NAME).read_text()


def bundled_math_spec_text() -> str:
    """The canonical exact-tier ``Math.spec`` source, read from the
    certora_autosetup package (the single source of the ``*Summary``
    functions; composer deliberately ships no copy of its own).

    Raises if the bundled file is missing/renamed — loud failure is wanted:
    the install helper and AutoSetup's summary closure must agree on this
    file's location for the single-name scheme to hold.
    """
    return (
        files("certora_autosetup")
        .joinpath("certora", *SUMMARIES_SUBDIR.parts, MATH_SPEC_NAME)
        .read_text()
    )


def install_cvlmath_resources(project_root: str | Path) -> list[CVLResource]:
    """Copy the CVL math library into *project_root*'s summaries dir
    (mirroring how ``setup_summaries`` writes ``custom_summaries.spec``) and
    return the :class:`CVLResource` entries advertising it to the spec author.

    Must run after the AutoSetup phase: the exact tier (``Math.spec``) is only
    written — and only advertised as a separate resource — when AutoSetup's
    summary closure did not already place it in the project. When it did, the
    ``*Summary`` functions are already reachable through the required
    AutoSetup summaries import, so a separate advertisement would be
    redundant. The abstract tier defines no ``*Summary`` names, so it always
    coexists with whatever summaries AutoSetup emitted and is always
    installed and advertised.
    """
    abstract_dest = under_project(project_root, CVLMATH_ABSTRACT_PROJECT_PATH)
    ensure_dir(abstract_dest.parent)
    abstract_dest.write_text(cvlmath_abstract_spec_text())
    resources = [
        CVLResource(
            path=CVLMATH_ABSTRACT_PROJECT_PATH,
            required=False,
            sort="import",
            description=(
                "Relational math abstractions (mulDiv/WAD *Abstract variants) "
                "for taming nonlinear arithmetic; constrained via require-based "
                "axioms (no overflow revert modeled), so contradicting "
                "assumptions prune paths silently — prefer for relational "
                "properties, avoid under satisfy"
            ),
        ),
    ]

    math_dest = under_project(project_root, MATH_SPEC_PROJECT_PATH)
    if not math_dest.exists():
        math_dest.write_text(bundled_math_spec_text())
        resources.append(
            CVLResource(
                path=MATH_SPEC_PROJECT_PATH,
                required=False,
                sort="import",
                description=(
                    "Exact math summaries (mulDiv/WAD/sqrt *Summary functions) "
                    "preserving exact rounding and revert behavior — the same "
                    "Math.spec AutoSetup ships with its curated summaries"
                ),
            )
        )
    return resources
