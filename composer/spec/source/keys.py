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
    └── ap-properties                       PROPERTIES_KEY(AP_PROPERTIES_KEY_NAME)
        └── … driver chain …
            └── {props digest}              FORMALIZATION_KEY      → GeneratedCVL
                ├── judge                   CVL_JUDGE_KEY
                └── last_attempt            LAST_ATTEMPT_KEY

Every family and constant is declared beside its cache model (harness,
summarizer, cvl_generation); this module declares none of its own and only
gathers them, so consumers (the backend pipeline, ``cache-autoprove``) import
from one registry rather than spelunking the producer modules.
"""

from composer.spec.cvl_generation import CVL_JUDGE_KEY, LAST_ATTEMPT_KEY
from composer.spec.source.harness import (
    AUTOSETUP_KEY, HARNESS_ANALYSIS_KEY, HARNESS_GENERATION_KEY,
    SYSTEM_SETUP_KEY, config_key,
)
from composer.spec.source.summarizer import SUMMARY_KEY

__all__ = [
    "AP_PROPERTIES_KEY_NAME",
    "AUTOSETUP_KEY",
    "CVL_JUDGE_KEY",
    "HARNESS_ANALYSIS_KEY",
    "HARNESS_GENERATION_KEY",
    "LAST_ATTEMPT_KEY",
    "SUMMARY_KEY",
    "SYSTEM_SETUP_KEY",
    "config_key",
]

#: The prover backend's ``SystemAnalysisSpec.properties_key``.
AP_PROPERTIES_KEY_NAME = "ap-properties"
