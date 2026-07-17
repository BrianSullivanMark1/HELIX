"""The V3 sight faculty — view_screen captures the display through the vision pipeline, stays
human-driven (fenced from autonomous agents), and the JIT connect_service tool opens the panel."""
from __future__ import annotations

import base64
import io

import pytest

from helix.domain.events import ConnectRequested
from helix.ports.llm import ToolOutput
from helix.services import images
from helix.services.conversation import BUILD_TOOLS
from helix.services.tools import ToolRegistry


class _Bus:
    def __init__(self):
        self.published = []

    def publish(self, ev):
        self.published.append(ev)


def _registry(bus=None) -> ToolRegistry:
    return ToolRegistry(forge=None, builds=None, bus=bus)


def _png_bytes(size=(20, 10), color=(30, 60, 90)) -> bytes:
    PIL = pytest.importorskip("PIL.Image")
    im = PIL.new("RGB", size, color)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def test_capture_screen_returns_a_model_ready_block(monkeypatch):
    PIL = pytest.importorskip("PIL.Image")
    from PIL import ImageGrab

    grabbed = PIL.open(io.BytesIO(_png_bytes(size=(2000, 800))))  # a wide multi-monitor grab
    monkeypatch.setattr(ImageGrab, "grab", lambda all_screens=True: grabbed)
    block = images.capture_screen()
    assert block is not None
    # Downscaled through the same pipeline as an attached image (long edge capped).
    decoded = PIL.open(io.BytesIO(base64.b64decode(block.data)))
    assert max(decoded.size) <= images.MAX_EDGE
    assert block.media_type in ("image/png", "image/jpeg")


def test_capture_screen_degrades_to_none_when_grab_fails(monkeypatch):
    pytest.importorskip("PIL.Image")
    from PIL import ImageGrab

    def _boom(all_screens=True):
        raise OSError("screen locked")

    monkeypatch.setattr(ImageGrab, "grab", _boom)
    assert images.capture_screen() is None


def test_view_screen_dispatch_hands_the_model_the_capture(monkeypatch):
    from helix.ports.llm import Image

    monkeypatch.setattr(
        images, "capture_screen", lambda: Image(media_type="image/png", data="ZmFrZQ==")
    )
    out = _registry().dispatch("view_screen", {})
    assert isinstance(out, ToolOutput) and len(out.images) == 1
    assert "screen" in out.text.lower()


def test_view_screen_reports_when_capture_is_impossible(monkeypatch):
    monkeypatch.setattr(images, "capture_screen", lambda: None)
    out = _registry().dispatch("view_screen", {})
    assert isinstance(out, str) and "couldn't capture" in out


def test_screen_sight_and_connect_are_fenced_from_autonomous_agents():
    # An unattended watcher must never photograph the display or pop a credential prompt.
    assert "view_screen" in BUILD_TOOLS
    assert "connect_service" in BUILD_TOOLS
    assert "prioritize_build" in BUILD_TOOLS and "cancel_build" in BUILD_TOOLS


def test_connect_service_publishes_the_panel_request():
    bus = _Bus()
    msg = _registry(bus).dispatch(
        "connect_service", {"service": "Slack", "reason": "the watcher needs a token"}
    )
    assert [type(e) for e in bus.published] == [ConnectRequested]
    ev = bus.published[0]
    assert ev.service_id == "slack" and "watcher" in ev.reason
    assert "panel" in msg.lower()
    # The model is told the value never crosses the conversation.
    assert "never appears in this conversation" in msg


def test_connect_service_refuses_an_unknown_service():
    bus = _Bus()
    msg = _registry(bus).dispatch("connect_service", {"service": "definitely-not-a-service"})
    assert "can't connect" in msg.lower()
    assert bus.published == []


def test_go_to_sleep_publishes_the_rest_request_and_stays_off_agents():
    from helix.domain.events import SleepRequested

    bus = _Bus()
    reg = _registry(bus)
    assert "go_to_sleep" in {s.name for s in reg.specs()}
    msg = reg.dispatch("go_to_sleep", {})
    assert [type(e) for e in bus.published] == [SleepRequested]
    assert "goodnight" in msg.lower()  # the model's reply is the goodnight, not a canned confirm
    # An unattended watcher must never be able to deafen HELIX from content it processes.
    assert "go_to_sleep" in BUILD_TOOLS
