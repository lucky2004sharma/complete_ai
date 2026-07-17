"""
=================================================================================
 SMART IMAGE RESIZER PRO — BACKEND (Flask + Pillow)
=================================================================================
Iss file ka kaam sirf ek hi hai: browser se ek image lena, usko resize/compress
karna, aur wapas bhej dena. Yeh "stateless" hai — matlab kuch bhi disk par save
nahi hota, sab kuch RAM (memory) mein hota hai, isliye fast hai aur server par
kachra (leftover files) nahi jamta.

PARENT-CHILD RELATIONSHIP (samajhne ke liye):
    app (Flask instance)                       <- sabse bada "parent"
      └── /resize route (child function)        <- isi ke andar saara kaam hota hai
            └── request.files['image']           <- frontend se aayi hui raw file
            └── request.form{...}                <- frontend se aaye hue settings (width, height, quality, target_kb)
            └── PIL.Image object                  <- image ko process karne wala core object
            └── io.BytesIO                        <- "virtual file" jisme processed image likhi jaati hai
            └── send_file()                       <- final response jo browser ko wapas jaata hai

Frontend (index.html) is Flask server se baat karta hai fetch() API ke zariye,
aur POST request bhejta hai '/resize' route par, jisme FormData ke through
image + settings dono ek saath jaate hain.
=================================================================================
"""

from flask import Flask, request, send_file, jsonify
from flask_cors import CORS
from PIL import Image
import io

# ---------------------------------------------------------------------------
# 1. FLASK APP INITIALIZE
# ---------------------------------------------------------------------------
# 'app' hi wo main object hai jo poore backend ko chalata hai. Har route
# (@app.route) isi 'app' ka child hai.
app = Flask(__name__)

# ---------------------------------------------------------------------------
# 2. CORS ENABLE
# ---------------------------------------------------------------------------
# Browser security ke wajah se, agar tumhara HTML file kisi doosre origin
# (jaise file:// ya kisi doosre port) se load ho raha hai, toh wo directly
# is server (port 5000) ko fetch() call nahi kar payega jab tak CORS allow
# na ho. Yeh line poore app ke liye CORS ko "on" kar deti hai.
CORS(app)

# ---------------------------------------------------------------------------
# 3. MAXIMUM FILE SIZE LIMIT (Server-level safety)
# ---------------------------------------------------------------------------
# 100MB = 100 * 1024 * 1024 bytes. Agar koi bahut badi file (jaise 5GB RAW
# photo) bhejne ki koshish kare, toh Flask use request ke andar aane se
# pehle hi reject kar dega (413 error), server crash hone se bach jaayega.
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024


def compress_to_target_kb(img, target_kb, save_format, min_quality=15, max_quality=95):
    """
    ---------------------------------------------------------------------
    SMART COMPRESSION FUNCTION — "binary search" se sahi quality dhoondhna
    ---------------------------------------------------------------------
    Purana version mein user khud quality (0-100) daalta tha aur guess
    karta tha ki file kitni badi banegi — yeh trial-and-error tha.

    Ab yeh function khud hi test karta hai: pehle beech ki quality (jaise
    55%) try karo, dekho file size target se bada hai ya chota — agar bada
    hai toh quality aur kam karo, agar chota hai toh quality badha sakte ho.
    Isko "binary search" kehte hain — har baar range aadhi ho jaati hai,
    isliye sirf 6-7 attempts mein sahi quality mil jaati hai (bruteforce
    karke 1-2-3...100 try karne se kahi zyada fast).

    PARAMETERS (inputs):
        img          -> PIL Image object (already resized, ready to save)
        target_kb    -> user ne jo maximum size chaha hai (KB mein), e.g. 50
        save_format  -> 'JPEG' ya 'WEBP' (PNG isme nahi aata, PNG lossless hai)
        min_quality  -> sabse kharab quality jise hum allow karenge (bahut neeche
                        jaane se image bahut kharab dikhne lagti hai)
        max_quality  -> sabse best quality jo try karenge

    RETURNS (output):
        (best_bytes, best_kb, achieved_quality) -> teeno cheezein wapas jaati hain
        taaki frontend ko pata chale final quality kya use hui.
    ---------------------------------------------------------------------
    """
    lo, hi = min_quality, max_quality
    best_bytes = None
    best_kb = None
    best_quality = lo

    # Maximum 7 baar try karenge — is se zyada karne ka fayda nahi, kyunki
    # binary search itni jaldi converge kar jaata hai ki extra attempts
    # sirf server ka time waste karenge.
    for _ in range(7):
        mid_quality = (lo + hi) // 2

        # Har attempt ek naya "virtual file" (BytesIO) banata hai, kyunki
        # ek baar likhne ke baad purani buffer reuse nahi ho sakti.
        buffer = io.BytesIO()
        img.save(buffer, save_format, quality=mid_quality, optimize=True)
        size_kb = len(buffer.getvalue()) / 1024

        if size_kb <= target_kb:
            # Yeh quality target ke andar fit hui — ise "best so far" maan lo,
            # aur ab thoda aur upar (behtar quality) try karke dekho.
            best_bytes = buffer.getvalue()
            best_kb = size_kb
            best_quality = mid_quality
            lo = mid_quality + 1
        else:
            # File abhi bhi bahut badi hai — quality aur kam karni padegi.
            hi = mid_quality - 1

        if lo > hi:
            break

    # Agar kabhi bhi target ke andar fit hi nahi hui (matlab min_quality
    # par bhi file target se badi hai), toh sabse kam quality wali file
    # hi de do — kam se kam sabse chota size toh milega.
    if best_bytes is None:
        buffer = io.BytesIO()
        img.save(buffer, save_format, quality=min_quality, optimize=True)
        best_bytes = buffer.getvalue()
        best_kb = len(best_bytes) / 1024
        best_quality = min_quality

    return best_bytes, best_kb, best_quality


# ---------------------------------------------------------------------------
# 4. MAIN API ROUTE: '/resize'
# ---------------------------------------------------------------------------
# Jab frontend ka fetch() '/resize' par POST request bhejega, Flask isi
# function ko call karega. Yeh function poore backend ka "brain" hai.
@app.route('/resize', methods=['POST'])
def resize_image():

    # ---- STEP A: file mili ya nahi, check karo -----------------------
    if 'image' not in request.files:
        return jsonify({'error': 'Koi image upload nahi hui'}), 400

    file = request.files['image']

    # ---- STEP B: width/height nikalo aur validate karo -----------------
    # request.form woh normal text fields hote hain jo FormData ke andar
    # image ke saath-saath bheje jaate hain (jaise ek form ke input boxes).
    try:
        width = int(request.form.get('width') or 0)
        height = int(request.form.get('height') or 0)
    except ValueError:
        # Agar number ki jagah text aaya (jaise 'abc'), crash hone se pehle
        # hi ek saaf error bhej do.
        return jsonify({'error': 'Width aur Height mein sirf numbers daaliye!'}), 400

    if width < 0 or height < 0:
        return jsonify({'error': 'Width aur Height negative (-) nahi ho sakte!'}), 400

    # ---- STEP C: compression mode nikalo --------------------------------
    # DO MODES hain ab (pehle sirf ek tha):
    #   1) 'manual'  -> user khud quality % chunta hai (jaise pehle tha)
    #   2) 'target'  -> user sirf "mujhe 50KB se chhoti file chahiye" bolta
    #                   hai, aur backend khud sahi quality dhoondh leta hai
    mode = request.form.get('mode', 'manual')
    quality = int(request.form.get('quality', 80))

    try:
        target_kb = float(request.form.get('target_kb') or 0)
    except ValueError:
        target_kb = 0

    mime_type = file.mimetype

    # PNG ko lossless hi rakhna better hai (transparency ke liye), isliye
    # 'target_kb' mode sirf JPEG/WEBP ke liye kaam karega — PNG apni
    # 'optimize' setting se hi chota hota hai.
    if mime_type == 'image/png':
        save_format = 'PNG'
        export_mimetype = 'image/png'
    elif mime_type == 'image/webp':
        save_format = 'WEBP'
        export_mimetype = 'image/webp'
    else:
        save_format = 'JPEG'
        export_mimetype = 'image/jpeg'

    try:
        # ---- STEP D: image ko memory mein khol lo ------------------------
        img = Image.open(file.stream)

        # ---- STEP E: agar width/height diye gaye hain toh resize karo ---
        if width > 0 and height > 0:
            # LANCZOS ek high-quality resampling algorithm hai — chhoti
            # karte waqt bhi image sharp rehti hai, blurry nahi hoti.
            img = img.resize((width, height), Image.Resampling.LANCZOS)

        # ---- STEP F: agar JPEG banana hai lekin original transparent tha -
        # (RGBA/P mode) toh transparency hata kar solid background do,
        # warna save karte waqt error aayega ("cannot write mode RGBA as JPEG").
        if save_format in ('JPEG',) and img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        # ---- STEP G: ab actual compression karo ---------------------------
        achieved_quality = quality  # default, agar manual mode hai

        if mode == 'target' and target_kb > 0 and save_format in ('JPEG', 'WEBP'):
            # SMART MODE: binary search function call karo
            img_bytes, final_kb, achieved_quality = compress_to_target_kb(
                img, target_kb, save_format
            )
            img_io = io.BytesIO(img_bytes)
        else:
            # MANUAL MODE (purana wala behaviour, bas thoda clean kiya):
            img_io = io.BytesIO()
            if save_format == 'PNG':
                img.save(img_io, save_format, optimize=True)
            else:
                img.save(img_io, save_format, quality=quality, optimize=True)

        img_io.seek(0)  # cursor ko wapas shuru mein le aao, warna file 0-byte download hogi

        # ---- STEP H: response ke headers mein extra info bhi bhej do -----
        # Yeh custom headers frontend ko batate hain ki final quality kya
        # thi — isse UI mein "Achieved: 62% quality" jaisa dikha sakte hain.
        response = send_file(
            img_io,
            mimetype=export_mimetype,
            as_attachment=True,
            download_name=file.filename
        )
        response.headers['X-Achieved-Quality'] = str(achieved_quality)
        return response

    except Exception as e:
        # Koi bhi anjaan error (jaise corrupt image file) yahan pakda jaayega
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# 5. ERROR HANDLER: file 100MB se badi hone par
# ---------------------------------------------------------------------------
@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({'error': 'File is too large! Maximum allowed size is 100MB.'}), 413


# ---------------------------------------------------------------------------
# 6. SERVER START
# ---------------------------------------------------------------------------
# 'debug=True' matlab code change karte hi server khud restart ho jaayega
# (development ke liye accha hai, production mein isko False rakhna).
if __name__ == '__main__':
    app.run(debug=True, port=5000)
