"""Crucible (Solana fuzzing) backend — Python-side deliverable model.

The backend logic is a Rust wheel (``rust/crucible-app``); this package holds the
Python pieces that do not fit the generic rustapp host — the multi-file
harness-crate deliverable (``docs/crucible-application.md`` §7.1) and the
Crucible-specific prepared-system / formalizer (``backend.py``).
"""
