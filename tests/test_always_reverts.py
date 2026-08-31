"""Dummy-project test for the always-reverts checker (checker 1 of the expected-vacuity flow).

Runs the real checker agent over a tiny on-disk contract and asserts it recognizes a method that is
designed to always revert. Marked ``expensive`` because it calls the LLM; skipped without a real
ANTHROPIC_API_KEY (conftest installs a placeholder key for import, which is not enough to run this).
"""
import os

import pytest

from sanity_analyzer.always_reverts import (
    AlwaysRevertArgsImpl,
    async_check_always_reverts,
)

_HAS_REAL_KEY = os.environ.get("ANTHROPIC_API_KEY", "dummy-key-for-tests") != "dummy-key-for-tests"

pytestmark = [
    pytest.mark.expensive,
    pytest.mark.asyncio,
    pytest.mark.skipif(not _HAS_REAL_KEY, reason="needs a real ANTHROPIC_API_KEY"),
]

_DISABLED_CONTRACT = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

contract Disabled {
    /// @notice This entry point is permanently disabled by design and always reverts.
    /// @dev Kept for interface compatibility; callers must never rely on it succeeding.
    function disabledEntryPoint() external pure {
        revert("disabled");
    }

    function add(uint256 a, uint256 b) external pure returns (uint256) {
        return a + b;
    }
}
"""


async def test_intended_always_revert(tmp_path):
    """A method that unconditionally reverts by design is classified as intended."""
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "Disabled.sol").write_text(_DISABLED_CONTRACT)

    args = AlwaysRevertArgsImpl(
        sources_dir=str(sources),
        contract="Disabled",
        method="disabledEntryPoint()",
    )
    result = await async_check_always_reverts(args)

    assert result is not None
    assert result.intended
