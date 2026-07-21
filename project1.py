"""
ImageReducer backend engine
===========================

KYA HAI:
    Yeh Flask + Pillow backend ``index_02`` resizer aur ``All_converter`` ke
    saare 42 cross-format routes ko ek hi ``/resize`` API se handle karta hai.

RUN KARNE KA TARIKA (VS Code terminal):
    py -m pip install flask pillow cairosvg
    py project1.py

IMPORTANT:
    * JPG, JPEG, WEBP, GIF, TIF, PNG aur SVG input/output supported hain.
    * ``expected_input_format`` aane par original upload ka REAL format check
      hota hai. Isliye PNG upload karke input dropdown ko JPEG bolne se request
      accept nahi hogi.
    * ``target_kb`` sabse high priority hai. 50 KB ko 50 * 1024 = 51,200 bytes
      maana jaata hai aur response exactly utne hi bytes ka banaya jaata hai.
    * SVG input ko raster image me kholne ke liye ``cairosvg`` dependency chahiye.

COMMENTS ITNE DETAIL ME KYUN HAIN:
    User beginner hai. Har important constant, function, loop aur if/else ke
    paas Hinglish explanation di gayi hai taaki future me value safely change
    ki ja sake aur us change ka effect samajh aaye.
"""

from __future__ import annotations

# Standard-library imports: inka alag installation nahi karna padta.
import base64
import io
import math
import re
import struct
import zlib
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, Iterable, Optional, Tuple

# Pillow image read, resize, filter aur encode karta hai.
from PIL import Image, ImageEnhance, ImageFilter, ImageOps, UnidentifiedImageError

# Flask optional-style import rakha hai taaki Flask absent ho to file ek clear
# install message de, cryptic ModuleNotFoundError par band na ho.
try:
    from flask import Flask, jsonify, make_response, request, send_file
except ImportError:  # pragma: no cover - sirf dependency-missing computer par chalega.
    Flask = None  # type: ignore[assignment]
    jsonify = make_response = request = send_file = None  # type: ignore[assignment]


# ============================================================================
# 01 // CONSTANTS
# KYA: Project-wide fixed values ek jagah rakhe hain.
# KYUN: Future me limit/port/format badalna ho to poori file search nahi karni.
# VALUE CHANGE KA EFFECT: MAX_UPLOAD_MB badhega to RAM usage bhi badh sakta hai.
# ============================================================================

KB_IN_BYTES = 1024
MB_IN_BYTES = 1024 * 1024
MAX_UPLOAD_MB = 25
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * MB_IN_BYTES
MAX_TARGET_MB = 50
MAX_TARGET_BYTES = MAX_TARGET_MB * MB_IN_BYTES
DEFAULT_DPI = 72
DEFAULT_QUALITY = 92
MIN_DIMENSION = 1
MAX_DIMENSION = 20_000

# Pillow decompression-bomb protection. Isse bahut bade pixel-count wali image
# server memory ko unexpectedly exhaust nahi karegi.
Image.MAX_IMAGE_PIXELS = 50_000_000

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
    """Target KB ko exact bytes me convert karta hai (50 KB -> 51,200 bytes)."""

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
            probe.verify()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
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
            png_bytes = cairosvg.svg2png(bytestring=data, unsafe=False)
            with Image.open(io.BytesIO(png_bytes)) as svg_image:
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

            # Phone-camera EXIF orientation ko pixels par physically apply karta hai.
            oriented = ImageOps.exif_transpose(source)

            # RGBA transparency preserve karta; CMYK/P mode ko standard RGB/RGBA banata hai.
            if "A" in oriented.getbands() or "transparency" in oriented.info:
                image = oriented.convert("RGBA")
            else:
                image = oriented.convert("RGB")

            image.load()
            return image
    except (UnidentifiedImageError, OSError, ValueError) as exc:
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
        return width, height

    # Sirf width diya to original ratio se height auto-calculate hoti hai.
    if width:
        height = max(MIN_DIMENSION, round(width * original_height / original_width))
        return width, height

    # Sirf height diya to original ratio se width auto-calculate hoti hai.
    if height:
        width = max(MIN_DIMENSION, round(height * original_width / original_height))
        return width, height

    return original_width, original_height


def apply_requested_edits(image: Image.Image, form: Any) -> Image.Image:
    """Rotation, resize aur index_02 ke enhancement sliders apply karta hai."""

    working = image.copy()

    # All_converter edited canvas already rotated pixels bhejti hai. Yeh field
    # future/TIFF fallback ke liye hai; blank ho to zero rotation.
    rotation = parse_int(form.get("rotation"), 0, 0, 359, "ROTATION")
    if rotation:
        # Pillow positive angle anti-clockwise hota hai; UI rotate button clockwise hai.
        working = working.rotate(-rotation, expand=True, resample=Image.Resampling.BICUBIC)

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
    return svg.encode("utf-8")


def encode_once(
    image: Image.Image,
    output_format: str,
    quality: int,
    dpi: int,
    palette_colors: Optional[int] = None,
) -> bytes:
    """Ek image ko ek baar requested settings par memory bytes me save karta hai."""

    output_format = normalize_format(output_format) or "PNG"

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
    return buffer.getvalue()


def palette_candidates() -> Iterable[Optional[int]]:
    """High color fidelity se low file size tak ordered palette options deta hai."""

    # None pehle full-color try karta hai; baaki values progressively size ghataati hain.
    yield None
    for colors in (256, 192, 128, 96, 64, 48, 32, 24, 16, 12, 8, 6, 4, 2):
        yield colors


def best_candidate_at_current_size(
    image: Image.Image,
    output_format: str,
    requested_quality: int,
    dpi: int,
    target_bytes: int,
) -> Tuple[bytes, bool]:
    """Current dimensions par best quality candidate dhoondhta hai."""

    if output_format in {"JPG", "JPEG", "WEBP"}:
        maximum_quality = min(95 if output_format in {"JPG", "JPEG"} else 100, requested_quality)
        high_data = encode_once(image, output_format, maximum_quality, dpi)

        # Requested quality already fit hai to aur quality reduce karne ki zarurat nahi.
        if len(high_data) <= target_bytes:
            return high_data, True

        low_data = encode_once(image, output_format, 1, dpi)
        if len(low_data) > target_bytes:
            return low_data, False

        # Binary search: highest quality jo target ke andar rahe wahi choose hoti hai.
        low_quality = 1
        high_quality = maximum_quality
        best_data = low_data

        while low_quality <= high_quality:
            middle_quality = (low_quality + high_quality) // 2
            candidate = encode_once(image, output_format, middle_quality, dpi)

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
        candidate = encode_once(image, output_format, requested_quality, dpi, colors)
        smallest = candidate

        if len(candidate) <= target_bytes:
            return candidate, True

    return smallest, False


# ============================================================================
# 06 // EXACT BYTE-SIZE PADDING
# KYA: Candidate target se chhota ho to invisible/metadata bytes safely add karta hai.
# KYUN: 55 KB PNG -> exactly 123 KB JPEG quality badha kar exact hona guaranteed nahi;
#       controlled padding exact size guarantee karta hai bina pixels badle.
# ============================================================================

def make_png_padding_chunk(payload_size: int) -> bytes:
    """Valid private ancillary PNG chunk banata hai."""

    chunk_type = b"npAD"
    payload = b"\x00" * payload_size
    checksum = zlib.crc32(chunk_type + payload) & 0xFFFFFFFF
    return struct.pack(">I", payload_size) + chunk_type + payload + struct.pack(">I", checksum)


def pad_png(data: bytes, extra_bytes: int) -> bytes:
    """PNG IEND se pehle padding chunk insert karta; tiny remainder trailing rakhta hai."""

    if extra_bytes >= 12 and data.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82"):
        # Chunk overhead 12 bytes hai; remaining bytes us chunk ka payload bante hain.
        chunk = make_png_padding_chunk(extra_bytes - 12)
        return data[:-12] + chunk + data[-12:]

    # 1..11 bytes me complete PNG chunk possible nahi. PNG readers IEND ke baad
    # trailing inert bytes ignore karte hain; pixels aur decode result same rehta hai.
    return data + (b"\x00" * extra_bytes)


def pad_webp(data: bytes, extra_bytes: int) -> bytes:
    """WEBP RIFF container me unknown PAD chunk add karta hai."""

    if extra_bytes >= 8 and extra_bytes % 2 == 0 and data[:4] == b"RIFF":
        payload_size = extra_bytes - 8
        padded = data + b"PAD " + struct.pack("<I", payload_size) + (b"\x00" * payload_size)
        # RIFF header byte 4..7 total file size minus 8 store karta hai.
        return padded[:4] + struct.pack("<I", len(padded) - 8) + padded[8:]

    # Odd/tiny difference rare hai (integer KB target always even). Fallback bytes
    # RIFF declared region ke bahar inert hain and common decoders ignore karte hain.
    return data + (b"\x00" * extra_bytes)


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
        # XML document ke closing root ke baad whitespace legal hai.
        return data + (b" " * extra_bytes)

    # GIF/TIFF decoders logical end marker/directory ke baad trailing bytes ignore
    # karte hain. Yeh bytes pixels, DPI ya dimensions ko touch nahi karti.
    return data + (b"\x00" * extra_bytes)


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

    return image.resize((new_width, new_height), Image.Resampling.LANCZOS)


def encode_with_optional_exact_target(
    image: Image.Image,
    output_format: str,
    quality: int,
    dpi: int,
    target_bytes: Optional[int],
) -> Tuple[bytes, Image.Image, bool]:
    """Normal encode ya exact-target iterative encode perform karta hai."""

    if target_bytes is None:
        return encode_once(image, output_format, quality, dpi), image, False

    working = image

    # 24 attempts practically 20,000px se 1px tak pahunchne ke liye enough hain;
    # ratio-based jump usually 2-5 attempts me result de deta hai.
    for _attempt in range(24):
        candidate, fits = best_candidate_at_current_size(
            working,
            output_format,
            quality,
            dpi,
            target_bytes,
        )

        if fits:
            exact = pad_to_exact_size(candidate, target_bytes, output_format)
            if len(exact) != target_bytes:
                raise ValueError("Exact target-size verification failed.")
            return exact, working, True

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

    data = upload.read()

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


def create_app() -> Any:
    """Flask app factory test/deployment dono ke liye application banata hai."""

    if Flask is None:
        raise RuntimeError("Flask is not installed. Run: py -m pip install flask")

    flask_app = Flask(__name__)

    # All_converter source + edited-canvas mila kar 25 MB se zyada request body ho
    # sakti hai. Per-file check 25 MB hi rahega; total multipart ceiling 60 MB hai.
    flask_app.config["MAX_CONTENT_LENGTH"] = 60 * MB_IN_BYTES
    flask_app.config["JSON_SORT_KEYS"] = False

    @flask_app.after_request
    def add_cors_headers(response: Any) -> Any:
        """Local HTML file ko localhost backend response read karne deta hai."""

        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Expose-Headers"] = (
            "X-Output-Width, X-Output-Height, X-Output-Format, X-Output-DPI, "
            "X-Output-Bytes, X-Target-Bytes, X-Target-Matched"
        )
        return response

    @flask_app.errorhandler(413)
    def request_too_large(_error: Any) -> Tuple[Any, int]:
        """Flask body limit cross hone par beginner-friendly JSON error deta hai."""

        return jsonify(error="Request is too large. Each image must be 25 MB or less."), 413

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
            target_size="EXACT BYTES",
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

        edited_image = apply_requested_edits(image, request.form)
        output_bytes, final_image, target_matched = encode_with_optional_exact_target(
            edited_image,
            output_format,
            quality,
            dpi,
            target_bytes,
        )

        verify_encoded_output(output_bytes, output_format)

        # Final exact assertion last safety gate hai: mismatch hua to download nahi hoga.
        if target_bytes is not None and len(output_bytes) != target_bytes:
            raise ValueError(
                f"Target verification failed: expected {target_bytes} bytes, got {len(output_bytes)}."
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
        response.headers["X-Target-Matched"] = "true" if target_matched else "not-requested"
        response.headers["X-Original-Width"] = str(original_image.width)
        response.headers["X-Original-Height"] = str(original_image.height)
        response.headers["Cache-Control"] = "no-store"
        return response

    return flask_app


# Flask installed computer par WSGI servers ``app`` variable import kar sakte hain.
app = create_app() if Flask is not None else None


if __name__ == "__main__":
    # Direct ``py project1.py`` run ka beginner-friendly entry point.
    if app is None:
        raise SystemExit(
            "Flask is missing. Run this first:\n"
            "py -m pip install flask pillow cairosvg\n"
            "Then run: py project1.py"
        )

    # debug=False public deployment safer hai. Code edit ke baad server manually restart karna hoga.
    app.run(host="127.0.0.1", port=5000, debug=False)