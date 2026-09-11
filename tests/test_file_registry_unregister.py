"""An agent must be able to take back a file registration.

``FileRegistry`` refuses the one bad registration we know about — a generated
interface, which has no bytecode and so fails scene assembly for every spec in
the session. That guard is a blocklist, and agents find registrations to regret
that are not on it. Registration is otherwise permanent: the namespace is keyed
by document digest, so an entry survives the run that wrote it and every later
run on the same document, whatever cache namespace it uses. ``unregister`` is
the escape hatch.
"""

import pytest
from langgraph.store.memory import InMemoryStore

from composer.spec.natspec.registry import FileEntry, FileRegistry
from composer.spec.types import SolidityIdentifier

CONTRACT = SolidityIdentifier("Foo")
STUB = "Foo.sol"
HELPER = "Helper.sol"
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
        _materializer=FakeMaterializer({STUB, HELPER, INTERFACE}),  # type: ignore[arg-type]
        _namespace=NS,
        _non_units=frozenset({INTERFACE}),
    )


@pytest.mark.asyncio
async def test_unregister_drops_the_path_and_leaves_the_rest(registry: FileRegistry) -> None:
    await registry.register(CONTRACT, STUB)
    await registry.register(CONTRACT, HELPER)

    message = await registry.unregister(CONTRACT, HELPER)

    assert HELPER in message
    assert await registry.read_all(CONTRACT) == [STUB]


@pytest.mark.asyncio
async def test_unregister_of_an_unregistered_path_changes_nothing(
    registry: FileRegistry,
) -> None:
    await registry.register(CONTRACT, STUB)

    message = await registry.unregister(CONTRACT, HELPER)

    assert "not registered" in message
    assert await registry.read_all(CONTRACT) == [STUB]


@pytest.mark.asyncio
async def test_a_registration_can_be_taken_back_and_made_again(
    registry: FileRegistry,
) -> None:
    """The point of the tool: a bad call is recoverable within the session."""
    await registry.register(CONTRACT, HELPER)
    await registry.unregister(CONTRACT, HELPER)
    assert await registry.read_all(CONTRACT) == []

    await registry.register(CONTRACT, HELPER)
    assert await registry.read_all(CONTRACT) == [HELPER]


@pytest.mark.asyncio
async def test_unregister_clears_an_interface_persisted_by_an_earlier_run(
    registry: FileRegistry,
) -> None:
    """``read_all`` already hides such an entry; unregister removes it for good."""
    await registry._write_contract(
        CONTRACT, [FileEntry(path=STUB), FileEntry(path=INTERFACE)]
    )

    message = await registry.unregister(CONTRACT, INTERFACE)

    assert INTERFACE in message
    assert await registry._read_contract(CONTRACT) == [FileEntry(path=STUB)]
