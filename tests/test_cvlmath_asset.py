"""Tests for the packaged CVLMath assets and their pipeline resource wiring.

The assets must be resolvable through ``importlib.resources`` (so they survive
being installed as a wheel, not just run from a source checkout), the install
helper must write the project copies exactly where the pipeline advertises
them — dropping the exact tier when AutoSetup already copied Math.spec — and
the shipped CVL must parse with the same syntax checker the authoring tools
use.
"""

import shutil
import subprocess

import pytest

from composer.spec.assets import (
    AUTOSETUP_MATH_SPEC_PATH,
    CVLMATH_ABSTRACT_PROJECT_PATH,
    CVLMATH_ABSTRACT_SPEC_NAME,
    CVLMATH_PROJECT_PATH,
    CVLMATH_SPEC_NAME,
    cvlmath_abstract_spec_text,
    cvlmath_spec_text,
    install_cvlmath_resource,
)


def test_abstract_asset_is_standalone_relational_tier():
    text = cvlmath_abstract_spec_text()
    # The relational tier plus the WAD/RAY definitions live here...
    assert "mulDivDownAbstract" in text
    assert "mulDivUpAbstract" in text
    assert "definition WAD()" in text
    assert "definition RAY()" in text
    # ...and no `*Summary` definition, so it can coexist with Math.spec.
    assert "Summary(" not in text


def test_cvlmath_asset_is_exact_tier_importing_abstract():
    text = cvlmath_spec_text()
    # Exact tier defined here (byte-identical to the AutoSetup Math.spec)...
    assert "mulDivDownSummary" in text
    assert "mulDivUpSummary" in text
    # ...with the relational tier pulled in by import, not redefined.
    assert f'import "{CVLMATH_ABSTRACT_SPEC_NAME}";' in text
    assert "function mulDivDownAbstract" not in text
    assert "definition WAD()" not in text


def test_cvlmath_exact_tier_matches_bundled_math_spec():
    """Guards the double-definition rationale: the install helper skips
    CVLMath.spec exactly because its exact tier duplicates the AutoSetup
    Math.spec definitions."""
    from pathlib import Path

    import certora_autosetup
    from certora_autosetup.utils.constants import SUMMARIES_SUBDIR

    bundled = (
        Path(certora_autosetup.__file__).parent / "certora" / SUMMARIES_SUBDIR / "Math.spec"
    )
    if not bundled.exists():
        pytest.skip(f"bundled Math.spec not found at {bundled}")
    assert bundled.read_text().strip() in cvlmath_spec_text()


def test_install_without_math_spec_ships_both_tiers(tmp_path):
    res = install_cvlmath_resource(tmp_path)
    # The resource path is canonical (project-root-relative) and optional —
    # this is the CVLResource the pipeline appends in stream_autosetup().
    assert res.path == CVLMATH_PROJECT_PATH
    assert res.sort == "import"
    assert not res.required
    assert (tmp_path / CVLMATH_PROJECT_PATH).read_text() == cvlmath_spec_text()
    # The abstract tier lands next to it so the relative import resolves.
    assert (
        tmp_path / CVLMATH_ABSTRACT_PROJECT_PATH
    ).read_text() == cvlmath_abstract_spec_text()


def test_install_with_math_spec_ships_abstract_tier_only(tmp_path):
    math_spec = tmp_path / AUTOSETUP_MATH_SPEC_PATH
    math_spec.parent.mkdir(parents=True)
    math_spec.write_text("// AutoSetup-copied exact summaries\n")
    res = install_cvlmath_resource(tmp_path)
    assert res.path == CVLMATH_ABSTRACT_PROJECT_PATH
    assert res.sort == "import"
    assert not res.required
    assert (
        tmp_path / CVLMATH_ABSTRACT_PROJECT_PATH
    ).read_text() == cvlmath_abstract_spec_text()
    # CVLMath.spec would double-define the *Summary functions — must not exist.
    assert not (tmp_path / CVLMATH_PROJECT_PATH).exists()


def test_install_with_math_spec_removes_stale_cvlmath(tmp_path):
    """Multi-run edge: run 1 installs the full library (no Math.spec yet),
    then a later run's AutoSetup closure pulls Math.spec in — the leftover
    CVLMath.spec must be removed or a cached generated spec importing it
    would double-define the *Summary functions."""
    first = install_cvlmath_resource(tmp_path)
    assert first.path == CVLMATH_PROJECT_PATH
    math_spec = tmp_path / AUTOSETUP_MATH_SPEC_PATH
    math_spec.write_text("// AutoSetup-copied exact summaries\n")
    res = install_cvlmath_resource(tmp_path)
    assert res.path == CVLMATH_ABSTRACT_PROJECT_PATH
    assert not (tmp_path / CVLMATH_PROJECT_PATH).exists()
    # The abstract tier is still (re)installed for the aggregator import.
    assert (
        tmp_path / CVLMATH_ABSTRACT_PROJECT_PATH
    ).read_text() == cvlmath_abstract_spec_text()


@pytest.mark.parametrize(
    "spec_name",
    [CVLMATH_ABSTRACT_SPEC_NAME, CVLMATH_SPEC_NAME],
)
def test_cvlmath_specs_pass_syntax_check(tmp_path, spec_name):
    """Parse the shipped specs with the same emv.jar checker ``put_cvl_raw`` uses."""
    from composer.certora_env import CertoraEnvironmentError, typechecker_jar

    if shutil.which("java") is None:
        pytest.skip("java not on PATH")
    try:
        jar = typechecker_jar()
    except CertoraEnvironmentError as exc:
        pytest.skip(f"Typechecker.jar unavailable: {exc}")
    # Write both files so CVLMath.spec's relative import of the abstract tier
    # resolves, then check the requested entry point.
    (tmp_path / CVLMATH_SPEC_NAME).write_text(cvlmath_spec_text())
    (tmp_path / CVLMATH_ABSTRACT_SPEC_NAME).write_text(cvlmath_abstract_spec_text())
    res = subprocess.run(
        ["java", "-classpath", str(jar), "EntryPointKt", str(tmp_path / spec_name)],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
