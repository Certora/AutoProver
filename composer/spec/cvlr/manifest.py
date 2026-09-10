"""Rule names from a built program's ``.cvlr_rules`` section.

``#[rule]`` writes one record per rule into that section: an ELF section of the
SBF ``.so`` on Solana, and a custom section of the ``.wasm`` on Soroban, which
is what ``link_section`` produces on a wasm target. Neither prover reads the
section, so a submission still names its rules in the conf.

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
    ``None`` when the file is not a readable ELF, or the section itself cannot
    be loaded.
    """

    def __init__(self, message: str, *, offset: int | None = None) -> None:
        super().__init__(message)
        self.offset = offset


def rule_names_in(artifact: Path) -> set[str]:
    """The rule names in a built artifact, whichever chain built it: wasm by its suffix, ELF
    otherwise. Raises as :func:`rule_names_in_elf` does."""
    if artifact.suffix == ".wasm":
        return rule_names_in_wasm(artifact)
    return rule_names_in_elf(artifact)


def rule_names_in_elf(so: Path) -> set[str]:
    """The rule names in ``so``'s ``.cvlr_rules`` section.

    :raises ManifestMissing: the section is absent.
    :raises ManifestCorrupt: the file or the section cannot be read. The
        names that parsed before the bad record are not returned.
    """
    return _names(_section(so))


def _section(so: Path) -> bytes:
    try:
        with so.open("rb") as stream:
            found = [s for s in ELFFile(stream).iter_sections() if s.name == _SECTION]
            if len(found) > 1:
                raise ManifestCorrupt(f"{so} has more than one {_SECTION} section")
            data = found[0].data() if found else None
    except (ELFError, OSError, ValueError) as exc:
        raise ManifestCorrupt(f"{so} could not be read as an ELF") from exc
    if data is None:
        raise ManifestMissing(f"{so} has no {_SECTION} section")
    return data


_WASM_MAGIC = b"\0asm"
_WASM_CUSTOM = 0


def _leb128(data: bytes, offset: int) -> tuple[int, int]:
    """An unsigned LEB128 at ``offset``: its value, and the offset after it."""
    value = shift = 0
    while True:
        if offset >= len(data) or shift > 35:
            raise ValueError("malformed LEB128")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return value, offset


def rule_names_in_wasm(wasm: Path) -> set[str]:
    """The rule names in ``wasm``'s ``.cvlr_rules`` custom section.

    :raises ManifestMissing: the section is absent.
    :raises ManifestCorrupt: the file or the section cannot be read.
    """
    return _names(_wasm_section(wasm))


def _wasm_section(wasm: Path) -> bytes:
    try:
        data = wasm.read_bytes()
    except OSError as exc:
        raise ManifestCorrupt(f"{wasm} could not be read") from exc
    if data[:4] != _WASM_MAGIC or len(data) < 8:
        raise ManifestCorrupt(f"{wasm} is not a wasm module")
    found: list[bytes] = []
    offset = 8
    try:
        while offset < len(data):
            section_id = data[offset]
            size, body = _leb128(data, offset + 1)
            end = body + size
            if end > len(data):
                raise ValueError("section runs past the end of the module")
            if section_id == _WASM_CUSTOM:
                name_len, name_at = _leb128(data, body)
                if data[name_at : name_at + name_len] == _SECTION.encode():
                    found.append(data[name_at + name_len : end])
            offset = end
    except ValueError as exc:
        raise ManifestCorrupt(f"{wasm} could not be read as a wasm module") from exc
    if len(found) > 1:
        raise ManifestCorrupt(f"{wasm} has more than one {_SECTION} section")
    if not found:
        raise ManifestMissing(f"{wasm} has no {_SECTION} section")
    return found[0]


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
