"""Console entry points for the Crucible (Solana fuzzing) application.

Crucible is now a *pure-Rust app* (``docs/rust-pure-app.md``): the ``crucible_app`` wheel + its
descriptor define everything (the shared fixture, crate deliverable, workspace prep, sandbox
grants, verdict summary), so these are the same two-line shims echoprover uses — no
Crucible-specific Python package. ``import composer.bind`` runs first (inside
``composer.rustapp.cli``) for the import-time DI / test-tape bootstrap.
"""

from composer.rustapp.cli import console_main, tui_main

_MODULE = "crucible_app"


def console_crucible() -> int:
    """Run the Crucible application in console mode."""
    return console_main(_MODULE)


def tui_crucible() -> int:
    """Run the Crucible application in the Textual TUI."""
    return tui_main(_MODULE)
