"""Natspec typecheck paths must point at files that were actually written.

``temp_certora_file`` yields a path already relative to the project root, i.e.
one that carries the ``certora/`` segment. ``ConfigurationBuilder`` prefixed that
segment a second time in two places — the conf's own location and the ``verify``
attribute — producing ``certora/certora/...`` paths nothing ever wrote to. The
Certora CLI then failed every natspec typecheck (``read_from_conf_file: ... not
found``, then ``attribute/flag 'verify': file ... not found``), and since
``publish`` is gated on a passing typecheck, no spec could ever be published.

The invariant both cases violated: every path the conf hands to the CLI must
resolve, from the project root, to a file on disk. ``typecheck.py`` runs the CLI
with ``cwd`` set to that root, so the CLI resolves them the same way.
"""

import json
import pathlib

from composer.spec.natspec.task_description import ConfigurationBuilder
from composer.spec.types import SolidityIdentifier
from composer.spec.util import temp_certora_file


def test_conf_paths_resolve_from_the_project_root(tmp_path: pathlib.Path) -> None:
    """Mirrors typecheck.run_typecheck: materialize a spec, build a conf around it."""
    with temp_certora_file(content="rule sanity { assert true; }", root=str(tmp_path), ext="spec") as spec_file:
        builder = (
            ConfigurationBuilder({"solc": "solc8.29"})
            .with_files(["A.sol"])
            .with_verify(main_contract=SolidityIdentifier("A"), spec_file=spec_file)
        )
        with builder.build_to(tmp_path) as conf:
            assert conf.is_file(), f"conf not written at yielded path: {conf}"
            # One `certora` segment in the conf's own location, never two.
            assert conf.relative_to(tmp_path).parts[:-1] == ("certora",)

            config = json.loads(conf.read_text())
            verify_path = config["verify"].partition(":")[2]
            assert (tmp_path / verify_path).is_file(), (
                f"verify points at a file that was never written: {verify_path}"
            )

    assert not conf.exists(), "conf should be cleaned up on context exit"
