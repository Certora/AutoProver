"""
Tests for SanityRuleGenerator.generate_sanity_specs.

Regression coverage for the non-idempotent re-run bug: a stale, populated
``{C}_call_resolution.spec`` left on disk by a prior run must be reset to empty
before the pre-call-resolution typechecker runs during warmup. If it is not,
the stale spec registers contracts that are absent from the freshly-stripped
scene and the typechecker aborts warmup with:

    Error in spec file (<C>_call_resolution.spec): Tried to register <inst>
    as <Contract> but <Contract> does not exist.

call_resolution.py repopulates the spec after the typechecker runs, so an empty
reset is always safe here.
"""
from pathlib import Path

import pytest

from certora_autosetup.setup.sanity_rule_generator import SanityRuleGenerator
from certora_autosetup.utils.paths import (
    user_call_resolution_spec_path,
    user_sanity_spec_path,
)

CONTRACT = "Foo"
# Content shaped like a real prior-run spec: registers an instance for a contract
# that will not be in the freshly-stripped scene on the re-run.
STALE_SPEC = "using Foo_inst as Foo;\n"


@pytest.fixture
def project(tmp_path: Path):
    """A temp project with a bundled-style sanity template under certora/.

    Providing the template keeps the test hermetic: generate_sanity_specs
    returns early (doing nothing) if it cannot find a sanity.spec template.
    """
    project_root = tmp_path
    certora_dir = project_root / "certora"
    certora_dir.mkdir()
    (certora_dir / "sanity.spec").write_text("rule sanity_check { assert true; }\n")

    # Swallow log output into a discarded list (references args so linters are happy).
    _sink: list = []
    gen = SanityRuleGenerator(certora_dir, log_func=lambda *a, **k: _sink.append((a, k)))
    summary_spec = (
        project_root / "certora" / "specs" / "summaries" / f"{CONTRACT}_base_summaries.spec"
    )
    return project_root, gen, summary_spec


def test_resets_stale_call_resolution_spec_on_rerun(project):
    """The bug: on a re-run, a populated call-resolution spec from a prior run
    was left untouched (guarded by ``if not exists()``), breaking the typechecker.
    It must now be reset to empty."""
    project_root, gen, summary_spec = project

    # Simulate a prior run: both the call-resolution spec (populated/stale) and the
    # user-facing sanity spec already exist on disk (neither is removed by --clear-cache).
    call_res_spec = user_call_resolution_spec_path(project_root, CONTRACT)
    call_res_spec.parent.mkdir(parents=True, exist_ok=True)
    call_res_spec.write_text(STALE_SPEC)

    sanity_spec = user_sanity_spec_path(project_root, CONTRACT)
    sanity_spec.parent.mkdir(parents=True, exist_ok=True)
    sanity_spec.write_text("// user-edited sanity spec\n")

    gen.generate_sanity_specs({CONTRACT: summary_spec})

    # The stale spec must be wiped so the pre-call-resolution typechecker sees an
    # empty (resolvable) import rather than dangling contract registrations.
    assert call_res_spec.read_text() == ""
    # And the skip-if-exists sanity spec must be preserved verbatim (user edits survive).
    assert sanity_spec.read_text() == "// user-edited sanity spec\n"


def test_first_run_creates_empty_call_resolution_spec(project):
    """Clean first run: the empty call-resolution spec is created so the static
    import in the sanity spec resolves, and the sanity spec imports it."""
    project_root, gen, summary_spec = project

    call_res_spec = user_call_resolution_spec_path(project_root, CONTRACT)
    sanity_spec = user_sanity_spec_path(project_root, CONTRACT)
    assert not call_res_spec.exists()
    assert not sanity_spec.exists()

    gen.generate_sanity_specs({CONTRACT: summary_spec})

    assert call_res_spec.exists()
    assert call_res_spec.read_text() == ""
    assert sanity_spec.exists()
    # The generated sanity spec statically imports the call-resolution spec.
    assert f"{CONTRACT}_call_resolution.spec" in sanity_spec.read_text()


def test_rerun_is_idempotent(project):
    """Two consecutive generations must leave the call-resolution spec empty and
    the sanity spec stable — the failure mode was divergence between run 1 and run 2."""
    project_root, gen, summary_spec = project

    gen.generate_sanity_specs({CONTRACT: summary_spec})
    call_res_spec = user_call_resolution_spec_path(project_root, CONTRACT)
    sanity_spec = user_sanity_spec_path(project_root, CONTRACT)
    first_sanity = sanity_spec.read_text()

    # A prior call-resolution pass would have populated the spec; emulate that,
    # then re-run generation (as a cache-miss re-run does).
    call_res_spec.write_text(STALE_SPEC)
    gen.generate_sanity_specs({CONTRACT: summary_spec})

    assert call_res_spec.read_text() == ""
    assert sanity_spec.read_text() == first_sanity
