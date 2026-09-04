try:
    import graphcore.graph
except ImportError:
    import pathlib
    composer_project_root = pathlib.Path(__file__).parent.parent
    if (graphcore_dir := (composer_project_root / "graphcore")).exists() and graphcore_dir.is_dir():
        import importlib
        import sys
        if "graphcore" in sys.modules:
            del sys.modules["graphcore"]
        sys.path.insert(0, str(graphcore_dir))
        importlib.invalidate_caches()

import logging

logging.getLogger("huggingface_hub.utils._http").addFilter(
    lambda r: "You are sending unauthenticated requests to the HF Hub." not in r.getMessage()
)

import os
if (_tape := os.environ.get("COMPOSER_TEST_TAPE")):
    import importlib
    _mod = importlib.import_module(f"composer.testing.ui_harness_{_tape}")
    _mod.install_harness_tape()
    # Resume-safe replay: with the tape's sidecar of prefix keys attached, entries
    # are served by the prompt's AI-message prefix instead of by cursor, so a
    # process resuming from a checkpoint lands on the right continuation.
    # COMPOSER_TAPE_LEARN_KEYS=1 instead serves positionally and writes that
    # sidecar at exit — run it once, on a fresh (non-resumed) replay.
    from composer.testing.harness_tape import attach_sidecar_keys, enable_key_learning
    if os.environ.get("COMPOSER_TAPE_LEARN_KEYS"):
        enable_key_learning(_tape)
    else:
        attach_sidecar_keys(_tape)
elif (_record := os.environ.get("COMPOSER_RECORD_TAPE")):
    # Record a real run into a replayable tape (inverse of COMPOSER_TEST_TAPE).
    # Optional COMPOSER_RECORD_OUT overrides the default
    # composer/testing/ui_harness_<name>.py output path.
    from composer.testing.record_tape import install_recorder
    install_recorder(
        _record,
        os.environ.get("COMPOSER_RECORD_OUT"),
        no_thinking=bool(os.environ.get("COMPOSER_RECORD_NO_THINKING")),
    )

if (_resp := os.environ.get("COMPOSER_RESPONSE_TAPE")):
    # Scripted human replies for console HITL interrupts (commit approvals,
    # requirement relaxations). Independent of — and replayed alongside —
    # COMPOSER_TEST_TAPE; the tape module exposes ``install_response_tape``.
    import importlib
    _rmod = importlib.import_module(f"composer.testing.ui_harness_{_resp}")
    _rmod.install_response_tape()
