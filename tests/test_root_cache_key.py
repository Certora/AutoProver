"""The root cache key must fold in the AutoProver build (`tool_version`).

Without it, re-running the same repo+doc+contract with a NEW prover/detector build reuses a prior
run's cached CVL + proof results (a "SUCCEEDED with 0 prover calls" no-op). These tests pin that the
build perturbs the key, that the historical inputs still do, and that the version helper is robust.
"""

import pathlib

from composer.pipeline.cli import autoprover_version, root_cache_key


def _key(tmp: pathlib.Path, *, tool_version: str = "v1", doc: str = "D") -> str:
    p = tmp / "design.md"
    p.write_text(doc)
    return root_cache_key(
        project_root=str(tmp),
        system_doc_path=p,
        relative_path="src/Router.sol",
        contract_name="Router",
        tool_version=tool_version,
    )


def test_same_inputs_same_key(tmp_path: pathlib.Path) -> None:
    assert _key(tmp_path) == _key(tmp_path)


def test_tool_version_perturbs_the_key(tmp_path: pathlib.Path) -> None:
    # A new prover/detector build must NOT reuse a prior build's cached CVL.
    assert _key(tmp_path, tool_version="build-A") != _key(tmp_path, tool_version="build-B")


def test_doc_and_contract_still_perturb_the_key(tmp_path: pathlib.Path) -> None:
    # The historical inputs remain part of the key.
    assert _key(tmp_path, doc="one") != _key(tmp_path, doc="two")


def test_autoprover_version_is_a_nonempty_string() -> None:
    # Best-effort identity of the running build; never raises, always a usable key component.
    v = autoprover_version()
    assert isinstance(v, str) and v
