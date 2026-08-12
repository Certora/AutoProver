"""``console-codegen`` accepts N specs, all gating the same generated contract.

The workflow has been plumbed for several specs (``InputData.specs``), but the
CLI mapped its triad to a one-element list, so there was no way to hand it more
than one. Natspec emits one spec per component — four for a modest contract —
and merging them by hand means reconciling four copies of the same ERC20 ghost
model, so the CLI is the thing that needed to move.

``vfs_path`` keys the specs downstream (audit's resume artifact indexes by it),
so these pin the naming: one spec keeps the conventional ``rules.spec``, several
take their file names, and a name collision is refused rather than silently
dropping a spec.
"""

import pytest

from composer.input.files import FileUploader
from composer.input.parsing import fresh_workflow_argument_parser, upload_input


class FakeUploader(FileUploader):
    """Records what it was asked to upload; returns the path as the document."""

    def __init__(self) -> None:
        self.uploaded: list[str] = []

    async def upload_text_file_if_needed(self, path: str):  # type: ignore[override]
        self.uploaded.append(path)
        return path

    async def get_document(self, path):  # type: ignore[override]
        return str(path)

    async def _upload_bytes(self, crc_basename: str, file_data: bytes, mime: str) -> str:
        raise AssertionError("upload_input should not reach the binary upload path")


def _parse(argv: list[str]):
    parser = fresh_workflow_argument_parser()
    import sys
    old, sys.argv = sys.argv, ["console-codegen", *argv]
    try:
        return parser.parse_args()
    finally:
        sys.argv = old


def test_single_spec_parses_as_before() -> None:
    args = _parse(["rules.spec", "IFoo.sol", "design.md"])

    assert args.spec_file == ["rules.spec"]
    assert args.interface_file == "IFoo.sol"
    assert args.system_doc == "design.md"


def test_several_specs_are_collected() -> None:
    args = _parse(["a.spec", "b.spec", "c.spec", "IFoo.sol", "design.md"])

    assert args.spec_file == ["a.spec", "b.spec", "c.spec"]
    assert args.interface_file == "IFoo.sol"
    assert args.system_doc == "design.md"


@pytest.mark.asyncio
async def test_one_spec_keeps_the_conventional_vfs_name() -> None:
    args = _parse(["some/where/views.spec", "IFoo.sol", "design.md"])

    data = await upload_input(FakeUploader(), args)

    assert [s.vfs_path for s in data.specs] == ["rules.spec"]


@pytest.mark.asyncio
async def test_several_specs_are_named_after_their_files() -> None:
    args = _parse(["core/views.spec", "core/withdrawal.spec", "IFoo.sol", "design.md"])

    data = await upload_input(FakeUploader(), args)

    assert [s.vfs_path for s in data.specs] == ["views.spec", "withdrawal.spec"]
    assert [s.file for s in data.specs] == ["core/views.spec", "core/withdrawal.spec"]


@pytest.mark.asyncio
async def test_colliding_spec_names_are_refused() -> None:
    args = _parse(["core/vault.spec", "periphery/vault.spec", "IFoo.sol", "design.md"])

    with pytest.raises(ValueError, match="vault.spec"):
        await upload_input(FakeUploader(), args)
