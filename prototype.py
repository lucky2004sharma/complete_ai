"""
ImageReducer backend engine
===========================

KYA HAI:
    Yeh Flask + Pillow backend ``index_02`` resizer aur ``All_converter`` ke
    saare 42 cross-format routes ko ek hi ``/resize`` API se handle karta hai.
    Isme 35 social-media/e-mail aspect-ratio presets bhi hain, jinhe
    ``/convert-photo-mode`` API se ek doosre me convert kiya ja sakta hai.

RUN KARNE KA TARIKA (VS Code terminal):
    py -m pip install flask pillow cairosvg
    py project1.py

IMPORTANT:
    * JPG, JPEG, WEBP, GIF, TIF, PNG aur SVG input/output supported hain.
    * ``expected_input_format`` aane par original upload ka REAL format check
      hota hai. Isliye PNG upload karke input dropdown ko JPEG bolne se request
      accept nahi hogi.
    * ``target_kb`` maximum output size hai. 50 KB ko 50 * 1024 = 51,200 bytes
      maana jaata hai aur response usse bada nahi hota. Fragile exact padding
      default OFF hai; double opt-in ke bina junk/trailing bytes add nahi hote.
    * SVG input ko raster image me kholne ke liye ``cairosvg`` dependency chahiye.
    * Har individual file, complete request aur generated output maximum 100 MB.
    * Production me IMAGE_REDUCER_PRODUCTION=1 + IMAGE_REDUCER_API_KEY required.
    * DOC/Excel parsing default OFF hai. Isse sirf external Docker/VM/seccomp
      sandbox ke andar enable karo; details Office security constants me hain.

COMMENTS ITNE DETAIL ME KYUN HAIN:
    User beginner hai. Har important constant, function, loop aur if/else ke
    paas Hinglish explanation di gayi hai taaki future me value safely change
    ki ja sake aur us change ka effect samajh aaye.
"""

from __future__ import annotations

# Standard-library imports: inka alag installation nahi karna padta.
import base64
import hmac
import io
import logging
import math
import os
import re
import signal
import struct
import threading
import time
import zlib
from collections import defaultdict, deque
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, Iterable, Optional, Tuple

# Pillow image read, resize, filter aur encode karta hai.
from PIL import Image, ImageEnhance, ImageFilter, ImageOps, UnidentifiedImageError

# Flask optional-style import rakha hai taaki Flask absent ho to file ek clear
# install message de, cryptic ModuleNotFoundError par band na ho.
try:
    from flask import Flask, g, jsonify, make_response, request, send_file
except ImportError:  # pragma: no cover - sirf dependency-missing computer par chalega.
    Flask = None  # type: ignore[assignment]
    g = jsonify = make_response = request = send_file = None  # type: ignore[assignment]


# ============================================================================
# 01 // CONSTANTS
# KYA: Project-wide fixed values ek jagah rakhe hain.
# KYUN: Future me limit/port/format badalna ho to poori file search nahi karni.
# VALUE CHANGE KA EFFECT: MAX_UPLOAD_MB badhega to RAM usage bhi badh sakta hai.
# ============================================================================

KB_IN_BYTES = 1024
MB_IN_BYTES = 1024 * 1024

# SECURITY CHANGE: Har individual uploaded file aur poori multipart request ka
# hard upper ceiling 100 MB hai. MAX_CONTENT_LENGTH request ko body parse hone
# se pehle stop karta hai; read_upload_bytes/document_read_upload per-file bytes
# ko dobara verify karte hain. Value badhaane se network, disk aur RAM risk badhega.
MAX_UPLOAD_MB = 100
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * MB_IN_BYTES
MAX_REQUEST_MB = 100
MAX_REQUEST_BYTES = MAX_REQUEST_MB * MB_IN_BYTES

# Generated image/PDF/ZIP bhi 100 MB se bada response nahi ban sakta. Isse ek
# allowed upload ko extremely large output me expand karke memory exhaust karna
# difficult hota hai.
MAX_OUTPUT_MB = 100
MAX_OUTPUT_BYTES = MAX_OUTPUT_MB * MB_IN_BYTES
MAX_TARGET_MB = 100
MAX_TARGET_BYTES = MAX_TARGET_MB * MB_IN_BYTES
DEFAULT_DPI = 72
DEFAULT_QUALITY = 92
MIN_DIMENSION = 1
MAX_DIMENSION = 20_000

# Pillow decompression-bomb protection upload DECODE ke waqt kaam karti hai.
# MAX_OUTPUT_PIXELS alag constant isliye hai kyunki Pillow ka `.resize()` naya
# canvas banate waqt Image.MAX_IMAGE_PIXELS automatically check nahi karta.
MAX_DECODE_PIXELS = 50_000_000
MAX_OUTPUT_PIXELS = 50_000_000
Image.MAX_IMAGE_PIXELS = MAX_DECODE_PIXELS

# Exact target fitting multiple expensive encodes kar sakti hai. Large canvases
# par target_kb use karna reject hota hai, total encode work bounded hai aur
# loop ke beech cooperative deadline check hota hai.
MAX_EXACT_TARGET_PIXELS = 8_000_000
MAX_EXACT_TARGET_RESIZE_ATTEMPTS = 6
MAX_EXACT_TARGET_ENCODE_OPERATIONS = 20
MAX_EXACT_TARGET_PIXEL_WORK = 64_000_000
EXACT_TARGET_TIME_LIMIT_SECONDS = 20.0

# Exact-size padding strict validators ke saath fragile ho sakti hai. Safe
# default OFF hai. Admin env EXACT_SIZE_PADDING_ENABLED=1 kare aur request form
# `allow_exact_padding=true` bheje tabhi container-aware padding use hogi.
EXACT_SIZE_PADDING_ENABLED = str(
    os.environ.get("EXACT_SIZE_PADDING_ENABLED", "0")
).strip().lower() in {"1", "true", "yes", "on"}

# Multi-page PDF encoder decoded images ko ek saath memory me rakhta hai. Har
# image ka individual 50M cap enough nahi, so combined pages ka smaller budget
# rakha hai. 60M RGB pixels roughly 180 MB raw pixel memory hoti hai.
MAX_PDF_TOTAL_INPUT_PIXELS = 60_000_000

# Request abuse controls. Heavy routes per IP 60 seconds me limited hain aur
# server par simultaneously sirf do CPU-heavy requests process hoti hain.
RATE_LIMIT_WINDOW_SECONDS = 60
GENERAL_RATE_LIMIT_PER_WINDOW = 30
HEAVY_RATE_LIMIT_PER_WINDOW = 8
MAX_CONCURRENT_HEAVY_REQUESTS = 2

# Logging backend terminal/file handler ko useful operational details deta hai.
# Raw errors HTTP response me nahi bheje jaate; logger administrator ke liye hai.
LOGGER = logging.getLogger("image_reducer")

# UI ke saat names ko Pillow ke actual encoder names se map kiya hai.
# JPG/JPEG bytes same codec use karte hain, par UI route identity alag rakhta hai.
PIL_FORMAT_BY_UI = {
    "JPG": "JPEG",
    "JPEG": "JPEG",
    "WEBP": "WEBP",
    "GIF": "GIF",
    "TIF": "TIFF",
    "PNG": "PNG",
    "SVG": "SVG",
}

# Browser ko correct Content-Type milega; isi se preview/download format samajhta hai.
MIME_BY_FORMAT = {
    "JPG": "image/jpeg",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "GIF": "image/gif",
    "TIF": "image/tiff",
    "PNG": "image/png",
    "SVG": "image/svg+xml",
}

# Download filename me kaunsa extension use hoga.
EXTENSION_BY_FORMAT = {
    "JPG": "jpg",
    "JPEG": "jpeg",
    "WEBP": "webp",
    "GIF": "gif",
    "TIF": "tif",
    "PNG": "png",
    "SVG": "svg",
}

# Pillow kabhi "TIFF" return karta hai; UI usse "TIF" bolti hai.
UI_FORMAT_BY_PIL = {
    "JPEG": "JPEG",
    "WEBP": "WEBP",
    "GIF": "GIF",
    "TIFF": "TIF",
    "PNG": "PNG",
}


# ============================================================================
# 01.1 // SHARED RESOURCE-SAFETY HELPERS
# KYA: Canvas pixels, output bytes aur boolean settings ko central rules se
#      validate karte hain.
# KYUN: Sirf upload decode secure karke resize/new-canvas paths open chhodna
#       memory-exhaustion DoS ko nahi rokta. Har allocation se pehle same helper
#       call karna future code ko bhi safer banata hai.
# ============================================================================

def parse_boolean(value: Any, *, default: bool = False) -> bool:
    """Form/env style value ko strict True/False me parse karta hai."""

    # Blank value documented default use karta hai. Yeh optional checkboxes ke
    # liye useful hai; missing checkbox normally request me aati hi nahi.
    if value is None or str(value).strip() == "":
        return default

    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False

    # Unknown value ko truthy guess nahi karte, warna typo security option ko
    # accidentally enable kar sakta hai.
    raise ValueError("Boolean value must be true/false, yes/no, on/off, or 1/0.")


def validate_pixel_budget(
    width: int,
    height: int,
    context: str = "Requested output",
    maximum_pixels: int = MAX_OUTPUT_PIXELS,
) -> int:
    """Canvas dimensions validate karke safe pixel count return karta hai."""

    # Zero/negative dimensions Pillow me cryptic error de sakti hain; API par
    # clear validation message beginner aur security dono ke liye better hai.
    if width < MIN_DIMENSION or height < MIN_DIMENSION:
        raise ValueError(f"{context} dimensions must be at least 1 × 1 pixel.")

    # Auto-ratio calculations parse only one user-provided side; calculated
    # second side bhi per-dimension 20,000 ceiling follow karni chahiye.
    if width > MAX_DIMENSION or height > MAX_DIMENSION:
        raise ValueError(
            f"{context} dimensions cannot exceed {MAX_DIMENSION:,} pixels per side."
        )

    pixel_count = int(width) * int(height)
    if pixel_count > maximum_pixels:
        estimated_rgba_mb = (pixel_count * 4) / MB_IN_BYTES
        raise ValueError(
            f"{context} would create {pixel_count:,} pixels (about "
            f"{estimated_rgba_mb:.1f} MB as an RGBA canvas). Maximum allowed is "
            f"{maximum_pixels:,} pixels. Reduce width or height."
        )

    return pixel_count


def validate_output_size(data: bytes, context: str = "Generated output") -> bytes:
    """Generated bytes ko global 100 MB output ceiling ke andar verify karta hai."""

    if len(data) > MAX_OUTPUT_BYTES:
        raise ValueError(
            f"{context} is larger than the {MAX_OUTPUT_MB} MB output limit. "
            "Use smaller dimensions, lower quality, or fewer pages."
        )

    return data


def validate_combined_upload_size(uploads: Iterable[Any], context: str) -> None:
    """Multiple FileStorage objects ka declared total 100 MB se upar block karta hai."""

    total_bytes = 0
    for upload in uploads:
        # Werkzeug content_length kabhi None/0 hota hai; actual read helper phir
        # bhi per-file limit enforce karega. Available value ko early rejection
        # ke liye use karte hain, security decision sirf isi par depend nahi hai.
        declared_size = int(getattr(upload, "content_length", 0) or 0)
        total_bytes += max(0, declared_size)

    if total_bytes > MAX_UPLOAD_BYTES:
        raise ValueError(
            f"{context} files together exceed the {MAX_UPLOAD_MB} MB request limit."
        )


def sanitize_subprocess_log(value: Any, maximum_characters: int = 4000) -> str:
    """Captured stdout/stderr ko terminal-safe, bounded single string banata hai."""

    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value or "")

    # Control characters terminal/log parser ko confuse kar sakte hain. Newline
    # useful diagnostic hai, baaki non-printable values spaces me normalize hote hain.
    cleaned = "".join(
        character if character in "\n\r\t" or character.isprintable() else " "
        for character in text
    ).strip()

    if len(cleaned) > maximum_characters:
        return cleaned[:maximum_characters] + " …[truncated]"
    return cleaned


# ============================================================================
# 02 // SMALL PARSING HELPERS
# KYA: FormData strings ko safe integer/float/format values me convert karte hain.
# KYUN: Browser se aaya text blindly use karna error aur security issue bana sakta hai.
# ============================================================================

def normalize_format(value: Any, *, allow_empty: bool = False) -> Optional[str]:
    """Format name ko UI ke seven canonical names me normalize karta hai."""

    text = str(value or "").strip().upper()

    # TIFF aur TIF same codec hain; project UI me canonical label TIF rakha hai.
    if text == "TIFF":
        text = "TIF"

    # Empty allowed sirf tab hai jab caller source format ko auto-detect karna chahe.
    if not text and allow_empty:
        return None

    # Unknown format ko silently JPG banana dangerous hota; clear ValueError better hai.
    if text not in PIL_FORMAT_BY_UI:
        raise ValueError(
            "Unsupported format. Use JPG, JPEG, WEBP, GIF, TIF, PNG or SVG."
        )

    return text


def parse_int(
    value: Any,
    default: int,
    minimum: int,
    maximum: int,
    field_name: str,
) -> int:
    """Ek form value ko bounded integer banata hai."""

    # Blank value ka matlab caller ka documented default use karo.
    if value is None or str(value).strip() == "":
        return default

    try:
        number = int(float(str(value)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a number.") from exc

    # Minimum/maximum se absurd dimensions, DPI ya quality block hoti hai.
    if number < minimum or number > maximum:
        raise ValueError(
            f"{field_name} must be between {minimum} and {maximum}."
        )

    return number


def parse_percent(value: Any, field_name: str) -> float:
    """Enhancement value -100 se +100 ke safe range me return karta hai."""

    if value is None or str(value).strip() == "":
        return 0.0

    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a number.") from exc

    if number < -100 or number > 100:
        raise ValueError(f"{field_name} must be between -100 and 100.")

    return number


def parse_target_bytes(value: Any) -> Optional[int]:
    """Maximum target KB ko bytes me convert karta hai (50 KB -> 51,200 bytes)."""

    if value is None or str(value).strip() == "":
        return None

    try:
        kb_value = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("TARGET SIZE must be a valid KB number.") from exc

    # UI minimum 1 KB hai. Isse chhote targets kuch formats me valid hi nahi bante.
    if kb_value < Decimal("1"):
        raise ValueError("TARGET SIZE must be at least 1 KB.")

    target_bytes = int(
        (kb_value * KB_IN_BYTES).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )

    if target_bytes > MAX_TARGET_BYTES:
        raise ValueError(f"TARGET SIZE cannot exceed {MAX_TARGET_MB} MB.")

    return target_bytes


def safe_base_name(filename: str) -> str:
    """Download filename se path/special characters hata kar safe base name deta hai."""

    base = str(filename or "image").replace("\\", "/").split("/")[-1]
    base = base.rsplit(".", 1)[0]
    base = re.sub(r"[^A-Za-z0-9_-]+", "_", base).strip("_")
    return base or "image"


# ============================================================================
# 03 // TRUE INPUT FORMAT DETECTION
# KYA: Extension ke saath actual file header/content bhi inspect hota hai.
# KYUN: PNG bytes ka naam photo.jpg rakh dena real conversion nahi hota.
# ============================================================================

def looks_like_svg(data: bytes) -> bool:
    """First bytes me real <svg root token dhoondhta hai."""

    # BOM/space hata kar limited header read kiya; poori large file regex nahi hoti.
    header = data[:8192].lstrip(b"\xef\xbb\xbf\x00\t\r\n ").lower()
    return b"<svg" in header and (header.startswith(b"<svg") or b"<?xml" in header)


def detect_input_format(data: bytes, filename: str) -> str:
    """Bytes + filename extension se exact UI format (JPG vs JPEG bhi) batata hai."""

    if looks_like_svg(data):
        return "SVG"

    try:
        with Image.open(io.BytesIO(data)) as probe:
            pillow_format = str(probe.format or "").upper()
            validate_pixel_budget(
                probe.width,
                probe.height,
                "Uploaded image",
                MAX_DECODE_PIXELS,
            )
            probe.verify()
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValueError("Uploaded file is not a readable supported image.") from exc

    if pillow_format not in UI_FORMAT_BY_PIL:
        raise ValueError(
            f"Detected {pillow_format or 'unknown'} input; this project supports only 7 formats."
        )

    detected = UI_FORMAT_BY_PIL[pillow_format]

    # JPEG bytes JPG/JPEG ko alag nahi bata sakti. Isliye in do UI routes ke liye
    # filename extension final identity decide karti hai.
    if pillow_format == "JPEG":
        extension = str(filename or "").lower().rsplit(".", 1)[-1]
        # समस्या (OLD CODE): यहाँ .jpg को "JPG" और .jpeg को "JPEG" अलग-अलग रिटर्न किया गया था। जब यूजर UI में 'JPG' चुनकर 'photo.jpeg' अपलोड करता था तो validate_expected_format() में "FORMAT MISMATCH" का 400 Bad Request एरर आता था।
        # if extension == "jpg":
        #     return "JPG"
        # if extension == "jpeg":
        #     return "JPEG"
        # समाधान (NEW CODE): दोनों एक्सटेंशन (.jpg और .jpeg) के लिए एक ही नॉर्मलाइज़्ड नाम "JPG" रिटर्न किया गया है ताकि वैलिडेशन कभी फेल न हो।
        if extension in {"jpg", "jpeg"}:
            return "JPG"
        raise ValueError("JPEG image filename must end in .jpg or .jpeg.")

    return detected


def validate_expected_format(detected: str, expected_value: Any) -> None:
    """Selected input route aur real upload same na ho to conversion rokta hai."""

    expected = normalize_format(expected_value, allow_empty=True)

    # Universal/index resizer request expected format nahi bhejti; auto-detect allowed hai.
    if expected is None:
        return

    # समस्या (OLD CODE): यहाँ सिर्फ exact string match चेक होता था। अगर detected="JPG" और expected="JPEG" होता था, तो असली JPEG फाइल होने के बावजूद 400 Bad Request एरर आ जाता था।
    # if detected != expected:
    # समाधान (NEW CODE): चूंकि JPG और JPEG दोनों एक ही इमेज फॉर्मेट (JPEG) हैं, इसलिए अगर दोनों में से कोई भी हो तो उसे आपस में compatible मानकर वैलिडेशन पास किया गया है।
    if detected != expected and not (detected in {"JPG", "JPEG"} and expected in {"JPG", "JPEG"}):
        raise ValueError(
            f"FORMAT MISMATCH: selected input is {expected}, but uploaded file is {detected}. "
            "Remove/reset the photo and start again with the correct route."
        )


def safe_svg_output_dimensions(data: bytes) -> Tuple[int, int]:
    """SVG root dimensions/viewBox se bounded raster canvas size calculate karta hai."""

    # CairoSVG external <image>/CSS URL fetch kar sakta hai. Untrusted upload ko
    # server-side HTTP/file access dena SSRF/local-file risk hai, isliye only
    # inline/data content allowed hai. Regex complete bytes par case-insensitive
    # scan karti hai without decoded duplicate string banaye.
    if re.search(
        br"(?:href\s*=\s*['\"]\s*|url\(\s*['\"]?\s*)(?:https?|file|ftp):",
        data,
        flags=re.IGNORECASE,
    ):
        raise ValueError("SVG external URLs/files are not allowed; embed resources as data URLs.")

    # CairoSVG ko raw SVG ke declared 20,000×20,000 canvas par chhodna resize
    # guard se pehle hi memory allocate kar sakta hai. Sirf root tag read karke
    # output_width/output_height explicitly dene se renderer ka canvas bounded hai.
    header_text = data[:65536].decode("utf-8", errors="ignore")
    root_match = re.search(r"<svg\b([^>]*)>", header_text, flags=re.IGNORECASE)
    if root_match is None:
        raise ValueError("SVG root element could not be read safely.")

    attributes = root_match.group(1)

    def read_numeric_attribute(name: str) -> Optional[float]:
        # px/unit-less values direct pixels hain. Physical CSS units ko 96-DPI
        # approximation se pixels me convert karte hain. Percentage dimensions
        # viewBox/default se resolve hongi, unhe yahan numeric guess nahi karte.
        match = re.search(
            rf"\b{name}\s*=\s*['\"]\s*([0-9]+(?:\.[0-9]+)?)\s*(px|pt|pc|in|cm|mm)?\s*['\"]",
            attributes,
            flags=re.IGNORECASE,
        )
        if match is None:
            return None

        number = float(match.group(1))
        unit = str(match.group(2) or "px").lower()
        multiplier = {
            "px": 1.0,
            "pt": 96.0 / 72.0,
            "pc": 16.0,
            "in": 96.0,
            "cm": 96.0 / 2.54,
            "mm": 96.0 / 25.4,
        }[unit]
        return number * multiplier

    width_value = read_numeric_attribute("width")
    height_value = read_numeric_attribute("height")

    viewbox_match = re.search(
        r"\bviewBox\s*=\s*['\"]\s*[-+0-9.eE]+[ ,]+[-+0-9.eE]+[ ,]+([-+0-9.eE]+)[ ,]+([-+0-9.eE]+)\s*['\"]",
        attributes,
        flags=re.IGNORECASE,
    )
    viewbox_width = float(viewbox_match.group(1)) if viewbox_match else None
    viewbox_height = float(viewbox_match.group(2)) if viewbox_match else None

    # Missing/percentage dimensions ke liye positive viewBox, aur uske bina SVG
    # standard-like 300×150 fallback use hota hai. One-side value par viewBox
    # ratio preserve karke second side calculate hoti hai.
    if width_value is None and height_value is None:
        width_value = viewbox_width if viewbox_width and viewbox_width > 0 else 300.0
        height_value = viewbox_height if viewbox_height and viewbox_height > 0 else 150.0
    elif width_value is None:
        ratio = (
            viewbox_width / viewbox_height
            if viewbox_width and viewbox_height and viewbox_width > 0 and viewbox_height > 0
            else 2.0
        )
        width_value = float(height_value or 150.0) * ratio
    elif height_value is None:
        ratio = (
            viewbox_height / viewbox_width
            if viewbox_width and viewbox_height and viewbox_width > 0 and viewbox_height > 0
            else 0.5
        )
        height_value = float(width_value) * ratio

    width = max(MIN_DIMENSION, int(math.ceil(float(width_value))))
    height = max(MIN_DIMENSION, int(math.ceil(float(height_value))))
    validate_pixel_budget(width, height, "SVG raster canvas", MAX_DECODE_PIXELS)
    return width, height


# ============================================================================
# 04 // IMAGE DECODING AND EDITS
# KYA: Source ko Pillow image banata, orientation fix karta, filters/resize lagata.
# ============================================================================

def open_image_bytes(data: bytes, detected_format: Optional[str] = None) -> Image.Image:
    """Raster + SVG bytes ko fully-loaded Pillow image me convert karta hai."""

    actual_format = detected_format

    # Working canvas ka filename original extension rakh sakta hai; isliye missing
    # detected_format case me bytes ko dobara independently inspect karte hain.
    if actual_format is None:
        if looks_like_svg(data):
            actual_format = "SVG"
        else:
            try:
                with Image.open(io.BytesIO(data)) as probe:
                    pillow_format = str(probe.format or "").upper()
                actual_format = UI_FORMAT_BY_PIL.get(pillow_format)
            except (UnidentifiedImageError, OSError) as exc:
                raise ValueError("Backend could not decode the working image.") from exc

    if actual_format == "SVG":
        try:
            import cairosvg  # type: ignore[import]
        # समस्या (OLD CODE): यहाँ सिर्फ ImportError को कैच किया गया था। Windows सिस्टम पर अगर C-libraries (GTK+ / libcairo-2.dll) मौजूद नहीं होती हैं, तो 'import cairosvg' रनटाइम पर क्रैश होकर OSError (DLL load failed) देता है और सर्वर बंद हो जाता है।
        # except ImportError as exc:
        #     raise ValueError(
        #         "SVG input needs CairoSVG. Run: py -m pip install cairosvg"
        #     ) from exc
        # समाधान (NEW CODE): Windows पर C-libraries (DLL) न होने पर आने वाले OSError को भी कैच किया गया है ताकि सर्वर क्रैश न हो और यूजर को स्पष्ट कारण पता चले।
        except (ImportError, OSError) as exc:
            raise ValueError(
                "SVG processing failed. Please install CairoSVG and its external GTK+/Cairo C-libraries (DLLs on Windows)."
            ) from exc

        try:
            # unsafe=False external entities/oversized XML expansion ko allow nahi karta.
            svg_width, svg_height = safe_svg_output_dimensions(data)
            png_bytes = cairosvg.svg2png(
                bytestring=data,
                unsafe=False,
                output_width=svg_width,
                output_height=svg_height,
            )
            with Image.open(io.BytesIO(png_bytes)) as svg_image:
                validate_pixel_budget(
                    svg_image.width,
                    svg_image.height,
                    "Rendered SVG",
                    MAX_DECODE_PIXELS,
                )
                image = svg_image.convert("RGBA")
                image.load()
                return image
        except Exception as exc:
            raise ValueError("SVG could not be rendered safely.") from exc

    try:
        with Image.open(io.BytesIO(data)) as source:
            # Animated GIF/TIF input ke case me first visible frame chosen hai.
            try:
                source.seek(0)
            except EOFError:
                pass

            # Pillow threshold warning ka wait nahi karte; exact 50M hard cap
            # conversion/copy se pehle enforce hota hai.
            validate_pixel_budget(
                source.width,
                source.height,
                "Uploaded image",
                MAX_DECODE_PIXELS,
            )

            # Phone-camera EXIF orientation ko pixels par physically apply karta hai.
            oriented = ImageOps.exif_transpose(source)

            # RGBA transparency preserve karta; CMYK/P mode ko standard RGB/RGBA banata hai.
            if "A" in oriented.getbands() or "transparency" in oriented.info:
                image = oriented.convert("RGBA")
            else:
                image = oriented.convert("RGB")

            image.load()
            return image
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValueError("Backend could not decode the uploaded image.") from exc


def calculate_requested_dimensions(
    image: Image.Image,
    width_value: Any,
    height_value: Any,
    ) -> Tuple[int, int]:
    """Blank/one-side/both-side resize inputs se final width-height nikalta hai."""

    original_width, original_height = image.size

    # Blank ka zero sentinel hai; parse_int ka minimum 0 isi limited helper me safe hai.
    width = parse_int(width_value, 0, 0, MAX_DIMENSION, "WIDTH")
    height = parse_int(height_value, 0, 0, MAX_DIMENSION, "HEIGHT")

    if width and height:
        final_width, final_height = width, height

    # Sirf width diya to original ratio se height auto-calculate hoti hai.
    elif width:
        height = max(MIN_DIMENSION, round(width * original_height / original_width))
        final_width, final_height = width, height

    # Sirf height diya to original ratio se width auto-calculate hoti hai.
    elif height:
        width = max(MIN_DIMENSION, round(height * original_width / original_height))
        final_width, final_height = width, height

    else:
        final_width, final_height = original_width, original_height

    # CRITICAL DoS FIX: MAX_DIMENSION per side enough nahi hai. 20,000×20,000
    # 400M pixels hota hai. Multiplication check `.resize()` call se PEHLE hoti
    # hai, so malicious small upload huge canvas allocate nahi kar sakti.
    validate_pixel_budget(final_width, final_height, "Requested resize")
    return final_width, final_height


def apply_requested_edits(image: Image.Image, form: Any) -> Image.Image:
    """Rotation, resize aur index_02 ke enhancement sliders apply karta hai."""

    working = image.copy()

    # All_converter edited canvas already rotated pixels bhejti hai. Yeh field
    # future/TIFF fallback ke liye hai; blank ho to zero rotation.
    rotation = parse_int(form.get("rotation"), 0, 0, 359, "ROTATION")
    if rotation:
        # expand=True ka bounding canvas pehle calculate hota hai. Validate
        # BEFORE Pillow allocation, warna 50M source 45° rotate hoke ~100M
        # temporary canvas bana sakti hai.
        radians = math.radians(rotation)
        rotated_width = int(
            math.ceil(abs(working.width * math.cos(radians)) + abs(working.height * math.sin(radians)))
        )
        rotated_height = int(
            math.ceil(abs(working.width * math.sin(radians)) + abs(working.height * math.cos(radians)))
        )
        validate_pixel_budget(rotated_width, rotated_height, "Rotated canvas")

        # Pillow positive angle anti-clockwise hota hai; UI rotate button clockwise hai.
        working = working.rotate(-rotation, expand=True, resample=Image.Resampling.BICUBIC)
        validate_pixel_budget(working.width, working.height, "Rotated canvas")

    # Brightness/contrast/saturation -100..100 ko Pillow factor 0..2 me map kiya.
    brightness = parse_percent(form.get("brightness"), "BRIGHTNESS")
    contrast = parse_percent(form.get("contrast"), "CONTRAST")
    saturation = parse_percent(form.get("saturation"), "SATURATION")
    sharpness = parse_percent(form.get("sharpness"), "SHARPNESS")

    if brightness:
        working = ImageEnhance.Brightness(working).enhance(max(0.0, 1 + brightness / 100))

    if contrast:
        working = ImageEnhance.Contrast(working).enhance(max(0.0, 1 + contrast / 100))

    if saturation:
        working = ImageEnhance.Color(working).enhance(max(0.0, 1 + saturation / 100))

    if sharpness > 0:
        # UnsharpMask positive sharpness par edges enhance karta hai.
        working = working.filter(
            ImageFilter.UnsharpMask(radius=2, percent=int(50 + sharpness * 2), threshold=3)
        )
    elif sharpness < 0:
        # Negative sharpness ko mild Gaussian blur me translate kiya.
        working = working.filter(ImageFilter.GaussianBlur(radius=abs(sharpness) / 50))

    requested_size = calculate_requested_dimensions(
        working,
        form.get("width"),
        form.get("height"),
    )

    if requested_size != working.size:
        working = working.resize(requested_size, Image.Resampling.LANCZOS)

    return working


# ============================================================================
# 05 // FORMAT ENCODERS
# KYA: Same Pillow image ko seven requested output labels me encode karte hain.
# ============================================================================

def flatten_transparency(image: Image.Image) -> Image.Image:
    """JPEG jaise no-alpha format ke liye transparent area white banata hai."""

    if image.mode == "RGBA":
        validate_pixel_budget(image.width, image.height, "Transparency canvas")
        background = Image.new("RGB", image.size, "white")
        background.paste(image, mask=image.getchannel("A"))
        return background

    return image.convert("RGB")


def quantize_image(image: Image.Image, colors: int) -> Image.Image:
    """PNG/GIF/TIF/SVG target fit ke liye color palette chhoti karta hai."""

    safe_colors = max(2, min(256, int(colors)))

    if image.mode == "RGBA":
        return image.quantize(colors=safe_colors, method=Image.Quantize.FASTOCTREE)

    return image.convert("RGB").quantize(
        colors=safe_colors,
        method=Image.Quantize.MEDIANCUT,
    )


def encode_svg(image: Image.Image, palette_colors: Optional[int]) -> bytes:
    """Raster result ko self-contained SVG image container me embed karta hai."""

    validate_pixel_budget(image.width, image.height, "SVG output canvas")
    embedded = image
    if palette_colors is not None:
        embedded = quantize_image(image, palette_colors)

    png_buffer = io.BytesIO()
    embedded.save(png_buffer, format="PNG", optimize=True, compress_level=9)
    encoded_png = base64.b64encode(png_buffer.getvalue()).decode("ascii")
    width, height = image.size

    # One-line XML intentional hai: byte-size calculation predictable rehta hai.
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}"><image width="{width}" height="{height}" '
        f'href="data:image/png;base64,{encoded_png}"/></svg>'
    )
    return validate_output_size(svg.encode("utf-8"), "Generated SVG")


def encode_once(
    image: Image.Image,
    output_format: str,
    quality: int,
    dpi: int,
    palette_colors: Optional[int] = None,
) -> bytes:
    """Ek image ko ek baar requested settings par memory bytes me save karta hai."""

    output_format = normalize_format(output_format) or "PNG"
    validate_pixel_budget(image.width, image.height, "Encoder canvas")

    if output_format == "SVG":
        return encode_svg(image, palette_colors)

    buffer = io.BytesIO()
    save_options: Dict[str, Any] = {"dpi": (dpi, dpi)}
    image_to_save = image

    if output_format in {"JPG", "JPEG"}:
        image_to_save = flatten_transparency(image)
        # Official Pillow guidance ke hisaab se useful JPEG quality ceiling 95 hai.
        save_options.update(
            quality=max(1, min(95, quality)),
            optimize=True,
            progressive=True,
        )

    elif output_format == "WEBP":
        image_to_save = image.convert("RGBA") if image.mode == "RGBA" else image.convert("RGB")
        save_options.update(quality=max(1, min(100, quality)), method=6)

    elif output_format == "PNG":
        if palette_colors is not None:
            image_to_save = quantize_image(image, palette_colors)
        save_options.update(optimize=True, compress_level=9)

    elif output_format == "GIF":
        image_to_save = quantize_image(image, palette_colors or 256)
        save_options.update(optimize=True)

    elif output_format == "TIF":
        if palette_colors is not None:
            image_to_save = quantize_image(image, palette_colors)
        elif image.mode not in {"RGB", "RGBA", "L"}:
            image_to_save = image.convert("RGBA" if "A" in image.getbands() else "RGB")
        save_options.update(compression="tiff_adobe_deflate")

    image_to_save.save(
        buffer,
        format=PIL_FORMAT_BY_UI[output_format],
        **save_options,
    )
    return validate_output_size(buffer.getvalue(), f"Generated {output_format}")


def palette_candidates() -> Iterable[Optional[int]]:
    """High color fidelity se low file size tak ordered palette options deta hai."""

    # SECURITY CHANGE: Purani 15-value list har resize attempt par 15 expensive
    # encodes kar sakti thi. Seven representative steps quality range preserve
    # karte hain aur CPU work ko predictable banate hain.
    yield None
    for colors in (256, 128, 64, 32, 16, 4, 2):
        yield colors


def consume_exact_target_budget(
    image: Image.Image,
    deadline: float,
    budget: Dict[str, int],
) -> None:
    """One planned encode ko time, operation aur cumulative-pixel budget me charge karta hai."""

    if time.monotonic() > deadline:
        raise ValueError(
            f"Target-size processing exceeded {EXACT_TARGET_TIME_LIMIT_SECONDS:.0f} seconds. "
            "Use a larger target size or smaller dimensions."
        )

    pixel_count = validate_pixel_budget(
        image.width,
        image.height,
        "Target-size encoder canvas",
        MAX_EXACT_TARGET_PIXELS,
    )
    next_operations = budget.get("operations", 0) + 1
    next_pixel_work = budget.get("pixel_work", 0) + pixel_count

    if next_operations > MAX_EXACT_TARGET_ENCODE_OPERATIONS:
        raise ValueError(
            "Target-size processing needs too many encode attempts. "
            "Use a larger target size or smaller dimensions."
        )

    if next_pixel_work > MAX_EXACT_TARGET_PIXEL_WORK:
        raise ValueError(
            "Target-size processing exceeds the per-request CPU budget. "
            "Use a smaller canvas or remove target_kb."
        )

    budget["operations"] = next_operations
    budget["pixel_work"] = next_pixel_work


def best_candidate_at_current_size(
    image: Image.Image,
    output_format: str,
    requested_quality: int,
    dpi: int,
    target_bytes: int,
    deadline: float,
    budget: Dict[str, int],
) -> Tuple[bytes, bool]:
    """Current dimensions par best quality candidate dhoondhta hai."""

    def budgeted_encode(quality_value: int, colors: Optional[int] = None) -> bytes:
        # Every encoder call se pehle cooperative deadline + cumulative CPU
        # budget check hota hai. Pillow encode ke beech Python interrupt nahi
        # kar sakta, isliye large exact-target canvases separately reject hote hain.
        consume_exact_target_budget(image, deadline, budget)
        return encode_once(image, output_format, quality_value, dpi, colors)

    if output_format in {"JPG", "JPEG", "WEBP"}:
        maximum_quality = min(95 if output_format in {"JPG", "JPEG"} else 100, requested_quality)
        high_data = budgeted_encode(maximum_quality)

        # Requested quality already fit hai to aur quality reduce karne ki zarurat nahi.
        if len(high_data) <= target_bytes:
            return high_data, True

        low_data = budgeted_encode(1)
        if len(low_data) > target_bytes:
            return low_data, False

        # Binary search: highest quality jo target ke andar rahe wahi choose hoti hai.
        low_quality = 1
        high_quality = maximum_quality
        best_data = low_data

        while low_quality <= high_quality:
            middle_quality = (low_quality + high_quality) // 2
            candidate = budgeted_encode(middle_quality)

            if len(candidate) <= target_bytes:
                best_data = candidate
                low_quality = middle_quality + 1
            else:
                high_quality = middle_quality - 1

        return best_data, True

    # PNG/GIF/TIF/SVG me JPEG-style quality ka reliable meaning nahi hota.
    # Palette list full-color se 2 colors tak try hoti hai.
    smallest = b""
    for colors in palette_candidates():
        candidate = budgeted_encode(requested_quality, colors)
        smallest = candidate

        if len(candidate) <= target_bytes:
            return candidate, True

    return smallest, False


# ============================================================================
# 06 // OPTIONAL STRICT-CONTAINER EXACT BYTE PADDING
# KYA: Double opt-in par candidate target se chhota ho to valid container
#      metadata/fill bytes add karta hai.
# KYUN: Purane trailing null/whitespace hacks strict validators reject kar sakte
#       the. Default maximum-size mode koi padding nahi karta; GIF/TIFF exact
#       padding intentionally unsupported hai.
# ============================================================================

def make_png_padding_chunk(payload_size: int) -> bytes:
    """Valid private ancillary PNG chunk banata hai."""

    chunk_type = b"npAD"
    payload = b"\x00" * payload_size
    checksum = zlib.crc32(chunk_type + payload) & 0xFFFFFFFF
    return struct.pack(">I", payload_size) + chunk_type + payload + struct.pack(">I", checksum)


def pad_png(data: bytes, extra_bytes: int) -> bytes:
    """PNG IEND se pehle complete private ancillary chunk insert karta hai."""

    if extra_bytes >= 12 and data.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82"):
        # Chunk overhead 12 bytes hai; remaining bytes us chunk ka payload bante hain.
        chunk = make_png_padding_chunk(extra_bytes - 12)
        return data[:-12] + chunk + data[-12:]

    # Strict mode incomplete chunk ya IEND ke baad junk bytes kabhi add nahi
    # karta. User target slightly change karke at least 12-byte gap de sakta hai.
    raise ValueError("Exact PNG padding needs at least 12 additional bytes.")


def pad_webp(data: bytes, extra_bytes: int) -> bytes:
    """WEBP RIFF container me unknown PAD chunk add karta hai."""

    if extra_bytes >= 8 and extra_bytes % 2 == 0 and data[:4] == b"RIFF":
        payload_size = extra_bytes - 8
        padded = data + b"PAD " + struct.pack("<I", payload_size) + (b"\x00" * payload_size)
        # RIFF header byte 4..7 total file size minus 8 store karta hai.
        return padded[:4] + struct.pack("<I", len(padded) - 8) + padded[8:]

    # RIFF ke bahar trailing bytes add karna intentionally removed hai. Valid
    # custom chunk ke liye even size aur minimum 8-byte overhead required hai.
    raise ValueError("Exact WEBP padding needs an even gap of at least 8 bytes.")


def pad_to_exact_size(data: bytes, target_bytes: int, output_format: str) -> bytes:
    """Encoded output ko exactly target_bytes length ka banata hai."""

    if len(data) > target_bytes:
        raise ValueError("Internal size fitter received an oversized candidate.")

    extra_bytes = target_bytes - len(data)
    if extra_bytes == 0:
        return data

    if output_format in {"JPG", "JPEG"}:
        # JPEG marker syntax EOI se pehle 0xFF fill bytes allow karti hai.
        eoi_index = data.rfind(b"\xff\xd9")
        if eoi_index < 0:
            raise ValueError("JPEG encoder returned output without an EOI marker.")
        return data[:eoi_index] + (b"\xff" * extra_bytes) + data[eoi_index:]

    if output_format == "PNG":
        return pad_png(data, extra_bytes)

    if output_format == "WEBP":
        return pad_webp(data, extra_bytes)

    if output_format == "SVG":
        # Root ke baad whitespace ke bajaye closing tag se pehle valid XML
        # comment insert hota hai. Comment overhead 7 bytes (`<!---->`).
        closing_index = data.rfind(b"</svg>")
        if closing_index < 0 or extra_bytes < 7:
            raise ValueError("Exact SVG padding needs a valid closing tag and 7-byte gap.")
        comment = b"<!--" + (b" " * (extra_bytes - 7)) + b"-->"
        return data[:closing_index] + comment + data[closing_index:]

    # GIF/TIFF me purana trailing-null hack strict validators reject kar sakte
    # the. Safe default: in formats ke liye exact padding unsupported hai.
    raise ValueError(
        f"Exact padding is not supported for {output_format}; use maximum target mode instead."
    )


def resize_for_next_attempt(image: Image.Image, current_size: int, target_size: int) -> Image.Image:
    """Oversized smallest candidate ke basis par next smaller dimensions nikalta hai."""

    width, height = image.size

    if width == MIN_DIMENSION and height == MIN_DIMENSION:
        return image

    # File bytes roughly pixel area ke proportional hoti hain. sqrt ratio side
    # scale deta hai; 0.92 safety margin next attempt ko target ke niche laata hai.
    ratio = math.sqrt(max(target_size, 1) / max(current_size, 1)) * 0.92
    ratio = max(0.20, min(0.90, ratio))
    new_width = max(MIN_DIMENSION, int(width * ratio))
    new_height = max(MIN_DIMENSION, int(height * ratio))

    # Rounding se same dimension aaye to at least one pixel reduce karna zaruri hai.
    if (new_width, new_height) == (width, height):
        new_width = max(MIN_DIMENSION, width - 1)
        new_height = max(MIN_DIMENSION, height - 1)

    validate_pixel_budget(new_width, new_height, "Target-size retry canvas")
    return image.resize((new_width, new_height), Image.Resampling.LANCZOS)


def encode_with_optional_exact_target(
    image: Image.Image,
    output_format: str,
    quality: int,
    dpi: int,
    target_bytes: Optional[int],
    allow_exact_padding: bool = False,
) -> Tuple[bytes, Image.Image, bool]:
    """Normal encode ya bounded maximum-target iterative encode perform karta hai."""

    if target_bytes is None:
        return encode_once(image, output_format, quality, dpi), image, False

    # CPU DoS FIX: target_kb ke saath big canvas reject hota hai even though
    # one normal encode global 50M canvas cap ke andar allowed ho sakta hai.
    validate_pixel_budget(
        image.width,
        image.height,
        "Target-size canvas",
        MAX_EXACT_TARGET_PIXELS,
    )

    working = image
    deadline = time.monotonic() + EXACT_TARGET_TIME_LIMIT_SECONDS
    budget: Dict[str, int] = {"operations": 0, "pixel_work": 0}

    # Purane 24 attempts ko six hard attempts me reduce kiya. Ratio-based jump
    # usually 2-5 attempts me result deta hai; otherwise clear error safer hai.
    for _attempt in range(MAX_EXACT_TARGET_RESIZE_ATTEMPTS):
        candidate, fits = best_candidate_at_current_size(
            working,
            output_format,
            quality,
            dpi,
            target_bytes,
            deadline,
            budget,
        )

        if fits:
            # Default behavior candidate ko target se chhota/equal return karta
            # hai—no junk bytes. Exact padding double opt-in (server + request)
            # hone par only container-aware padding helpers run karte hain.
            if allow_exact_padding:
                exact = pad_to_exact_size(candidate, target_bytes, output_format)
                if len(exact) != target_bytes:
                    raise ValueError("Exact target-size verification failed.")
                return validate_output_size(exact), working, True

            return candidate, working, True

        smaller = resize_for_next_attempt(working, len(candidate), target_bytes)
        if smaller.size == working.size:
            break
        working = smaller

    raise ValueError(
        "Requested target is smaller than the minimum valid output for this format."
    )


def verify_encoded_output(data: bytes, output_format: str) -> None:
    """Response bhejne se pehle final bytes still readable hain ya nahi check karta hai."""

    if output_format == "SVG":
        if not looks_like_svg(data):
            raise ValueError("Generated SVG verification failed.")
        return

    try:
        with Image.open(io.BytesIO(data)) as check:
            check.verify()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValueError("Generated output verification failed.") from exc


# ============================================================================
# 07 // FLASK APPLICATION AND ROUTES
# KYA: Browser requests receive karke JSON inspection ya image response bhejta hai.
# ============================================================================

def read_upload_bytes(upload: Any, field_name: str) -> bytes:
    """Werkzeug upload ko bytes me read karke size/empty validation karta hai."""

    if upload is None or not getattr(upload, "filename", ""):
        raise ValueError(f"Missing {field_name} upload.")

    # Limit+1 bytes enough hain size violation identify karne ke liye; malformed
    # stream ko unlimited `.read()` se memory me pull nahi karte.
    data = upload.read(MAX_UPLOAD_BYTES + 1)

    if not data:
        raise ValueError(f"{field_name} upload is empty.")

    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f"{field_name} exceeds the {MAX_UPLOAD_MB} MB limit.")

    return data


def image_dpi_from_info(data: bytes, detected_format: str) -> int:
    """Original raster metadata DPI read karta; SVG/blank me 72 return karta hai."""

    if detected_format == "SVG":
        return DEFAULT_DPI

    try:
        with Image.open(io.BytesIO(data)) as image:
            dpi_value = image.info.get("dpi", (DEFAULT_DPI, DEFAULT_DPI))
            if isinstance(dpi_value, (tuple, list)):
                return max(1, int(round(float(dpi_value[0]))))
            return max(1, int(round(float(dpi_value))))
    except (OSError, TypeError, ValueError, IndexError):
        return DEFAULT_DPI


# ============================================================================
# 08 // SMART PHOTO-MODE ENGINE  (NAYA FEATURE — ADDED, PURANA KUCH BHI NAHI HATAYA)
# ============================================================================
# KYA HAI:
#     Yeh poora section ek "extra dimaag" hai jo upar ke purane code ke saath
#     kaam karta hai. Isme hum:
#       1) Kisi bhi uploaded photo ka size/orientation/aspect-ratio batate hain
#          (jaise "yeh photo LANDSCAPE hai, ratio approx 16:9 hai").
#       2) Photo ko ready-made social-media "modes" me convert karte hain:
#          Facebook, Instagram, YouTube, X, LinkedIn, Pinterest, TikTok,
#          Tumblr, Snapchat, Vinted aur E-mail ke saare requested presets;
#          aur in sabke beech AAGE-PEECHHE (vice versa) bhi convert kar sakte
#          hain — matlab Thumbnail -> Banner, Banner -> Instagram Post, Post ->
#          Story, Story -> Thumbnail... koi bhi combination, kyunki neeche wala
#          conversion-function generic hai (kisi bhi mode-name ko hardcode
#          nahi karta, sirf SMART_MODE_PRESETS dictionary padhta hai).
#       3) Export karte waqt teen quality options dete hain: HD, FULL HD,
#          ULTRA HD 4K — jinme size ke saath-saath encode-quality (compression
#          strength) bhi badhti hai, taaki photo genuinely sharper/crisper aaye.
#       4) Agar user khud manually width/height daalta hai (preset mode use
#          nahi karta) to hum ek "flash message" (warning text list) bhejte
#          hain jisme batate hain ki quality/crop par kya asar padega.
#
# YEH PURANE CODE PAR ASAR KYU NAHI DAALEGA:
#     - Humne upar ki (Section 01 se 07 tak) koi bhi line edit/delete nahi ki.
#     - Naye functions/routes bilkul ALAG naam se bane hain (jaise
#       "smart_", "photo_mode_", "SMART_" prefix), isliye purane
#       function/route/constant names se koi naam-clash nahi hoga.
#     - Naye Flask routes ek alag function `register_smart_photo_mode_routes()`
#       ke andar register hote hain, jise hum niche `app` object bann jaane ke
#       BAAD call karte hain. Isse purana `create_app()` function bhi
#       bilkul untouched (waisa hi) rehta hai.
#     - Agar future me is naye Section 08 ko poora hata bhi diya jaye, to
#       purana /, /inspect, /resize, /convert wala system bilkul waise hi
#       chalta rahega — koi dependency ulti taraf (purana -> naya) nahi hai.
# ============================================================================


# ----------------------------------------------------------------------------
# 08.1 // MODE PRESETS  (kaunsa social-media "mode" kitne pixels ka hota hai)
# KYA: Har mode ka standard/recommended width, height aur uska aspect-ratio
#      (jaise 16:9, 1:1, 9:16) ek dictionary me store kiya hai.
# KYUN: Agar kabhi kisi platform ka recommended size badal de, to sirf
#      yahi dictionary update karni hai; neeche wale saare functions is
#      dictionary ko "read" karte hain, size ko kahin bhi hardcode nahi karte.
# VALUE CHANGE KA EFFECT:
#      - "width"/"height" change karoge to us mode me convert hone wali HAR
#        future photo turant naye size me export hogi (HD tier isi naye size
#        ko 1x maanega, FULL_HD 1.5x, ULTRA_HD_4K 3x — dekho Section 08.2).
#      - Naya mode add karna ho, to bas
#        isi dictionary me ek naya key-value pair daal do; list, analyze aur
#        convert — teeno automatically naye mode ko bhi support karne lagenge,
#        kyunki koi bhi neeche wala function mode-name ko hardcode nahi karta.
#
# IMPORTANT PIXEL-SIZE RULE:
#      User ki list me aspect ratios diye gaye the, exact pixel dimensions
#      nahi. Neeche practical base canvases choose kiye gaye hain jo requested
#      ratio ko EXACTLY preserve karte hain. HD base size use karta hai;
#      FULL_HD aur ULTRA_HD_4K Section 08.2 ke multiplier se scale hote hain.
# ----------------------------------------------------------------------------

SMART_MODE_PRESETS: Dict[str, Dict[str, Any]] = {
    # ------------------------------ Facebook ------------------------------
    "FACEBOOK_PROFILE_PHOTO": {
        "label": "Profile Photo Size",
        "platform": "Facebook",
        "width": 1080,
        "height": 1080,
        "aspect_label": "1:1",
        "default_fit": "cover",
    },
    "FACEBOOK_POST_SQUARE": {
        "label": "Post Square Size",
        "platform": "Facebook",
        "width": 1080,
        "height": 1080,
        "aspect_label": "1:1",
        "default_fit": "cover",
    },
    "FACEBOOK_STORIES": {
        "label": "Stories Size",
        "platform": "Facebook",
        "width": 1080,
        "height": 1920,
        "aspect_label": "9:16",
        "default_fit": "cover",
    },
    "FACEBOOK_REELS": {
        "label": "Reels Size",
        "platform": "Facebook",
        "width": 1080,
        "height": 1920,
        "aspect_label": "9:16",
        "default_fit": "cover",
    },
    "FACEBOOK_GROUP_COVER_IMAGE": {
        "label": "Group Cover Image Size",
        "platform": "Facebook",
        "width": 1920,
        "height": 1080,
        "aspect_label": "16:9",
        "default_fit": "cover",
    },
    "FACEBOOK_COVER_EVENT_IMAGE": {
        "label": "Cover Event Image Size",
        "platform": "Facebook",
        "width": 1920,
        "height": 1000,
        "aspect_label": "1.92:1",
        "default_fit": "cover",
    },

    # ------------------------------ Instagram -----------------------------
    "INSTAGRAM_PROFILE_PICTURE": {
        "label": "Profile Picture Size",
        "platform": "Instagram",
        "width": 1080,
        "height": 1080,
        "aspect_label": "1:1",
        "default_fit": "cover",
    },
    "INSTAGRAM_POST": {
        # Existing API key preserved: purana frontend INSTAGRAM_POST bhej sakta hai.
        "label": "Post (square) Size",
        "platform": "Instagram",
        "width": 1080,
        "height": 1080,
        "aspect_label": "1:1",
        "default_fit": "cover",
    },
    "INSTAGRAM_POST_PORTRAIT": {
        "label": "Post (portrait) Size",
        "platform": "Instagram",
        "width": 1080,
        "height": 1350,
        "aspect_label": "4:5",
        "default_fit": "cover",
    },
    "INSTAGRAM_POST_LANDSCAPE": {
        "label": "Post (landscape) Size",
        "platform": "Instagram",
        "width": 1910,
        "height": 1000,
        "aspect_label": "1.91:1",
        "default_fit": "cover",
    },
    "INSTAGRAM_STORY": {
        # Existing API key preserved: purana frontend INSTAGRAM_STORY bhej sakta hai.
        "label": "Story Size",
        "platform": "Instagram",
        "width": 1080,
        "height": 1920,
        "aspect_label": "9:16",
        "default_fit": "cover",
    },
    "INSTAGRAM_REELS": {
        "label": "Reels Size",
        "platform": "Instagram",
        "width": 1080,
        "height": 1920,
        "aspect_label": "9:16",
        "default_fit": "cover",
    },
    "INSTAGRAM_REELS_COVER": {
        "label": "Reels Cover Size",
        "platform": "Instagram",
        "width": 1080,
        "height": 1920,
        "aspect_label": "9:16",
        "default_fit": "cover",
    },

    # ------------------------------- YouTube -------------------------------
    "YOUTUBE_PROFILE_PHOTO": {
        "label": "Profile Photo Size",
        "platform": "YouTube",
        "width": 1080,
        "height": 1080,
        "aspect_label": "1:1",
        "default_fit": "cover",
    },
    "YOUTUBE_THUMBNAIL": {
        # Existing API key preserved.
        "label": "Thumbnail Size",
        "platform": "YouTube",
        "width": 1280,
        "height": 720,
        "aspect_label": "16:9",
        "default_fit": "cover",
    },
    "YOUTUBE_BANNER": {
        # Existing API key preserved.
        "label": "Banner Size",
        "platform": "YouTube",
        "width": 2560,
        "height": 1440,
        "aspect_label": "16:9",
        "default_fit": "cover",
    },

    # ---------------------------------- X ----------------------------------
    "X_PROFILE_PICTURE": {
        "label": "Profile Picture Size",
        "platform": "X",
        "width": 1080,
        "height": 1080,
        "aspect_label": "1:1",
        "default_fit": "cover",
    },
    "X_HEADER_PICTURE": {
        "label": "Header Picture Size",
        "platform": "X",
        "width": 1920,
        "height": 1080,
        "aspect_label": "16:9",
        "default_fit": "cover",
    },

    # -------------------------- LinkedIn Personal --------------------------
    "LINKEDIN_PERSONAL_PROFILE_PHOTO": {
        "label": "Profile Photo Size",
        "platform": "LinkedIn Personal",
        "width": 1080,
        "height": 1080,
        "aspect_label": "1:1",
        "default_fit": "cover",
    },
    "LINKEDIN_PERSONAL_BACKGROUND_PHOTO": {
        "label": "Background Photo Size",
        "platform": "LinkedIn Personal",
        "width": 1584,
        "height": 396,
        "aspect_label": "4:1",
        "default_fit": "cover",
    },

    # -------------------------- LinkedIn Company ---------------------------
    "LINKEDIN_COMPANY_LOGO": {
        "label": "Company Logo Size",
        "platform": "LinkedIn Company",
        "width": 1080,
        "height": 1080,
        "aspect_label": "1:1",
        "default_fit": "cover",
    },
    "LINKEDIN_COMPANY_COVER_PHOTO": {
        "label": "Cover Photo Size",
        "platform": "LinkedIn Company",
        "width": 1182,
        "height": 200,
        "aspect_label": "5.91:1",
        "default_fit": "cover",
    },

    # ------------------------------ Pinterest ------------------------------
    "PINTEREST_SQUARE_IMAGES_PIN": {
        "label": "Square Images Pin Size",
        "platform": "Pinterest",
        "width": 1080,
        "height": 1080,
        "aspect_label": "1:1",
        "default_fit": "cover",
    },
    "PINTEREST_STANDARD_IMAGE_PIN": {
        "label": "Standard Image Pin Size",
        "platform": "Pinterest",
        "width": 1000,
        "height": 1500,
        "aspect_label": "2:3",
        "default_fit": "cover",
    },
    "PINTEREST_VERTICAL_IMAGE_PIN": {
        "label": "Vertical Image Pin Size",
        "platform": "Pinterest",
        "width": 1080,
        "height": 1920,
        "aspect_label": "9:16",
        "default_fit": "cover",
    },

    # -------------------------------- TikTok -------------------------------
    "TIKTOK_PROFILE_PICTURE": {
        "label": "Profile Picture Size",
        "platform": "TikTok",
        "width": 1080,
        "height": 1080,
        "aspect_label": "1:1",
        "default_fit": "cover",
    },
    "TIKTOK_IN_FEED_AD_IMAGE": {
        "label": "In-feed Ad Image Size",
        "platform": "TikTok",
        "width": 1080,
        "height": 1080,
        "aspect_label": "1:1",
        "default_fit": "cover",
    },

    # -------------------------------- Tumblr -------------------------------
    "TUMBLR_PROFILE_PICTURE": {
        "label": "Profile Picture Size",
        "platform": "Tumblr",
        "width": 1080,
        "height": 1080,
        "aspect_label": "1:1",
        "default_fit": "cover",
    },
    "TUMBLR_HEADER_IMAGE": {
        "label": "Header Image Size",
        "platform": "Tumblr",
        "width": 1920,
        "height": 1080,
        "aspect_label": "16:9",
        "default_fit": "cover",
    },

    # ------------------------------- Snapchat ------------------------------
    "SNAPCHAT_IMAGE_SHARE": {
        "label": "Image Share Size",
        "platform": "Snapchat",
        "width": 1080,
        "height": 1920,
        "aspect_label": "9:16",
        "default_fit": "cover",
    },

    # -------------------------------- Vinted -------------------------------
    "VINTED_PROFILE_PICTURE": {
        "label": "Profile Picture Size",
        "platform": "Vinted",
        "width": 1080,
        "height": 1080,
        "aspect_label": "1:1",
        "default_fit": "cover",
    },
    "VINTED_ITEM_PHOTO": {
        "label": "Item Photo Size",
        "platform": "Vinted",
        "width": 1000,
        "height": 1500,
        "aspect_label": "2:3",
        "default_fit": "cover",
    },

    # -------------------------------- E-mail -------------------------------
    "EMAIL_BLOG_FEATURED_3_1": {
        "label": "Email Blog Featured Size",
        "platform": "E-mail",
        "width": 1200,
        "height": 400,
        "aspect_label": "3:1",
        "default_fit": "cover",
    },
    "EMAIL_BLOG_IMAGE": {
        "label": "Email Blog Image Size",
        "platform": "E-mail",
        "width": 1080,
        "height": 1080,
        "aspect_label": "1:1",
        "default_fit": "cover",
    },
    "EMAIL_BLOG_FEATURED_2_1": {
        "label": "Email Blog Featured Size",
        "platform": "E-mail",
        "width": 1200,
        "height": 600,
        "aspect_label": "2:1",
        "default_fit": "cover",
    },
}


# ----------------------------------------------------------------------------
# 08.2 // EXPORT QUALITY TIERS  (HD / FULL HD / ULTRA HD 4K)
# KYA: Har tier batata hai ki us mode ke base (preset) size ko kitna bada
#      karna hai ("multiplier"), aur encode quality (JPEG/WEBP compression
#      strength) kitni rakhni hai.
# KYUN: User ne mangi thi "export ke time 3 options do: HD, Full HD, Ultra HD
#      4K, aur size ke saath quality bhi badhni chahiye". Isliye har agla
#      tier bada multiplier + zyada encode-quality dono use karta hai, taaki
#      photo genuinely sharper/crisper lage — sirf naam alag na ho.
# VALUE CHANGE KA EFFECT:
#      - "multiplier" badhaoge to us tier me exported photo ka pixel-size
#        badh jaayega (file size bhi usi hisaab se badhega).
#      - "encode_quality" badhaoge to compression kam hoga → photo crisper
#        dikhegi, par file size bhi badhega. 100 ke bahut paas jaane se fayda
#        bahut kam hota hai par size bahut zyada badh jaata hai, isliye
#        humne practical/production-safe values rakhi hain.
# ----------------------------------------------------------------------------

QUALITY_EXPORT_TIERS: Dict[str, Dict[str, Any]] = {
    "HD": {
        "label": "HD Photo",
        "multiplier": 1.0,     # Mode ka standard/base size, jaisa preset me likha hai.
        "encode_quality": 85,  # Halka compression → chhoti file, phir bhi achhi dikhti hai.
        "description": "Platform ka standard recommended size. Fastest upload, chhoti file size.",
    },
    "FULL_HD": {
        "label": "Full HD",
        "multiplier": 1.5,     # Base size se 1.5x zyada pixels (sharper).
        "encode_quality": 92,  # Kam compression, zyada detail retain hoti hai.
        "description": "Base size se 1.5x sharper. Quality aur file-size ka best balance.",
    },
    "ULTRA_HD_4K": {
        "label": "Ultra HD 4K",
        "multiplier": 3.0,     # Base size se 3x zyada pixels (maximum sharpness).
        "encode_quality": 97,  # Bahut kam compression, best possible detail retain hota hai.
        "description": "Maximum resolution aur sharpness. Sabse badi file size.",
    },
}


# ----------------------------------------------------------------------------
# 08.3 // ASPECT RATIO + ORIENTATION DETECTION
# KYA: In helper functions se hum kisi bhi width/height se batate hain ki
#      photo LANDSCAPE hai, PORTRAIT/SCREEN hai, ya SQUARE(normal) hai, aur
#      uska ratio (jaise "16:9", "4:5") kya hai.
# KYUN: User ne mangi thi "photo upload karte hi uska size/format/mode batao
#      (landscape, screen/portrait, normal)". Yeh functions wahi guidance
#      generate karte hain, purane /inspect route se bilkul alag naye
#      /analyze-photo-mode route ke liye.
# ----------------------------------------------------------------------------

# Yeh list un aspect-ratios ki hai jo real duniya me sabse zyada common hain.
# Har uploaded photo ka EXACT ratio kabhi bhi in numbers se 100% match nahi
# karega (camera sensors thode-bahut idhar-udhar hote hain), isliye hum
# "closest match" dhoondhte hain, exact match nahi.
COMMON_ASPECT_RATIOS: Tuple[Tuple[str, float], ...] = (
    ("1:1", 1 / 1),    # Perfect square, jaise Instagram Post.
    ("16:9", 16 / 9),  # Widescreen video/thumbnail, jaise YouTube.
    ("9:16", 9 / 16),  # Tall/vertical, jaise Instagram Story ya mobile screen.
    ("1.92:1", 1.92 / 1),  # Facebook event cover.
    ("1.91:1", 1.91 / 1),  # Instagram landscape post.
    ("4:1", 4 / 1),    # LinkedIn personal background.
    ("5.91:1", 5.91 / 1),  # LinkedIn company cover.
    ("3:1", 3 / 1),    # E-mail featured image.
    ("2:1", 2 / 1),    # E-mail featured image alternative.
    ("4:3", 4 / 3),    # Purane cameras/TV ka classic landscape ratio.
    ("3:4", 3 / 4),    # 4:3 ka portrait (khada) version.
    ("4:5", 4 / 5),    # Instagram ka "portrait post" ratio.
    ("5:4", 5 / 4),    # 4:5 ka ulta (landscape) version.
    ("3:2", 3 / 2),    # DSLR/mirrorless photography ka common landscape ratio.
    ("2:3", 2 / 3),    # 3:2 ka portrait version.
    ("21:9", 21 / 9),  # Ultra-wide cinematic banner jaisa ratio.
)


def classify_orientation(width: int, height: int) -> str:
    """Width/height ke ratio se batata hai photo LANDSCAPE / PORTRAIT / SQUARE hai."""

    # Divide-by-zero se bachne ke liye safety check. Practically kabhi 0 nahi
    # aayega kyunki Pillow image ki width/height hamesha >= 1 hoti hai, par
    # is function ko standalone (jaise future unit-test) safe rakhne ke liye
    # yeh guard rakha hai.
    if height <= 0:
        return "UNKNOWN"

    ratio = width / height

    # Chhoti si tolerance (0.05) rakhi hai kyunki 1000x998 jaisi photo bhi
    # practically "square" hi lagti hai, exact 1:1 na hone ke bawajood.
    if abs(ratio - 1.0) <= 0.05:
        return "SQUARE"

    # Ratio 1 se zyada matlab width > height, yani photo "chaudi/wide" hai.
    if ratio > 1.0:
        return "LANDSCAPE"

    # Baaki bacha ek hi case: width < height, yani photo "khadi/lambi" hai.
    # UI/product terminology me isse "PORTRAIT" ya "SCREEN/STORY MODE" bola jaata hai.
    return "PORTRAIT"


def nearest_common_ratio_label(width: int, height: int) -> str:
    """Actual pixel ratio ko sabse paas wale common label (jaise '16:9') se match karta hai."""

    if height <= 0:
        return "UNKNOWN"

    actual_ratio = width / height

    best_label = "UNKNOWN"
    best_difference: Optional[float] = None

    # Har known ratio ke saath actual ratio ka farak (difference) nikal rahe
    # hain; jiska farak sabse kam hoga wahi label final answer banega.
    for label, reference_ratio in COMMON_ASPECT_RATIOS:
        difference = abs(actual_ratio - reference_ratio)

        if best_difference is None or difference < best_difference:
            best_difference = difference
            best_label = label

    return best_label


def describe_photo_profile(width: int, height: int) -> Dict[str, Any]:
    """Ek photo ke width/height se poora human-readable "profile" (guide) banata hai."""

    orientation = classify_orientation(width, height)
    ratio_label = nearest_common_ratio_label(width, height)

    # Beginner-friendly ek-line message banaya hai jo directly frontend par
    # dikhaya ja sakta hai, jaise: "Yeh photo LANDSCAPE mode me hai (ratio ~16:9)."
    if orientation == "SQUARE":
        human_message = f"Yeh photo SQUARE (normal) mode me hai — ratio approx {ratio_label}."
    elif orientation == "LANDSCAPE":
        human_message = f"Yeh photo LANDSCAPE (chaudi/wide) mode me hai — ratio approx {ratio_label}."
    elif orientation == "PORTRAIT":
        human_message = f"Yeh photo PORTRAIT/SCREEN (khadi/lambi) mode me hai — ratio approx {ratio_label}."
    else:
        # Yeh branch practically kabhi nahi chalega (Pillow image ki width/
        # height hamesha valid hoti hain), par function ko crash-proof rakhne
        # ke liye fallback message rakha hai.
        human_message = "Photo ka orientation detect nahi ho paaya."

    return {
        "width": width,
        "height": height,
        "orientation": orientation,          # LANDSCAPE / PORTRAIT / SQUARE
        "aspect_ratio_label": ratio_label,   # jaise "16:9"
        "message": human_message,
    }


def suggest_closest_modes(width: int, height: int, top_n: int = 2) -> Any:
    """Uploaded photo ke ratio se sabse milte-julte 'top_n' ready-made modes suggest karta hai."""

    if height <= 0:
        return []

    actual_ratio = width / height
    scored_modes = []

    # Har preset mode ke ratio se actual photo ke ratio ka farak nikal rahe hain.
    for mode_key, preset in SMART_MODE_PRESETS.items():
        preset_ratio = preset["width"] / preset["height"]
        difference = abs(actual_ratio - preset_ratio)
        scored_modes.append(
            {
                "mode": mode_key,
                "label": preset["label"],
                "aspect_label": preset["aspect_label"],
                # match_score 1.0 ke jitna paas hoga utna better match hai;
                # difference bahut chhota ho to score ~1.0 ke bahut paas rahega.
                "match_score": round(1 / (1 + difference), 4),
            }
        )

    # Sabse kam "difference" (yani sabse zyada match_score) wale modes upar
    # aa jaayein, isliye match_score ke descending (bade se chhote) order me sort kiya hai.
    scored_modes.sort(key=lambda item: item["match_score"], reverse=True)

    # top_n se zyada items list me nahi bhejni; UI ko sirf best suggestions chahiye.
    return scored_modes[:max(1, top_n)]


# ----------------------------------------------------------------------------
# 08.4 // TARGET-SIZE CALCULATION (mode + quality tier + optional manual size)
# KYA: Final export width/height decide karta hai — ya to preset mode +
#      quality tier se (automatic), ya user ke diye hue manual numbers se.
# KYUN: User ne mangi thi ki agar wo khud size daale to "flash message"
#      (quality/crop warning) mile. Yeh function hi wo warnings generate
#      karta hai, taaki route-code saaf/simple rahe.
# ----------------------------------------------------------------------------

def compute_mode_export_size(mode_key: str, tier_key: str) -> Tuple[int, int, int]:
    """Preset mode + quality tier se final width/height/encode-quality nikalta hai."""

    if mode_key not in SMART_MODE_PRESETS:
        # Galat/typo mode name aane par silently kuch guess karna dangerous
        # hai; clear error dena beginner ke liye debug karna aasan banata hai.
        raise ValueError(
            f"Unknown photo mode '{mode_key}'. Valid modes: "
            f"{', '.join(SMART_MODE_PRESETS.keys())}."
        )

    if tier_key not in QUALITY_EXPORT_TIERS:
        raise ValueError(
            f"Unknown quality tier '{tier_key}'. Valid tiers: "
            f"{', '.join(QUALITY_EXPORT_TIERS.keys())}."
        )

    preset = SMART_MODE_PRESETS[mode_key]
    tier = QUALITY_EXPORT_TIERS[tier_key]

    # Base preset size ko tier ke multiplier se badhaya jaa raha hai.
    # Example: YOUTUBE_THUMBNAIL base 1280x720 hai. FULL_HD tier (1.5x) me
    # yeh 1920x1080 ban jaayega. ULTRA_HD_4K tier (3x) me 3840x2160 ban jaayega.
    raw_width = preset["width"] * tier["multiplier"]
    raw_height = preset["height"] * tier["multiplier"]

    # MAX_DIMENSION (20,000 px, Section 01 me define hai) se upar jaana Pillow
    # aur server-RAM ke liye risky hai, isliye humesha safe range ke andar
    # clamp (limit) kar rahe hain. MIN_DIMENSION se neeche bhi nahi jaane dete.
    final_width = max(MIN_DIMENSION, min(MAX_DIMENSION, round(raw_width)))
    final_height = max(MIN_DIMENSION, min(MAX_DIMENSION, round(raw_height)))

    # Per-side clamp ke baad area check essential hai: 20k×20k otherwise pass
    # ho jaata. Preset table future me badle tab bhi unsafe canvas block hogi.
    validate_pixel_budget(final_width, final_height, "Photo-mode output")

    return final_width, final_height, tier["encode_quality"]


def build_manual_quality_warnings(
    original_width: int,
    original_height: int,
    target_width: int,
    target_height: int,
    mode_key: Optional[str],
) -> Any:
    """Manual size use hone par user ko quality/crop ke baare me warning-messages ki list deta hai."""

    warnings = []

    original_pixels = max(1, original_width * original_height)
    target_pixels = max(1, target_width * target_height)

    # Agar target area original se bada hai, matlab image "upscale" ho rahi
    # hai. Upscaling me naye pixels software dwara "guess/interpolate" hote
    # hain (kyunki wo asal me camera se capture nahi hue), isliye thoda
    # softness/blur aana natural hai — user ko yeh clearly bata dena chahiye.
    if target_pixels > original_pixels:
        growth_times = round(target_pixels / original_pixels, 2)
        warnings.append(
            f"QUALITY ALERT: Aap photo ko approx {growth_times}x bada (upscale) kar rahe ho. "
            "Naye pixels software se predict/interpolate hote hain, isliye photo thodi soft "
            "ya kam sharp dikh sakti hai. Best result ke liye original photo ka resolution "
            "target size jitna ya usse bada hona chahiye."
        )

    # Agar target ka aspect-ratio, uploaded photo ke aspect-ratio se kaafi
    # alag hai, to "cover" fit-strategy use karne par kuch hissa (edges)
    # automatically crop ho jaayega — user ko pehle se pata hona chahiye.
    original_ratio = original_width / max(1, original_height)
    target_ratio = target_width / max(1, target_height)
    ratio_difference = abs(original_ratio - target_ratio)

    if ratio_difference > 0.05:
        warnings.append(
            "CROP ALERT: Is size ka aspect-ratio aapki original photo se match nahi karta. "
            "Photo ko is size me fit karne ke liye kuch hissa (edges) automatically crop "
            "ho sakta hai, taaki poora canvas bina kisi khaali jagah/stretch ke bhar jaaye."
        )

    # Agar user ne koi bhi preset mode select nahi kiya (pura custom size),
    # to ek general reminder de dete hain.
    if mode_key is None:
        warnings.append(
            "MANUAL SIZE: Aapne size khud enter ki hai (koi preset mode select nahi kiya). "
            "Behtar platform-perfect result ke liye Facebook, Instagram, YouTube, X, LinkedIn, "
            "Pinterest, TikTok, Tumblr, Snapchat, Vinted ya E-mail ka preset choose karna recommended hai."
        )

    return warnings


def resolve_export_target(
    mode_key: Optional[str],
    tier_key: str,
    manual_width: Any,
    manual_height: Any,
    original_width: int,
    original_height: int,
) -> Tuple[int, int, int, Any, bool]:
    """Final width/height/quality/warnings decide karta hai — manual aur automatic dono cases handle karta hai."""

    # Blank/None form value ko 0 treat karte hain (parse_int ka documented
    # default), isliye "0 = user ne yeh field nahi bhari" ka matlab lete hain.
    manual_width_value = parse_int(manual_width, 0, 0, MAX_DIMENSION, "MANUAL WIDTH")
    manual_height_value = parse_int(manual_height, 0, 0, MAX_DIMENSION, "MANUAL HEIGHT")
    used_manual_size = bool(manual_width_value or manual_height_value)

    if used_manual_size:
        # ---- CASE 1: User ne khud size di hai (poora ya partial). ----
        if manual_width_value and manual_height_value:
            # Dono values diye — exact wahi canvas size use hogi.
            target_width, target_height = manual_width_value, manual_height_value
        elif manual_width_value:
            # Sirf width di — original photo ke ratio se height auto-calculate
            # hogi (taaki photo distort/stretch na ho).
            target_width = manual_width_value
            target_height = max(
                MIN_DIMENSION,
                round(manual_width_value * original_height / max(1, original_width)),
            )
        else:
            # Sirf height di — original photo ke ratio se width auto-calculate hogi.
            target_height = manual_height_value
            target_width = max(
                MIN_DIMENSION,
                round(manual_height_value * original_width / max(1, original_height)),
            )

        # Manual size me quality-tier sirf "encode quality" (compression
        # strength) decide karne ke liye use hota hai; pixel-size user ke
        # diye numbers se hi aati hai, tier ka multiplier yahan ignore hota hai.
        tier = QUALITY_EXPORT_TIERS.get(tier_key, QUALITY_EXPORT_TIERS["HD"])
        encode_quality = tier["encode_quality"]

        warnings = build_manual_quality_warnings(
            original_width, original_height, target_width, target_height, mode_key
        )
        validate_pixel_budget(target_width, target_height, "Manual photo-mode output")
        return target_width, target_height, encode_quality, warnings, True

    # ---- CASE 2: User ne preset mode + quality tier choose kiya (automatic). ----
    if mode_key is None:
        # Na mode diya, na manual size — is request se kuch bhi karna
        # impossible hai, isliye clear error dena zaroori hai.
        raise ValueError(
            "Provide either a valid photo 'mode' from /photo-modes or a manual width/height."
        )

    target_width, target_height, encode_quality = compute_mode_export_size(mode_key, tier_key)

    # Automatic preset path me bhi hum ek chhota informational (warning nahi,
    # sirf info) message bhej sakte hain agar original photo target se bahut
    # chhoti hai — taaki user surprise na ho ki quality thodi soft aayi.
    warnings = []
    if (target_width * target_height) > (original_width * original_height):
        warnings.append(
            "INFO: Selected quality tier ka size original photo se bada hai, isliye thoda "
            "upscaling hoga. Sabse sharp result ke liye ek high-resolution original photo "
            "upload karna best rehta hai."
        )

    return target_width, target_height, encode_quality, warnings, False


# ----------------------------------------------------------------------------
# 08.5 // MODE-FIT RESIZER  (cover / contain) + SHARPENING AFTER RESIZE
# KYA: Photo ko exact target width/height ke canvas me "fit" karta hai bina
#      use stretch/distort kiye.
# KYUN: Seedha .resize(width, height) karne se agar ratio match nahi karta to
#      photo "chapat" (squeezed/stretched) dikhti hai — jo kisi bhi
#      professional photo-editor me acceptable nahi hota. Cover/contain dono
#      hi is problem ko sahi tareeke se solve karte hain.
# ----------------------------------------------------------------------------

def fit_image_to_target_canvas(
    image: Image.Image,
    target_width: int,
    target_height: int,
    fit_strategy: str,
) -> Image.Image:
    """Image ko target canvas me 'cover' (crop-to-fill) ya 'contain' (letterbox) se fit karta hai."""

    # ImageOps.contain/Image.new/ImageOps.fit allocation se pehle shared canvas
    # area check hota hai. Yeh smart photo-mode ke manual 20k×20k DoS ko rokta hai.
    validate_pixel_budget(target_width, target_height, "Photo-mode canvas")

    if fit_strategy == "contain":
        # ---- CONTAIN: poori photo dikhti hai, zaroorat par khaali jagah (background) aati hai. ----
        # Pehle photo ko is tarah chhota/bada karte hain ki wo target canvas
        # ke ANDAR poori aa jaaye (kisi bhi side se crop nahi hoti).
        resized = ImageOps.contain(
            image, (target_width, target_height), method=Image.Resampling.LANCZOS
        )

        # Transparency support karne wale formats (RGBA) ke liye transparent
        # background, warna neutral white background use karte hain.
        if image.mode == "RGBA":
            canvas = Image.new("RGBA", (target_width, target_height), (0, 0, 0, 0))
        else:
            canvas = Image.new("RGB", (target_width, target_height), "white")

        # Resized photo ko canvas ke exact center me paste karte hain, taaki
        # dono taraf equal khaali jagah (agar koi ho) symmetrical rahe.
        paste_x = (target_width - resized.width) // 2
        paste_y = (target_height - resized.height) // 2

        if resized.mode == "RGBA":
            canvas.paste(resized, (paste_x, paste_y), mask=resized.getchannel("A"))
        else:
            canvas.paste(resized, (paste_x, paste_y))

        return canvas

    # ---- COVER (default): canvas 100% bharega, zaroorat par thoda crop hoga. ----
    # ImageOps.fit photo ko resize + center-crop dono ek saath karta hai taaki
    # final image exactly (target_width, target_height) ki bane aur poora
    # canvas bina kisi khaali jagah ke bhar jaaye — jaisa Instagram/YouTube
    # khud apne editors me karte hain.
    return ImageOps.fit(
        image,
        (target_width, target_height),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),  # (0.5, 0.5) = photo ka bilkul center crop ke liye use hota hai.
    )


def sharpen_if_upscaled(image: Image.Image, original_pixel_count: int) -> Image.Image:
    """Photo enlarge (upscale) hui ho to halka sharpening laga kar softness kam karta hai."""

    new_pixel_count = image.width * image.height

    # Sirf tab sharpen karo jab photo genuinely badi hui ho. Chhoti/waisi hi
    # size par extra sharpening lagana photo ko "over-processed"/artificial
    # dikha sakta hai, isliye yeh condition zaroori hai.
    if new_pixel_count > original_pixel_count:
        # radius/percent halke (mild) rakhe hain taaki natural detail badhe,
        # halo/noise jaisa fake-sharp look na aaye. Yeh hi is feature ki
        # "compress/suppress hone par bhi excellent quality" wali requirement
        # poori karta hai.
        return image.filter(ImageFilter.UnsharpMask(radius=1.4, percent=70, threshold=2))

    return image


# ----------------------------------------------------------------------------
# 08.6 // FLASK ROUTES FOR SMART PHOTO MODES
# KYA: Yeh function purane `create_app()` ke BAHAR rehta hai aur naye routes
#      ko already-bane hue `flask_app` object par register karta hai.
# KYUN: Isse purana `create_app()` function bilkul untouched rehta hai — hum
#      sirf app object ko "extend" kar rahe hain, use replace nahi kar rahe.
# ----------------------------------------------------------------------------

class _FormWithoutSizeFields:
    """request.form ka ek chhota "wrapper" jo width/height fields ko chhupa deta hai.

    KYA: Yeh class request.form jaisa hi `.get(key, default)` method deti
         hai, par "width" aur "height" keys ke liye hamesha None return
         karti hai; baaki sabhi keys (rotation, brightness, contrast, ...)
         original form se as-is pass ho jaati hain.

    KYUN: Purana `apply_requested_edits()` function (Section 04, line ~393)
         khud "width"/"height" form-fields padh kar seedha `.resize()`
         (stretch-style) kar deta hai. Hamare naye "mode-convert" route me
         wahi "width"/"height" naam manual-size ke liye use ho rahe hain
         (jinhe hum khud cover/contain se sambhalte hain). Agar hum seedha
         `request.form` bhej dete, to photo DO BAAR resize ho jaati —
         pehle purana function stretch kar deta, phir hamara naya function
         crop/fit karta — jisse final photo distort ho sakti thi.

    EFFECT: Is wrapper ki wajah se purana `apply_requested_edits()` function
         sirf rotation/brightness/contrast/saturation/sharpness apply karta
         hai (jo bilkul sahi hai), aur size/canvas ka final faisla sirf
         hamara naya `fit_image_to_target_canvas()` function karta hai.
         Purane function ka code ek line bhi change nahi hua — hum sirf
         usko ek "customised" form-object de rahe hain.
    """

    def __init__(self, original_form: Any) -> None:
        self._original_form = original_form

    def get(self, key: str, default: Any = None) -> Any:
        if key in ("width", "height"):
            # Yeh do keys jaan-boojh kar hide ki hain (upar wali docstring
            # explain karti hai kyun).
            return None
        return self._original_form.get(key, default)


def register_smart_photo_mode_routes(flask_app: Any) -> None:
    """Naye /photo-modes, /analyze-photo-mode aur /convert-photo-mode routes register karta hai."""

    @flask_app.route("/photo-modes", methods=["GET"])
    def list_photo_modes() -> Any:
        """Frontend dropdown ke liye saare available modes aur quality tiers ki list deta hai."""

        modes_payload = {
            mode_key: {
                "label": preset["label"],
                "platform": preset["platform"],
                "width": preset["width"],
                "height": preset["height"],
                "aspect_label": preset["aspect_label"],
            }
            for mode_key, preset in SMART_MODE_PRESETS.items()
        }

        tiers_payload = {
            tier_key: {
                "label": tier["label"],
                "description": tier["description"],
            }
            for tier_key, tier in QUALITY_EXPORT_TIERS.items()
        }

        return jsonify(modes=modes_payload, quality_tiers=tiers_payload)

    @flask_app.route("/analyze-photo-mode", methods=["POST", "OPTIONS"])
    def analyze_photo_mode() -> Any:
        """Upload hote hi photo ka size/orientation/ratio aur best-matching modes batata hai."""

        if request.method == "OPTIONS":
            # Browser ka CORS "preflight" request hai; koi processing nahi,
            # sirf khaali 204 (No Content) reply dena hota hai.
            return make_response("", 204)

        upload = request.files.get("image")
        data = read_upload_bytes(upload, "image")           # Purana helper reuse (size/empty check).
        detected_format = detect_input_format(data, upload.filename)  # Purana helper reuse.
        image = open_image_bytes(data, detected_format)     # Purana helper reuse (EXIF-safe decode).

        profile = describe_photo_profile(image.width, image.height)
        closest_modes = suggest_closest_modes(image.width, image.height, top_n=2)

        return jsonify(
            format=detected_format,
            width=image.width,
            height=image.height,
            dimensions=f"{image.width} × {image.height} px",
            size_bytes=len(data),
            orientation=profile["orientation"],
            aspect_ratio_label=profile["aspect_ratio_label"],
            message=profile["message"],
            recommended_modes=closest_modes,
        )

    @flask_app.route("/convert-photo-mode", methods=["POST", "OPTIONS"])
    def convert_photo_mode() -> Any:
        """Ek photo-mode se doosre mode me (ya manual size me) photo convert karta hai."""

        if request.method == "OPTIONS":
            return make_response("", 204)

        upload = request.files.get("image")
        data = read_upload_bytes(upload, "image")
        detected_format = detect_input_format(data, upload.filename)
        image = open_image_bytes(data, detected_format)

        # ---- "mode" field padhna (blank ho sakta hai agar manual size use ho) ----
        raw_mode = str(request.form.get("mode") or "").strip().upper()

        if raw_mode == "":
            # Mode khaali hai — user manual width/height use karega.
            mode_key: Optional[str] = None
        elif raw_mode in SMART_MODE_PRESETS:
            mode_key = raw_mode
        else:
            # Typo/invalid mode name — silently kuch guess karne ke bajaye
            # clear error dena beginner-friendly aur safe dono hai.
            raise ValueError(
                f"Unknown photo mode '{raw_mode}'. Valid modes: "
                f"{', '.join(SMART_MODE_PRESETS.keys())}."
            )

        # ---- "quality_tier" field padhna, default HD ----
        tier_key = str(request.form.get("quality_tier") or "HD").strip().upper()
        if tier_key not in QUALITY_EXPORT_TIERS:
            raise ValueError(
                f"Unknown quality_tier '{tier_key}'. Valid tiers: "
                f"{', '.join(QUALITY_EXPORT_TIERS.keys())}."
            )

        # ---- "fit_strategy" field padhna (cover/contain), mode ka apna default use hota hai ----
        default_fit = SMART_MODE_PRESETS[mode_key]["default_fit"] if mode_key else "cover"
        fit_strategy = str(request.form.get("fit_strategy") or default_fit).strip().lower()
        if fit_strategy not in {"cover", "contain"}:
            raise ValueError("fit_strategy must be 'cover' or 'contain'.")

        # Manual size sirf tab consider hoti hai jab form me bheji gayi ho.
        manual_width = request.form.get("width")
        manual_height = request.form.get("height")

        target_width, target_height, encode_quality, warnings, used_manual = resolve_export_target(
            mode_key,
            tier_key,
            manual_width,
            manual_height,
            image.width,
            image.height,
        )

        # ---- Optional enhancement sliders bhi reuse kar rahe hain (purana function) ----
        # Isse user ek hi request me rotation/brightness/contrast/saturation/
        # sharpness + mode-convert dono ek saath kar sakta hai; koi naya
        # duplicate editing-code likhne ki zarurat nahi padi.
        # _FormWithoutSizeFields wrapper isliye use kiya hai taaki purana
        # function "width"/"height" fields ko na chhoo paaye (upar wali
        # class ki docstring me poori wajah likhi hai).
        pre_edited_image = apply_requested_edits(image, _FormWithoutSizeFields(request.form))

        original_pixel_count = image.width * image.height

        # Photo ko exact target canvas me fit karna (cover = crop-to-fill,
        # contain = poori photo + background padding).
        fitted_image = fit_image_to_target_canvas(
            pre_edited_image, target_width, target_height, fit_strategy
        )

        # Agar photo enlarge hui hai (upscale), to halka sharpening laga kar
        # perceived quality behtar karte hain.
        final_image = sharpen_if_upscaled(fitted_image, original_pixel_count)

        # Output format: agar user ne explicitly nahi diya, to original
        # upload ka hi format use hota hai (jaisa purane /resize route me hota hai).
        output_format = normalize_format(
            request.form.get("output_format") or detected_format
        ) or detected_format

        dpi = parse_int(request.form.get("dpi"), DEFAULT_DPI, 1, 2400, "DPI")

        # Purane encode_once() function ko hi reuse kar rahe hain — isse
        # JPEG/PNG/WEBP/GIF/TIF/SVG saare formats automatically already-tested
        # tareeke se encode hote hain, koi naya encoder duplicate nahi likha.
        output_bytes = encode_once(final_image, output_format, encode_quality, dpi)
        verify_encoded_output(output_bytes, output_format)  # Purana safety-check reuse.

        download_name = (
            f"{safe_base_name(upload.filename)}_"
            f"{(mode_key or 'CUSTOM').lower()}_{tier_key.lower()}."
            f"{EXTENSION_BY_FORMAT[output_format]}"
        )

        response = send_file(
            io.BytesIO(output_bytes),
            mimetype=MIME_BY_FORMAT[output_format],
            as_attachment=False,
            download_name=download_name,
            max_age=0,
        )

        # ---- Response headers: frontend inhi se flash-message/dimensions dikhayega ----
        response.headers["X-Output-Width"] = str(final_image.width)
        response.headers["X-Output-Height"] = str(final_image.height)
        response.headers["X-Output-Format"] = output_format
        response.headers["X-Output-Bytes"] = str(len(output_bytes))
        response.headers["X-Applied-Mode"] = mode_key or "CUSTOM"
        response.headers["X-Quality-Tier"] = tier_key
        response.headers["X-Fit-Strategy"] = fit_strategy
        response.headers["X-Used-Manual-Size"] = "true" if used_manual else "false"
        response.headers["X-Original-Width"] = str(image.width)
        response.headers["X-Original-Height"] = str(image.height)

        # Warnings list ko ek hi header-string me " || " se jodkar bhejte
        # hain, kyunki HTTP headers me list/array directly nahi bheja ja
        # sakta. Frontend isse `.split(" || ")` karke wapas list bana sakta hai.
        response.headers["X-Quality-Warning"] = " || ".join(warnings) if warnings else ""
        response.headers["Cache-Control"] = "no-store"

        return response


# ============================================================================
# 09 // DOCUMENT ↔ IMAGE CONVERTER  (NAYA FEATURE — SIRF ADD KIYA GAYA HAI)
# ============================================================================
# KYA HAI:
#     Yeh naya, self-contained section existing image resizer ko chhede bina
#     document conversion ki APIs add karta hai:
#       1) PDF ke har page ko ek alag JPG/JPEG/PNG/WEBP/TIF/GIF/BMP image.
#       2) Word/LibreOffice documents aur Excel/spreadsheet files ko pehle
#          backend me PDF render karke, phir har rendered page ko image.
#       3) Maximum 30 uploaded images ko ek PDF me jodna; har input image ka
#          exactly ek PDF page banta hai aur upload-order preserve hota hai.
#       4) `/inspect-document` se frontend conversion se PEHLE actual page
#          count, default image count aur maximum allowed count jaan sakta hai.
#
# PURANE PYTHON CODE PAR ASAR:
#     - Section 01 se Section 08 ka koi constant/function replace nahi hua.
#     - Naye helpers ka `document_` prefix hai, isliye purane helper names se
#       clash nahi hota.
#     - Naye routes ko `register_document_conversion_routes()` alag se add
#       karta hai. Agar future me sirf yeh Section 09 aur iska registration
#       hata diya jaye, purana resize/photo-mode engine pehle jaisa chalega.
#
# REQUIRED BACKEND DEPENDENCIES:
#     py -m pip install pymupdf
#     PDF aur images->PDF ke liye Pillow pehle se project dependency hai.
#     DOC/DOCX/ODT/RTF/XLS/XLSX/ODS/CSV/TSV ke liye computer par LibreOffice
#     install hona chahiye; server `libreoffice` ya `soffice` command dhoondhta
#     hai. Dependency absent ho to API clear error degi, server crash nahi hoga.
# ============================================================================


# ----------------------------------------------------------------------------
# 09.1 // DOCUMENT LIMITS AUR SUPPORTED FORMAT TABLES
# KYA: Naye converter ki saari limits/formats ek jagah rakhe gaye hain.
# KYUN: Future me 30 ko 20 karna ho ya naya extension add karna ho to neeche
#       functions me multiple hard-coded values dhoondhne ki zarurat na pade.
# ----------------------------------------------------------------------------

# 30 user ki requested 20-30 range ka upper end hai. Iska matlab ek PDF/Office
# file me maximum 30 rendered pages aur ek images->PDF request me maximum 30
# input photos allowed hain. Value badhaane se CPU, RAM aur ZIP/PDF size badhega.
DOCUMENT_MAX_PAGES = 30
DOCUMENT_MAX_OUTPUT_IMAGES = 30
DOCUMENT_MAX_INPUT_IMAGES = 30

# Office/Excel manual increase ka multiplier 2 hai: actual 3 pages ka maximum
# 3 * 2 = 6 images. Isse 3 karoge to 3-page file max 9 images maangegi, lekin
# global DOCUMENT_MAX_OUTPUT_IMAGES (30) phir bhi final hard ceiling rahegi.
DOCUMENT_OFFICE_MAX_MULTIPLIER = 2

# 144 DPI default readable text aur practical download size ka balance hai.
# User 72-200 DPI choose kar sakta hai. Upper limit badhaane se har page ke
# pixels/RAM quadratic tareeke se badhenge (double DPI ≈ four times pixels).
DOCUMENT_DEFAULT_DPI = 144
DOCUMENT_MIN_DPI = 72
DOCUMENT_MAX_DPI = 200

# LibreOffice untrusted complex files ke liye security-sensitive dependency hai.
# Office parsing secure-by-default OFF hai. Admin ko container/VM/seccomp jaisi
# external isolation confirm karke dono env flags enable karne honge:
#   ENABLE_OFFICE_CONVERSION=1
#   OFFICE_SANDBOX_CONFIRMED=1
# Python process alone LibreOffice CVE ko fully sandbox nahi kar sakta.
DOCUMENT_OFFICE_CONVERSION_ENABLED = parse_boolean(
    os.environ.get("ENABLE_OFFICE_CONVERSION"),
    default=False,
)
DOCUMENT_OFFICE_SANDBOX_CONFIRMED = parse_boolean(
    os.environ.get("OFFICE_SANDBOX_CONFIRMED"),
    default=False,
)

# Optional trusted wrapper example: firejail/bwrap/container-exec arguments.
# shlex.split use hoga; value sirf server administrator set kare, user form nahi.
DOCUMENT_OFFICE_SANDBOX_PREFIX = str(
    os.environ.get("LIBREOFFICE_SANDBOX_PREFIX", "")
).strip()

# Wall time, CPU time, address-space, output-file aur open-file budgets subprocess
# ko runaway hone se rokne ki second layer hain. Linux `prlimit` available ho to
# yeh child + inherited processes par apply hote hain.
DOCUMENT_OFFICE_TIMEOUT_SECONDS = 60
DOCUMENT_OFFICE_CPU_SECONDS = 45
DOCUMENT_OFFICE_MEMORY_MB = 1024
DOCUMENT_OFFICE_OPEN_FILES = 64
DOCUMENT_OFFICE_SEMAPHORE = threading.BoundedSemaphore(value=1)

# PDF/Office page output ke common raster formats. SVG intentionally nahi hai:
# rendered document page raster image hota hai; fake raster-wrapped SVG dena
# user ko real vector quality ka galat impression deta.
DOCUMENT_IMAGE_OUTPUTS: Dict[str, Dict[str, str]] = {
    "JPG": {"pillow": "JPEG", "extension": "jpg", "mime": "image/jpeg"},
    "JPEG": {"pillow": "JPEG", "extension": "jpeg", "mime": "image/jpeg"},
    "PNG": {"pillow": "PNG", "extension": "png", "mime": "image/png"},
    "WEBP": {"pillow": "WEBP", "extension": "webp", "mime": "image/webp"},
    "TIF": {"pillow": "TIFF", "extension": "tif", "mime": "image/tiff"},
    "GIF": {"pillow": "GIF", "extension": "gif", "mime": "image/gif"},
    "BMP": {"pillow": "BMP", "extension": "bmp", "mime": "image/bmp"},
}

# `.doc` old Word aur `.docx` modern Word dono accepted hain. ODT/RTF ko bhi
# same document pipeline handle karti hai, kyunki LibreOffice inhe render karta hai.
DOCUMENT_WORD_EXTENSIONS = {".doc", ".docx", ".odt", ".rtf"}

# Excel ke old/new formats, macro-enabled workbook, LibreOffice spreadsheet,
# aur plain CSV/TSV accepted hain. In sab par same actual-page limit apply hogi.
DOCUMENT_SHEET_EXTENSIONS = {
    ".xls",
    ".xlsx",
    ".xlsm",
    ".xlsb",
    ".ods",
    ".csv",
    ".tsv",
}


def document_normalize_image_format(value: Any) -> str:
    """Document-page output format ko safe canonical key me badalta hai."""

    # Blank field ka default PNG hai, kyunki documents me text/screenshot edges
    # PNG me lossless aur clear rehte hain. `.png` bhejne par leading dot bhi hataate hain.
    text = str(value or "PNG").strip().upper().lstrip(".")

    # TIFF ko UI ke short TIF key me normalize karte hain; codec same hai.
    if text == "TIFF":
        text = "TIF"

    # Unknown value ko silently PNG banana dangerous hai: user kuch aur expect
    # karega. Isliye exact supported-list ke saath 400 error diya jaata hai.
    if text not in DOCUMENT_IMAGE_OUTPUTS:
        raise ValueError(
            "output_format must be JPG, JPEG, PNG, WEBP, TIF, GIF or BMP."
        )

    # Caller ko canonical name milta hai; isi se encoder/extension choose hota hai.
    return text


def document_extension_and_kind(filename: str) -> Tuple[str, str]:
    """Filename extension se PDF, WORD ya SPREADSHEET category return karta hai."""

    # pathlib local import rakha hai: purane import section me ek line bhi add/edit
    # nahi karni padi. Suffix lower-case hai, isliye FILE.DOCX bhi accept hogi.
    from pathlib import Path

    extension = Path(str(filename or "")).suffix.lower()

    # PDF ka page-count fixed rule use hota hai: one page = one image.
    if extension == ".pdf":
        return extension, "PDF"

    # Word-like documents me manual image-count actual se maximum double tak hai.
    if extension in DOCUMENT_WORD_EXTENSIONS:
        return extension, "WORD"

    # Excel aur sheet formats Word jaisi rendered-page count policy use karte hain.
    if extension in DOCUMENT_SHEET_EXTENSIONS:
        return extension, "SPREADSHEET"

    # Unsupported extension ko LibreOffice ko blindly dene ke bajaye yahin clear
    # message dete hain. Naya extension add karne ke liye upar wale sets update karo.
    raise ValueError(
        "Unsupported document. Use PDF, DOC, DOCX, ODT, RTF, XLS, XLSX, "
        "XLSM, XLSB, ODS, CSV or TSV."
    )


def document_read_upload(upload: Any, field_name: str) -> bytes:
    """Document/photo upload ko empty aur per-file size checks ke saath read karta hai."""

    # Missing multipart field par AttributeError aane dene ke bajaye frontend ko
    # exact field-name bataya jaata hai (`document` ya `images`).
    if upload is None or not getattr(upload, "filename", ""):
        raise ValueError(f"Missing uploaded file in '{field_name}' field.")

    # Limit+1 read oversized stream ko poora memory me load kiye bina reject karta hai.
    data = upload.read(MAX_UPLOAD_BYTES + 1)

    # Zero-byte file valid PDF/document/image nahi hoti; conversion se pehle stop.
    if not data:
        raise ValueError(f"Uploaded {field_name} file is empty.")

    # Shared 100 MB per-file limit purane image aur naye document paths dono par
    # identical hai. Global multipart body limit iske upar second safety layer hai.
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f"{field_name} exceeds the {MAX_UPLOAD_MB} MB per-file limit.")

    # Validated bytes caller ko milti hain; function file ko disk par permanent save nahi karta.
    return data


def document_require_pymupdf() -> Any:
    """PyMuPDF lazy-load karta hai aur missing dependency par install hint deta hai."""

    try:
        # Package install-name `pymupdf` hai, lekin modern import bhi `pymupdf`
        # hai. Lazy import se purane photo routes dependency absent hone par bhi chalenge.
        import pymupdf
    except ImportError as exc:
        # Is ValueError ko existing Flask error-handler clean JSON 400 me convert karega.
        raise ValueError(
            "PDF conversion dependency is missing. Run: py -m pip install pymupdf"
        ) from exc

    # Module return karne se baaki helpers global import/change ke bina use karte hain.
    return pymupdf


def _document_convert_office_to_pdf_isolated(data: bytes, filename: str) -> bytes:
    """Validated Office bytes ko temp workspace me limited LibreOffice child se PDF banata hai."""

    # Imports function ke andar hain taaki Section 01 ka old import block untouched rahe.
    import shlex
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    extension, kind = document_extension_and_kind(filename)

    # Yeh helper PDF ke liye nahi hai; galat internal call ho to early error code bug dikhata hai.
    if kind == "PDF":
        raise ValueError("Internal conversion error: PDF was sent to the Office converter.")

    # Windows/Linux installations me executable ka naam alag ho sakta hai.
    office_command = shutil.which("libreoffice") or shutil.which("soffice")

    # Missing LibreOffice par old image features band nahi hoti; sirf Office route error deta hai.
    if office_command is None:
        raise ValueError(
            "DOC/Excel conversion needs LibreOffice installed on the backend. "
            "Install LibreOffice, restart Python, and try again."
        )

    # TemporaryDirectory request ke baad automatically delete hoti hai. User document
    # ya converted PDF server disk par permanently store nahi kiya jaata.
    with tempfile.TemporaryDirectory(prefix="document_converter_") as temp_root:
        input_directory = os.path.join(temp_root, "input")
        output_directory = os.path.join(temp_root, "output")
        profile_directory = os.path.join(temp_root, "libreoffice_profile")

        # Teen folders separate rakhne se source, result aur LibreOffice settings mix nahi hote.
        os.makedirs(input_directory, exist_ok=True)
        os.makedirs(output_directory, exist_ok=True)
        os.makedirs(profile_directory, exist_ok=True)

        # Existing safe_base_name path characters hataata hai; original extension preserve
        # hoti hai taaki LibreOffice sahi import filter choose kar sake.
        safe_filename = f"{safe_base_name(filename)}{extension}"
        input_path = os.path.join(input_directory, safe_filename)

        # Yeh write sirf temporary conversion copy hai; with-block end par delete ho jaayegi.
        with open(input_path, "wb") as input_file:
            input_file.write(data)

        # Alag user-profile concurrent requests ko ek global LibreOffice lock/profile
        # share karne se bachata hai. `as_uri()` Windows/Linux dono par valid file URI deta hai.
        profile_uri = Path(profile_directory).resolve().as_uri()
        office_arguments = [
            office_command,
            "--headless",
            "--safe-mode",
            "--nologo",
            "--nodefault",
            "--nolockcheck",
            "--norestore",
            f"-env:UserInstallation={profile_uri}",
            "--convert-to",
            "pdf",
            "--outdir",
            output_directory,
            input_path,
        ]

        # Trusted admin wrapper (for example a firejail/bwrap command) first
        # priority hai. User request is string ko control nahi karti.
        if DOCUMENT_OFFICE_SANDBOX_PREFIX:
            command = shlex.split(DOCUMENT_OFFICE_SANDBOX_PREFIX) + office_arguments
        else:
            command = office_arguments

        # Linux util-linux `prlimit` milne par Office child ko CPU/RAM/file
        # limits ke andar launch karte hain. External container/seccomp phir bhi
        # required hai; yeh defense-in-depth hai, full sandbox replacement nahi.
        prlimit_command = shutil.which("prlimit")
        if prlimit_command is not None:
            command = [
                prlimit_command,
                f"--as={DOCUMENT_OFFICE_MEMORY_MB * MB_IN_BYTES}",
                f"--cpu={DOCUMENT_OFFICE_CPU_SECONDS}",
                f"--fsize={MAX_OUTPUT_BYTES}",
                f"--nofile={DOCUMENT_OFFICE_OPEN_FILES}:{DOCUMENT_OFFICE_OPEN_FILES}",
                "--",
            ] + command

        # HOME/TMP/LibreOffice profile sab same per-request temporary root me
        # point karte hain. Process normal user config/cache ko read/write nahi karega.
        child_environment = os.environ.copy()
        child_environment.update(
            {
                "HOME": temp_root,
                "TMPDIR": temp_root,
                "TEMP": temp_root,
                "TMP": temp_root,
                "SAL_USE_VCLPLUGIN": "svp",
            }
        )

        try:
            # Popen + process group isliye use hota hai taaki timeout par direct
            # soffice ke saath uske child processes bhi terminate ho sakein.
            creation_flags = 0
            if os.name == "nt" and hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
                creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP

            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=temp_root,
                env=child_environment,
                start_new_session=(os.name != "nt"),
                creationflags=creation_flags,
            )
            stdout_bytes, stderr_bytes = process.communicate(
                timeout=DOCUMENT_OFFICE_TIMEOUT_SECONDS
            )
        except subprocess.TimeoutExpired as exc:
            # POSIX session ke poore process group ko kill karna orphan soffice
            # process ko background me CPU consume karte rehne se rokta hai.
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    process.kill()
            else:
                process.kill()

            stdout_bytes, stderr_bytes = process.communicate()
            LOGGER.warning(
                "LibreOffice timed out after %s seconds. stderr=%s",
                DOCUMENT_OFFICE_TIMEOUT_SECONDS,
                sanitize_subprocess_log(stderr_bytes),
            )
            raise ValueError(
                f"Office conversion took more than {DOCUMENT_OFFICE_TIMEOUT_SECONDS} seconds. "
                "Try a smaller or simpler document."
            ) from exc
        except OSError as exc:
            LOGGER.exception("LibreOffice sandbox/process could not start")
            raise ValueError(
                "LibreOffice sandbox process could not start. Check the configured "
                "executable and sandbox wrapper."
            ) from exc

        # Stderr pehle silently discard hota tha. Ab terminal/server logs me
        # sanitized + truncated diagnostic milta hai, HTTP user ko raw paths nahi.
        stderr_text = sanitize_subprocess_log(stderr_bytes)
        stdout_text = sanitize_subprocess_log(stdout_bytes)
        if stderr_text:
            log_method = LOGGER.warning if process.returncode != 0 else LOGGER.info
            log_method("LibreOffice stderr (code %s): %s", process.returncode, stderr_text)

        # LibreOffice normally one PDF banata hai. Directory scan exact basename/output
        # capitalization differences ko safely handle karta hai.
        pdf_candidates = sorted(Path(output_directory).glob("*.pdf"))

        # Non-zero code YA missing result dono failure hain; raw terminal output user ko
        # nahi bhejte kyunki usme server paths ho sakte hain.
        if process.returncode != 0 or not pdf_candidates:
            LOGGER.error(
                "LibreOffice conversion failed: code=%s stdout=%s stderr=%s",
                process.returncode,
                stdout_text,
                stderr_text,
            )
            raise ValueError(
                "LibreOffice could not render this file. Check that it is not corrupt, "
                "password-protected, or using an unsupported document feature."
            )

        # Disk size pehle check hoti hai, taaki oversized generated PDF ko
        # `.read_bytes()` se process RAM me load na kiya jaaye.
        if pdf_candidates[0].stat().st_size > MAX_OUTPUT_BYTES:
            raise ValueError(
                f"LibreOffice PDF exceeds the {MAX_OUTPUT_MB} MB output limit."
            )

        # Sirf first/expected PDF read hoti hai; ek input request ko ek output document maana hai.
        pdf_data = pdf_candidates[0].read_bytes()
        validate_output_size(pdf_data, "LibreOffice PDF")

        # Output signature validate karna extension rename ko successful conversion maanne se rokta hai.
        if not pdf_data.lstrip().startswith(b"%PDF-"):
            raise ValueError("LibreOffice returned an unreadable PDF result.")

        # Bytes memory me return hoti hain; TemporaryDirectory exit hote hi disk files delete hongi.
        return pdf_data


def document_convert_office_to_pdf(data: bytes, filename: str) -> bytes:
    """Security gate + one-at-a-time slot ke saath Office conversion run karta hai."""

    # Secure-by-default gate: untrusted Office parsing tabhi on hoti hai jab
    # administrator explicitly feature enable aur external sandbox confirm kare.
    if not DOCUMENT_OFFICE_CONVERSION_ENABLED or not DOCUMENT_OFFICE_SANDBOX_CONFIRMED:
        raise ValueError(
            "Office conversion is disabled for safety. Run LibreOffice inside a Docker/VM/"
            "seccomp-style sandbox, then set ENABLE_OFFICE_CONVERSION=1 and "
            "OFFICE_SANDBOX_CONFIRMED=1. PDF conversion remains available."
        )

    # Only one LibreOffice child at a time. Non-blocking acquire queued requests
    # ko 60 seconds hold karne ke bajaye immediate retry message deta hai.
    if not DOCUMENT_OFFICE_SEMAPHORE.acquire(blocking=False):
        raise ValueError("Another Office document is being converted. Try again shortly.")

    try:
        return _document_convert_office_to_pdf_isolated(data, filename)
    finally:
        DOCUMENT_OFFICE_SEMAPHORE.release()


def document_prepare_pdf_bytes(data: bytes, filename: str) -> Tuple[bytes, str]:
    """PDF ko validate, ya Office/Excel ko PDF render karke `(bytes, kind)` deta hai."""

    _extension, kind = document_extension_and_kind(filename)

    # Real PDF bytes ka header check hota hai; sirf `.pdf` naam enough nahi hai.
    if kind == "PDF":
        if not data.lstrip().startswith(b"%PDF-"):
            raise ValueError("The uploaded .pdf file does not contain a valid PDF header.")
        return data, kind

    # Word/Spreadsheet dono same LibreOffice render pipeline use karte hain.
    return document_convert_office_to_pdf(data, filename), kind


def document_open_pdf_and_validate(pdf_data: bytes) -> Tuple[Any, int]:
    """PDF open karke password, empty-file aur 30-page limit validate karta hai."""

    pymupdf = document_require_pymupdf()

    try:
        pdf_document = pymupdf.open(stream=pdf_data, filetype="pdf")
    except Exception as exc:
        # PyMuPDF ke internal exception types version ke saath badal sakte hain;
        # API boundary par stable user-facing ValueError rakhna zyada reliable hai.
        raise ValueError("Uploaded/rendered PDF is corrupt or unreadable.") from exc

    # Password-protected content bina password UI ke render nahi ho sakta.
    if pdf_document.needs_pass:
        pdf_document.close()
        raise ValueError("Password-protected PDFs/documents are not supported.")

    page_count = int(pdf_document.page_count)

    # Empty PDF se empty ZIP milna confusing hota, isliye explicit error.
    if page_count < 1:
        pdf_document.close()
        raise ValueError("The document has no renderable pages.")

    # User-requested maximum. Office file bhi PDF banne ke BAAD isi actual page count se check hoti hai.
    if page_count > DOCUMENT_MAX_PAGES:
        pdf_document.close()
        raise ValueError(
            f"Document has {page_count} pages; maximum allowed is {DOCUMENT_MAX_PAGES}."
        )

    # Caller render/count ke baad document.close() karega; count separately convenience ke liye return hai.
    return pdf_document, page_count


def document_count_limits(kind: str, actual_pages: int) -> Tuple[int, int]:
    """Default aur maximum output image count ko requested policy se calculate karta hai."""

    # Automatic/default behavior hamesha actual rendered page count hai.
    default_count = actual_pages

    # PDF me strict one-page-one-image rule: manual count change allowed nahi hai.
    if kind == "PDF":
        return default_count, actual_pages

    # Word/Excel maximum actual pages ka double, lekin global 30-image limit kabhi cross nahi hoti.
    maximum_count = min(
        DOCUMENT_MAX_OUTPUT_IMAGES,
        actual_pages * DOCUMENT_OFFICE_MAX_MULTIPLIER,
    )

    # Example: actual 3 pages => default 3, maximum 6. Actual 20 => maximum 30,
    # kyunki global output ceiling double-rule se bhi zyada strict ho jaati hai.
    return default_count, maximum_count


def document_resolve_requested_image_count(
    raw_value: Any,
    kind: str,
    actual_pages: int,
) -> Tuple[int, int]:
    """Optional manual image count validate karke `(requested, maximum)` return karta hai."""

    default_count, maximum_count = document_count_limits(kind, actual_pages)

    # Blank field ka matlab backend automatically actual pages jitni images banaye.
    if raw_value is None or str(raw_value).strip() == "":
        return default_count, maximum_count

    # Global parser decimal-looking text ko int me convert karta hai aur 1..30 boundary check karta hai.
    requested_count = parse_int(
        raw_value,
        default_count,
        1,
        DOCUMENT_MAX_OUTPUT_IMAGES,
        "image_count",
    )

    # PDF me user count ko page count se alag nahi kar sakta; data loss/duplicate pages avoid hote hain.
    if kind == "PDF" and requested_count != actual_pages:
        raise ValueError(
            f"PDF has {actual_pages} pages, so image_count must be exactly {actual_pages}."
        )

    # Office/Excel me reduction currently allowed nahi: content ka koi original page drop/merge nahi karna.
    if kind != "PDF" and requested_count < actual_pages:
        raise ValueError(
            f"This {kind.lower()} file renders to {actual_pages} pages. image_count cannot be "
            f"below {actual_pages}, because every original page must remain visible."
        )

    # Double/global limit cross karne par user ke requested count aur exact max dono error me milte hain.
    if requested_count > maximum_count:
        raise ValueError(
            f"This {kind.lower()} file renders to {actual_pages} pages, so maximum image_count "
            f"is {maximum_count} (up to {DOCUMENT_OFFICE_MAX_MULTIPLIER}x, "
            f"never above {DOCUMENT_MAX_OUTPUT_IMAGES})."
        )

    return requested_count, maximum_count


def document_encode_page_image(
    image: Image.Image,
    output_format: str,
    quality: int,
    dpi: int,
) -> bytes:
    """Ek rendered/split page ko selected raster format ke bytes me encode karta hai."""

    output_info = DOCUMENT_IMAGE_OUTPUTS[output_format]
    pillow_format = output_info["pillow"]
    buffer = io.BytesIO()

    # JPEG/BMP alpha channel store nahi karte; white background par flatten karna
    # transparency ko black blocks banne se bachata hai.
    if pillow_format in {"JPEG", "BMP"}:
        prepared = flatten_transparency(image)
    elif pillow_format == "GIF":
        # GIF palette maximum 256 colors hai; adaptive palette readable page preview deta hai.
        prepared = image.convert("P", palette=Image.Palette.ADAPTIVE, colors=256)
    else:
        # PNG/WEBP/TIFF RGB/RGBA directly sambhaal sakte hain.
        prepared = image

    # Har encoder ke relevant options alag hain; irrelevant `quality` PNG/BMP ko nahi bhejte.
    if pillow_format == "JPEG":
        prepared.save(
            buffer,
            format="JPEG",
            quality=quality,
            optimize=True,
            progressive=True,
            dpi=(dpi, dpi),
        )
    elif pillow_format == "PNG":
        prepared.save(buffer, format="PNG", optimize=True, dpi=(dpi, dpi))
    elif pillow_format == "WEBP":
        prepared.save(buffer, format="WEBP", quality=quality, method=6)
    elif pillow_format == "TIFF":
        prepared.save(buffer, format="TIFF", compression="tiff_lzw", dpi=(dpi, dpi))
    elif pillow_format == "GIF":
        prepared.save(buffer, format="GIF", optimize=True)
    elif pillow_format == "BMP":
        prepared.save(buffer, format="BMP", dpi=(dpi, dpi))
    else:
        # Table/function future me out-of-sync ho to silent wrong file ke bajaye clear developer error.
        raise ValueError(f"No document page encoder is configured for {output_format}.")

    # Bytes ZIP me jayengi; individual page bhi global output limit verify hota
    # hai. Combined archive budget document_render_zip separately track karta hai.
    return validate_output_size(buffer.getvalue(), f"Rendered {output_format} page")


def document_split_page_vertically(image: Image.Image) -> Tuple[Image.Image, Image.Image]:
    """Office page ko top/bottom do content-preserving images me split karta hai."""

    # `// 2` exact middle pixel choose karta hai. Rendered page kam se kam 2px high honi chahiye.
    middle_y = image.height // 2

    if middle_y < 1 or middle_y >= image.height:
        raise ValueError("Rendered page is too small to split into extra images.")

    # crop boxes `(left, top, right, bottom)` hain. Koi pixel overlap/drop nahi hota.
    top_half = image.crop((0, 0, image.width, middle_y))
    bottom_half = image.crop((0, middle_y, image.width, image.height))

    # Caller dono halves ko sequence me encode karega, so reading order top then bottom preserve hota hai.
    return top_half, bottom_half


def document_render_zip(
    pdf_document: Any,
    source_filename: str,
    kind: str,
    actual_pages: int,
    requested_count: int,
    maximum_count: int,
    output_format: str,
    quality: int,
    dpi: int,
) -> bytes:
    """Validated PDF pages render/split karke images + manifest wala ZIP banata hai."""

    import json
    import zipfile

    pymupdf = document_require_pymupdf()
    archive_buffer = io.BytesIO()

    # Office/Excel me requested count actual se bada ho sakta hai. Difference jitna
    # hai utne original pages ko exactly 2 vertical parts me split karna enough hai,
    # kyunki allowed maximum actual ka double hai.
    extra_images_needed = requested_count - actual_pages
    output_number = 0
    total_output_image_bytes = 0
    base_name = safe_base_name(source_filename)
    extension = DOCUMENT_IMAGE_OUTPUTS[output_format]["extension"]

    # ZIP_DEFLATED manifest aur formats like BMP ko compact karta hai; already-compressed
    # JPEG/PNG par harmless hai.
    with zipfile.ZipFile(
        archive_buffer,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as archive:
        # Page loop sirf maximum 30 baar chalega, kyunki validation pehle ho chuki hai.
        for page_index in range(actual_pages):
            page = pdf_document.load_page(page_index)

            # PDF points 72 DPI base par hote hain; scale=dpi/72 requested raster detail deta hai.
            scale = dpi / 72.0

            # CRITICAL: get_pixmap allocation se pehle page rectangle × DPI se
            # predicted raster size validate hota hai. Malicious PDF giant page
            # box se PyMuPDF ko enormous pixmap allocate nahi karwa sakti.
            estimated_width = max(1, int(math.ceil(float(page.rect.width) * scale)))
            estimated_height = max(1, int(math.ceil(float(page.rect.height) * scale)))
            validate_pixel_budget(
                estimated_width,
                estimated_height,
                f"Rendered document page {page_index + 1}",
            )

            pixmap = page.get_pixmap(
                matrix=pymupdf.Matrix(scale, scale),
                colorspace=pymupdf.csRGB,
                alpha=False,
            )

            # Library rounding/rotation differences ke baad actual dimensions
            # bhi second time check hoti hain before Pillow Image.frombytes.
            validate_pixel_budget(
                pixmap.width,
                pixmap.height,
                f"Rendered document page {page_index + 1}",
            )

            # Pixmap bytes ko Pillow image me badalte hain; RGB forced hai so encoder behavior predictable hai.
            rendered_page = Image.frombytes(
                "RGB",
                (pixmap.width, pixmap.height),
                pixmap.samples,
            )
            # Pillow ne samples copy kar liye; PyMuPDF pixmap buffer ab release
            # ho sakta hai, so same page ki do full RGB buffers unnecessarily nahi rehti.
            del pixmap

            # First `extra_images_needed` pages split hoti hain. Example 3 actual,
            # 5 requested => page 1 and 2 split, page 3 whole => exactly 5 images.
            if page_index < extra_images_needed:
                output_parts = document_split_page_vertically(rendered_page)
            else:
                output_parts = (rendered_page,)

            try:
                # Ek page ke one/two parts ko top-to-bottom order me ZIP me write karte hain.
                for part_image in output_parts:
                    output_number += 1
                    image_bytes = document_encode_page_image(
                        part_image,
                        output_format,
                        quality,
                        dpi,
                    )
                    total_output_image_bytes += len(image_bytes)
                    if total_output_image_bytes > MAX_OUTPUT_BYTES:
                        raise ValueError(
                            f"Document images exceed the {MAX_OUTPUT_MB} MB combined output limit. "
                            "Use lower DPI/quality or a smaller document."
                        )
                    image_filename = (
                        f"{base_name}_image_{output_number:03d}.{extension}"
                    )
                    archive.writestr(image_filename, image_bytes)
            finally:
                # Encode/archive error aaye tab bhi split halves aur full page
                # immediately close hoti hain; garbage collector ka wait nahi.
                for part_image in output_parts:
                    if part_image is not rendered_page:
                        part_image.close()
                rendered_page.close()

        # Defensive assertion: algorithm bug se count mismatch ho to incomplete ZIP download nahi hogi.
        if output_number != requested_count:
            raise ValueError(
                f"Internal image-count verification failed: expected {requested_count}, "
                f"created {output_number}."
            )

        # Manifest beginner/frontend ko ZIP ke andar conversion decisions samjhata hai.
        manifest = {
            "source_file": str(source_filename or "document"),
            "source_kind": kind,
            "actual_rendered_pages": actual_pages,
            "output_images_created": output_number,
            "maximum_images_allowed_for_this_file": maximum_count,
            "output_format": output_format,
            "render_dpi": dpi,
            "quality": quality,
            "rule": (
                "PDF uses exactly one image per page."
                if kind == "PDF"
                else "Office/Excel defaults to one image per rendered page; extra images split pages vertically."
            ),
        }
        archive.writestr(
            "conversion_manifest.json",
            json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8"),
        )

    # Entire ZIP bytes Flask send_file ko milti hain; compressed archive par
    # final 100 MB limit independently verify hoti hai.
    return validate_output_size(archive_buffer.getvalue(), "Document image ZIP")


def document_open_any_image(data: bytes, filename: str) -> Image.Image:
    """Pillow-supported raster ya existing SVG helper se ek PDF-ready RGB image kholta hai."""

    # Existing SVG detection + CairoSVG-aware opener reuse karne se SVG bhi images->PDF me supported hai.
    if looks_like_svg(data):
        opened = open_image_bytes(data, "SVG")
    else:
        try:
            # Pillow plugins BMP/PNG/JPEG/WEBP/TIFF/GIF aur installed optional formats detect karte hain.
            with Image.open(io.BytesIO(data)) as source_image:
                source_image.seek(0)  # Animated/multi-frame upload ka first frame ek input photo maana hai.
                validate_pixel_budget(
                    source_image.width,
                    source_image.height,
                    f"Uploaded image '{filename or 'image'}'",
                    MAX_DECODE_PIXELS,
                )
                opened = ImageOps.exif_transpose(source_image).copy()
        except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError) as exc:
            raise ValueError(
                f"'{filename or 'image'}' is not a readable Pillow-supported image."
            ) from exc

    # PDF encoder ke liye white-background RGB safest mode hai; alpha black nahi banega.
    return flatten_transparency(opened)


def document_build_pdf_from_images(
    uploads: Any,
    dpi: int,
) -> Tuple[bytes, int]:
    """Ordered uploaded images ko exactly same page-count wale PDF bytes me jodta hai."""

    pdf_pages = []
    total_input_bytes = 0
    total_input_pixels = 0
    output_buffer = io.BytesIO()

    try:
        # Upload list maximum 30 validate hone ke baad hi yahan aati hai. Actual
        # bytes sum declared Content-Length ke alawa authoritative 100 MB check hai.
        for image_index, upload in enumerate(uploads, start=1):
            data = document_read_upload(upload, f"images[{image_index}]")
            total_input_bytes += len(data)

            if total_input_bytes > MAX_UPLOAD_BYTES:
                raise ValueError(
                    f"All images together exceed the {MAX_UPLOAD_MB} MB upload limit."
                )

            page_image = document_open_any_image(data, upload.filename)

            # Individual dimensions + combined decoded pixel budget dono check
            # hote hain. 30 separate 50M images ko list me rakhna OOM karega.
            page_pixels = validate_pixel_budget(
                page_image.width,
                page_image.height,
                f"Image {image_index}",
                MAX_DECODE_PIXELS,
            )
            total_input_pixels += page_pixels

            if total_input_pixels > MAX_PDF_TOTAL_INPUT_PIXELS:
                page_image.close()
                raise ValueError(
                    "Images are individually valid but too large together for one PDF. "
                    f"Combined maximum is {MAX_PDF_TOTAL_INPUT_PIXELS:,} decoded pixels."
                )

            # Append order exactly browser upload order hai; isi order me PDF pages banenge.
            pdf_pages.append(page_image)

        # Function normally non-empty list paata hai; direct future calls ke liye guard rakha hai.
        if not pdf_pages:
            raise ValueError("Upload at least one image to create a PDF.")

        # Pillow ka multi-page PDF encoder first image ko base page aur baaki ko
        # append_images se same order me add karta hai. Thus 10 inputs = 10 pages.
        pdf_pages[0].save(
            output_buffer,
            format="PDF",
            save_all=True,
            append_images=pdf_pages[1:],
            resolution=float(dpi),
            title="Images converted to PDF",
        )
        pdf_bytes = validate_output_size(output_buffer.getvalue(), "Generated PDF")

        # Last signature check ensures response actually PDF hai, renamed bytes nahi.
        if not pdf_bytes.startswith(b"%PDF-"):
            raise ValueError("PDF output verification failed.")

        # Page count upload list ke equal return hota hai; route/header isse verify kar sakta hai.
        return pdf_bytes, len(uploads)
    except OSError as exc:
        raise ValueError("Could not encode the uploaded images as PDF.") from exc
    finally:
        # Decode/validation/save kisi bhi point par fail ho, already-opened images
        # close hoti hain. Purane code me input loop error par early images leak ho sakti thi.
        for page_image in pdf_pages:
            page_image.close()


# ----------------------------------------------------------------------------
# 09.2 // FLASK ROUTES FOR DOCUMENT CONVERSION
# KYA: Existing `app` object par five additive endpoints register hote hain.
# KYUN: Old `create_app()` body untouched rahe aur new feature independently remove/test ho sake.
# ----------------------------------------------------------------------------

def register_document_conversion_routes(flask_app: Any) -> None:
    """Document inspect/convert aur images->PDF routes existing Flask app me add karta hai."""

    @flask_app.route("/document-converter-info", methods=["GET"])
    def document_converter_info() -> Any:
        """Frontend ko formats, fields, dependency aur limit policy batata hai."""

        # Yeh endpoint file process nahi karta; UI dropdown/help text is JSON se build kar sakta hai.
        return jsonify(
            document_inputs={
                "pdf": ["PDF"],
                "word": sorted(extension.lstrip(".").upper() for extension in DOCUMENT_WORD_EXTENSIONS),
                "spreadsheet": sorted(
                    extension.lstrip(".").upper() for extension in DOCUMENT_SHEET_EXTENSIONS
                ),
            },
            image_outputs=list(DOCUMENT_IMAGE_OUTPUTS.keys()),
            image_to_pdf_inputs="Any readable Pillow raster format; SVG when cairosvg is installed.",
            office_conversion={
                "enabled": (
                    DOCUMENT_OFFICE_CONVERSION_ENABLED
                    and DOCUMENT_OFFICE_SANDBOX_CONFIRMED
                ),
                "security_policy": (
                    "Office parsing requires explicit enablement and external sandbox confirmation."
                ),
                "wall_timeout_seconds": DOCUMENT_OFFICE_TIMEOUT_SECONDS,
                "cpu_limit_seconds": DOCUMENT_OFFICE_CPU_SECONDS,
                "memory_limit_mb_when_prlimit_is_available": DOCUMENT_OFFICE_MEMORY_MB,
            },
            limits={
                "maximum_request_mb": MAX_REQUEST_MB,
                "maximum_individual_file_mb": MAX_UPLOAD_MB,
                "maximum_generated_output_mb": MAX_OUTPUT_MB,
                "maximum_canvas_pixels": MAX_OUTPUT_PIXELS,
                "maximum_document_pages": DOCUMENT_MAX_PAGES,
                "maximum_output_images": DOCUMENT_MAX_OUTPUT_IMAGES,
                "maximum_input_images_for_pdf": DOCUMENT_MAX_INPUT_IMAGES,
                "maximum_combined_images_to_pdf_pixels": MAX_PDF_TOTAL_INPUT_PIXELS,
                "office_manual_maximum_rule": (
                    f"min(actual_pages * {DOCUMENT_OFFICE_MAX_MULTIPLIER}, "
                    f"{DOCUMENT_MAX_OUTPUT_IMAGES})"
                ),
                "pdf_output_rule": "exactly one image per PDF page",
            },
            form_fields={
                "inspect_document": ["document"],
                "document_to_images": [
                    "document",
                    "output_format (optional, default PNG)",
                    "image_count (optional; Office/Excel only)",
                    "dpi (optional, 72-200, default 144)",
                    "quality (optional, 1-100, default 92)",
                ],
                "images_to_pdf": ["images (repeat field in desired page order)", "dpi (optional)"],
            },
        )

    @flask_app.route("/inspect-document", methods=["POST", "OPTIONS"])
    def inspect_document_for_conversion() -> Any:
        """Actual rendered pages aur user-selectable image-count limit preview karta hai."""

        # Browser preflight ko upload/conversion run kiye bina success response.
        if request.method == "OPTIONS":
            return make_response("", 204)

        upload = request.files.get("document")
        data = document_read_upload(upload, "document")
        pdf_data, kind = document_prepare_pdf_bytes(data, upload.filename)
        pdf_document, actual_pages = document_open_pdf_and_validate(pdf_data)

        try:
            default_count, maximum_count = document_count_limits(kind, actual_pages)
        finally:
            # Count milne ke baad PyMuPDF handle close hona zaroori hai, especially Windows par.
            pdf_document.close()

        # Frontend isi response se count input ka min/default/max set kar sakta hai.
        return jsonify(
            source_file=upload.filename,
            source_kind=kind,
            actual_rendered_pages=actual_pages,
            default_image_count=default_count,
            minimum_image_count=actual_pages,
            maximum_image_count=maximum_count,
            manual_increase_allowed=(kind != "PDF" and maximum_count > actual_pages),
            message=(
                f"This PDF must create exactly {actual_pages} images."
                if kind == "PDF"
                else (
                    f"Backend rendered {actual_pages} pages. Default is {default_count} images; "
                    f"you may choose up to {maximum_count}."
                )
            ),
        )

    @flask_app.route("/pdf-to-images", methods=["POST", "OPTIONS"])
    @flask_app.route("/office-to-images", methods=["POST", "OPTIONS"])
    @flask_app.route("/document-to-images", methods=["POST", "OPTIONS"])
    def convert_document_to_images() -> Any:
        """PDF/Word/Excel ko selected-format images ke downloadable ZIP me badalta hai."""

        # Teeno route names same generic engine use karte hain; aliases frontend naming freedom dete hain.
        if request.method == "OPTIONS":
            return make_response("", 204)

        upload = request.files.get("document")
        data = document_read_upload(upload, "document")
        pdf_data, kind = document_prepare_pdf_bytes(data, upload.filename)
        pdf_document, actual_pages = document_open_pdf_and_validate(pdf_data)

        try:
            # Format/DPI/quality conversion start se pehle validate hote hain.
            output_format = document_normalize_image_format(
                request.form.get("output_format")
            )
            dpi = parse_int(
                request.form.get("dpi"),
                DOCUMENT_DEFAULT_DPI,
                DOCUMENT_MIN_DPI,
                DOCUMENT_MAX_DPI,
                "dpi",
            )
            quality = parse_int(
                request.form.get("quality"),
                DEFAULT_QUALITY,
                1,
                100,
                "quality",
            )
            requested_count, maximum_count = document_resolve_requested_image_count(
                request.form.get("image_count"),
                kind,
                actual_pages,
            )

            # ZIP creation page count/order verification ke saath hoti hai.
            zip_bytes = document_render_zip(
                pdf_document,
                upload.filename,
                kind,
                actual_pages,
                requested_count,
                maximum_count,
                output_format,
                quality,
                dpi,
            )
        finally:
            # Encoding/split error aaye tab bhi PDF object close hota hai.
            pdf_document.close()

        download_name = f"{safe_base_name(upload.filename)}_{output_format.lower()}_images.zip"
        response = send_file(
            io.BytesIO(zip_bytes),
            mimetype="application/zip",
            as_attachment=True,
            download_name=download_name,
            max_age=0,
        )

        # Headers optional quick metadata hain; full explanation ZIP manifest me bhi hai.
        response.headers["X-Document-Pages"] = str(actual_pages)
        response.headers["X-Images-Created"] = str(requested_count)
        response.headers["X-Maximum-Images"] = str(maximum_count)
        response.headers["X-Output-Format"] = output_format
        response.headers["Cache-Control"] = "no-store"
        return response

    @flask_app.route("/images-to-pdf", methods=["POST", "OPTIONS"])
    def convert_images_to_pdf() -> Any:
        """1-30 uploaded images ko upload-order me same-page-count PDF banata hai."""

        # CORS preflight par images expect nahi karni.
        if request.method == "OPTIONS":
            return make_response("", 204)

        # Preferred field plural `images` hai. Singular `image` alias simple clients ke liye hai.
        uploads = request.files.getlist("images")
        if not uploads:
            uploads = request.files.getlist("image")

        # Empty request clear error deti hai; PDF encoder tak nahi jaati.
        if not uploads:
            raise ValueError("Upload at least one image in the 'images' field.")

        # 31st image se pehle request reject hoti hai; user-requested 20-30 range me max 30 fixed hai.
        if len(uploads) > DOCUMENT_MAX_INPUT_IMAGES:
            raise ValueError(
                f"You uploaded {len(uploads)} images; maximum allowed is "
                f"{DOCUMENT_MAX_INPUT_IMAGES}."
            )

        # Declared sizes available hon to decoding se pehle combined 100 MB
        # check. Actual bytes document_build_pdf_from_images dobara sum karta hai.
        validate_combined_upload_size(uploads, "Uploaded image")

        # PDF resolution metadata user choose kar sakta hai; same safe 72-200 range rakhi hai.
        dpi = parse_int(
            request.form.get("dpi"),
            DOCUMENT_DEFAULT_DPI,
            DOCUMENT_MIN_DPI,
            DOCUMENT_MAX_DPI,
            "dpi",
        )
        pdf_bytes, page_count = document_build_pdf_from_images(uploads, dpi)

        # Optional output_name form field path/special chars se sanitize hota hai.
        output_name = safe_base_name(request.form.get("output_name") or "converted_images")
        response = send_file(
            io.BytesIO(pdf_bytes),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"{output_name}.pdf",
            max_age=0,
        )

        # Strong invariant frontend/tests verify kar sakte hain: input count == PDF page count.
        response.headers["X-Input-Images"] = str(len(uploads))
        response.headers["X-PDF-Pages"] = str(page_count)
        response.headers["Cache-Control"] = "no-store"
        return response


def create_app() -> Any:
    """Flask app factory test/deployment dono ke liye application banata hai."""

    if Flask is None:
        raise RuntimeError("Flask is not installed. Run: py -m pip install flask")

    flask_app = Flask(__name__)

    # Flask official request-body gate parser/read se pehle 413 raise karta hai.
    # Whole multipart request—including all files together—100 MB se upar nahi.
    flask_app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES

    # Flask 3.1+ non-file form memory aur multipart part-count controls. Older
    # Flask versions unknown config keys ignore karte hain; app-level validation
    # phir bhi active rehti hai.
    flask_app.config["MAX_FORM_MEMORY_SIZE"] = 2 * MB_IN_BYTES
    flask_app.config["MAX_FORM_PARTS"] = 100
    flask_app.config["JSON_SORT_KEYS"] = False

    # CORS wildcard removed. Comma-separated env value se production frontend
    # origins add ho sakte hain, e.g. ALLOWED_ORIGINS=https://app.example.com.
    default_allowed_origins = {
        "http://127.0.0.1:5000",
        "http://localhost:5000",
        "http://127.0.0.1:5500",
        "http://localhost:5500",
        "http://127.0.0.1:8000",
        "http://localhost:8000",
    }
    configured_origins = {
        origin.strip().rstrip("/")
        for origin in str(os.environ.get("ALLOWED_ORIGINS", "")).split(",")
        if origin.strip()
    }
    allowed_origins = configured_origins or default_allowed_origins

    # API key optional only for loopback local use. Production mode explicitly
    # enabled ho to missing key startup error hai, silent insecure deployment nahi.
    api_key = str(os.environ.get("IMAGE_REDUCER_API_KEY", "")).strip()
    production_mode = parse_boolean(
        os.environ.get("IMAGE_REDUCER_PRODUCTION"),
        default=False,
    )
    if production_mode and not api_key:
        raise RuntimeError(
            "IMAGE_REDUCER_API_KEY is required when IMAGE_REDUCER_PRODUCTION=1."
        )

    # Processing endpoints authentication, rate-limit aur concurrency controls
    # share karte hain. Endpoint names Flask function names hote hain, URL aliases
    # same bucket use karte hain.
    protected_processing_endpoints = {
        "inspect_image",
        "resize_or_convert",
        "analyze_photo_mode",
        "convert_photo_mode",
        "inspect_document_for_conversion",
        "convert_document_to_images",
        "convert_images_to_pdf",
    }
    heavy_processing_endpoints = {
        "resize_or_convert",
        "convert_photo_mode",
        "inspect_document_for_conversion",
        "convert_document_to_images",
        "convert_images_to_pdf",
    }

    # In-memory limiter single-process deployment ke liye dependency-free hai.
    # Multi-worker production me reverse proxy/Redis shared limiter bhi use karo.
    rate_buckets: Dict[Tuple[str, str], Any] = defaultdict(deque)
    rate_lock = threading.Lock()
    heavy_semaphore = threading.BoundedSemaphore(MAX_CONCURRENT_HEAVY_REQUESTS)

    def release_heavy_slot() -> None:
        """Current request ka acquired semaphore slot maximum ek baar release karta hai."""

        if getattr(g, "heavy_slot_acquired", False):
            g.heavy_slot_acquired = False
            heavy_semaphore.release()

    @flask_app.before_request
    def enforce_request_security() -> Optional[Any]:
        """Origin, API key, rate aur concurrency checks body processing se pehle lagata hai."""

        origin = str(request.headers.get("Origin") or "").rstrip("/")

        # Browser attacker origin ko preflight/POST dono par clear 403 milta hai.
        # Origin absent curl/server request ho sakti hai; auth/rate rules below apply.
        if origin and origin not in allowed_origins:
            return jsonify(error="This browser origin is not allowed."), 403

        # Preflight file processing/auth run nahi karta. Actual request API key
        # ke saath separately validate hogi.
        if request.method == "OPTIONS":
            return None

        endpoint = str(request.endpoint or "")
        if endpoint not in protected_processing_endpoints:
            return None

        remote_address = str(request.remote_addr or "unknown")
        is_loopback = remote_address in {"127.0.0.1", "::1"}

        # Configured key constant-time compare hoti hai. X-API-Key ke saath
        # standard Authorization: Bearer fallback bhi supported hai.
        if api_key:
            supplied_key = str(request.headers.get("X-API-Key") or "").strip()
            if not supplied_key:
                authorization = str(request.headers.get("Authorization") or "")
                if authorization.lower().startswith("bearer "):
                    supplied_key = authorization[7:].strip()

            if not supplied_key or not hmac.compare_digest(supplied_key, api_key):
                return jsonify(error="A valid API key is required."), 401
        elif not is_loopback:
            # Local no-key development preserved; non-loopback processing never
            # silently becomes unauthenticated even if production flag forgotten.
            return jsonify(
                error="Set IMAGE_REDUCER_API_KEY before allowing remote processing."
            ), 401

        bucket_name = "heavy" if endpoint in heavy_processing_endpoints else "general"
        request_limit = (
            HEAVY_RATE_LIMIT_PER_WINDOW
            if bucket_name == "heavy"
            else GENERAL_RATE_LIMIT_PER_WINDOW
        )
        now = time.monotonic()
        bucket_key = (remote_address, bucket_name)

        with rate_lock:
            bucket = rate_buckets[bucket_key]
            cutoff = now - RATE_LIMIT_WINDOW_SECONDS
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()

            if len(bucket) >= request_limit:
                retry_after = max(1, int(math.ceil(RATE_LIMIT_WINDOW_SECONDS - (now - bucket[0]))))
                response = jsonify(
                    error="Too many processing requests. Wait and try again."
                )
                response.status_code = 429
                response.headers["Retry-After"] = str(retry_after)
                return response

            bucket.append(now)

            # Many unique IP keys indefinitely store na hon. Threshold par only
            # fully expired buckets remove hote hain; active limiter data stays.
            if len(rate_buckets) > 10_000:
                expired_keys = [
                    key for key, values in rate_buckets.items()
                    if not values or values[-1] <= cutoff
                ]
                for key in expired_keys:
                    rate_buckets.pop(key, None)

        if endpoint in heavy_processing_endpoints:
            if not heavy_semaphore.acquire(blocking=False):
                response = jsonify(
                    error="Server is busy with other conversions. Try again shortly."
                )
                response.status_code = 503
                response.headers["Retry-After"] = "2"
                return response
            g.heavy_slot_acquired = True

        return None

    @flask_app.after_request
    def add_cors_headers(response: Any) -> Any:
        """Allowed browser origin ko scoped CORS headers deta aur request slot release karta hai."""

        release_heavy_slot()
        origin = str(request.headers.get("Origin") or "").rstrip("/")

        # Explicit origin reflect sirf allowlist match par hota hai. Vary: Origin
        # caches ko one origin ka response doosre origin ko serve karne se rokta hai.
        if origin in allowed_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.vary.add("Origin")
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = (
                "Content-Type, X-API-Key, Authorization"
            )
            response.headers["Access-Control-Max-Age"] = "600"

        response.headers["Access-Control-Expose-Headers"] = (
            "X-Output-Width, X-Output-Height, X-Output-Format, X-Output-DPI, "
            "X-Output-Bytes, X-Target-Bytes, X-Target-Matched, X-Applied-Mode, "
            "X-Quality-Tier, X-Fit-Strategy, X-Used-Manual-Size, "
            "X-Original-Width, X-Original-Height, X-Quality-Warning, "
            "X-Output-Padded, X-Document-Pages, X-Images-Created, "
            "X-Maximum-Images, X-Input-Images, X-PDF-Pages"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @flask_app.teardown_request
    def release_slot_after_exception(_error: Optional[BaseException]) -> None:
        """after_request tak response na bane tab bhi heavy slot leak nahi hone deta."""

        release_heavy_slot()

    @flask_app.errorhandler(413)
    def request_too_large(_error: Any) -> Tuple[Any, int]:
        """Flask body limit cross hone par beginner-friendly JSON error deta hai."""

        return jsonify(
            error=(
                f"Request is too large. The complete request and every individual file "
                f"must be {MAX_UPLOAD_MB} MB or less."
            )
        ), 413

    @flask_app.errorhandler(Exception)
    def handle_unexpected_error(error: Exception) -> Tuple[Any, int]:
        """Known ValueError 400, unexpected issue 500 me JSON banata hai."""

        if isinstance(error, ValueError):
            return jsonify(error=str(error)), 400

        # Production me raw stack/browser ko secret details nahi bhejni chahiye.
        flask_app.logger.exception("Unhandled image-processing error")
        return jsonify(error="Unexpected backend error. Check the Python terminal."), 500

    @flask_app.route("/", methods=["GET"])
    def health() -> Any:
        """Browser me localhost:5000 kholne par engine-ready status dikhata hai."""

        return jsonify(
            status="ENGINE ONLINE",
            formats=list(PIL_FORMAT_BY_UI.keys()),
            routes=42,
            social_photo_modes=len(SMART_MODE_PRESETS),
            social_platforms=list(
                dict.fromkeys(preset["platform"] for preset in SMART_MODE_PRESETS.values())
            ),
            target_size="MAXIMUM BYTES (exact padding is optional and disabled by default)",
            maximum_upload_mb=MAX_UPLOAD_MB,
            maximum_output_mb=MAX_OUTPUT_MB,
            maximum_output_pixels=MAX_OUTPUT_PIXELS,
            authentication=("API key required" if api_key else "loopback-only without API key"),
        )

    @flask_app.route("/inspect", methods=["POST", "OPTIONS"])
    def inspect_image() -> Any:
        """Upload ke original format/dimensions/DPI ko conversion se pehle return karta hai."""

        if request.method == "OPTIONS":
            return make_response("", 204)

        upload = request.files.get("image")
        data = read_upload_bytes(upload, "image")
        detected = detect_input_format(data, upload.filename)
        validate_expected_format(detected, request.form.get("expected_input_format"))
        image = open_image_bytes(data, detected)

        return jsonify(
            format=detected,
            width=image.width,
            height=image.height,
            # Encoding fix: real multiplication sign use kiya hai; "Ã—" UTF-8 mojibake tha.
            dimensions=f"{image.width} × {image.height} px",
            size_bytes=len(data),
            dpi=image_dpi_from_info(data, detected),
        )

    @flask_app.route("/resize", methods=["POST", "OPTIONS"])
    @flask_app.route("/convert", methods=["POST", "OPTIONS"])
    def resize_or_convert() -> Any:
        """Index resize aur All_converter ke 42 routes ka main processing endpoint."""

        if request.method == "OPTIONS":
            return make_response("", 204)

        # source_image original bytes hoti hain. All_converter canvas edits alag
        # image field me bhejti hai, par format-lock original source par validate hota hai.
        working_upload = request.files.get("image")
        source_upload = request.files.get("source_image") or working_upload

        source_data = read_upload_bytes(source_upload, "source_image")
        detected_source = detect_input_format(source_data, source_upload.filename)
        validate_expected_format(
            detected_source,
            request.form.get("expected_input_format"),
        )

        # Working upload absent ho to original process hoti; normally index me same hai.
        if working_upload is source_upload:
            working_data = source_data
            working_detected = detected_source
        else:
            working_data = read_upload_bytes(working_upload, "image")
            working_detected = None  # Canvas bytes independently detect hongi.

        image = open_image_bytes(working_data, working_detected)
        original_image = open_image_bytes(source_data, detected_source)

        output_format = normalize_format(
            request.form.get("output_format") or detected_source
        ) or detected_source
        quality = parse_int(
            request.form.get("quality"),
            DEFAULT_QUALITY,
            1,
            100,
            "QUALITY",
        )
        dpi = parse_int(
            request.form.get("dpi"),
            DEFAULT_DPI,
            1,
            2400,
            "DPI",
        )
        target_bytes = parse_target_bytes(request.form.get("target_kb"))
        requested_exact_padding = parse_boolean(
            request.form.get("allow_exact_padding"),
            default=False,
        )

        # Fragile byte-padding double opt-in hai. Request checkbox true ho lekin
        # server policy disabled ho to silent fallback ke bajaye clear error.
        if requested_exact_padding and not EXACT_SIZE_PADDING_ENABLED:
            raise ValueError(
                "Exact byte padding is disabled by server policy. Remove "
                "allow_exact_padding or set EXACT_SIZE_PADDING_ENABLED=1."
            )

        allow_exact_padding = requested_exact_padding and EXACT_SIZE_PADDING_ENABLED

        edited_image = apply_requested_edits(image, request.form)
        output_bytes, final_image, target_matched = encode_with_optional_exact_target(
            edited_image,
            output_format,
            quality,
            dpi,
            target_bytes,
            allow_exact_padding=allow_exact_padding,
        )

        verify_encoded_output(output_bytes, output_format)

        # Default target_kb ab maximum-size contract hai: output target se chhota
        # ho sakta hai but kabhi bada nahi. Exact assertion only explicit strict
        # padding mode me required hai.
        if target_bytes is not None and len(output_bytes) > target_bytes:
            raise ValueError(
                f"Target verification failed: maximum {target_bytes} bytes, got {len(output_bytes)}."
            )

        if allow_exact_padding and target_bytes is not None and len(output_bytes) != target_bytes:
            raise ValueError(
                f"Exact target verification failed: expected {target_bytes} bytes, "
                f"got {len(output_bytes)}."
            )

        download_name = (
            f"{safe_base_name(source_upload.filename)}_converted."
            f"{EXTENSION_BY_FORMAT[output_format]}"
        )
        response = send_file(
            io.BytesIO(output_bytes),
            mimetype=MIME_BY_FORMAT[output_format],
            as_attachment=False,
            download_name=download_name,
            max_age=0,
        )

        # Frontend response blob ke saath actual backend dimensions/target match padhta hai.
        response.headers["X-Output-Width"] = str(final_image.width)
        response.headers["X-Output-Height"] = str(final_image.height)
        response.headers["X-Output-Format"] = output_format
        response.headers["X-Output-DPI"] = str(dpi)
        response.headers["X-Output-Bytes"] = str(len(output_bytes))
        response.headers["X-Target-Bytes"] = str(target_bytes or "")
        if target_bytes is None:
            target_status = "not-requested"
        elif allow_exact_padding and len(output_bytes) == target_bytes:
            target_status = "exact"
        elif target_matched and len(output_bytes) <= target_bytes:
            target_status = "within-limit"
        else:
            target_status = "failed"
        response.headers["X-Target-Matched"] = target_status
        response.headers["X-Output-Padded"] = "true" if allow_exact_padding else "false"
        response.headers["X-Original-Width"] = str(original_image.width)
        response.headers["X-Original-Height"] = str(original_image.height)
        response.headers["Cache-Control"] = "no-store"
        return response

    return flask_app


# Flask installed computer par WSGI servers ``app`` variable import kar sakte hain.
app = create_app() if Flask is not None else None


# ----------------------------------------------------------------------------
# NAYA REGISTRATION STEP: purane `app` object par naye "Smart Photo-Mode"
# routes (/photo-modes, /analyze-photo-mode, /convert-photo-mode) jodna.
# KYA: Yeh sirf tab chalta hai jab Flask installed ho aur `app` successfully
#      ban chuka ho (upar wali line se).
# KYUN: Naye routes isi `app` object par register hote hain jise purana
#      `create_app()` factory function pehle hi bana chuka hai — hum sirf
#      us object me kuch extra routes "add" kar rahe hain.
# EFFECT AGAR YEH LINE HATA DOGE: Sirf teen naye routes (/photo-modes,
#      /analyze-photo-mode, /convert-photo-mode) disable ho jaayenge; purane
#      /, /inspect, /resize, /convert routes bilkul waise hi chalte rahenge,
#      kyunki unka registration `create_app()` ke andar hi hota hai.
# ----------------------------------------------------------------------------
if app is not None:
    register_smart_photo_mode_routes(app)


# ----------------------------------------------------------------------------
# NAYA REGISTRATION STEP: Section 09 ke document-converter routes ko usi
# already-created Flask `app` object par ADD karna.
# KYA: `/document-converter-info`, `/inspect-document`, `/document-to-images`,
#      uske PDF/Office aliases, aur `/images-to-pdf` register hote hain.
# KYUN: Registration `app` banne ke baad honi chahiye; isse purane
#      `create_app()` ke andar koi line edit karne ki zarurat nahi padi.
# PURANE CODE PAR EFFECT: Is block ko future me hataoge to sirf Section 09 ke
#      naye routes disable honge. Purana resize/converter/photo-mode behavior
#      aur upar wala registration bilkul waise hi rahega.
# ----------------------------------------------------------------------------
if app is not None:
    register_document_conversion_routes(app)


if __name__ == "__main__":
    # Direct ``py project1.py`` run ka beginner-friendly entry point.
    if app is None:
        raise SystemExit(
            "Flask is missing. Run this first:\n"
            "py -m pip install flask pillow cairosvg\n"
            "Then run: py project1.py"
        )

    # Local dev server explicitly threaded hai, so one slow request health/info
    # endpoints ko completely freeze nahi karegi. Heavy semaphore phir bhi CPU
    # conversions maximum two concurrent rakhta hai.
    #
    # IMPORTANT: Internet production ke liye Flask dev server use na karein.
    # Gunicorn/Waitress + reverse-proxy request timeout/rate limit use karein,
    # IMAGE_REDUCER_PRODUCTION=1 aur IMAGE_REDUCER_API_KEY configure karein.
    app.run(
        host="127.0.0.1",
        port=5000,
        debug=False,
        threaded=True,
        use_reloader=False,
    )