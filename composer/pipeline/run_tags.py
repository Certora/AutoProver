"""Tag schema for auto-prove pipeline runs.

Records the inputs that drive the auto-prove cache-key layout into the
run's ``cache_root`` run-data record — written once the design doc
(hence the cache root) is resolved — so downstream tooling
(``cache-autoprove``) can rehydrate every namespace, including the
plugin-suffixed per-component keys, from a run id alone.

This is the *canonical* shape — both the writer (``cli_pipeline``) and
the reader (``cache-autoprove``) reference this model; round-trip via
``model_dump()`` into the record and ``model_validate(...)`` back.
"""

from pydantic import BaseModel, Field


CACHE_ROOT_RECORD = "cache_root"
"""Run-data key the tags are stored under (``get_run_data(store, run_id, CACHE_ROOT_RECORD)``)."""


class AutoProveCacheTags(BaseModel):
    cache_root: list[str] | None
    """Resolved cache namespace tuple; ``None`` when the run had no ``--cache-ns``."""

    contract_name: str

    memory_ns: str | None
    """Fully-qualified (uid-prefixed) memory namespace, if memory was enabled."""

    plugins: list[str] = Field(default_factory=list)
    """Sorted plugin manifest active for the run — the input to
    ``manifest_digest``, which is suffixed onto per-component cache keys."""

    threat_model_digest: str | None = None
    """``Document.to_digest()`` of the threat model, which parameterizes
    ``bug_analysis_key``; ``None`` for runs without one."""

    interactive: bool | None = None
    """Whether the run used interactive refinement (selects the ``|refine``
    bug-analysis key variant). ``None`` on records written before this
    field existed — readers must probe both variants."""
