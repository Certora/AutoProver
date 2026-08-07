"""Soroban ecosystem: system model + (in the ecosystem module) prompts and wiring.

The Soroban (Stellar) chain of the ecosystem abstraction (see docs/ecosystem-abstraction.md).
This package holds the Soroban-native system model the shared analysis phase produces
(``SorobanApplication`` — contracts, their entry-point functions, the storage entries those
functions touch, and the ``ContractComponent`` capabilities grouping them) and the
index-wrapper instances the driver iterates (``SorobanContractInstance`` /
``SorobanComponentInstance``, the latter satisfying the ``FeatureUnit`` protocol). The
ecosystem object that binds these + the Rust language facet + the Soroban prompts lives in
``composer/pipeline/ecosystem.py``.
"""
