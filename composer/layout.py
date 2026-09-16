"""Project-root directory names shared across backends.

:data:`CERTORA_DIR` is what a user keeps; :data:`INTERNAL_DIR` is what a user ignores. They live
here rather than in :mod:`composer.spec.gen_types`, which declares the per-backend subdirectories
under them, because :mod:`composer.sandbox` needs :data:`INTERNAL_DIR` and stays pydantic-free
(see :mod:`composer.sandbox.config`).
"""

from pathlib import Path

#: Deliverable layout under the project root (specs, confs, reports).
CERTORA_DIR = Path("certora")

#: Everything generated that is NOT a deliverable: diagnostics, scratch, and accumulated build
#: output. Every source surface withholds it whole (``fs_forbidden_read``, ``RUST_FORBIDDEN_READ``),
#: so a directory that grows without bound is safe here and nowhere else.
INTERNAL_DIR = Path(".certora_internal")
