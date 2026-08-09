"""Plugin scoping: which installed plugins apply to a run over a given unit type.

There are no in-tree plugins (nothing registers under ``certora.autoprove.plugins``), so these
tests stand in for the entry points with fakes. What's under test is the narrowing that decides
(a) whose hooks the driver may call, (b) whose ``initialize`` runs at all, and (c) which names are
hashed into the per-component cache keys.
"""

import contextlib
from dataclasses import dataclass
from typing import Any

import pytest

from composer.pipeline.plugin_api import (
    AnyEcosystem, ForEcosystem, PipelinePlugin, PipelinePluginLoader, PluginScope,
)
from composer.pipeline import plugins as plugins_mod
from composer.pipeline.plugins import (
    _applies, applicable_plugin_manifest, load_plugins, manifest_digest,
)
from composer.spec.system_model import ContractComponentInstance, FeatureUnit
from composer.spec.solana.model import SolanaComponentInstance


# ---------------------------------------------------------------------------
# Fakes standing in for installed plugins
# ---------------------------------------------------------------------------


class _Plugin(PipelinePlugin[Any]):
    def __init__(self, name: str) -> None:
        self.NAME = name


@dataclass
class _Loader(PipelinePluginLoader[Any]):
    """A loader that records whether it was ever initialized, so a test can assert that an
    inapplicable plugin never gets the chance to acquire resources."""

    name: str
    _scope: PluginScope[Any]
    initialized: bool = False

    @property
    def scope(self) -> PluginScope[Any]:
        return self._scope

    @contextlib.asynccontextmanager
    async def initialize(self):
        self.initialized = True
        yield _Plugin(self.name)


@dataclass
class _EntryPoint:
    """The subset of ``importlib.metadata.EntryPoint`` that ``_declared_loaders`` touches."""

    name: str
    _loader_cls: type
    module: str = "fake.module"
    attr: str = "Loader"

    def load(self) -> type:
        return self._loader_cls


def _install(monkeypatch, *scoped: tuple[str, PluginScope[Any]]) -> dict[str, _Loader]:
    """Register fake entry points for ``scoped``, returning the loader instance per name so a test
    can inspect ``initialized`` afterwards.

    ``_declared_loaders`` constructs the class the entry point resolves to, so each fake gets its
    own subclass whose ``__init__`` files the instance into ``built``."""
    built: dict[str, _Loader] = {}
    eps: list[_EntryPoint] = []

    for name, scope in scoped:
        class Loader(_Loader):
            # Evaluated in the class body, so each iteration captures its own pair. Read back via
            # `type(self)`, not the name `Loader` — that name is rebound every iteration, so a
            # method closing over it would see only the last class defined.
            _bound = (name, scope)

            def __init__(self) -> None:
                super().__init__(*type(self)._bound)
                built[self.name] = self

        eps.append(_EntryPoint(name, Loader))

    def fake_entry_points(*, group: str) -> list[_EntryPoint]:
        assert group == plugins_mod.PLUGIN_ENTRY_POINT_GROUP
        return eps

    monkeypatch.setattr(plugins_mod.importlib.metadata, "entry_points", fake_entry_points)
    return built


# ---------------------------------------------------------------------------
# _applies
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("unit_type", [ContractComponentInstance, SolanaComponentInstance])
def test_agnostic_scope_applies_everywhere(unit_type):
    assert _applies(AnyEcosystem(), unit_type)


def test_ecosystem_scope_applies_only_to_its_own_unit():
    evm = ForEcosystem(ContractComponentInstance)
    solana = ForEcosystem(SolanaComponentInstance)

    assert _applies(evm, ContractComponentInstance)
    assert not _applies(evm, SolanaComponentInstance)
    assert _applies(solana, SolanaComponentInstance)
    assert not _applies(solana, ContractComponentInstance)


def test_feature_unit_is_not_runtime_checkable():
    """The reason ``ForEcosystem`` carries a *concrete* unit type rather than being matched
    against the protocol: ``FeatureUnit`` is deliberately not ``@runtime_checkable``, so this is
    the error a protocol-based check would hit at the entry-point boundary."""
    with pytest.raises(TypeError):
        issubclass(ContractComponentInstance, FeatureUnit)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Manifest / cache-key effects
# ---------------------------------------------------------------------------


def test_manifest_excludes_other_ecosystems(monkeypatch):
    _install(
        monkeypatch,
        ("agnostic", AnyEcosystem()),
        ("evm-only", ForEcosystem(ContractComponentInstance)),
        ("solana-only", ForEcosystem(SolanaComponentInstance)),
    )

    assert applicable_plugin_manifest(ContractComponentInstance) == ["agnostic", "evm-only"]
    assert applicable_plugin_manifest(SolanaComponentInstance) == ["agnostic", "solana-only"]


def test_other_ecosystems_plugin_does_not_perturb_cache_keys(monkeypatch):
    """The property that motivated scoping: installing a Solana plugin must leave every EVM
    component's cache key untouched, since it never contributed to one."""
    _install(monkeypatch, ("evm-only", ForEcosystem(ContractComponentInstance)))
    before = manifest_digest(applicable_plugin_manifest(ContractComponentInstance))

    _install(
        monkeypatch,
        ("evm-only", ForEcosystem(ContractComponentInstance)),
        ("solana-only", ForEcosystem(SolanaComponentInstance)),
    )
    after = manifest_digest(applicable_plugin_manifest(ContractComponentInstance))

    assert before == after


def test_no_plugins_means_no_digest(monkeypatch):
    """With nothing applicable the digest is ``None``, which is what keeps per-component keys
    byte-identical to a build with no plugin support at all."""
    _install(monkeypatch, ("solana-only", ForEcosystem(SolanaComponentInstance)))
    assert manifest_digest(applicable_plugin_manifest(ContractComponentInstance)) is None


# ---------------------------------------------------------------------------
# load_plugins
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_plugins_initializes_only_applicable(monkeypatch):
    built = _install(
        monkeypatch,
        ("agnostic", AnyEcosystem()),
        ("evm-only", ForEcosystem(ContractComponentInstance)),
        ("solana-only", ForEcosystem(SolanaComponentInstance)),
    )

    # `run` is only stored on the manager, never consulted while narrowing.
    async with load_plugins(None, ContractComponentInstance) as mgr:  # type: ignore[arg-type]
        assert mgr.plugin_manifest == ["agnostic", "evm-only"]

    # The point of scoping on the *loader*: a plugin for another ecosystem never reaches
    # `initialize`, where PLUGIN.md says resources get acquired.
    assert built["agnostic"].initialized
    assert built["evm-only"].initialized
    assert not built["solana-only"].initialized


@pytest.mark.asyncio
async def test_duplicate_plugin_names_rejected(monkeypatch):
    _install(monkeypatch, ("dup", AnyEcosystem()), ("dup", AnyEcosystem()))
    with pytest.raises(RuntimeError, match="Multiple plugins with name"):
        applicable_plugin_manifest(ContractComponentInstance)
