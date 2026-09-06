"""certora-cli reads namespace ids with a narrower alphabet than ERC-7201 allows.

An id is cut at the first character outside [a-zA-Z.0-9], so every id under a
prefix like `my_project.` reaches the Prover as `my` and lands on one storage
slot. Two such structs in one contract make the Prover reject the build outright;
one on its own just verifies against the wrong slot. Autosetup reads the ids
correctly, so it is the one place that can tell before it turns the annotation on.
"""

import json
from pathlib import Path

import pytest

from certora_autosetup.setup.setup_erc7201 import (
    ERC7201Scanner,
    prover_visible_namespace,
    run,
)

TRUNCATED = [
    "my_project.storage.Token",
    "my_project.storage.Vault",
]
READ_WHOLE = "openzeppelin.storage.Ownable"


def test_an_id_with_an_underscore_reaches_the_prover_cut_short() -> None:
    assert prover_visible_namespace(TRUNCATED[0]) == "my"
    assert prover_visible_namespace(TRUNCATED[1]) == "my"


def test_an_id_the_prover_can_read_comes_back_whole() -> None:
    assert prover_visible_namespace(READ_WHOLE) == READ_WHOLE


def test_ids_that_collapse_are_grouped_under_what_the_prover_sees() -> None:
    scanner = ERC7201Scanner()
    scanner.found_patterns = {
        "Token.sol": [(TRUNCATED[0], 12)],
        "Vault.sol": [(TRUNCATED[1], 20)],
        "Ownable.sol": [(READ_WHOLE, 8)],
    }
    assert scanner.truncated_namespaces() == {"my": sorted(TRUNCATED)}


@pytest.mark.parametrize(
    "annotation",
    [
        "/// @custom:storage-location erc7201:{ns}",
        "/** @custom:storage-location erc7201:{ns} */",
        "/** @custom:storage-location erc7201:{ns}*/",
        " * @custom:storage-location erc7201:{ns}",
    ],
    ids=["line", "block", "block-closed-tight", "block-continuation"],
)
def test_every_comment_style_is_detected(tmp_path: Path, annotation: str) -> None:
    """OpenZeppelin's upgradeable bases annotate with `/** */`, not `///`."""
    (tmp_path / "Token.sol").write_text(
        annotation.format(ns=READ_WHOLE) + "\nstruct S { uint256 x; }\n"
    )
    scanner = ERC7201Scanner()
    scanner.scan_directory(tmp_path)
    assert scanner.get_unique_namespaces() == {READ_WHOLE}


def _project(tmp_path: Path, namespace: str, conf: dict | None = None) -> Path:
    (tmp_path / "Token.sol").write_text(
        f"/// @custom:storage-location erc7201:{namespace}\nstruct S {{ uint256 x; }}\n"
    )
    conf_path = tmp_path / "run.conf"
    conf_path.write_text(json.dumps(conf if conf is not None else {"files": ["Token.sol"]}))
    return conf_path


def test_annotation_stays_off_for_an_id_the_prover_would_truncate(tmp_path: Path) -> None:
    conf = _project(tmp_path, TRUNCATED[0])

    _, usable = run(directory=str(tmp_path), spec_output=str(tmp_path / "erc7201.spec"))

    assert usable is False
    assert "storage_extension_annotation" not in json.loads(conf.read_text())


def test_an_annotation_left_by_an_earlier_run_is_cleared(tmp_path: Path) -> None:
    """Withholding the flag does not help a config that already carries it."""
    conf = _project(
        tmp_path,
        TRUNCATED[0],
        conf={"files": ["Token.sol"], "storage_extension_annotation": True},
    )

    run(directory=str(tmp_path), spec_output=str(tmp_path / "erc7201.spec"))

    assert "storage_extension_annotation" not in json.loads(conf.read_text())


def test_annotation_is_still_enabled_for_an_id_the_prover_reads_whole(tmp_path: Path) -> None:
    conf = _project(tmp_path, READ_WHOLE)

    _, usable = run(directory=str(tmp_path), spec_output=str(tmp_path / "erc7201.spec"))

    assert usable is True
    assert json.loads(conf.read_text())["storage_extension_annotation"] is True
