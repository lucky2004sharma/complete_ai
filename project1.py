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
            "X-Output-Bytes, X-Target-Bytes, X-Target-Matched, X-Applied-Mode, "
            "X-Quality-Tier, X-Fit-Strategy, X-Used-Manual-Size, "
            "X-Original-Width, X-Original-Height, X-Quality-Warning"
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
            social_photo_modes=len(SMART_MODE_PRESETS),
            social_platforms=list(
                dict.fromkeys(preset["platform"] for preset in SMART_MODE_PRESETS.values())
            ),
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


if __name__ == "__main__":
    # Direct ``py project1.py`` run ka beginner-friendly entry point.
    if app is None:
        raise SystemExit(
            "Flask is missing. Run this first:\n"
            "py -m pip install flask pillow cairosvg\n"
            "Then run: py project1.py"
        )

    # debug=False public deployment safer hai. Code edit ke baad server manually restart karna hoga.
    # restart karein; debug=True karne se auto-reload hoga but production me na karein.
    app.run(host="127.0.0.1", port=5000, debug=False)