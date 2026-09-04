"""A stub that compiles but yields no bytecode must be rejected.

`_compile_stub` gates every stub update. It used to trust solc's exit status,
which an `abstract contract` satisfies happily — solc returns 0 and emits no
bytecode. Certora's scene assembly then rejects the verification unit:

    Contract StreamShareSplitter has no bytecode. It may be caused because the
    contract is abstract, or is missing constructor code.

Every later typecheck in that session fails, `publish` is gated on the
typecheck, and nothing the CVL author does can recover it — the bad stub is
already in the VFS. A registry agent asked for storage fields, got back an
`abstract contract`, and the run produced four judge-approved specs that could
never be published.

Implicit abstractness — inheriting a function you don't implement — has the
same effect and is covered here too, since it is easier to write by accident.
"""

import contextlib
import pathlib
import shutil
import tempfile

import pytest

from composer.spec.natspec.registry import _compile_stub
from composer.spec.natspec.task_description import Assembler

SOLC_VERSION = "8.29"
STUB_PATH = "Foo.sol"

INTERFACE = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.29;

interface IFoo {
    function a() external view returns (uint256);
    function b() external view returns (uint256);
}
"""

CONCRETE = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.29;

import "IFoo.sol";

contract Foo is IFoo {
    function a() external view returns (uint256) { return 0; }
    function b() external view returns (uint256) { return 0; }
}
"""

# Compiles, emits no bytecode.
ABSTRACT = CONCRETE.replace("contract Foo", "abstract contract Foo")

# Also compiles, also emits no bytecode: `b` is inherited but never implemented,
# which makes Foo implicitly abstract.
INCOMPLETE = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.29;

import "IFoo.sol";

contract Foo is IFoo {
    function a() external view returns (uint256) { return 0; }
}
"""

pytestmark = pytest.mark.skipif(
    shutil.which(f"solc{SOLC_VERSION}") is None,
    reason=f"solc{SOLC_VERSION} not on PATH",
)


class InterfaceOnlyAssembler(Assembler):
    """Project tree holding just the interface; the stub is written by the validator."""

    @contextlib.asynccontextmanager
    async def _dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "IFoo.sol").write_text(INTERFACE)
            yield root

    def project_directory(self):
        return self._dir()


@pytest.mark.asyncio
async def test_concrete_stub_is_accepted() -> None:
    assert await _compile_stub(CONCRETE, InterfaceOnlyAssembler(), SOLC_VERSION, STUB_PATH) is None


@pytest.mark.asyncio
async def test_abstract_stub_is_rejected() -> None:
    """The regression: solc exits 0 here and emits nothing, so only the
    bytecode check catches it."""
    error = await _compile_stub(ABSTRACT, InterfaceOnlyAssembler(), SOLC_VERSION, STUB_PATH)

    assert error is not None, "an abstract stub was accepted"
    # The message has to tell the agent what to change; it gets one retry loop.
    assert "no bytecode" in error and "abstract" in error


@pytest.mark.asyncio
async def test_stub_with_an_unimplemented_member_is_rejected() -> None:
    """Rejected by solc itself ("should be marked as abstract", non-zero exit)
    rather than by the bytecode check — pinned so the two paths stay distinct."""
    error = await _compile_stub(INCOMPLETE, InterfaceOnlyAssembler(), SOLC_VERSION, STUB_PATH)

    assert error is not None, "a stub with an unimplemented member was accepted"
    assert "should be marked as abstract" in error
