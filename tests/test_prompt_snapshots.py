"""Golden-file snapshots of the analysis / property prompt templates, per ecosystem.

These pin the *rendered* text of the front-half prompt templates so that refactors which
factor shared boilerplate into partials are provably behaviour-preserving. The EVM prompts are
the production ones — their goldens must never change unintentionally.

The templates read a rich, ecosystem-specific ``context`` object plus a few control-flow vars
(``sort`` / ``prior_properties`` / ``has_doc``). We render with a *permissive* context stub
that yields empty for any attribute / iteration, so the dynamic per-target content blanks out
while every piece of static boilerplate — the part shared-partial extraction touches — renders
in full. The control-flow vars are real, so the ``{% if prior_properties %}`` and ``sort``
branches are exercised.

Regenerate goldens after an intentional wording change with::

    UPDATE_PROMPT_GOLDENS=1 pytest tests/test_prompt_snapshots.py
"""

import os
import pathlib
from collections import namedtuple

import pytest

from composer.templates.loader import load_jinja_template

_GOLDEN_DIR = pathlib.Path(__file__).parent / "golden" / "prompts"

# The four (system, initial) prompt pairs, per ecosystem, keyed by a stable golden basename.
_TEMPLATES = {
    "evm_analysis_system": "application_analysis_system.j2",
    "evm_analysis_prompt": "application_analysis_prompt.j2",
    "evm_property_system": "property_analysis_system_prompt.j2",
    "evm_property_prompt": "property_analysis_prompt.j2",
    "solana_analysis_system": "solana/analysis_system.j2",
    "solana_analysis_prompt": "solana/analysis_prompt.j2",
    "solana_property_system": "solana/property_system.j2",
    "solana_property_prompt": "solana/property_prompt.j2",
}


class _Permissive:
    """Renders as empty for any attribute access, item access, call, or iteration — so a
    template's dynamic ``context.*`` bits blank out while its static boilerplate renders."""

    def __getattr__(self, _):
        return _Permissive()

    def __getitem__(self, _):
        return _Permissive()

    def __call__(self, *_, **__):
        return _Permissive()

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0

    def __bool__(self):
        return False

    def __str__(self):
        return ""


_Round = namedtuple("_Round", "items reasoning")
_Prop = namedtuple("_Prop", "sort title description")

# One prior round with one property, so the shared iterative-refinement block renders in full.
_PRIOR = [_Round(items=[_Prop(sort="invariant", title="P1", description="a prior property")],
                 reasoning="prior-round reasoning")]

# Two render variants exercise the shared control-flow branches: with/without prior rounds and
# the greenfield vs existing-source split.
_VARIANTS = {
    "existing": dict(sort="existing", has_doc=True, prior_properties=_PRIOR),
    "greenfield": dict(sort="greenfield", has_doc=True, prior_properties=[]),
}


def _render(template: str, variant: dict) -> str:
    return load_jinja_template(
        template,
        context=_Permissive(),
        backend_guidance="[BACKEND GUIDANCE]",
        **variant,
    )


def _cases():
    for name, template in _TEMPLATES.items():
        for variant_name, variant in _VARIANTS.items():
            yield f"{name}__{variant_name}", template, variant


@pytest.mark.parametrize("golden_name,template,variant", list(_cases()),
                         ids=[c[0] for c in _cases()])
def test_prompt_snapshot(golden_name: str, template: str, variant: dict):
    rendered = _render(template, variant)
    golden = _GOLDEN_DIR / f"{golden_name}.txt"

    if os.environ.get("UPDATE_PROMPT_GOLDENS"):
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(rendered)
        pytest.skip(f"regenerated {golden.name}")

    assert golden.exists(), (
        f"missing golden {golden.name}; run UPDATE_PROMPT_GOLDENS=1 pytest {__file__}"
    )
    assert rendered == golden.read_text(), (
        f"{template} ({golden_name}) rendered differently from its golden. If intentional, "
        f"regenerate with UPDATE_PROMPT_GOLDENS=1."
    )
