"""Residual shared bits for the autoprove cache hierarchy.

The pipeline orchestration that used to live here now lives in the generic
driver (``composer.pipeline.core``) and the prover backend (``pipeline.py``).
What remains is consumed only by the cache explorer (the cache keys + their
component/batch helpers) and ``AutoProveResult``.
"""

from dataclasses import dataclass, field

from composer.spec.context import CacheKey, Properties, ComponentGroup
from composer.spec.cvl_generation import GeneratedCVL
from composer.spec.prop import PropertyFormulation
from composer.spec.system_model import ContractComponentInstance
from composer.spec.util import string_hash


PROPERTIES_KEY = CacheKey[None, Properties]("properties")
INV_CVL_KEY = CacheKey[None, GeneratedCVL]("invariant-cvl")


def _component_cache_key(
    component: ContractComponentInstance,
) -> CacheKey[Properties, ComponentGroup]:
    combined = "|".join([component.app.model_dump_json(), str(component.ind), str(component._contract.ind)])
    return CacheKey(string_hash(combined))


def _batch_cache_key(props: list[PropertyFormulation]) -> CacheKey[ComponentGroup, GeneratedCVL]:
    combined = "|".join(p.model_dump_json() for p in props)
    return CacheKey(string_hash(combined))


@dataclass
class AutoProveResult:
    n_components: int
    n_properties: int
    failures: list[str] = field(default_factory=list)
