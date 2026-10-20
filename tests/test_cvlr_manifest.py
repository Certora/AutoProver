"""The ``.cvlr_rules`` reader, against ELF files built in the test.

The records are the contract with ``#[rule]``: a tag, a little-endian length,
and the payload. These files are not produced by rustc. A real ``.so`` is what
the expensive probes read, after they have built one.
"""

import struct
from pathlib import Path

import pytest

from composer.spec.cvlr.manifest import ManifestCorrupt, ManifestMissing, rule_names_in_elf

_SHT_PROGBITS = 1


def record(tag: int, payload: bytes) -> bytes:
    """One ``.cvlr_rules`` record: tag, little-endian length, payload."""
    return bytes([tag]) + len(payload).to_bytes(4, "little") + payload


def elf_with_cvlr_rules(payload: bytes, *, copies: int = 1) -> bytes:
    """An ELF64 little-endian file whose ``.cvlr_rules`` payload is ``payload``.

    ``copies`` writes the section that many times, under the same name.
    """
    return _elf([(".cvlr_rules", payload)] * copies)


def elf_without_cvlr_rules() -> bytes:
    """An ELF64 little-endian file with a string table and no ``.cvlr_rules``."""
    return _elf([])


def _elf(sections: list[tuple[str, bytes]]) -> bytes:
    strtab = bytearray(b"\x00")
    offsets: dict[str, int] = {}

    def name_at(name: str) -> int:
        if name not in offsets:
            offsets[name] = len(strtab)
            strtab.extend(name.encode() + b"\x00")
        return offsets[name]

    name_at(".shstrtab")
    for name, _ in sections:
        name_at(name)

    strtab_off = 64
    cursor = strtab_off + len(strtab)
    located: list[tuple[str, int, bytes]] = []
    for name, payload in sections:
        located.append((name, cursor, payload))
        cursor += len(payload)
    shoff = cursor
    shnum = 2 + len(sections)

    def shdr(name_off: int, sh_type: int, offset: int, size: int) -> bytes:
        return struct.pack("<IIQQQQIIQQ", name_off, sh_type, 0, 0, offset, size, 0, 0, 1, 0)

    headers = bytearray()
    headers += shdr(0, 0, 0, 0)
    headers += shdr(offsets[".shstrtab"], 3, strtab_off, len(strtab))
    for name, offset, payload in located:
        headers += shdr(offsets[name], _SHT_PROGBITS, offset, len(payload))

    e_ident = b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 8
    header = struct.pack(
        "<16sHHIQQQIHHHHHH",
        e_ident,
        3,  # ET_DYN
        0x3E,  # EM_X86_64
        1,
        0,
        0,
        shoff,
        0,
        64,
        0,
        0,
        64,
        shnum,
        1,  # shstrndx
    )
    blob = bytearray(shoff + len(headers))
    blob[:64] = header
    blob[strtab_off : strtab_off + len(strtab)] = strtab
    for _, offset, payload in located:
        blob[offset : offset + len(payload)] = payload
    blob[shoff:] = headers
    return bytes(blob)


def _so(tmp_path: Path, blob: bytes) -> Path:
    path = tmp_path / "prog.so"
    path.write_bytes(blob)
    return path


def test_two_names_and_an_anchor_are_the_names(tmp_path):
    payload = record(0, b"CV1") + record(1, b"rule_b") + record(1, b"rule_a")
    assert rule_names_in_elf(_so(tmp_path, elf_with_cvlr_rules(payload))) == {"rule_a", "rule_b"}


def test_a_repeated_name_is_one_rule(tmp_path):
    payload = record(1, b"rule_a") + record(0, b"CV1") + record(1, b"rule_a")
    assert rule_names_in_elf(_so(tmp_path, elf_with_cvlr_rules(payload))) == {"rule_a"}


def test_an_anchor_alone_lists_no_rules(tmp_path):
    assert rule_names_in_elf(_so(tmp_path, elf_with_cvlr_rules(record(0, b"CV1")))) == set()


def test_an_empty_section_lists_no_rules(tmp_path):
    assert rule_names_in_elf(_so(tmp_path, elf_with_cvlr_rules(b""))) == set()


def test_a_missing_section_is_not_an_empty_list(tmp_path):
    with pytest.raises(ManifestMissing):
        rule_names_in_elf(_so(tmp_path, elf_without_cvlr_rules()))


_NAME = record(1, b"rule_a")


@pytest.mark.parametrize(
    ("payload", "offset"),
    [
        pytest.param(bytes([1, 4, 0, 0, 0, 0x61]), 0, id="payload cut short"),
        pytest.param(_NAME + b"\x01\x01", len(_NAME), id="header cut short"),
        pytest.param(record(1, b"\xff"), 0, id="name not utf-8"),
        pytest.param(record(0, b"CV2"), 0, id="bad anchor"),
        pytest.param(record(1, b""), 0, id="empty name"),
        pytest.param(_NAME + record(2, b"SECRET_SECTION_BYTES"), len(_NAME), id="unknown tag"),
    ],
)
def test_a_bad_record_is_corrupt_and_drops_the_names_before_it(tmp_path, payload, offset):
    with pytest.raises(ManifestCorrupt) as exc:
        rule_names_in_elf(_so(tmp_path, elf_with_cvlr_rules(payload)))
    assert exc.value.offset == offset
    assert "SECRET_SECTION_BYTES" not in str(exc.value)


@pytest.mark.parametrize(
    "so",
    [
        pytest.param(lambda d: _so(d, elf_with_cvlr_rules(_NAME, copies=2)), id="two sections"),
        pytest.param(lambda d: _so(d, b"not an elf"), id="not an elf"),
        pytest.param(lambda d: d / "absent.so", id="missing file"),
    ],
)
def test_an_unreadable_file_is_corrupt(tmp_path, so):
    with pytest.raises(ManifestCorrupt) as exc:
        rule_names_in_elf(so(tmp_path))
    assert exc.value.offset is None
