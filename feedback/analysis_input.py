"""Read-only contract shared by ORM and external-table analysis adapters.

Adapters expose fixed input versions; they never create website submissions.
"""

from dataclasses import dataclass
from typing import Iterable, Mapping, Protocol, runtime_checkable


@dataclass(frozen=True)
class AnalysisField:
    name: str
    data_type: str
    nullable: bool
    title: str = ""
    kind: str = "long_text"
    options: tuple[str, ...] = ()
    tracked: bool = False


@runtime_checkable
class AnalysisInput(Protocol):
    """Versioned, read-only rows for statistics, NLP, or future training."""

    @property
    def dataset_version(self) -> str: ...

    def fields(self) -> tuple[AnalysisField, ...]: ...

    def scan(self, columns: tuple[str, ...]) -> Iterable[Mapping[str, object]]: ...
