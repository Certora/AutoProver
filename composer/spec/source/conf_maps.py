"""Per-file compiler-map maintenance for prover confs.

certoraRun's per-file compiler maps are all-or-nothing: when a map attribute is
present in a conf, *every* input file must be matched by it — an unmatched file
is a hard error — and when it is absent, introducing it for a single new file
would instantly unmatch every other input. So a seam that adds a file to a conf
must supply an entry for each present map (unless the map already carries the
file's entry), and must refuse a setting aimed at an absent map.
:func:`extend_compiler_maps` enforces both directions and reports violations as
data; there is no defaulting in either direction.

Map keys are CONTRACT names, not paths: a ``files`` entry contributes the
contract named by its ``:Contract`` suffix when present, and its file stem
otherwise (the same convention ``config_edit``'s AddFile encodes). The map-key
roster comes from the certora prover CLI's own map attributes, with the vyper
maps omitted (autoprover does not do vyper).
"""

from dataclasses import dataclass
from pathlib import PurePosixPath

from pydantic import BaseModel, Field


class CompilerSettings(BaseModel):
    """Explicit per-file compiler settings for a new file. Each field is
    required exactly when the prover configuration carries the corresponding
    per-file map and the map lacks the file's entry — and is rejected when
    the configuration does not carry that map (the global settings already
    govern every file)."""

    solc: str | None = Field(
        default=None,
        description="Compiler executable for this file, e.g. 'solc4.24'; configuration map attribute: `solc_map` / `compiler_map`",
    )
    optimize: str | int | None = Field(
        default=None,
        description="Optimizer setting for this file; configuration map attribute: `solc_optimize_map`",
    )
    via_ir: bool | None = Field(
        default=None,
        description="Whether this file compiles via IR; configuration map attribute `solc_via_ir_map`",
    )
    evm_version: str | None = Field(
        default=None,
        description="EVM version for this file; configuration map attributes `solc_evm_version_map`",
    )


# CompilerSettings field -> the conf map spellings it feeds.
_SETTING_TO_MAPS: dict[str, tuple[str, ...]] = {
    "solc": ("solc_map", "compiler_map"),
    "optimize": ("solc_optimize_map",),
    "via_ir": ("solc_via_ir_map",),
    "evm_version": ("solc_evm_version_map",),
}

COMPILER_MAP_KEYS = tuple(k for keys in _SETTING_TO_MAPS.values() for k in keys)

type MapValue = str | int | bool


def files_entry_contract(entry: str) -> str:
    """The contract a ``files`` entry contributes to the scene — the explicit
    ``:Contract`` override when present, the file stem otherwise. This is the
    key the per-file compiler maps are indexed by."""
    path, sep, contract = entry.partition(":")
    if sep:
        return contract
    return PurePosixPath(path).stem


@dataclass(frozen=True)
class ExtendedMaps:
    """The updated conf, plus ``{contract: {map_key: value}}`` for the entries
    written (empty when the conf has no per-file maps or every contract was
    already present)."""
    config: dict
    written: dict[str, dict[str, MapValue]]


@dataclass(frozen=True)
class RequiredSetting:
    """One present map demanding an entry for a file: the ``CompilerSettings``
    field to set, the conf map that demands it, and the distinct values the map
    already uses (as candidates)."""
    field: str
    map_key: str
    existing: tuple[MapValue, ...]


@dataclass(frozen=True)
class MissingSettings:
    """``entry``'s contract is absent from one or more present maps and no
    explicit setting was given for them."""
    entry: str
    contract: str
    required: tuple[RequiredSetting, ...]


@dataclass(frozen=True)
class SpuriousSettings:
    """``entry`` declares settings for maps the conf does not carry."""
    entry: str
    fields: tuple[str, ...]


@dataclass(frozen=True)
class MapViolations:
    """Everything wrong with the requested additions, both directions at once,
    so a caller can fix the whole file set in one pass."""
    missing: tuple[MissingSettings, ...]
    spurious: tuple[SpuriousSettings, ...]


def map_violation_message(v: MapViolations) -> str:
    """Agent-facing rendering of :class:`MapViolations`."""
    lines = ["The prover configuration's per-file compiler maps reject this file set:"]
    for m in v.missing:
        for r in m.required:
            lines.append(
                f"- {m.entry} (contract `{m.contract}`): the configuration carries "
                f"`{r.map_key}` and has no entry for this contract, so "
                f"`compiler_settings.{r.field}` must be provided "
                f"(values the map currently uses: {', '.join(str(e) for e in r.existing)})"
            )
    for s in v.spurious:
        named = ", ".join(f"compiler_settings.{f}" for f in s.fields)
        lines.append(
            f"- {s.entry}: {named} declared, but the configuration carries no corresponding "
            "per-file map — do not set these; the global settings already govern every file"
        )
    return "\n".join(lines)


def _distinct(m: dict) -> tuple[MapValue, ...]:
    return tuple(dict.fromkeys(m.values()))


def extend_compiler_maps(
    config: dict, new_files: list[tuple[str, CompilerSettings | None]]
) -> ExtendedMaps | MapViolations:
    """A copy of ``config`` whose per-file compiler maps carry an entry for
    each of ``new_files``, or the full set of violations when the declared
    settings disagree with which maps the conf carries. Maps are keyed by
    contract name (see :func:`files_entry_contract`). An explicit setting
    always writes the contract's entry; an unset one is acceptable only when
    the map already has that contract. ``config`` is never mutated; maps that
    gain entries are fresh dicts, everything else is shared."""
    additions: dict[str, dict[str, MapValue]] = {}
    written: dict[str, dict[str, MapValue]] = {}
    missing: list[MissingSettings] = []
    spurious: list[SpuriousSettings] = []

    for f, settings in new_files:
        contract = files_entry_contract(f)
        required: list[RequiredSetting] = []
        spurious_fields: list[str] = []
        for field, spellings in _SETTING_TO_MAPS.items():
            present = [
                k for k in spellings
                if isinstance(config.get(k), dict) and config[k]
            ]
            explicit: MapValue | None = (
                getattr(settings, field) if settings is not None else None
            )
            if explicit is not None:
                if not present:
                    spurious_fields.append(field)
                    continue
                for k in present:
                    additions.setdefault(k, {})[contract] = explicit
                    written.setdefault(contract, {})[k] = explicit
            else:
                for k in present:
                    if contract not in config[k]:
                        required.append(RequiredSetting(
                            field=field, map_key=k, existing=_distinct(config[k]),
                        ))
        if required:
            missing.append(MissingSettings(
                entry=f, contract=contract, required=tuple(required),
            ))
        if spurious_fields:
            spurious.append(SpuriousSettings(entry=f, fields=tuple(spurious_fields)))

    if missing or spurious:
        return MapViolations(missing=tuple(missing), spurious=tuple(spurious))

    updated = dict(config)
    for k, adds in additions.items():
        updated[k] = {**config[k], **adds}
    return ExtendedMaps(config=updated, written=written)
