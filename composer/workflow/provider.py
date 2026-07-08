"""Back-compat shim.

Provider classification + the model feature matrices moved to ``composer.llm``.
New code should import ``ProviderKind`` from ``composer.llm.provider`` and
``provider_for`` / ``get_provider_for`` from ``composer.llm.registry`` directly.
"""

from composer.llm.provider import ProviderKind
from composer.llm.registry import provider_for

__all__ = ["ProviderKind", "provider_for"]
