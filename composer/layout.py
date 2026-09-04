"""Project-root directory names shared across backends.

These two paths are the pair every surface agrees on: :data:`CERTORA_DIR` is what a user keeps,
:data:`INTERNAL_DIR` is what a user ignores. They live here — not in
:mod:`composer.spec.gen_types`, which is where the per-backend subdirectories under them are
declared — because :mod:`composer.sandbox` needs :data:`INTERNAL_DIR` and must not pull pydantic in
to get it: the sandbox-escape suite runs ``composer.sandbox`` inside a guest that has only pytest.
"""

from pathlib import Path

#: Deliverable layout under the project root (specs, confs, reports).
CERTORA_DIR = Path("certora")

#: Everything generated that is NOT a deliverable: diagnostics, scratch, and the build outputs a
#: run accumulates. A project that ignores this directory ignores all of it, and every source
#: surface withholds it whole (``fs_forbidden_read``, ``RUST_FORBIDDEN_READ``). Spelled once
#: because a subdirectory that grows without bound is only safe while it is *inside* the directory
#: those rules name.
INTERNAL_DIR = Path(".certora_internal")
