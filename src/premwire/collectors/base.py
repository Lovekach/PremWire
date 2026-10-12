from __future__ import annotations

from dataclasses import dataclass

from premwire.models import IncomingItem


class CollectorError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CollectionBatch:
    collector: str
    items: tuple[IncomingItem, ...]
    warnings: tuple[str, ...] = ()
