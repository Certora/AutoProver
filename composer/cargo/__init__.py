"""Cargo workspace, session, and chain-specific builds.

Shared by Python backends that submit programs to the Prover and Rust wheels
that load programs into LiteSVM. Metadata, session, and dep-info are independent
of target; :mod:`composer.cargo.sbf` and :mod:`composer.cargo.symbols` are
Solana-specific.
"""
