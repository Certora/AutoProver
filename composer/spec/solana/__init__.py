"""Solana ecosystem: system model + (in the ecosystem module) prompts and wiring.

The Solana chain of the ecosystem abstraction (see docs/ecosystem-abstraction.md). This
package holds the Solana-native system model the shared analysis phase produces
(``SolanaApplication`` — programs, their instructions, and the ``ProgramComponent``
capabilities grouping them) and the index-wrapper instances the driver iterates
(``SolanaProgramInstance`` / ``SolanaComponentInstance``, the latter satisfying the
``FeatureUnit`` protocol). The ecosystem object that binds these + the Rust language facet +
the Solana prompts lives in ``composer/pipeline/ecosystem.py``.
"""
