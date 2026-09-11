"""Generate a verifiable contract harness for a library main contract."""

from certora_autosetup.harnesser.detect import contract_kind, is_library_main_contract
from certora_autosetup.harnesser.model import HarnessPlan, LibraryApi, LibraryHarnessError

__all__ = [
    "contract_kind",
    "is_library_main_contract",
    "HarnessPlan",
    "LibraryApi",
    "LibraryHarnessError",
]
