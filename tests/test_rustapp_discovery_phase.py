"""Which phase the design-doc discovery task is grouped under (``composer.rustapp.entry``).

Discovery runs in the *entry point*, before the pipeline, and only when the doc wasn't passed on the
command line — so it is not one of the four phases the driver tags. A wheel that wants it in a
section of its own claims ``CoreSlot.DISCOVERY``; the host used to look for a phase whose key was
literally ``"discover_design_doc"``, a convention a wheel author had to spell exactly right with no
error if they didn't.

No wheel and no services — the descriptor and the synthesized enum are all this needs.
"""

from typing import Any, cast

from composer.rustapp.descriptor import AppDescriptor, CoreSlot
from composer.rustapp.entry import _discovery_phase
from composer.rustapp.host import (
    RustApplication,
    build_core_phases,
    build_phase_enum,
    resolve_ecosystem,
)

PHASES = [
    {"key": "analysis", "label": "A", "order": 0, "core_slot": "analysis"},
    {"key": "extraction", "label": "E", "order": 1, "core_slot": "extraction"},
    {"key": "formalization", "label": "F", "order": 2, "core_slot": "formalization"},
    {"key": "report", "label": "R", "order": 3, "core_slot": "report"},
]


def _app(*, claims_discovery: bool) -> RustApplication:
    descriptor = AppDescriptor.model_validate(
        {
            "name": "app", "header_text": "h", "ecosystem": "solana", "backend_tag": "prover",
            "backend_guidance": "g", "analysis_key": "k",
            "phases": [
                *PHASES,
                {
                    "key": "find_doc", "label": "Design Doc", "order": 4,
                    "core_slot": "discovery" if claims_discovery else None,
                },
            ],
            "artifact_layout": {
                "deliverable_dir": "d", "internal_dir": "i", "report_dir": "r", "artifact_dir": "a",
                "artifact_prefix": "p", "artifact_extension": "rs", "property_suffix": "s",
            },
        }
    )
    phase = build_phase_enum(descriptor)
    ordered = descriptor.ordered_phases()
    return RustApplication(
        descriptor=descriptor, module=cast(Any, object()),
        ecosystem=resolve_ecosystem(descriptor), phase=phase,
        core_phases=build_core_phases(descriptor, phase),
        phase_labels={phase[p.key]: p.label for p in ordered},
        section_order=[p.label for p in ordered],
    )


def test_the_discovery_task_uses_the_phase_that_claims_the_slot():
    app = _app(claims_discovery=True)
    assert app.descriptor.core_slot_map()[CoreSlot.DISCOVERY] == "find_doc"
    assert _discovery_phase(app) is app.phase["find_doc"]


def test_an_unclaimed_slot_falls_back_to_the_first_phase():
    # The common case: most wheels don't care where the task is grouped, and none has to know a
    # magic key to opt in.
    app = _app(claims_discovery=False)
    assert CoreSlot.DISCOVERY not in app.descriptor.core_slot_map()
    assert _discovery_phase(app) is app.phase["analysis"]


def test_the_optional_slot_is_not_required_of_every_application():
    # `build_core_phases` must keep demanding the four the driver tags — and only those.
    app = _app(claims_discovery=False)
    assert set(app.descriptor.core_slot_map()) == set(CoreSlot.required())
