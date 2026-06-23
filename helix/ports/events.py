"""EventBus port — decouples 'a build finished' from 'the menu refreshes'."""
from __future__ import annotations

from typing import Callable, Protocol, TypeVar

from helix.domain.events import Event

E = TypeVar("E", bound=Event)


class EventBus(Protocol):
    def publish(self, event: Event) -> None: ...

    def subscribe(self, event_type: type[E], handler: Callable[[E], None]) -> None: ...
