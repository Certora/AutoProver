"""The prover (CVL) backend's cache sub-chain, declared in one place.

Everything the prover backend adds around the Auto-Prove driver chain
(``composer.pipeline.keys``), in tree order::

    run root
    ├── config                              config_key             → ContractSetup
    │   ├── system-setup-{app digest}       SYSTEM_SETUP_KEY       → SystemDescriptionHarnessed
    │   │   ├── harness-analysis            HARNESS_ANALYSIS_KEY   → AgentSystemDescription
    │   │   └── {instructions digest}       HARNESS_GENERATION_KEY → HarnessResult
    │   └── autosetup-{app+opts digest}     AUTOSETUP_KEY          → SetupSuccess
    ├── summary-{config digest}             SUMMARY_KEY            → _SummaryCache
    ├── structural-inv                      STRUCTURAL_INV_KEY     → Invariants
    │   └── judge                           INV_JUDGE_KEY          → (judge memory)
    ├── invariant-cvl                       INV_CVL_KEY            → GeneratedCVL
    │   ├── judge                           CVL_JUDGE_KEY          → (judge memory)
    │   └── last_attempt                    LAST_ATTEMPT_KEY       → _LastAttemptCache
    └── ap-properties                       PROPERTIES_KEY(AP_PROPERTIES_KEY_NAME)
        └── … driver chain …
            └── {props digest}              FORMALIZATION_KEY      → GeneratedCVL
                ├── judge                   CVL_JUDGE_KEY
                └── last_attempt            LAST_ATTEMPT_KEY

Families and constants are declared beside their cache models (harness,
summarizer, struct_invariant, cvl_generation) and gathered here;
consumers (the backend pipeline, ``cache-autoprove``) import from this
registry rather than spelunking the producer modules.
"""

from composer.spec.context import CacheKey
from composer.spec.cvl_generation import (
    CVL_JUDGE_KEY, LAST_ATTEMPT_KEY, GeneratedCVL,
)
from composer.spec.source.harness import (
    AUTOSETUP_KEY, HARNESS_ANALYSIS_KEY, HARNESS_GENERATION_KEY,
    SYSTEM_SETUP_KEY, config_key,
)
from composer.spec.source.struct_invariant import INV_JUDGE_KEY, STRUCTURAL_INV_KEY
from composer.spec.source.summarizer import SUMMARY_KEY

__all__ = [
    "AP_PROPERTIES_KEY_NAME",
    "AUTOSETUP_KEY",
    "CVL_JUDGE_KEY",
    "HARNESS_ANALYSIS_KEY",
    "HARNESS_GENERATION_KEY",
    "INV_CVL_KEY",
    "INV_JUDGE_KEY",
    "LAST_ATTEMPT_KEY",
    "STRUCTURAL_INV_KEY",
    "SUMMARY_KEY",
    "SYSTEM_SETUP_KEY",
    "config_key",
]

#: The prover backend's ``SystemAnalysisSpec.properties_key``.
AP_PROPERTIES_KEY_NAME = "ap-properties"

#: CVL generation for the structural invariants (the per-component peer is
#: reached via ``FORMALIZATION_KEY``).
INV_CVL_KEY = CacheKey[None, GeneratedCVL]("invariant-cvl")
