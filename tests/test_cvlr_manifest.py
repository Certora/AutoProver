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
_SHT_NOBITS = 8


def record(tag: int, payload: bytes) -> bytes:
    """One ``.cvlr_rules`` record: tag, little-endian length, payload."""
    return bytes([tag]) + len(payload).to_bytes(4, "little") + payload


def elf_with_cvlr_rules(payload: bytes, *, copies: int = 1, sh_type: int = _SHT_PROGBITS) -> bytes:
    """An ELF64 little-endian file whose ``.cvlr_rules`` payload is ``payload``.

    ``copies`` writes the section that many times, under the same name. A
    ``sh_type`` of ``SHT_NOBITS`` gives the section a size and no file bytes.
    """
    sections = [(".cvlr_rules", sh_type, payload)] * copies
    return _elf(sections)


def elf_without_cvlr_rules() -> bytes:
    """An ELF64 little-endian file with a string table and no ``.cvlr_rules``."""
    return _elf([])


def _elf(sections: list[tuple[str, int, bytes]]) -> bytes:
    strtab = bytearray(b"\x00")
    offsets: dict[str, int] = {}

    def name_at(name: str) -> int:
        if name not in offsets:
            offsets[name] = len(strtab)
            strtab.extend(name.encode() + b"\x00")
        return offsets[name]

    name_at(".shstrtab")
    for name, _, _ in sections:
        name_at(name)

    strtab_off = 64
    cursor = strtab_off + len(strtab)
    located: list[tuple[str, int, int, bytes]] = []
    for name, sh_type, payload in sections:
        located.append((name, sh_type, cursor, payload))
        if sh_type != _SHT_NOBITS:
            cursor += len(payload)
    shoff = cursor
    shnum = 2 + len(sections)

    def shdr(name_off: int, sh_type: int, offset: int, size: int) -> bytes:
        return struct.pack("<IIQQQQIIQQ", name_off, sh_type, 0, 0, offset, size, 0, 0, 1, 0)

    headers = bytearray()
    headers += shdr(0, 0, 0, 0)
    headers += shdr(offsets[".shstrtab"], 3, strtab_off, len(strtab))
    for name, sh_type, offset, payload in located:
        headers += shdr(offsets[name], sh_type, offset, len(payload))

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
    for _, sh_type, offset, payload in located:
        if sh_type != _SHT_NOBITS:
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


def test_a_record_cut_short_is_corrupt(tmp_path):
    # Length says four payload bytes. One is present.
    with pytest.raises(ManifestCorrupt) as exc:
        rule_names_in_elf(_so(tmp_path, elf_with_cvlr_rules(bytes([1, 4, 0, 0, 0, 0x61]))))
    assert exc.value.offset == 0


def test_a_header_cut_short_is_corrupt(tmp_path):
    payload = record(1, b"rule_a") + b"\x01\x01"
    with pytest.raises(ManifestCorrupt) as exc:
        rule_names_in_elf(_so(tmp_path, elf_with_cvlr_rules(payload)))
    assert exc.value.offset == len(record(1, b"rule_a"))


def test_a_name_that_is_not_utf8_is_corrupt(tmp_path):
    with pytest.raises(ManifestCorrupt) as exc:
        rule_names_in_elf(_so(tmp_path, elf_with_cvlr_rules(record(1, b"\xff"))))
    assert exc.value.offset == 0


def test_an_unknown_tag_is_corrupt_and_drops_the_names_before_it(tmp_path):
    payload = record(1, b"rule_a") + record(2, b"SECRET_SECTION_BYTES")
    with pytest.raises(ManifestCorrupt) as exc:
        rule_names_in_elf(_so(tmp_path, elf_with_cvlr_rules(payload)))
    assert exc.value.offset == len(record(1, b"rule_a"))
    assert "SECRET_SECTION_BYTES" not in str(exc.value)


def test_a_bad_anchor_is_corrupt(tmp_path):
    with pytest.raises(ManifestCorrupt) as exc:
        rule_names_in_elf(_so(tmp_path, elf_with_cvlr_rules(record(0, b"CV2"))))
    assert exc.value.offset == 0


def test_an_empty_name_is_corrupt(tmp_path):
    with pytest.raises(ManifestCorrupt) as exc:
        rule_names_in_elf(_so(tmp_path, elf_with_cvlr_rules(record(1, b""))))
    assert exc.value.offset == 0


def test_a_second_copy_of_the_section_is_corrupt(tmp_path):
    blob = elf_with_cvlr_rules(record(1, b"rule_a"), copies=2)
    with pytest.raises(ManifestCorrupt) as exc:
        rule_names_in_elf(_so(tmp_path, blob))
    assert exc.value.offset is None


def test_a_nobits_section_is_corrupt(tmp_path):
    # Size zero. Reading the section as present bytes would report no rules.
    blob = elf_with_cvlr_rules(b"", sh_type=_SHT_NOBITS)
    with pytest.raises(ManifestCorrupt) as exc:
        rule_names_in_elf(_so(tmp_path, blob))
    assert exc.value.offset is None


def test_a_file_that_is_not_elf_is_corrupt(tmp_path):
    with pytest.raises(ManifestCorrupt) as exc:
        rule_names_in_elf(_so(tmp_path, b"not an elf"))
    assert exc.value.offset is None


def test_an_elf_that_is_not_64_bit_little_endian_is_corrupt(tmp_path):
    blob = bytearray(elf_with_cvlr_rules(record(1, b"rule_a")))
    blob[4] = 1  # ELFCLASS32
    with pytest.raises(ManifestCorrupt) as exc:
        rule_names_in_elf(_so(tmp_path, bytes(blob)))
    assert exc.value.offset is None


def test_a_missing_file_is_corrupt(tmp_path):
    with pytest.raises(ManifestCorrupt) as exc:
        rule_names_in_elf(tmp_path / "absent.so")
    assert exc.value.offset is None
