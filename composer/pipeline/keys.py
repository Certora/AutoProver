"""The Auto-Prove driver's cache tree, declared in one place.

Every edge the backend-agnostic driver (``composer.pipeline.core``)
traverses below the run root, in tree order::

    run root  (``composer.pipeline.cli``: ``user_ns(cache_ns, root_cache_key(...))``)
    ├── {analysis key}                         SYSTEM_ANALYSIS_KEY  → ecosystem App model
    └── {properties key}                       PROPERTIES_KEY       → Properties
        ├── {unit digest}[-{plugin digest}]    COMPONENT_KEY        → ComponentGroup
        │   ├── bug_analysis[|refine][-tm-…][-xc-…]
        │   │                                  BUG_ANALYSIS_KEY     → _BugAnalysisCache
        │   │   └── agent_bug_analysis         AGENT_RESULT_KEY     → _AgentResult
        │   │       └── round-{i}              AGENT_ROUND_KEY      → _AgentRoundWithHistory
        │   ├── final_props[|refine][-tm-…][-xc-…]
        │   │                                  FINAL_PROPERTIES_KEY → FinalProperties
        │   └── {props digest}                 FORMALIZATION_KEY    → backend result (FormT)
        │       └── plugin-artifacts           PLUGIN_ARTIFACTS_KEY → RegisteredArtifacts
        ├── {unit digest}-{plugin}-pre         PRE_PROPERTY_KEY     → PrePropertyInference
        ├── {unit digest}-{plugin}-{props}     POST_PROPERTY_KEY    → PostPropertyInference
        └── prioritization-{all props}[-tm-…][-xc-…]
                                               PRIORITIZATION_KEY   → PropertyRanking

The extraction-layer families (bug analysis, agent rounds) are declared
in ``composer.spec.prop_inference`` beside their cache models and
re-exported here. The prover backend's sub-chain (config / harness /
autosetup / summaries / invariants / CVL generation) has its own
registry: ``composer.spec.source.keys``.
"""

from typing import Any

from composer.pipeline.ptypes import FinalProperties, RegisteredArtifacts
from composer.spec.context import CacheKey, ComponentGroup, Properties
from composer.spec.key_family import KeyFamily, PolyKeyFamily
from composer.pipeline.run_mode import RunModeName
from composer.spec.prioritize import PropertyRanking
from composer.spec.prop_inference import (
    AGENT_RESULT_KEY, AGENT_ROUND_KEY, BUG_ANALYSIS_KEY,
)
from composer.spec.system_model import FeatureUnit
from composer.spec.types import PropertyFormulation
from composer.spec.util import string_hash

from .plugin_api import PostPropertyInference, PrePropertyInference, FormalizationTool

__all__ = [
    "AGENT_RESULT_KEY",
    "AGENT_ROUND_KEY",
    "BUG_ANALYSIS_KEY",
    "COMMON_SYSTEM_CACHE_KEY",
    "COMPONENT_KEY",
    "FINAL_PROPERTIES_KEY",
    "FORMALIZATION_KEY",
    "PLUGIN_ARTIFACTS_KEY",
    "POST_PROPERTY_KEY",
    "PRIORITIZATION_KEY",
    "PRE_PROPERTY_KEY",
    "PROPERTIES_KEY",
    "SYSTEM_ANALYSIS_KEY",
    "component_digest",
]

#: The analysis-slot name both current backends declare in their
#: ``SystemAnalysisSpec``.
COMMON_SYSTEM_CACHE_KEY = "system-analysis"


def _slot_name(name: str) -> str:
    return name


def component_digest(c: FeatureUnit) -> str:
    """``cache_material`` is the ecosystem-agnostic view of what identifies a
    unit; EVM's implementation reproduces the previous inline key
    (app JSON | ind | contract ind) exactly."""
    return string_hash(c.cache_material())


def _props_digest(props: list[PropertyFormulation]) -> str:
    return string_hash("|".join(p.model_dump_json() for p in props))


#: System analysis, keyed by the backend's declared slot name
#: (``SystemAnalysisSpec.analysis_key``). The child is the ecosystem's
#: analyzed model — pass ``ecosystem.system_model``.
SYSTEM_ANALYSIS_KEY = PolyKeyFamily(type(None), _slot_name)

#: The per-backend properties subtree root
#: (``SystemAnalysisSpec.properties_key``).
PROPERTIES_KEY = KeyFamily(type(None), Properties, _slot_name)


def _component_key(feat: FeatureUnit, plugin_digest: str | None) -> str:
    raw_digest = component_digest(feat)
    if plugin_digest is not None:
        raw_digest += f"-{plugin_digest}"
    return raw_digest

#: One unit's extraction subtree; the whole subtree moves when the active
#: plugin set changes (``plugins.manifest_digest``).
COMPONENT_KEY = KeyFamily(Properties, ComponentGroup, _component_key)

def _final_properties_key(
    threat_model_digest: str | None,
    with_refinement: bool,
    extra_context_digest: str | None,
    run_mode: RunModeName,
) -> str:
    # Parameterized exactly like BUG_ANALYSIS_KEY: the component namespace is
    # shared across runs, so the entry must be keyed by what distinguishes one
    # run's extraction from another's — a single fixed leaf would be
    # last-write-wins across divergent runs.
    base_key = "final_props"
    if with_refinement:
        base_key += "|refine"
    if threat_model_digest is not None:
        base_key += "-tm-" + threat_model_digest
    if extra_context_digest is not None:
        base_key += "-xc-" + extra_context_digest
    if run_mode != "comprehensive":
        # A prioritized run writes a *pruned* batch under this component. Without the mode
        # in the key it would overwrite the comprehensive record at the same fixed leaf, and
        # an offline walker reconstructing the formalization edge would read a one-property
        # batch as if it were everything inference found.
        base_key += "-mode-" + run_mode
    return base_key

#: The property batch as it left the property pipeline (post-inference plugin
#: rewrites applied) plus the tool-contributing plugin ids — the exact
#: derivation inputs of the FORMALIZATION_KEY sibling. Written by the driver;
#: read by the offline cache walker (``composer.cli.cache_autoprove``) to reconstruct that edge.
FINAL_PROPERTIES_KEY = KeyFamily(ComponentGroup, FinalProperties, _final_properties_key)


def _props_and_plugins(props: list[PropertyFormulation], plugins: list[str] | None = None) -> str:
    if not plugins:
        return _props_digest(props)
    return _props_digest(props) + "-" + string_hash("|".join(plugins))

#: One unit's formalization result, keyed by the exact property batch. The
#: child is the backend's result type — pass ``formalizer.formalized_type``.
FORMALIZATION_KEY = PolyKeyFamily(ComponentGroup, _props_and_plugins)

#: The verification artifacts a batch's plugin tools registered, cached under
#: the formalization child so cache replays (where the tools never run) still
#: carry them into the report. Parent is the backend's result-typed namespace,
#: which no static declaration can name — hence ``Any``.
PLUGIN_ARTIFACTS_KEY = CacheKey[Any, RegisteredArtifacts]("plugin-artifacts")


def _plugin_formalization_key(plugin: str):
    return f"formalization-plugin-{plugin}"

PLUGIN_FORMALIZATION_KEY = KeyFamily(ComponentGroup, FormalizationTool, _plugin_formalization_key)

def _pre_property_key(feat: FeatureUnit, plugin: str) -> str:
    return f"{component_digest(feat)}-{string_hash(plugin)}-pre"

#: A plugin's private pre-inference namespace: sibling of the unit's
#: COMPONENT_KEY subtree, one per (unit, plugin).
PRE_PROPERTY_KEY = KeyFamily(Properties, PrePropertyInference, _pre_property_key)


def _post_property_key(
    feat: FeatureUnit, plugin: str, curr_props: list[PropertyFormulation]
) -> str:
    return f"{component_digest(feat)}-{string_hash(plugin)}-{_props_digest(curr_props)}"

#: A plugin's post-inference namespace, additionally keyed by the property
#: list entering the hook (each plugin in the post chain sees — and is keyed
#: by — its predecessor's output).
POST_PROPERTY_KEY = KeyFamily(Properties, PostPropertyInference, _post_property_key)


def _prioritization_key(
    candidates_digest: str,
    threat_model_digest: str | None = None,
    extra_context_digest: str | None = None,
) -> str:
    # Parameterized like the extraction leaves: the properties namespace is shared across
    # runs, so the entry must be keyed by everything the ranker saw, or two runs over the
    # same project with different guidance documents would be last-write-wins.
    base_key = "prioritization-" + candidates_digest
    if threat_model_digest is not None:
        base_key += "-tm-" + threat_model_digest
    if extra_context_digest is not None:
        base_key += "-xc-" + extra_context_digest
    return base_key


def candidates_digest(batches: list[tuple[FeatureUnit, list[PropertyFormulation]]]) -> str:
    """The ranker's whole input, as one digest: every unit paired with the properties it
    contributed, in driver order."""
    return string_hash(
        "|".join(f"{component_digest(feat)}:{_props_digest(props)}" for feat, props in batches)
    )


#: The run-wide property ranking, a sibling of the per-unit extraction subtrees: it is the
#: one step that sees every component at once, so it hangs off ``Properties`` rather than off
#: any single component.
PRIORITIZATION_KEY = KeyFamily(Properties, PropertyRanking, _prioritization_key)
