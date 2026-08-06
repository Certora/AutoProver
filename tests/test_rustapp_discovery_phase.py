"""Which phase the design-doc discovery task is grouped under (``composer.rustapp.entry``).

Discovery runs in the *entry point*, before the pipeline, and only when the doc wasn't passed on the
command line — so it is not one of the four phases the driver tags. A wheel that wants it in a
section of its own claims ``PhaseRole.DISCOVERY``, rather than spelling a magic phase key the host
recognizes by name and silently ignores when it is misspelled.

No wheel and no services — the descriptor and the synthesized enum are all this needs.
"""

import json
from typing import Any, cast

from composer.rustapp.descriptor import AppDescriptor, PhaseRole
from composer.rustapp.entry import _discovery_phase
from composer.rustapp.host import (
    RustApplication,
    build_core_phases,
    build_phase_enum,
    resolve_ecosystem,
)
from tests.conftest import wire_descriptor, wire_phase

PHASES = [
    {"key": "analysis", "label": "A", "order": 0, "role": "analysis"},
    {"key": "extraction", "label": "E", "order": 1, "role": "extraction"},
    {"key": "formalization", "label": "F", "order": 2, "role": "formalization"},
    {"key": "report", "label": "R", "order": 3, "role": "report"},
]


def _app(*, claims_discovery: bool) -> RustApplication:
    descriptor = AppDescriptor.model_validate(
        wire_descriptor(
            name="app", ecosystem="solana",
            phases=[
                *PHASES,
                wire_phase("find_doc", "Design Doc", 4,
                           "discovery" if claims_discovery else "grouping"),
            ],
        )
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
    assert app.descriptor.role_map()[PhaseRole.DISCOVERY] == "find_doc"
    assert _discovery_phase(app) is app.phase["find_doc"]


def test_an_unclaimed_slot_falls_back_to_the_first_phase():
    # The common case: most wheels don't care where the task is grouped, and none has to know a
    # magic key to opt in.
    app = _app(claims_discovery=False)
    assert PhaseRole.DISCOVERY not in app.descriptor.role_map()
    assert _discovery_phase(app) is app.phase["analysis"]


def test_the_optional_slot_is_not_required_of_every_application():
    # `build_core_phases` must keep demanding the four the driver tags — and only those.
    app = _app(claims_discovery=False)
    assert set(app.descriptor.role_map()) == set(PhaseRole.required())
