"""Canonical environment variable names and paths used across PreAudit."""

from enum import Enum
from pathlib import Path


class SolcConvention(Enum):
    CERTORA = "certora"  # solc8.34
    SOLC_SELECT = "solc-select"  # solc-0.8.34


class LLMBackend(str, Enum):
    """Allowed values for the PREAUDIT_LLM_BACKEND environment variable.

    Subclasses `str` so members compare equal to their string values (e.g.
    `LLMBackend.ANTHROPIC == "anthropic"`), keeping callers that still hold
    a raw string from the environment seamlessly interoperable.
    """
    ANTHROPIC = "anthropic"
    LOCAL = "local"
    MOCK = "mock"
    CUSTOM_ON_CLOUD = "custom_on_cloud"


# This is the default env var name used by Anthropic's SDKs (Python, TypeScript, etc.)
ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"

# Optional override of the default Anthropic model used by call_llm_* functions when the
# caller doesn't pass an explicit model=. Falls back to _DEFAULT_ANTHROPIC_MODEL in llm_util.
ANTHROPIC_MODEL_ENV = "PREAUDIT_ANTHROPIC_MODEL"

CERTORA_REPORTS_DIR = ".CertoraProverLiteReports"

# Local LLM backend configuration
LLM_BACKEND_ENV = "PREAUDIT_LLM_BACKEND"
LOCAL_LLM_BASE_URL_ENV = "PREAUDIT_LOCAL_LLM_BASE_URL"
LOCAL_LLM_MODEL_ENV = "PREAUDIT_LOCAL_LLM_MODEL"
DEFAULT_LOCAL_LLM_BASE_URL = "http://localhost:11434/v1"
DEFAULT_LOCAL_LLM_MODEL = "qwen2.5-coder:32b"  # 14b for 32GB RAM machines, 32b for 64GB+ RAM machines

# Custom-on-cloud LLM backend configuration (OpenAI-compatible hosted backends, e.g. Together AI).
# All three are required when PREAUDIT_LLM_BACKEND=custom_on_cloud. No defaults — selection is explicit.
CUSTOM_ON_CLOUD_API_KEY_ENV = "PREAUDIT_CUSTOM_ON_CLOUD_API_KEY"
CUSTOM_ON_CLOUD_BASE_URL_ENV = "PREAUDIT_CUSTOM_ON_CLOUD_BASE_URL"
CUSTOM_ON_CLOUD_MODEL_ENV = "PREAUDIT_CUSTOM_ON_CLOUD_MODEL"

# Logger reads this inline to force all logs to stdout (disables muting). Lives here so
# every env-var name is in one place; logger.py imports the constant rather than hard-coding.
ALL_LOGS_IN_STDOUT_ENV = "PREAUDIT_ALL_LOGS_IN_STDOUT"

# Default Solidity compiler version in Certora format (e.g., "solc8.34")
# Used when no explicit version is specified via CLI or config
DEFAULT_SOLC_VERSION = "solc8.34"

# Directory names for internal state
DIR_CERTORA_INTERNAL = ".certora_internal"
DIR_TARBALL_CACHE = "tarball_cache"
DIR_EXTRACTED_TARS = "extracted_tars"
DIR_EXTRACTED = "extracted"

# Cache/state subdirectories under .certora_internal (AutoSetup-owned)
DIR_JOB_RESULT_CACHE = "job_result_cache"
DIR_SIGNATURE_STATE = "signature_state"
DIR_CONTENT_CACHE = "content_cache"
DIR_LLM_CACHE = "llm_cache"
DIR_LLM_INPUT_DUMPS = "llm_input_dumps"
DIR_SANITY_ANALYSIS = "sanity_analysis"
DIR_WORKTREE_LOGS = "worktree_logs"
DIR_PREAUDIT_DEBUG = "preaudit_debug"
FILE_AUTOSETUP_RESULT = "autosetup_result.json"
FILE_LLM_USAGE = "llm_usage.json"

# Compiled-scene method inventory emitted under .certora_internal/
FILE_ALL_METHODS_JSON = "all_methods.json"
PATH_ALL_METHODS_JSON = Path(DIR_CERTORA_INTERNAL) / FILE_ALL_METHODS_JSON

SUMMARIES_SUBDIR = Path("specs") / "summaries"

# User-facing layout under certora/
DIR_USER_CERTORA = Path("certora")
DIR_USER_CONFS = DIR_USER_CERTORA / "confs"
DIR_USER_SPECS = DIR_USER_CERTORA / "specs"
DIR_USER_HARNESSES = DIR_USER_CERTORA / "harnesses"

# Internal autosetup-managed layout under .certora_internal/
DIR_INTERNAL_SPECS = Path(DIR_CERTORA_INTERNAL) / "specs"
DIR_INTERNAL_ROUND_SUMMARIES = DIR_INTERNAL_SPECS / "summaries"
DIR_INTERNAL_CONFS = Path(DIR_CERTORA_INTERNAL) / "confs"
DIR_INTERNAL_TYPECHECKER_ROUNDS = DIR_INTERNAL_CONFS / "typechecker_rounds"
DIR_INTERNAL_MULTI_ASSERT = DIR_INTERNAL_CONFS / "multi_assert"
DIR_INTERNAL_DIFFICULT_RETRY = DIR_INTERNAL_CONFS / "difficult_retry"

FILE_COMPILATION_CONF = "compilation.conf"
FILE_COMPILATION_DUMMY_SPEC = "compilation_dummy.spec"
FILE_ERC7201_SPEC = "erc7201.spec"
# The AST dump the Certora build writes into its build directory (``--dump_asts``).
FILE_BUILD_ASTS = ".asts.json"
