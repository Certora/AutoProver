"""Tests for the packaged CVL math assets and their pipeline resource wiring.

The abstract-tier asset must be resolvable through ``importlib.resources`` (so
it survives being installed as a wheel, not just run from a source checkout),
the exact tier must come from the ONE canonical certora_autosetup-bundled
Math.spec (composer ships no copy), the install helper must write the project
files exactly where the pipeline advertises them — adding Math.spec only when
AutoSetup did not already ship it — and the shipped CVL must parse with the
same syntax checker the authoring tools use.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from composer.spec.assets import (
    CVLMATH_ABSTRACT_PROJECT_PATH,
    CVLMATH_ABSTRACT_SPEC_NAME,
    MATH_SPEC_NAME,
    MATH_SPEC_PROJECT_PATH,
    bundled_math_spec_text,
    cvlmath_abstract_spec_text,
    install_cvlmath_resources,
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


def test_bundled_math_spec_is_resolvable_and_is_the_exact_tier():
    """Drift guard for the single-source scheme: the canonical exact tier is
    certora_autosetup's bundled Math.spec, read through importlib.resources.
    If that file is ever moved or renamed this test must FAIL (not skip) —
    that is precisely the event that would break the install helper and any
    generated spec importing summaries/Math.spec."""
    import certora_autosetup
    from certora_autosetup.utils.constants import SUMMARIES_SUBDIR

    fs_path = (
        Path(certora_autosetup.__file__).parent
        / "certora"
        / SUMMARIES_SUBDIR
        / MATH_SPEC_NAME
    )
    assert fs_path.exists(), (
        f"canonical bundled Math.spec missing at {fs_path}; the composer "
        "install helper and generated-spec imports depend on this exact path"
    )
    # bundled_math_spec_text() must resolve to that same file (raises rather
    # than skipping if the packaged layout diverges).
    text = bundled_math_spec_text()
    assert text == fs_path.read_text()
    # And it is the exact `*Summary` tier the resource description promises.
    assert "mulDivDownSummary" in text
    assert "mulDivUpSummary" in text
    assert "sqrtSummaryPrecise" in text


def test_install_without_math_spec_ships_both_tiers(tmp_path):
    resources = install_cvlmath_resources(tmp_path)
    # Resource paths are canonical (project-root-relative) and optional —
    # these are the CVLResources the pipeline appends in stream_autosetup().
    by_path = {res.path: res for res in resources}
    assert set(by_path) == {CVLMATH_ABSTRACT_PROJECT_PATH, MATH_SPEC_PROJECT_PATH}
    for res in resources:
        assert res.sort == "import"
        assert not res.required
    assert "require" in by_path[CVLMATH_ABSTRACT_PROJECT_PATH].description
    # Both tiers land in the summaries dir, byte-identical to their sources.
    assert (
        tmp_path / CVLMATH_ABSTRACT_PROJECT_PATH
    ).read_text() == cvlmath_abstract_spec_text()
    assert (tmp_path / MATH_SPEC_PROJECT_PATH).read_text() == bundled_math_spec_text()


def test_install_with_math_spec_present_ships_abstract_resource_only(tmp_path):
    math_spec = tmp_path / MATH_SPEC_PROJECT_PATH
    math_spec.parent.mkdir(parents=True)
    sentinel = "// AutoSetup-copied exact summaries\n"
    math_spec.write_text(sentinel)
    resources = install_cvlmath_resources(tmp_path)
    # Only the abstract tier is advertised: the exact tier is already
    # reachable through the required AutoSetup summaries import.
    assert [res.path for res in resources] == [CVLMATH_ABSTRACT_PROJECT_PATH]
    assert (
        tmp_path / CVLMATH_ABSTRACT_PROJECT_PATH
    ).read_text() == cvlmath_abstract_spec_text()
    # The existing Math.spec is AutoSetup's to manage — never overwritten.
    assert math_spec.read_text() == sentinel


def test_install_is_idempotent_across_runs(tmp_path):
    """Multi-run edge: run 1 installs Math.spec (no AutoSetup copy yet); a
    later run must leave the on-disk exact tier intact at the same single
    path, so a cached generated spec importing summaries/Math.spec still
    typechecks."""
    first = install_cvlmath_resources(tmp_path)
    assert {res.path for res in first} == {
        CVLMATH_ABSTRACT_PROJECT_PATH,
        MATH_SPEC_PROJECT_PATH,
    }
    second = install_cvlmath_resources(tmp_path)
    # Math.spec already exists (identical canonical bytes), so it is not
    # re-advertised; the file itself must survive with the same content.
    assert [res.path for res in second] == [CVLMATH_ABSTRACT_PROJECT_PATH]
    assert (tmp_path / MATH_SPEC_PROJECT_PATH).read_text() == bundled_math_spec_text()
    assert (
        tmp_path / CVLMATH_ABSTRACT_PROJECT_PATH
    ).read_text() == cvlmath_abstract_spec_text()


@pytest.mark.parametrize(
    "spec_name",
    [CVLMATH_ABSTRACT_SPEC_NAME, MATH_SPEC_NAME],
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
        return  # unreachable (skip raises); keeps `jar` provably bound below
    # Both files are standalone (no imports), so each is checked on its own.
    (tmp_path / CVLMATH_ABSTRACT_SPEC_NAME).write_text(cvlmath_abstract_spec_text())
    (tmp_path / MATH_SPEC_NAME).write_text(bundled_math_spec_text())
    res = subprocess.run(
        ["java", "-classpath", str(jar), "EntryPointKt", str(tmp_path / spec_name)],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
