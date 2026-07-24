"""Solana ecosystem: system model + (in the ecosystem module) prompts and wiring.

The Solana chain of the ecosystem abstraction (see docs/ecosystem-abstraction.md). This
package holds the Solana-native system model the shared analysis phase produces
(``SolanaApplication``) and the index-wrapper instances the driver iterates
(``SolanaProgramInstance`` / ``SolanaInstructionInstance``, the latter satisfying the
``FeatureUnit`` protocol). The ecosystem object that binds these + the Rust language facet +
the Solana prompts lives in ``composer/pipeline/ecosystem.py``.
"""
