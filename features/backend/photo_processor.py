from __future__ import annotations

import io
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import Optional

from PIL import (
    Image,
    ImageColor,
    ImageDraw,
    ImageFont,
    ImageOps,
    UnidentifiedImageError,
)

def _env(name: str, default, cast=int):
    try:
        return cast(os.environ.get(name, default))
    except ValueError:
        return default


MAX_UPLOAD_BYTES = _env("FLOWFILES_MAX_UPLOAD_MB", 25) * 1024 * 1024
MAX_PIXELS = _env("FLOWFILES_MAX_PIXELS", 40_000_000)      # decompression-bomb guard
MAX_DIMENSION = 16_000
MAX_TEXT_LENGTH = 300
PROCESS_TIMEOUT_S = _env("FLOWFILES_PROCESS_TIMEOUT_S", 30.0, float)
MAX_BATCH_FILES = _env("FLOWFILES_MAX_BATCH_FILES", 20)
MAX_BATCH_BYTES = _env("FLOWFILES_MAX_BATCH_MB", 100) * 1024 * 1024
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


class ImageError(Exception):
    """A problem the caller can understand and fix (bad file, bad option)."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code



_ALIASES = {
    "jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP",
    "bmp": "BMP", "gif": "GIF", "tif": "TIFF", "tiff": "TIFF",
}
_MIME = {
    "JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp",
    "BMP": "image/bmp", "GIF": "image/gif", "TIFF": "image/tiff",
}
_EXT = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "BMP": "bmp", "GIF": "gif", "TIFF": "tiff"}
SUPPORTED_FORMATS = tuple(sorted(_MIME))


def normalize_format(name: str) -> str:
    key = (name or "").strip().lower().lstrip(".")
    if key not in _ALIASES:
        raise ImageError(
            f"Unsupported format {name!r}. Use one of: {', '.join(SUPPORTED_FORMATS)}.", 400
        )
    return _ALIASES[key]


@dataclass
class LoadedImage:
    image: Image.Image
    format: str                       # detected input format, e.g. "JPEG"
    size_bytes: int
    taken_at: Optional[datetime] = None   # from EXIF, if present
    frames: int = 1                       # >1 means an animated image


@dataclass
class EncodedImage:
    data: bytes
    format: str
    info: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    size: Optional[tuple] = None          # (width, height) of the encoded image
    verified: bool = False                # output was re-opened and checked

    @property
    def mime(self) -> str:
        return _MIME[self.format]

    @property
    def ext(self) -> str:
        return _EXT[self.format]


def _read_taken_at(img: Image.Image) -> Optional[datetime]:
    try:
        exif = img.getexif()
        raw = exif.get_ifd(0x8769).get(36867) or exif.get(36867) or exif.get(306)
        if isinstance(raw, bytes):
            raw = raw.decode("ascii", "ignore")
        if raw:
            return datetime.strptime(str(raw).strip()[:19], "%Y:%m:%d %H:%M:%S")
    except Exception:
        pass
    return None


def _normalize_mode(img: Image.Image) -> Image.Image:
    if img.mode in ("RGB", "RGBA"):
        return img
    has_alpha = img.mode in ("LA", "PA") or "transparency" in img.info
    return img.convert("RGBA" if has_alpha else "RGB")


def load_image(data: bytes) -> LoadedImage:
    """Validate raw bytes and return a decoded, upright RGB/RGBA image."""
    if not data:
        raise ImageError("The uploaded file is empty.", 400)
    if len(data) > MAX_UPLOAD_BYTES:
        raise ImageError(f"File is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.", 413)

    try:
        with Image.open(io.BytesIO(data)) as probe:
            fmt = probe.format
            if fmt not in _MIME:
                raise ImageError(f"Unsupported image type {fmt!r}.", 415)
            if probe.width * probe.height > MAX_PIXELS:
                raise ImageError("Image has too many pixels.", 413)
            probe.verify()
    except ImageError:
        raise
    except UnidentifiedImageError:
        raise ImageError("This file is not a recognised image.", 415)
    except Image.DecompressionBombError:
        raise ImageError("Image has too many pixels.", 413)
    except Exception:
        raise ImageError("The image file is corrupted.", 422)

    try:
        img = Image.open(io.BytesIO(data))
        img.load()                       # forces full decode; catches truncation
        frames = getattr(img, "n_frames", 1)
        taken_at = _read_taken_at(img)
        img = ImageOps.exif_transpose(img)   # honour phone-camera rotation
        img = _normalize_mode(img)
    except Exception:
        raise ImageError("The image file is corrupted or truncated.", 422)

    return LoadedImage(img, fmt, len(data), taken_at, frames)



_NO_ALPHA = ("JPEG", "BMP", "GIF")      # formats that cannot store transparency


def has_transparency(img: Image.Image) -> bool:
    return img.mode == "RGBA" and img.getchannel("A").getextrema()[0] < 255


def _flatten(img: Image.Image, background=(255, 255, 255)) -> Image.Image:
    """Remove transparency (for formats that cannot store it)."""
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, background)
        bg.paste(img, mask=img.getchannel("A"))
        return bg
    return img.convert("RGB")


def verify_output(data: bytes, fmt: str, size: tuple) -> None:
    """Re-open the encoded bytes; never report success for a broken output."""
    try:
        with Image.open(io.BytesIO(data)) as out:
            if out.format != fmt or out.size != tuple(size):
                raise ImageError("Output verification failed: unexpected format or size.", 500)
            out.load()
    except ImageError:
        raise
    except Exception:
        raise ImageError("Output verification failed: the result could not be read back.", 500)


def _mark_verified(out: EncodedImage) -> EncodedImage:
    verify_output(out.data, out.format, out.size)
    out.verified = True
    return out


def encode(
    img: Image.Image,
    fmt: str,
    quality: int = 90,
    flatten_background: Optional[str] = None,
    verify: bool = True,
) -> EncodedImage:
    """Encode ``img`` as ``fmt``.

    PNG, WEBP and TIFF keep transparency. JPEG, BMP and GIF cannot, so an image
    that really is transparent needs an explicit ``flatten_background`` colour.
    """
    fmt = normalize_format(fmt)
    if not 1 <= quality <= 100:
        raise ImageError("quality must be between 1 and 100.", 400)

    warnings: list = []
    source = img
    if fmt in _NO_ALPHA:
        if has_transparency(img):
            if not flatten_background:
                raise ImageError(
                    f"This image has transparency, which {fmt} cannot store. "
                    "Choose a background colour (flatten_background, e.g. #FFFFFF) "
                    "or pick PNG/WEBP as the output format.", 400)
            source = _flatten(img, _rgb(flatten_background, "flatten_background"))
            warnings.append(f"Transparency was replaced with the background {flatten_background}.")
        else:
            source = _flatten(img)

    buf = io.BytesIO()
    try:
        if fmt == "JPEG":
            source.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
        elif fmt == "PNG":
            source.save(buf, "PNG", optimize=True)
        elif fmt == "WEBP":
            source.save(buf, "WEBP", quality=quality, method=4)
        elif fmt == "BMP":
            source.save(buf, "BMP")
        elif fmt == "GIF":
            source.convert("P", palette=Image.Palette.ADAPTIVE, colors=256).save(
                buf, "GIF", optimize=True)
        elif fmt == "TIFF":
            source.save(buf, "TIFF", compression="tiff_lzw")
    except (OSError, ValueError):
        raise ImageError(f"This image cannot be saved as {fmt} (check its dimensions).", 422)

    out = EncodedImage(buf.getvalue(), fmt, {"quality": quality}, warnings, source.size)
    return _mark_verified(out) if verify else out


def convert_format(img: Image.Image, target_format: str, quality: int = 90,
                   flatten_background: Optional[str] = None) -> EncodedImage:
    return encode(img, target_format, quality, flatten_background)



def resize_image(
    img: Image.Image,
    width: Optional[int] = None,
    height: Optional[int] = None,
    scale: Optional[float] = None,
    mode: str = "fit",
) -> Image.Image:
    """Resize by ``scale`` or by width/height.

    mode (when both width and height are given):
      fit   - keep aspect ratio, image fits inside the box
      exact - stretch to exactly width x height
      fill  - keep aspect ratio, crop the overflow so the box is filled
    A single dimension always keeps the aspect ratio.
    """
    w, h = img.size
    if scale is not None:
        if width or height:
            raise ImageError("Use either scale or width/height, not both.", 400)
        if not 0.01 <= scale <= 10:
            raise ImageError("scale must be between 0.01 and 10.", 400)
        new = (max(1, round(w * scale)), max(1, round(h * scale)))
    else:
        if not width and not height:
            raise ImageError("Provide width, height or scale.", 400)
        for label, v in (("width", width), ("height", height)):
            if v is not None and v <= 0:
                raise ImageError(f"{label} must be a positive number.", 400)
        if mode not in ("fit", "exact", "fill"):
            raise ImageError("mode must be fit, exact or fill.", 400)
        if width and not height:
            new = (width, max(1, round(h * width / w)))
        elif height and not width:
            new = (max(1, round(w * height / h)), height)
        elif mode == "exact" or mode == "fill":
            new = (width, height)
        else:
            ratio = min(width / w, height / h)
            new = (max(1, round(w * ratio)), max(1, round(h * ratio)))

    if max(new) > MAX_DIMENSION or new[0] * new[1] > MAX_PIXELS:
        raise ImageError("Requested size is too large.", 400)

    if mode == "fill" and width and height and scale is None:
        return ImageOps.fit(img, new, Image.Resampling.LANCZOS)
    return img.resize(new, Image.Resampling.LANCZOS)


def check_deadline(deadline: Optional[float]) -> None:
    if deadline is not None and time.monotonic() > deadline:
        raise ImageError("Processing exceeded the time limit.", 504)


def _encode_png_reduced(img: Image.Image, quality: int) -> EncodedImage:
    """Lossy PNG: reduce the palette (quality 100 = lossless)."""
    if quality >= 100:
        return encode(img, "PNG")
    colors = max(16, min(256, int(quality * 2.56)))
    method = Image.Quantize.FASTOCTREE if img.mode == "RGBA" else Image.Quantize.MEDIANCUT
    buf = io.BytesIO()
    img.quantize(colors=colors, method=method).save(buf, "PNG", optimize=True)
    out = EncodedImage(buf.getvalue(), "PNG", {"quality": quality, "colors": colors},
                       [f"PNG colours were reduced to {colors} (lossy)."], img.size)
    return _mark_verified(out)


def compress_image(
    img: Image.Image,
    fmt: str,
    quality: int = 75,
    max_bytes: Optional[int] = None,
    max_dimension: Optional[int] = None,
    flatten_background: Optional[str] = None,
    deadline: Optional[float] = None,
) -> EncodedImage:
    """Shrink file size. JPEG/WEBP use quality; PNG reduces its palette.

    ``max_bytes`` (JPEG/WEBP only) searches for the highest quality that fits,
    and gently downsizes the image if quality alone is not enough.
    """
    fmt = normalize_format(fmt)
    if fmt not in ("JPEG", "WEBP", "PNG"):
        raise ImageError("Compression supports JPEG, PNG and WEBP output.", 400)
    if not 1 <= quality <= 100:
        raise ImageError("quality must be between 1 and 100.", 400)

    warnings: list = []
    work = img
    if max_dimension is not None:
        if not 16 <= max_dimension <= MAX_DIMENSION:
            raise ImageError(f"max_dimension must be between 16 and {MAX_DIMENSION}.", 400)
        work = img.copy()
        work.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
        if work.size != img.size:
            warnings.append(f"The image was downscaled to fit {max_dimension}px.")

    if max_bytes is None:
        out = (_encode_png_reduced(work, quality) if fmt == "PNG"
               else encode(work, fmt, quality, flatten_background))
        out.warnings = warnings + out.warnings
        return out

    if fmt == "PNG":
        raise ImageError("A target file size is supported for JPEG and WEBP only.", 400)
    if max_bytes < 1024:
        raise ImageError("The target size must be at least 1 KB.", 400)

    scale = 1.0
    for _ in range(10):
        cur = work if scale == 1.0 else work.resize(
            (max(1, int(work.width * scale)), max(1, int(work.height * scale))),
            Image.Resampling.LANCZOS,
        )
        lo, hi, best = 20, 95, None
        while lo <= hi:                      
            check_deadline(deadline)
            mid = (lo + hi) // 2
            out = encode(cur, fmt, mid, flatten_background, verify=False)
            if len(out.data) <= max_bytes:
                best, lo = out, mid + 1
            else:
                hi = mid - 1
        if best:
            best.info.update(scale=round(scale, 3), target_bytes=max_bytes)
            if scale < 1.0:
                warnings.append(f"The image was also downscaled to {round(scale * 100)}% to reach the target size.")
            best.warnings = warnings + best.warnings
            return _mark_verified(best)
        scale *= 0.85
    raise ImageError("Could not reach the requested file size.", 422)



POSITIONS = {
    "top-left": (0, 0), "top-center": (.5, 0), "top-right": (1, 0),
    "middle-left": (0, .5), "center": (.5, .5), "middle-right": (1, .5),
    "bottom-left": (0, 1), "bottom-center": (.5, 1), "bottom-right": (1, 1),
}


@dataclass
class TextStyle:
    position: str = "bottom-right"
    font_size: Optional[int] = None        
    color: str = "#FFFFFF"
    opacity: float = 1.0
    stroke_width: Optional[int] = None     
    stroke_color: str = "#000000"
    background: Optional[str] = None       # e.g. "#000000" for a label box
    background_opacity: float = 0.5
    margin_ratio: float = 0.02


def _rgb(color: str, label: str = "color"):
    try:
        return ImageColor.getrgb(color)[:3]
    except (ValueError, AttributeError):
        raise ImageError(f"Invalid {label}: {color!r}. Use a name or hex like #FFAA00.", 400)


def _fractions(position: str):
    if position not in POSITIONS:
        raise ImageError(f"position must be one of: {', '.join(POSITIONS)}.", 400)
    return POSITIONS[position]


def _place(base_size, box_size, position, margin):
    fx, fy = _fractions(position)
    bw, bh = base_size
    w, h = box_size
    x = margin + fx * max(0, bw - w - 2 * margin)
    y = margin + fy * max(0, bh - h - 2 * margin)
    return max(0, round(x)), max(0, round(y))


@lru_cache(maxsize=64)
def _font(size: int):
    for name in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf", "Arial.ttf", "arial.ttf", "Roboto-Regular.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)     


def _solid(rgb, mask: Image.Image) -> Image.Image:
    layer = Image.new("RGBA", mask.size, tuple(rgb) + (0,))
    layer.putalpha(mask)
    return layer


def _scaled(mask: Image.Image, factor: float) -> Image.Image:
    return mask if factor >= 1 else mask.point(lambda v: int(v * factor))


def _clean_text(text: str, label: str = "text") -> str:
    text = (text or "").strip()
    if not text:
        raise ImageError(f"{label} must not be empty.", 400)
    if len(text) > MAX_TEXT_LENGTH:
        raise ImageError(f"{label} is longer than {MAX_TEXT_LENGTH} characters.", 400)
    return text


def _render_label(text: str, style: TextStyle, base_size, margin: int) -> Image.Image:
    """Draw the text (and optional box) on a small transparent layer."""
    fill, stroke_rgb = _rgb(style.color), _rgb(style.stroke_color, "stroke color")
    bg_rgb = _rgb(style.background, "background") if style.background else None
    if not 0 <= style.opacity <= 1 or not 0 <= style.background_opacity <= 1:
        raise ImageError("opacity values must be between 0 and 1.", 400)
    fx, _ = _fractions(style.position)
    align = {0: "left", .5: "center", 1: "right"}[fx]

    size = style.font_size or max(12, int(min(base_size) * 0.05))
    if not 6 <= size <= 2000:
        raise ImageError("font_size must be between 6 and 2000.", 400)

    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    max_w, max_h = base_size[0] - 2 * margin, base_size[1] - 2 * margin
    while True:                                   
        stroke = style.stroke_width if style.stroke_width is not None else size // 15
        pad = size // 4 if bg_rgb else 0
        font = _font(size)
        l, t, r, b = probe.multiline_textbbox((0, 0), text, font=font, stroke_width=stroke, align=align)
        l, t = math.floor(l), math.floor(t)
        w, h = math.ceil(r) - l, math.ceil(b) - t
        if (w + 2 * pad <= max_w and h + 2 * pad <= max_h) or size <= 8:
            break
        size = max(8, int(size * 0.9))

    W, H = w + 2 * pad, h + 2 * pad
    origin = (pad - l, pad - t)
    label = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    if bg_rgb:
        m = Image.new("L", (W, H), 0)
        ImageDraw.Draw(m).rounded_rectangle(
            (0, 0, W - 1, H - 1), radius=pad, fill=int(255 * style.background_opacity)
        )
        label.alpha_composite(_solid(bg_rgb, m))
    if stroke > 0:
        m = Image.new("L", (W, H), 0)
        ImageDraw.Draw(m).multiline_text(origin, text, font=font, fill=255, stroke_width=stroke, align=align)
        label.alpha_composite(_solid(stroke_rgb, _scaled(m, style.opacity)))
    m = Image.new("L", (W, H), 0)
    ImageDraw.Draw(m).multiline_text(origin, text, font=font, fill=255, align=align)
    label.alpha_composite(_solid(fill, _scaled(m, style.opacity)))
    return label


def _stamp(img: Image.Image, layer: Image.Image, position: str, margin: int) -> Image.Image:
    base = img.convert("RGBA")                    
    x, y = _place(base.size, layer.size, position, margin)
    base.alpha_composite(layer, dest=(x, y))
    return base if img.mode == "RGBA" else base.convert("RGB")


def add_text(img: Image.Image, text: str, style: Optional[TextStyle] = None) -> Image.Image:
    style = style or TextStyle()
    text = _clean_text(text)
    margin = max(2, int(min(img.size) * style.margin_ratio))
    return _stamp(img, _render_label(text, style, img.size, margin), style.position, margin)


def add_date(
    img: Image.Image,
    when: Optional[datetime] = None,
    date_format: str = "%d %b %Y",
    style: Optional[TextStyle] = None,
) -> Image.Image:
    """Stamp a date. ``when`` defaults to today."""
    if len(date_format) > 40:
        raise ImageError("date_format is too long.", 400)
    try:
        text = (when or datetime.now()).strftime(date_format)
    except ValueError:
        raise ImageError("Invalid date_format.", 400)
    return add_text(img, text, style)


def parse_date(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        raise ImageError("date must look like 2025-03-31 or 2025-03-31T14:05:00.", 400)


def add_name(img: Image.Image, name: str, style: Optional[TextStyle] = None) -> Image.Image:
    """Stamp a name/caption (credit line, owner, watermark)."""
    return add_text(img, _clean_text(name, "name"), style)


def prepare_signature(
    sig: Image.Image,
    remove_background: bool = True,
    color: Optional[str] = None,
    threshold: int = 235,
) -> Image.Image:
    """Turn a signature photo/scan/drawing into a cropped transparent stamp."""
    sig = sig.convert("RGBA")
    alpha = sig.getchannel("A")
    if remove_background and alpha.getextrema()[0] == 255:
        alpha = sig.convert("L").point(lambda v: max(0, min(255, (threshold - v) * 6)))
        sig.putalpha(alpha)
    if color:
        sig = _solid(_rgb(color), alpha)
    bbox = alpha.point(lambda a: 255 if a > 8 else 0).getbbox()
    if bbox is None:
        raise ImageError("The signature image appears to be blank.", 422)
    return sig.crop(bbox)


def add_signature(
    img: Image.Image,
    signature: Image.Image,
    position: str = "bottom-right",
    width_ratio: float = 0.25,
    opacity: float = 1.0,
    margin_ratio: float = 0.03,
    remove_background: bool = True,
    color: Optional[str] = None,
) -> Image.Image:
    if not 0.05 <= width_ratio <= 1.0:
        raise ImageError("width_ratio must be between 0.05 and 1.0.", 400)
    if not 0 <= opacity <= 1:
        raise ImageError("opacity must be between 0 and 1.", 400)
    _fractions(position)

    sig = prepare_signature(signature, remove_background, color)
    margin = max(2, int(min(img.size) * margin_ratio))
    new_w = max(1, int(img.width * width_ratio))
    new_h = max(1, round(sig.height * new_w / sig.width))
    if new_h > img.height - 2 * margin:           
        new_h = max(1, img.height - 2 * margin)
        new_w = max(1, round(sig.width * new_h / sig.height))
    sig = sig.resize((new_w, new_h), Image.Resampling.LANCZOS)
    if opacity < 1:
        sig.putalpha(_scaled(sig.getchannel("A"), opacity))
    return _stamp(img, sig, position, margin)