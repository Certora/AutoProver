"""Interfaces must never reach the Certora conf's ``files`` list.

Certora's scene assembly requires every entry in ``files`` to compile to
bytecode. An interface does not, so a single interface entry fails the build
for *every* spec authored in that session:

    Contract IFoo has no bytecode. It may be caused because the contract is
    abstract, or is missing constructor code.

The CVL-authoring agent used to register them — the spec references the
interface, and the tool invited it — with no way to undo. Worse, the registry's
namespace is keyed by document digest and not by cache namespace, so a bad
registration outlived every subsequent run on the same document, including runs
under a fresh ``--cache-ns``. Hence both guards: refuse at registration, and
filter at read so already-persisted entries stay out of the conf.
"""

import pytest
from langgraph.store.memory import InMemoryStore

from composer.spec.natspec.registry import FileEntry, FileRegistry
from composer.spec.types import SolidityIdentifier

CONTRACT = SolidityIdentifier("Foo")
STUB = "Foo.sol"
INTERFACE = "IFoo.sol"
NS = ("test", "spec_files")


class FakeMaterializer:
    """Composite-FS stand-in: every known path resolves, others don't."""

    def __init__(self, paths: set[str]):
        self._paths = paths

    def get(self, path: str) -> bytes | None:
        return b"// solidity" if path in self._paths else None


@pytest.fixture
def registry() -> FileRegistry:
    return FileRegistry(
        _store=InMemoryStore(),
        _materializer=FakeMaterializer({STUB, INTERFACE}),  # type: ignore[arg-type]
        _namespace=NS,
        _non_units=frozenset({INTERFACE}),
    )


@pytest.mark.asyncio
async def test_register_refuses_an_interface(registry: FileRegistry) -> None:
    await registry.register(CONTRACT, STUB)
    message = await registry.register(CONTRACT, INTERFACE)

    assert INTERFACE in message and "compilation unit" in message
    assert await registry.read_all(CONTRACT) == [STUB]


@pytest.mark.asyncio
async def test_read_all_filters_an_interface_persisted_by_an_earlier_run(
    registry: FileRegistry,
) -> None:
    """A registration written before the guard existed must not resurface."""
    await registry._write_contract(
        CONTRACT, [FileEntry(path=STUB), FileEntry(path=INTERFACE)]
    )

    assert await registry.read_all(CONTRACT) == [STUB]
