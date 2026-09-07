"""EventBus adapter — a tiny thread-safe synchronous pub/sub keyed by event type.

Handlers run in the publisher's thread. UI subscribers are responsible for marshalling onto the Qt
thread (the UI wraps its handlers in a queued signal); see helix/ui.
"""
from __future__ import annotations

import threading
from collections import defaultdict
from typing import Callable

from helix.domain.events import Event
from helix.logging_setup import get_logger

_LOG = get_logger("events")


class SignalBus:
    def __init__(self) -> None:
        self._subs: dict[type, list[Callable]] = defaultdict(list)
        self._lock = threading.RLock()

    def publish(self, event: Event) -> None:
        with self._lock:
            handlers = list(self._subs.get(type(event), ()))
        for handler in handlers:
            try:
                handler(event)
            except Exception:  # a bad subscriber must not break the publisher
                _LOG.exception("event handler failed for %s", type(event).__name__)

    def subscribe(self, event_type: type, handler: Callable) -> None:
        with self._lock:
            self._subs[event_type].append(handler)
