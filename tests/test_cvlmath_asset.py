"""Tests for the packaged CVLMath asset and its pipeline resource wiring.

The asset must be resolvable through ``importlib.resources`` (so it survives
being installed as a wheel, not just run from a source checkout), the install
helper must write the project copy exactly where the pipeline advertises it,
and the shipped CVL must parse with the same syntax checker the authoring
tools use.
"""

import shutil
import subprocess

import pytest

from composer.spec.assets import (
    CVLMATH_PROJECT_PATH,
    CVLMATH_SPEC_NAME,
    cvlmath_spec_text,
    install_cvlmath_resource,
)
from composer.spec.gen_types import SUMMARIES_DIR


def test_asset_resolvable_with_both_tiers():
    text = cvlmath_spec_text()
    # Spot-check that both tiers ship: exact summaries + relational abstractions.
    assert "mulDivDownSummary" in text
    assert "mulDivUpSummary" in text
    assert "mulDivDownAbstract" in text
    assert "mulDivUpAbstract" in text
    assert "definition WAD()" in text
    assert "definition RAY()" in text


def test_install_writes_project_copy_and_builds_resource(tmp_path):
    res = install_cvlmath_resource(tmp_path)
    # The resource path is canonical (project-root-relative) and optional —
    # this is the CVLResource the pipeline appends in stream_autosetup().
    assert res.path == CVLMATH_PROJECT_PATH
    assert res.sort == "import"
    assert not res.required
    written = tmp_path / SUMMARIES_DIR / CVLMATH_SPEC_NAME
    assert written.read_text() == cvlmath_spec_text()


def test_cvlmath_spec_passes_syntax_check(tmp_path):
    """Parse the shipped spec with the same emv.jar checker ``put_cvl_raw`` uses."""
    from composer.certora_env import CertoraEnvironmentError, typechecker_jar

    if shutil.which("java") is None:
        pytest.skip("java not on PATH")
    try:
        jar = typechecker_jar()
    except CertoraEnvironmentError as exc:
        pytest.skip(f"Typechecker.jar unavailable: {exc}")
    spec = tmp_path / CVLMATH_SPEC_NAME
    spec.write_text(cvlmath_spec_text())
    res = subprocess.run(
        ["java", "-classpath", str(jar), "EntryPointKt", str(spec)],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
