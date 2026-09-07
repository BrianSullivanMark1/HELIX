"""Image attachments → model-ready vision blocks, across both model paths.

No network, no Qt: builds real images with Pillow, runs them through the images service, and checks the
Anthropic encoder + the subscription message builder emit correct image content blocks. Invariants:
big images are downscaled to the long-edge cap, transparency picks PNG (else JPEG), non-images drop
out cleanly, the count is capped, and both model paths carry the image alongside the question.
"""
from __future__ import annotations

import asyncio
import base64
import io

import pytest

from helix.adapters.agent_sdk_chat import _image_message
from helix.adapters.anthropic_chat import AnthropicChat
from helix.domain.models import Role
from helix.ports.llm import Image, Text, Turn
from helix.services import images as im

PIL = pytest.importorskip("PIL")
from PIL import Image as PILImage  # noqa: E402


def _png(size, color, mode="RGB") -> bytes:
    buf = io.BytesIO()
    PILImage.new(mode, size, color).save(buf, "PNG")
    return buf.getvalue()


def _decode(block: Image):
    return PILImage.open(io.BytesIO(base64.b64decode(block.data)))


# ----- classification -----
def test_is_image_and_split():
    assert im.is_image("photo.PNG") and im.is_image("a.jpeg") and not im.is_image("notes.txt")
    imgs, others = im.split_images(["a.png", "b.txt", "c.JPG", "d.pdf", "e.webp"])
    assert imgs == ["a.png", "c.JPG", "e.webp"]
    assert others == ["b.txt", "d.pdf"]


# ----- encoding -----
def test_large_opaque_image_is_downscaled_to_jpeg():
    block = im.encode_image_bytes(_png((4000, 2000), (200, 100, 50)))
    assert block is not None and block.media_type == "image/jpeg"
    w, h = _decode(block).size
    assert max(w, h) == im.MAX_EDGE and (w, h) == (1568, 784)  # aspect preserved


def test_small_image_is_not_upscaled():
    block = im.encode_image_bytes(_png((40, 30), (10, 20, 30)))
    assert block is not None
    assert _decode(block).size == (40, 30)  # never enlarged


def test_transparency_keeps_png():
    block = im.encode_image_bytes(_png((60, 60), (0, 0, 0, 0), mode="RGBA"))
    assert block is not None and block.media_type == "image/png"


def test_base64_round_trips_to_a_valid_image():
    block = im.encode_image_bytes(_png((100, 80), (0, 128, 255)))
    assert block is not None
    img = _decode(block)  # decodes without raising
    assert img.size == (100, 80)


def test_non_image_bytes_drop_out():
    assert im.encode_image_bytes(b"this is not an image") is None
    assert im.encode_image_bytes(b"") is None


def test_load_images_from_files_and_cap(tmp_path):
    paths = []
    for i in range(im.MAX_IMAGES + 3):
        p = tmp_path / f"img{i}.png"
        p.write_bytes(_png((20, 20), (i % 256, 0, 0)))
        paths.append(p)
    blocks = im.load_images(paths)
    assert len(blocks) == im.MAX_IMAGES  # capped
    assert all(isinstance(b, Image) for b in blocks)


def test_load_image_block_bad_path_is_none(tmp_path):
    assert im.load_image_block(tmp_path / "nope.png") is None
    junk = tmp_path / "fake.png"
    junk.write_bytes(b"not really a png")
    assert im.load_image_block(junk) is None


# ----- API path: Anthropic content blocks -----
def test_anthropic_encode_turn_emits_image_block():
    block = im.encode_image_bytes(_png((50, 50), (1, 2, 3)))
    turn = Turn(Role.USER, (Text("what is this?"), block))
    encoded = AnthropicChat._encode_turn(turn)
    assert encoded["role"] == "user"
    kinds = [c["type"] for c in encoded["content"]]
    assert kinds == ["text", "image"]
    src = encoded["content"][1]["source"]
    assert src == {"type": "base64", "media_type": block.media_type, "data": block.data}


# ----- subscription path: stream-json user message -----
def test_subscription_image_message_shape():
    a = Image(media_type="image/jpeg", data="Zm9v")
    b = Image(media_type="image/png", data="YmFy")

    async def _first():
        async for msg in _image_message("describe these", [a, b]):
            return msg

    msg = asyncio.run(_first())
    assert msg["type"] == "user" and msg["parent_tool_use_id"] is None
    content = msg["message"]["content"]
    assert msg["message"]["role"] == "user"
    assert [c["type"] for c in content] == ["image", "image", "text"]
    assert content[0]["source"] == {"type": "base64", "media_type": "image/jpeg", "data": "Zm9v"}
    assert content[-1]["text"] == "describe these"


# ----- tool results that carry images (a located photo the model SEES) -----
def test_tool_result_with_images_encodes_for_api():
    from helix.ports.llm import ToolResult, Turn
    tr = ToolResult("tu_1", "Found 1 image.", images=(Image("image/png", "QUJD"),))
    encoded = AnthropicChat._encode_turn(Turn(Role.USER, (tr,)))
    block = encoded["content"][0]
    assert block["type"] == "tool_result"
    assert [c["type"] for c in block["content"]] == ["text", "image"]
    assert block["content"][1]["source"] == {"type": "base64", "media_type": "image/png", "data": "QUJD"}
    # a plain (text-only) tool result stays a bare string, unchanged
    plain = AnthropicChat._encode_turn(Turn(Role.USER, (ToolResult("tu_2", "just text"),)))
    assert plain["content"][0]["content"] == "just text"


def test_find_images_dispatch_returns_tooloutput_with_vision(tmp_path):
    from helix.ports.llm import ToolOutput
    from helix.services.files import FilesService
    from helix.services.tools import ToolRegistry

    class _S:
        def get(self, k, default=None):
            return None

    for nm in ("photo_one.png", "photo_two.png"):
        buf = io.BytesIO(); PILImage.new("RGB", (32, 32), (9, 9, 9)).save(buf, "PNG")
        (tmp_path / nm).write_bytes(buf.getvalue())
    fs = FilesService(_S(), root=tmp_path / "no_root", data=tmp_path / "no_data")
    reg = ToolRegistry(forge=None, builds=None, files=fs)

    out = reg.dispatch("find_images", {"folder": str(tmp_path)})
    assert isinstance(out, ToolOutput) and len(out.images) == 2 and "<<<IMAGES-" in out.text
    assert all(isinstance(b, Image) for b in out.images)

    view = reg.dispatch("view_image", {"path": str(tmp_path / "photo_one.png")})
    assert isinstance(view, ToolOutput) and len(view.images) == 1
    # a non-image path comes back as a plain string refusal, not a ToolOutput
    (tmp_path / "n.txt").write_text("x")
    assert isinstance(reg.dispatch("view_image", {"path": str(tmp_path / "n.txt")}), str)
