"""view_image: let the model look at a PNG or JPEG it (or someone) produced.

Tool results go back to the model as text. An image instead goes into the
conversation as a user message with an `image_url` part, right after the
turn's tool results. Each image is resent on every later turn, so only the
latest one stays; an earlier one is replaced by a note.

The bytes come through the same file tools as `file_read_bytes` (named roots,
the flavour's read permissions and size limit). Only the header is parsed
here: the format from its magic bytes, the size from the PNG IHDR or the JPEG
start-of-frame. Nothing is decoded or rendered.
"""
from __future__ import annotations

import base64
import hashlib
import struct
from typing import Any

from boring_worker_kit.outcomes import Refusal

# A data URL of this size is about 1 MiB in the conversation (the limit is 2 MiB).
MAX_IMAGE_BYTES = 768 * 1024
MAX_SIDE = 4096
MAX_PIXELS = 12_000_000
CHUNK = 65536
DEFINITION = {
    "name": "view_image",
    "description": ("Look at a PNG or JPEG file from a named root: it is attached to your next turn as an image. "
                    "Render SVG or diagrams to PNG first. Only the latest image stays in the conversation."),
    "parameters": {"type": "object", "properties": {"root": {"type": "string"}, "path": {"type": "string"}},
                   "required": ["root", "path"], "additionalProperties": False}}
NOTE = "[an earlier image was here; only the latest image stays in the conversation]"


def dimensions(data: bytes) -> tuple[str, int, int]:
    """(media type, width, height) from the header, or a refusal."""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR" and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return "image/png", width, height
    if data.startswith(b"\xff\xd8"):
        at = 2
        while at + 9 < len(data):
            if data[at] != 0xFF:
                break
            marker = data[at + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                at += 2
                continue
            length = struct.unpack(">H", data[at + 2:at + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                height, width = struct.unpack(">HH", data[at + 5:at + 9])
                return "image/jpeg", width, height
            at += 2 + length
        raise Refusal("invalid_request", "JPEG without a readable frame header")
    raise Refusal("invalid_request", "view_image takes PNG or JPEG files (render SVG to PNG first)")


async def read_image(files: Any, call_id: str, root: str, path: str) -> tuple[dict[str, Any], str]:
    """The tool's text result and the data URL to attach."""
    data, offset = b"", 0
    while True:
        chunk = await files.tools.call("file_read_bytes", {"operation": "file_read_bytes", "request_id": call_id,
                                                           "root": root, "path": path, "offset": offset,
                                                           "length": CHUNK})
        if chunk["bytes"] > MAX_IMAGE_BYTES:
            raise Refusal("limit", f"image is {chunk['bytes']} bytes; view_image takes up to {MAX_IMAGE_BYTES}")
        data += base64.b64decode(chunk["data"])
        offset += chunk["length"]
        if chunk["eof"] or not chunk["length"]:
            break
    if hashlib.sha256(data).hexdigest() != chunk["sha256"]:
        raise Refusal("busy", "the image changed while it was read; try again")
    media, width, height = dimensions(data)
    if not 0 < width <= MAX_SIDE or not 0 < height <= MAX_SIDE or width * height > MAX_PIXELS:
        raise Refusal("limit", f"image is {width}x{height}; view_image takes up to {MAX_SIDE} px a side "
                               f"and {MAX_PIXELS} pixels")
    value = {"attached": True, "root": root, "path": path, "media_type": media, "bytes": len(data),
             "width": width, "height": height, "sha256": chunk["sha256"]}
    return value, f"data:{media};base64,{base64.b64encode(data).decode('ascii')}"


def attach(messages: list[dict[str, Any]], path: str, url: str) -> None:
    """Append the image as the next user message; earlier images become a note."""
    for message in messages:
        content = message.get("content")
        if message.get("role") == "user" and isinstance(content, list):
            message["content"] = [part if part.get("type") != "image_url" else {"type": "text", "text": NOTE}
                                  for part in content]
    messages.append({"role": "user", "content": [
        {"type": "text", "text": f"The image you asked to see: {path}"},
        {"type": "image_url", "image_url": url}]})
