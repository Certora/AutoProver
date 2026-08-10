"""``ConfigurationBuilder.build_to`` must yield the path it actually wrote to.

``temp_certora_file`` yields a path already relative to the project root, i.e.
one that carries the ``certora/`` segment. Re-prefixing it produced
``<root>/certora/certora/run_<uid>.conf`` — a path nothing ever wrote — and the
Certora CLI failed every natspec typecheck with ``read_from_conf_file: ... not
found``. Since ``publish`` is gated on a passing typecheck, no spec could ever
be published.
"""

import json
import pathlib

from composer.spec.natspec.task_description import ConfigurationBuilder


def test_build_to_yields_the_written_conf(tmp_path: pathlib.Path) -> None:
    builder = ConfigurationBuilder({"solc": "solc8.29"}).with_files(["A.sol"])

    with builder.build_to(tmp_path) as conf:
        assert conf.is_file(), f"conf not written at yielded path: {conf}"
        assert json.loads(conf.read_text()) == {"solc": "solc8.29", "files": ["A.sol"]}
        # The regression: one `certora` segment, never two.
        assert conf.relative_to(tmp_path).parts[:-1] == ("certora",)

    assert not conf.exists(), "conf should be cleaned up on context exit"
