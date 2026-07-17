"""Signal registry: the ordered list of all deterministic amenability signals."""

from certora_autosetup.amenability.signals.arithmetic import mixed_theory, unchecked_nonlinear
from certora_autosetup.amenability.signals.assembly import (
    asm_density,
    asm_fp_manipulation,
    asm_trampoline,
)
from certora_autosetup.amenability.signals.bitmask import bitmask_style
from certora_autosetup.amenability.signals.calls import external_call_surface
from certora_autosetup.amenability.signals.functions import function_length, surface_shape
from certora_autosetup.amenability.signals.loops import dynamic_loops
from certora_autosetup.amenability.signals.storage import storage_packing
from certora_autosetup.amenability.signals.summaries import curated_summary_hits

ALL_SIGNALS = [
    asm_density,
    asm_fp_manipulation,
    asm_trampoline,
    bitmask_style,
    function_length,
    unchecked_nonlinear,
    mixed_theory,
    curated_summary_hits,
    storage_packing,
    external_call_surface,
    dynamic_loops,
    surface_shape,
]

__all__ = ["ALL_SIGNALS"]
