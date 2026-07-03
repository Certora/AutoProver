"""Static CVL assets shipped into generated projects.

The ``.spec`` files in this directory are packaged data (declared under
``[tool.setuptools.package-data]`` in pyproject.toml) resolved through
``importlib.resources``, so they work both from a source checkout and from an
installed wheel. The pipeline copies them into the project's ``certora/specs``
tree so generated specs can import them like any other summary file.

The CVLMath library ships as two files so the exact tier can be dropped when
it would clash:

* ``CVLMathAbstract.spec`` — the relational ``*Abstract`` tier plus WAD/RAY.
  Standalone; always installed.
* ``CVLMath.spec`` — the exact ``*Summary`` tier (byte-identical to the
  AutoSetup-bundled Math.spec) importing the abstract file. Installed only
  when AutoSetup did NOT already copy Math.spec into the project, since
  importing both would define the ``*Summary`` functions twice and fail CVL
  typechecking.
"""

from importlib.resources import files
from pathlib import Path

from composer.spec.gen_types import CVLResource, SUMMARIES_DIR, under_project
from composer.spec.util import ensure_dir

CVLMATH_SPEC_NAME = "CVLMath.spec"
CVLMATH_ABSTRACT_SPEC_NAME = "CVLMathAbstract.spec"
#: Where the AutoSetup-bundled exact Math.spec lands when the curated summary
#: closure pulls it in (copy_summaries_folder preserves the bundled layout).
#: Its presence is the "exact tier already imported" signal.
AUTOSETUP_MATH_SPEC_PATH = SUMMARIES_DIR / "Math.spec"
#: Canonical (project-root-relative) install locations — next to the AutoSetup /
#: custom summaries so imports look uniform to the spec author.
CVLMATH_PROJECT_PATH = SUMMARIES_DIR / CVLMATH_SPEC_NAME
CVLMATH_ABSTRACT_PROJECT_PATH = SUMMARIES_DIR / CVLMATH_ABSTRACT_SPEC_NAME


def cvlmath_spec_text() -> str:
    """The packaged two-tier CVLMath library source (exact + import of abstract)."""
    return (files(__package__) / CVLMATH_SPEC_NAME).read_text()


def cvlmath_abstract_spec_text() -> str:
    """The packaged standalone relational-abstraction library source."""
    return (files(__package__) / CVLMATH_ABSTRACT_SPEC_NAME).read_text()


def install_cvlmath_resource(project_root: str | Path) -> CVLResource:
    """Copy the packaged CVLMath library into *project_root*'s summaries dir
    (mirroring how ``setup_summaries`` writes ``custom_summaries.spec``) and
    return the :class:`CVLResource` advertising it to the spec author.

    Must run after the AutoSetup phase: it checks whether AutoSetup already
    copied Math.spec into the project. If so, only the abstract tier is
    installed (and advertised) — the exact ``*Summary`` functions are already
    reachable via the AutoSetup summaries import, and installing CVLMath.spec
    too would double-define them. Otherwise the full two-file library is
    installed and CVLMath.spec (which imports the abstract file) is advertised.
    """
    # The abstract tier defines no `*Summary` names, so it can always coexist
    # with whatever summaries AutoSetup emitted.
    abstract_dest = under_project(project_root, CVLMATH_ABSTRACT_PROJECT_PATH)
    ensure_dir(abstract_dest.parent)
    abstract_dest.write_text(cvlmath_abstract_spec_text())

    if under_project(project_root, AUTOSETUP_MATH_SPEC_PATH).exists():
        return CVLResource(
            path=CVLMATH_ABSTRACT_PROJECT_PATH,
            required=False,
            sort="import",
            description=(
                "Relational math abstractions (mulDiv/WAD Abstract variants) "
                "for taming nonlinear arithmetic; the exact *Summary models "
                "already ship with the AutoSetup summaries (Math.spec)"
            ),
        )

    dest = under_project(project_root, CVLMATH_PROJECT_PATH)
    dest.write_text(cvlmath_spec_text())
    return CVLResource(
        path=CVLMATH_PROJECT_PATH,
        required=False,
        sort="import",
        description=(
            "Reusable math abstractions (mulDiv/WAD): exact summaries and "
            "relational Abstract variants for taming nonlinear arithmetic"
        ),
    )
