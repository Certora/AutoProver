from typing import Protocol

class ReportIdentifier(Protocol):
    @property
    def stem(self) -> str: ...

