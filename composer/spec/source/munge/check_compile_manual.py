"""Manual driver for `check_edits_compile` against the `_compile_fixture` tree.

Run it from the repo root:

    uv run composer/spec/source/munge/check_compile_manual.py

Needs a working `certoraRun` and a Solidity 0.8.x compiler on PATH. If your
default `solc` isn't 0.8.x, point it at the right one:

    SOLC=solc8.19 uv run composer/spec/source/munge/check_compile_manual.py

Each scenario overlays some edits onto the on-disk fixture (`Counter.sol` +
`Math.sol`) and prints which `CompileCheck` variant comes back.
"""

import asyncio
import os
from pathlib import Path

from graphcore.tools.vfs import VFSState, vfs_tools

from composer.spec.source.munge.compile_check import check_edits_compile


PROJECT = Path(__file__).parent / "_compile_fixture"
SOLC = os.environ.get("SOLC", "solc")


# Counter.sol edited so it still compiles and still imports Math — the edited
# file must show up in the build's touched-source list.
VALID_EDIT = """\
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "./Math.sol";

contract Counter {
    uint256 public count;

    function increment(uint256 by) external {
        count = Math.add(count, by);
    }

    function reset() external {
        count = 0;
    }
}
"""

# A valid file that nothing in the build graph imports — solc never parses it,
# so it should surface as an edit that didn't reach the compilation.
UNUSED_EDIT = """\
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

contract Unused {
    uint256 public x;
}
"""

# Counter.sol with a syntax error (missing semicolon) — the build must fail.
BROKEN_EDIT = """\
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "./Math.sol";

contract Counter {
    uint256 public count;

    function increment(uint256 by) external {
        count = Math.add(count, by)
    }
}
"""


def _accessor():
    _tools, accessor = vfs_tools(
        {"immutable": False, "fs_layer": str(PROJECT)}, VFSState
    )
    return accessor


async def _run(name: str, vfs: dict[str, str], files: list[str]) -> None:
    state: VFSState = {"vfs": vfs}
    config = {"files": ["Counter.sol"], "solc": SOLC}
    result = await check_edits_compile(state, _accessor(), config, files)
    print(f"[{name}] {type(result).__name__}: {result}")


async def main() -> None:
    await _run("valid-edit", {"Counter.sol": VALID_EDIT}, ["Counter.sol"])
    await _run(
        "orphan-edit",
        {"Counter.sol": VALID_EDIT, "Unused.sol": UNUSED_EDIT},
        ["Counter.sol"],
    )
    await _run("broken-edit", {"Counter.sol": BROKEN_EDIT}, ["Counter.sol"])


if __name__ == "__main__":
    asyncio.run(main())
