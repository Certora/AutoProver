"""The name of a cargo feature."""

from typing import TYPE_CHECKING

# ``CargoFeature``: a feature's name, as ``[features]`` declares it and ``--features`` takes it.
if TYPE_CHECKING:
    class CargoFeature(str): ...
else:
    CargoFeature = str
