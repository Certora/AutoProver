"""Static CVL assets shipped into generated projects.

The ``.spec`` files in this directory are packaged data (declared under
``[tool.setuptools.package-data]`` in pyproject.toml) resolved through
``importlib.resources``, so they work both from a source checkout and from an
installed wheel. The pipeline copies them into the project's ``certora/specs``
tree so generated specs can import them like any other summary file.
"""

from importlib.resources import files
from pathlib import Path

from composer.spec.gen_types import CVLResource, SUMMARIES_DIR, under_project
from composer.spec.util import ensure_dir

CVLMATH_SPEC_NAME = "CVLMath.spec"
#: Canonical (project-root-relative) install location — next to the AutoSetup /
#: custom summaries so imports look uniform to the spec author.
CVLMATH_PROJECT_PATH = SUMMARIES_DIR / CVLMATH_SPEC_NAME


def cvlmath_spec_text() -> str:
    """The packaged CVLMath library source."""
    return (files(__package__) / CVLMATH_SPEC_NAME).read_text()


def install_cvlmath_resource(project_root: str | Path) -> CVLResource:
    """Copy the packaged CVLMath library into *project_root*'s summaries dir
    (mirroring how ``setup_summaries`` writes ``custom_summaries.spec``) and
    return the :class:`CVLResource` advertising it to the spec author.
    """
    dest = under_project(project_root, CVLMATH_PROJECT_PATH)
    ensure_dir(dest.parent)
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
