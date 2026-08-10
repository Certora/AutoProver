"""ERC-7201 namespaced-storage-location derivation.

The formula (from the standard):

    erc7201(id) = keccak256(abi.encode(uint256(keccak256(id)) - 1)) & ~bytes32(uint256(0xff))

Provided to the editor as a tool so storage-location constants are computed,
never modeled: a hallucinated hash is self-consistent, compiles, and survives
every downstream gate.
"""

from typing import override

from pydantic import Field

from graphcore.tools.schemas import WithImplementation


def _keccak256(data: bytes) -> bytes:
    from Crypto.Hash import keccak

    return keccak.new(digest_bits=256, data=data).digest()


def erc7201_slot(namespace: str) -> str:
    """The ERC-7201 storage location for ``namespace``, as a 0x-prefixed
    32-byte hex constant."""
    inner = int.from_bytes(_keccak256(namespace.encode("utf-8")), "big")
    outer = _keccak256((inner - 1).to_bytes(32, "big"))
    return "0x" + (outer[:-1] + b"\x00").hex()


def keccak256_hex(text: str) -> str:
    """keccak256 of the UTF-8 encoding of ``text``, as a 0x-prefixed 32-byte
    hex literal."""
    return "0x" + _keccak256(text.encode("utf-8")).hex()


class ComputeKeccakString(WithImplementation[str]):
    """
    Compute keccak256 of a string's UTF-8 bytes, as a 0x-prefixed bytes32 hex
    literal. Use this to reproduce the value of a hash constant the source
    already declares — e.g. embedding the value of keccak256("some.namespace")
    in a @custom:certoralink annotation. For minting a NEW namespaced storage
    location use erc7201_slot instead; the two formulas differ.
    """

    text: str = Field(description="The exact string to hash, e.g. \"some.namespace\"")

    @override
    def run(self) -> str:
        return f'keccak256("{self.text}") = {keccak256_hex(self.text)}'


class ComputeErc7201Slot(WithImplementation[str]):
    """
    Compute the ERC-7201 storage location for a namespace id:
    keccak256(abi.encode(uint256(keccak256(id)) - 1)) & ~bytes32(uint256(0xff)).
    Every namespaced storage-location constant you write comes from this tool;
    never derive one yourself.
    """

    namespace: str = Field(description="The namespace id, e.g. \"example.storage.Main\"")

    @override
    def run(self) -> str:
        slot = erc7201_slot(self.namespace)
        return (
            f"ERC-7201 storage location for {self.namespace}:\n"
            f"  slot constant: {slot}\n"
            f"  struct annotation: /// @custom:storage-location erc7201:{self.namespace}"
        )
