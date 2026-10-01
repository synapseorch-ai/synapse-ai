"""Images — PNG, JPEG, TIFF (every frame), WebP, and HEIC with pillow-heif — read by OCR only.

An image is a document whose every page (frame) has no text layer. The
reader renders each to a grayscale PNG for the OCR seam, upright by its EXIF
orientation and at most `MAX_SIDE` pixels on its longer side; the job's
process sends them and merges the text (`ocr.py`). Without an OCR provider an
image is not read at all: `formats` refuses it as `unsupported_format`, and
reconcile reads it once a provider is set (D162 #2).

Pillow's decompression-bomb guard stays on: an image claiming more than
`MAX_PIXELS` is refused as `over_size_cap`.
"""
from __future__ import annotations

import io

from core.knowledge.parse.formats import ReadContext
from core.knowledge.parse.limits import ExpandsTooFar, OverPageCap, ParseRefused
from core.knowledge.parse.tree import DocBuilder, no_text_layer_warning
from core.knowledge.parsed import PageQuality, ParsedDocument, ParserInfo

PARSER = ParserInfo(name="image", version="1")
MAX_PIXELS = 100_000_000
MAX_SIDE = 4000


def read_image(data: bytes, ctx: ReadContext) -> ParsedDocument:
    from PIL import Image, ImageOps, UnidentifiedImageError

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    if data[4:8] == b"ftyp":
        try:
            import pillow_heif

            pillow_heif.register_heif_opener()
        except ImportError as exc:
            raise ParseRefused("A HEIC photo needs pillow-heif (the knowledge extra).") from exc
    try:
        image = Image.open(io.BytesIO(data))
        frames = getattr(image, "n_frames", 1)
    except Image.DecompressionBombError as exc:
        raise ExpandsTooFar(f"The image is larger than {MAX_PIXELS // 1_000_000} megapixels.") from exc
    except (UnidentifiedImageError, OSError) as exc:
        raise ParseRefused(f"The image is damaged and cannot be opened: {exc}.") from exc
    if ctx.max_pages and frames > ctx.max_pages:
        raise OverPageCap(frames, ctx.max_pages)
    builder = DocBuilder()
    for number in range(1, frames + 1):
        image.seek(number - 1)
        frame = ImageOps.exif_transpose(image.copy()).convert("L")
        if max(frame.size) > MAX_SIDE:
            frame.thumbnail((MAX_SIDE, MAX_SIDE))
        builder.page(number, width=float(frame.width), height=float(frame.height),
                     quality=PageQuality(no_text_layer=True, image_only=True))
        if ctx.render is not None:
            buf = io.BytesIO()
            frame.save(buf, format="PNG")
            ctx.render(number, buf.getvalue())
    builder.warnings.append(no_text_layer_warning(list(range(1, frames + 1)), ocr=False))
    return builder.build(ctx.source, PARSER)
