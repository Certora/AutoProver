"""Rule names from a built program's ``.cvlr_rules`` section.

``#[rule]`` writes one record per rule into that section of the SBF ``.so``.
The Solana prover does not read the section, so a submission still names its
rules in the conf. The names come from the ``.so`` the gate just built.

A record is a tag byte, a little-endian ``u32`` length, and that many payload
bytes. Tag 0 is the anchor and its payload is ``CV1``. Tag 1 is one rule
name, UTF-8, at least one byte. A repeated name is one rule. Any other tag,
a bad anchor, an empty name, bytes that are not UTF-8, or a record cut short
is a section that cannot be trusted, and the read stops there.
"""

from pathlib import Path

from elftools.common.exceptions import ELFError
from elftools.elf.elffile import ELFFile

_SECTION = ".cvlr_rules"
_ANCHOR = b"CV1"
_HEADER = 5


class ManifestMissing(Exception):
    """The shared object has no ``.cvlr_rules`` section.

    An empty section is a different result: the build listed no rules. A
    missing section means the rules were not listed at all.
    """


class ManifestCorrupt(Exception):
    """The ``.cvlr_rules`` section, or the ELF around it, could not be read.

    ``offset`` is the byte in the section where the bad record starts. It is
    ``None`` when the file is not a readable ELF64 little-endian object, or
    the section itself cannot be loaded.
    """

    def __init__(self, message: str, *, offset: int | None = None) -> None:
        super().__init__(message)
        self.offset = offset


def rule_names_in_elf(so: Path) -> set[str]:
    """The rule names in ``so``'s ``.cvlr_rules`` section.

    :raises ManifestMissing: the section is absent.
    :raises ManifestCorrupt: the file or the section cannot be read. The
        names that parsed before the bad record are not returned.
    """
    try:
        data = _section(so)
    except (ManifestMissing, ManifestCorrupt):
        raise
    except (ELFError, OSError, ValueError) as exc:
        raise ManifestCorrupt(f"{so} could not be read as an ELF") from exc
    return _names(data)


def _section(so: Path) -> bytes:
    with so.open("rb") as stream:
        ident = stream.read(16)
        stream.seek(0)
        # EI_CLASS 2 is 64-bit. EI_DATA 1 is little-endian. Anything else is
        # not an SBF object, and a 32-bit parse of these headers is not a list
        # of rules.
        if len(ident) < 6 or ident[:4] != b"\x7fELF" or ident[4] != 2 or ident[5] != 1:
            raise ManifestCorrupt(f"{so} is not an ELF64 little-endian file")
        elf = ELFFile(stream)
        found = [section for section in elf.iter_sections() if section.name == _SECTION]
        if not found:
            raise ManifestMissing(f"{so} has no {_SECTION} section")
        if len(found) != 1:
            raise ManifestCorrupt(f"{so} has more than one {_SECTION} section")
        section = found[0]
        # No file bytes. ``data()`` would invent zeros, and those are not records.
        if section["sh_type"] == "SHT_NOBITS":
            raise ManifestCorrupt(f"{so} has a {_SECTION} section with no file bytes")
        return section.data()


def _names(data: bytes) -> set[str]:
    names: set[str] = set()
    offset = 0
    end = len(data)
    while offset < end:
        if offset + _HEADER > end:
            raise ManifestCorrupt("truncated record header", offset=offset)
        tag = data[offset]
        length = int.from_bytes(data[offset + 1 : offset + _HEADER], "little")
        payload_at = offset + _HEADER
        if payload_at + length > end:
            raise ManifestCorrupt("truncated record payload", offset=offset)
        payload = data[payload_at : payload_at + length]
        if tag == 0:
            if payload != _ANCHOR:
                raise ManifestCorrupt("anchor payload is not CV1", offset=offset)
        elif tag == 1:
            if not payload:
                raise ManifestCorrupt("rule name is empty", offset=offset)
            try:
                name = payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ManifestCorrupt("rule name is not UTF-8", offset=offset) from exc
            names.add(name)
        else:
            raise ManifestCorrupt(f"unknown record tag {tag}", offset=offset)
        offset = payload_at + length
    return names
