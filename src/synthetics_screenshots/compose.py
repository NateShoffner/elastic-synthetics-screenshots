"""Stitch screenshot blocks back into a full image."""

from __future__ import annotations

from io import BytesIO
from typing import Any, Mapping

from PIL import Image


def compose(
    width: int,
    height: int,
    blocks: list[dict[str, Any]],
    blobs: Mapping[str, bytes],
) -> tuple[Image.Image, int]:
    """Build the full screenshot described by a screenshot_ref document.

    Returns the image and the number of blocks that had no blob available.
    Missing blocks are left black.
    """
    canvas = Image.new("RGB", (width, height))
    missing = 0
    for block in blocks:
        blob = blobs.get(block["hash"])
        if blob is None:
            missing += 1
            continue
        with Image.open(BytesIO(blob)) as tile:
            canvas.paste(tile.convert("RGB"), (block["left"], block["top"]))
    return canvas, missing
