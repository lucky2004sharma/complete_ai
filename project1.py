# ======================================================================
# LIBRARIES IMPORT SECTION
# KYA KAR RAHA HAIN: Yahan hum Python ke dusre tools (libraries) apne code mein laa rahe hain.
# KYUN KAR RAHA HAIN: Kyunki hum HTTP requests handle karna, image compress karna sab khud scratch se (zero se) nahi likh sakte.
# EFFECT KYA HOGA: Isse humein ready-made functions mil jayenge jaise Image.open(), request.form, etc.
# PYTHON FILE PAR ASAR: Ye sabse top par hona chahiye. Agar koi library miss hui, to program wahi crash ho jayega.
# ======================================================================
from flask import Flask, request, send_file, jsonify
# Flask: Ye humara main web server framework banata hai (Jo continuously run karke request sunta hai).
# request: Frontend (HTML/JS) se aane wale data (photo, width, height, colors) ko pakadne ke liye.
# send_file: Processed photo ko wapas frontend pe bhejne/download karwane ke liye.
# jsonify: Frontend ko samajh aane wale JSON format (dictionary jaisa) me error messages bhejne ke liye.

from flask_cors import CORS
# CORS: Cross-Origin Resource Sharing. 
# KYA/KYUN: Agar aapka HTML file (Browser) kisi aur port (jaise 5500) se chal raha hai aur Python (Server) 5000 par, toh browser security reason se inko baat nahi karne deta. CORS is restriction ko hatata hai taaki dono baat kar sakein.

from PIL import Image, ImageEnhance, ImageFilter 
# PIL (Pillow): Python ki sabse best image processing library hai. Ye photo open karti hai, chota karti hai, aur save karti hai.
# ImageEnhance: Isse hum photo ki Brightness, Contrast, Saturation aur Sharpness badalenge.

import io
# io: Input/Output. Ye memory (RAM) me ek virtual file banata hai. Hum photo ko computer ki hard disk me save nahi kar rahe, balki RAM (BytesIO) me save karke direct user ko bhej rahe hain (Isse website ki speed 10x fast hoti hai aur server full nahi hota).

import re  
# re (Regular Expressions): Text ko pattern ke hisaab se dhundhne aur badalne ka tool.
# KYUN: SVG files image nahi balki text/XML code hoti hain, unhe edit karne ke liye text search & replace ki zaroorat hoti hai.

# ======================================================================
# RATE LIMITER SETUP (SECURITY BLOCK)
# KYA KAR RAHA HAIN: Flask-Limiter humare server ko protect karta hai.
# KYUN KAR RAHA HAIN: Taki koi hacker ya bot ek sath 10,000 requests bhej kar server ki memory down (DDoS attack) na kar de.
# EFFECT KYA HOGA: Agar limit cross hui, to server photo process karne se mana kar dega aur error message dega.
# ======================================================================
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# 1. Flask app start karte hain (Yahi aapka main server object hai jo poora website chalayega)
app = Flask(__name__)

# CORS ko app ke sath jod diya taaki frontend isse easily connect kar sake.
CORS(app) 

# ------------------------------------------------------------------
# APP CONFIGURATION (MAX FILE SIZE)
# KYA KAR RAHA HAIN: Server par aane wali file ka maximum size limit 100MB set kiya hai.
# KYUN KAR RAHA HAIN: Taki koi 2GB ki heavy 4K movie upload karke server ki memory (RAM) blast na kar de.
# EFFECT KYA HOGA: Agar file 100MB se badi hui, to code yahin reject kar dega. (100 * 1024 * 1024 = 100 Megabytes).
# ------------------------------------------------------------------
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024

# Limiter ko configure kar rahe hain
limiter = Limiter(
    get_remote_address, # Ye user ka IP address (internet id) check karta hai, taaki har user ko alag se limit kiya jaye.
    app=app,
    storage_uri="memory://", # Limit ka record RAM me save rakhta hai (Fastest option).
    default_limits=["200 per day"] # Ek din me ek user max 200 photo process kar sakta hai.
)

# ======================================================================
# FUNCTION: resize_image() (MAIN ROUTE API)
# KYA KAR RAHA HAIN: Ye main function (Route) hai jo '/resize' URL par aane wali POST requests ko handle karega.
# KYUN KAR RAHA HAIN: Jab frontend se JavaScript `fetch('http://127.0.0.1:5000/resize')` karta hai, toh control seedha is function ke paas aata hai.
# PYTHON FILE PAR ASAR: Ye poori API ki backbone (reed ki haddi) hai. Sara logic isi ke andar hai.
# ======================================================================
@app.route('/resize', methods=['POST'])
@limiter.limit("5 per minute") # Ek minute me 1 user max 5 requests (photos) bhej sakta hai (Spam rokne ke liye).
def resize_image():
    
    # ------------------------------------------------------------------
    # IF CONDITION: Check image in request
    # KYA KAR RAHA HAIN: Check kar raha hai ki frontend ne 'image' keyword ke andar koi file bheji hai ya nahi.
    # KYUN KAR RAHA HAIN: Agar user bina photo upload kiye "Download" daba dega, to server ke paas kuch nahi hoga process karne ke liye aur wo crash ho jayega.
    # EFFECT KYA HOGA: Agar photo nahi mili (not in), toh 'return' command code ko yahin rok degi aur 400 (Bad Request) error frontend ko bhejegi. Iske aage ka koi code nahi chalega.
    # ------------------------------------------------------------------
    if 'image' not in request.files:
        return jsonify({'error': 'Koi image upload nahi hui. Kripya image select karein.'}), 400

    # Frontend se aayi hui original photo ko 'file' naam ke variable me store/save kar liya.
    file = request.files['image']
    
    # ------------------------------------------------------------------
    # TRY-EXCEPT BLOCK: Form Data Validation & Extraction
    # KYA KAR RAHA HAIN: Frontend se aayi hui saari settings (width, height, dpi, etc.) ko padh raha hai aur variables me save kar raha hai.
    # KYUN KAR RAHA HAIN: Kyunki JS form data hamesha 'String' (Text) format me bhejta hai. Humein unhe 'int' (Integer/Number) ya 'float' (Decimal point number) me badalna hota hai taaki math calculations ho sakein.
    # EFFECT KYA HOGA: Agar JS ne width="100px" (text) bhej diya jise int() me convert nahi kiya ja sakta, toh code tootne ki bajaye EXCEPT block me jayega aur error dikhayega.
    # ------------------------------------------------------------------
    try:
        # request.form.get('width') frontend se width lata hai. 'or 0' ka matlab agar khali (empty) aaya toh usko 0 maan lo.
        width = int(request.form.get('width') or 0)
        height = int(request.form.get('height') or 0)
        scale_percent = float(request.form.get('scale_percent') or 100.0)
        
        target_kb = request.form.get('target_kb')
        # IF CONDITION: Target KB Extraction
        # KYA/KYUN: Agar frontend ne target_kb bheja hai (yaani input khali nahi tha), tabhi usko number (int) me badlo. Warna usko waisa hi (None) chhod do.
        if target_kb:
            target_kb = int(target_kb)
            
        target_dpi = int(request.form.get('dpi') or 72) # Print quality resolution. Default web ke liye 72 rakha hai.
        
        # .lower() sab letters ko chota (small case) kar dega (jaise PNG -> png).
        # .strip() text ke aage-peeche ke faaltu spaces (gaps) ko hata dega taaki exact match ho sake.
        output_format = request.form.get('output_format', '').lower().strip()
        
        # Converter check ke liye variable liya (jab user specific "JPG to PNG" tool use karega).
        expected_input_format = request.form.get('expected_input_format', '').lower().strip()

        # Resizer format check ke liye variable liya (jab user sirf "PNG Resizer" use karega).
        expected_resizer_format = request.form.get('expected_resizer_format', '').lower().strip()

        # Image colors (Enhancement) ke values liye. Ye float (0.5, 1.2, etc.) me hote hain. Default 0 hai (mtlb koi change nahi).
        brightness_val = float(request.form.get('brightness') or 0)
        contrast_val = float(request.form.get('contrast') or 0)
        saturation_val = float(request.form.get('saturation') or 0)
        sharpness_val = float(request.form.get('sharpness') or 0)
        
    except ValueError:
        # EXCEPT BLOCK: Agar upar ke TRY block me conversion fail hua (kisi ne number ki jagah ABCD bhej diya).
        # KYA HOGA: Code yahin ruk jayega aur JSON format me error frontend ko jayega.
        return jsonify({'error': 'Dimensions, Scale, Target KB aur DPI mein sirf numbers daaliye!'}), 400

    # ------------------------------------------------------------------
    # IF CONDITION: Negative value validation (Security Check)
    # KYA KAR RAHA HAIN: Check karta hai ki width, height ya dpi minus me (jaise -100) toh nahi hai.
    # KYUN KAR RAHA HAIN: Image ka size ya DPI minus me impossible hota hai. Agar koi hacker system break karne ki koshish kare, toh usko rokna hai.
    # ------------------------------------------------------------------
    if width < 0 or height < 0 or target_dpi < 1 or scale_percent <= 0:
        return jsonify({'error': 'Values negative ya zero nahi ho sakte!'}), 400

    # Quality setting nikalna (1 se 100 ke beech). Default 80 set hai agar frontend se kuch nahi aaya.
    quality = int(request.form.get('quality', 80))
    
    # mime_type file ka actual type batata hai browser ki language me (jaise 'image/jpeg', 'image/png').
    mime_type = file.mimetype 
    # file.filename user ke computer me photo ka asli naam hota hai (jaise 'my_cat.jpg').
    download_filename = file.filename 

    # ======================================================================
    # STRICT INPUT FORMAT VALIDATION (For Converters Only)
    # KYA KAR RAHA HAIN: Ye verify kar raha hai ki agar user 'JPG to PNG' converter use kar raha hai, toh usne input file sacchi me JPG hi dali hai ya koi dhoka diya hai (jaise GIF daal di).
    # EFFECT KYA HOGA: Galat file daalne par turant block kar dega (400 error).
    # ======================================================================
    # Agar expected_input_format me kuch hai, iska matlab tool ek specific converter hai.
    if expected_input_format:
        is_valid_format = False
        
        # IF-ELIF BLOCK: Match expected format with actual file mimetype.
        if expected_input_format in ['jpg', 'jpeg']:
            if mime_type in ['image/jpeg', 'image/jpg']:
                is_valid_format = True
                
        elif expected_input_format == 'png':
            if mime_type == 'image/png':
                is_valid_format = True
                
        elif expected_input_format == 'svg':
            # SVG ka mimetype hamesha fixed nahi hota, isliye filename ka end (extension .svg) bhi check karte hain safe side ke liye.
            if mime_type == 'image/svg+xml' or download_filename.lower().endswith('.svg'):
                is_valid_format = True
                
        elif expected_input_format == 'webp':
            if mime_type == 'image/webp':
                is_valid_format = True
                
        elif expected_input_format == 'gif':
            if mime_type == 'image/gif':
                is_valid_format = True
                
        else:
            # ELSE BLOCK: Agar admin ne koi ajeeb format mangwaya hai jiska strict rule nahi likha hai yahan, to bina block kiye use jaane do.
            is_valid_format = True 

        # IF CONDITION: Agar upar ke rules match fail hue (is_valid_format False hi raha).
        # RETURN: Process rok do aur user ko samjhao usne galat file daali hai.
        if not is_valid_format:
            return jsonify({
                'error': f'Invalid image format! Aapne {expected_input_format.upper()} Converter chuna hai, lekin aapne doosra format upload kiya. Kripya sirf {expected_input_format.upper()} file hi upload karein.'
            }), 400

    # ======================================================================
    # NAYA UPDATE - STRICT RESIZER VALIDATION
    # KYA KAR RAHA HAIN: Ye check kar raha hai ki "PNG Resizer" me sirf PNG hi aaye, "JPG Resizer" me sirf JPG aaye.
    # KYUN KAR RAHA HAIN: Taki specific pages (jo SEO k liye banaye gaye hain) sirf wahi photo lein jinka wahan kaam hai.
    # ======================================================================
    if expected_resizer_format:
        is_valid_resize_format = False
        
        # IF-ELIF BLOCK: Check actual file mime_type against required resizer format.
        if expected_resizer_format in ['jpg', 'jpeg']:
            if mime_type in ['image/jpeg', 'image/jpg']:
                is_valid_resize_format = True
        elif expected_resizer_format == 'png':
            if mime_type == 'image/png':
                is_valid_resize_format = True
        elif expected_resizer_format == 'webp':
            if mime_type == 'image/webp':
                is_valid_resize_format = True
        elif expected_resizer_format == 'svg':
            if mime_type == 'image/svg+xml' or download_filename.lower().endswith('.svg'):
                is_valid_resize_format = True
        else:
            # Koi un-registered format aaye toh allow kar lo.
            is_valid_resize_format = True
            
        # Error throw karo agar mismatch hua
        if not is_valid_resize_format:
            return jsonify({
                'error': f'Galat Image Format! Aapne {expected_resizer_format.upper()} Resizer khola hai, isliye yahan sirf {expected_resizer_format.upper()} file hi allow hai. Koi aur format ki file upload mat karein.'
            }), 400
            
        # ======================================================================
        # CORE LOGIC: Override output format to match input resizer format.
        # KYA: Agar PNG resizer khula hai, toh output automatically 'png' kar do.
        # KYUN: User ne specifically kaha tha "output is also same as input" for these specific resizers. Isse format change skip ho jayega.
        # ======================================================================
        output_format = expected_resizer_format


    # ======================================================================
    # SVG HANDLING BLOCK (VECTOR IMAGES)
    # KYA KAR RAHA HAIN: SVG files ko alag se process (resize) karne ka code.
    # KYUN KAR RAHA HAIN: SVG files actual photos (pixels jaise JPG) nahi hoti, wo sirf math aur text hoti hain. Humari normal 'Pillow' library SVG open karte hi crash ho jayegi.
    # EFFECT KYA HOGA: Is block me hum SVG ki XML text file kholte hain aur search & replace (re.sub) lagakar purani width/height ko hata kar naye user-provided dimensions ghusa dete hain. Isme "Quality" ya "Target KB" ka rules lagu nahi hota.
    # ======================================================================
    
    # IF CONDITION: Checking if it's an SVG file.
    if mime_type == 'image/svg+xml' or download_filename.lower().endswith('.svg'):
        try:
            # File ko byte stream se padh kar usko normal English text ('utf-8') me decode/convert karna.
            svg_data = file.read().decode('utf-8')
            
            # IF CONDITION: Agar user ne custom width aur height mangi hai (> 0).
            if width > 0 and height > 0:
                # Regex (r'(<svg[^>]*?)width="[^"]*"') matlab: <svg> tag ke andar jahan bhi 'width="kuch_bhi"' likha ho, usko dhoondo.
                # fr'\1width="{width}px"' matlab: Pura tag waisa hi rakho, bas usme 'width' ki value badal kar user wali dal do (e.g. 500px).
                svg_data = re.sub(r'(<svg[^>]*?)width="[^"]*"', fr'\1width="{width}px"', svg_data, count=1)
                svg_data = re.sub(r'(<svg[^>]*?)height="[^"]*"', fr'\1height="{height}px"', svg_data, count=1)
            
            # Update kiye hue text ko wapas binary (machine language bytes) me encode karke RAM (BytesIO) me load karna.
            img_io = io.BytesIO(svg_data.encode('utf-8'))
            img_io.seek(0) # Cursor ko line 0 par set kiya taaki shuru se file padhi/download ki ja sake.
            
            # RETURN: Yahan se seedha SVG file user ko download ke liye chali jati hai. Niche wale saare "Raster image" block skip ho jate hain!
            return send_file(img_io, mimetype='image/svg+xml', as_attachment=True, download_name=download_filename)
            
        except Exception as e:
            # Agar text file corrupt hui, to error response bhejo.
            return jsonify({'error': f'SVG processing me error aayi: {str(e)}'}), 500


    # ======================================================================
    # RASTER IMAGES BLOCK (PNG, JPG, WEBP, GIF, ETC.)
    # KYA KAR RAHA HAIN: Yahan se asli image processing (Pillow library) shuru hoti hai un photos ke liye jo pixels se bani hain.
    # ======================================================================
    try:
        # file.stream ka data RAM se lekar Pillow usko apni photo (object) me badal leta hai.
        img = Image.open(file.stream)
        
        # Photo ke metadata (under the hood details) me se original DPI check karke nikalna. Default (72,72) liya gaya hai.
        present_dpi = img.info.get('dpi', (72, 72))[0]
        
        # ======================================================================
        # IMAGE ENHANCEMENT BLOCK (Colors & Light)
        # KYA KAR RAHA HAIN: Frontend ke un 4 sliders (Brightness, Contrast, Saturation, Sharpness) ka asar apply karta hai.
        # KYUN KAR RAHA HAIN: Taki user downloaded photo me bhi wahi badlaav dekhe jo usne website pe live dekhe the.
        # MATH LOGIC (KYUN/EFFECT): Frontend se -100 se 100 ki value aati hai. Pillow API ko 0.0 se 2.0 tak ki value samajh aati hai.
        # Isliye hum (value + 100) / 100.0 formula use karte hain. 
        # Example: frontend bheja 50. -> (50+100)/100.0 = 1.5. Pillow ko 1.5 mila matlab 50% zyada contrast!
        # ======================================================================
        
        # IF CONDITION: Brightness
        # Agar user ne brightness change ki hai (value 0 nahi hai)
        if brightness_val != 0:
            enhancer = ImageEnhance.Brightness(img)
            img = enhancer.enhance((brightness_val + 100) / 100.0) # Photo ki nayi state overwrite kar di gayi (img = ...)
            
        # IF CONDITION: Contrast
        if contrast_val != 0:
            enhancer = ImageEnhance.Contrast(img)
            img = enhancer.enhance((contrast_val + 100) / 100.0)
            
        # IF CONDITION: Saturation (Colors ko dark ya light karna)
        if saturation_val != 0:
            enhancer = ImageEnhance.Color(img)
            img = enhancer.enhance((saturation_val + 100) / 100.0)
            
        # IF CONDITION: Sharpness
        if sharpness_val != 0:
            enhancer = ImageEnhance.Sharpness(img)
            img = enhancer.enhance((sharpness_val + 100) / 100.0)
        
        # ------------------------------------------------------------------
        # IF-ELIF-ELSE BLOCK: FORMAT CONVERSION LOGIC
        # KYA KAR RAHA HAIN: Ye section decide karta hai ki image save hone ke baad kaunse Format (jaise JPEG) me bachegi.
        # KYUN KAR RAHA HAIN: Taki user apna format badal sake (WebP to JPG, PNG to GIF, etc).
        # EFFECT: 'download_filename' ka last hissa (extension) aur mimetype (browser lang) badal diya jata hai.
        # ------------------------------------------------------------------
        if output_format in ['jpg', 'jpeg']:
            save_format = 'JPEG'
            export_mimetype = 'image/jpeg'
            # rsplit('.', 1)[0] ka matlab: Photo ka naam (jaise cat.png) ko piche se '.' ke pass kato.
            # ['cat', 'png'] bachega. Usko [0] yani 'cat' lo aur usme naya '.jpg' jod do -> 'cat.jpg'.
            download_filename = download_filename.rsplit('.', 1)[0] + '.jpg'
            
        elif output_format == 'png':
            save_format = 'PNG'
            export_mimetype = 'image/png'
            download_filename = download_filename.rsplit('.', 1)[0] + '.png'
            
        elif output_format == 'webp':
            save_format = 'WEBP'
            export_mimetype = 'image/webp'
            download_filename = download_filename.rsplit('.', 1)[0] + '.webp'
            
        elif output_format == 'gif':
            save_format = 'GIF'
            export_mimetype = 'image/gif'
            download_filename = download_filename.rsplit('.', 1)[0] + '.gif'
            
        elif output_format == 'bmp':
            save_format = 'BMP'
            export_mimetype = 'image/bmp'
            download_filename = download_filename.rsplit('.', 1)[0] + '.bmp'
            
        elif output_format in ['tiff', 'tif']:
            save_format = 'TIFF'
            export_mimetype = 'image/tiff'
            download_filename = download_filename.rsplit('.', 1)[0] + '.tiff'
            
        else:
            # ELSE BLOCK: Agar frontend ne koi specified 'output_format' bheja hi nahi, toh original mime_type detect karo.
            # Matlab file jaisi aayi thi usko waise hi chhod do, koi conversion mat karo.
            if mime_type == 'image/png':
                save_format = 'PNG'
                export_mimetype = 'image/png'
            elif mime_type == 'image/webp':
                save_format = 'WEBP'
                export_mimetype = 'image/webp'
            elif mime_type == 'image/gif':
                save_format = 'GIF'
                export_mimetype = 'image/gif'
            elif mime_type == 'image/bmp':
                save_format = 'BMP'
                export_mimetype = 'image/bmp'
            elif mime_type in ['image/tiff', 'image/tif']:
                save_format = 'TIFF'
                export_mimetype = 'image/tiff'
            else:
                # Agar koi unknown random type hai, toh usko by default universal JPEG me badal do (Safe side).
                save_format = 'JPEG'
                export_mimetype = 'image/jpeg'

        # ------------------------------------------------------------------
        # IF-ELIF BLOCK: IMAGE RESIZING LOGIC (Dimension change)
        # KYA KAR RAHA HAIN: Yahan actual height/width stretch ya shrink ki jati hai.
        # KYUN KAR RAHA HAIN: Taaki passport size ya banner size banaya ja sake.
        # EFFECT: Image.Resampling.LANCZOS sabse highest-quality filter hota hai, isse pixel blocky (fate hue) nahi dikhte.
        # ------------------------------------------------------------------
        if width > 0 and height > 0:
            # User ne specific number (pixels) me lambaai/chaudai di hai.
            img = img.resize((width, height), Image.Resampling.LANCZOS)
        elif scale_percent != 100.0:
            # User ne pixel nahi diye, balki Percentage (jaise 50%) chuna hai.
            # Toh existing dimensions ka math percentage nikal kar nayi width/height set hoti hai.
            new_w = int(img.width * (scale_percent / 100.0))
            new_h = int(img.height * (scale_percent / 100.0))
            img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

        # Ye virtual RAM space initialize ki gayi hai final ready photo daalne ke liye.
        img_io = io.BytesIO()
        
        # ------------------------------------------------------------------
        # IF CONDITION: Transparency Check (PNG to JPG Conversion Fix)
        # KYA KAR RAHA HAIN: Ye transparent photos (RGB-Alpha) ke transparent background ko pure White background me convert karta hai.
        # KYUN KAR RAHA HAIN: JPEG aur BMP formats transparency (aar-paar dikhne) ko support nahi karte. Agar aap kisi transparent PNG ko direct JPG me save karne ki command denge, to Pillow turant FATAL ERROR dekar crash ho jayega.
        # EFFECT: Alpha layer (mask) ka use karke photo ko white canvas (bg) ke upar paste/stamp kiya jata hai, aur us combination ko nayi 'img' maan liya jata hai.
        # ------------------------------------------------------------------
        if save_format in ['JPEG', 'BMP'] and img.mode in ("RGBA", "P", "LA"):
            # Naya Safed (White) blank canvas banaya original size ka (RGB mode yani solid colors).
            background = Image.new('RGB', img.size, (255, 255, 255))
            if img.mode == 'RGBA':
                # Transparent image ko canvas par chipkao (mask parameter se transparent area chipakne se bachta hai aur peeche ka white dikhta hai).
                background.paste(img, mask=img.split()[3]) 
            else:
                background.paste(img)
            # ab 'img' variable humara white-background wala photo ban chuka hai.
            img = background

        # ======================================================================
        # TARGET KB LOGIC (EXACT SIZE SHRINKER / BINARY SEARCH ALGORITHM)
        # KYA KAR RAHA HAIN: Ye algorithm photo ko multiple baar compress karta hai taaki uska final file size 'target_kb' ke exact/kareeb aa jaye.
        # KYUN KAR RAHA HAIN: Kyunki quality slider par anumaan (guess) lagana user ke liye mushkil hota hai ki 50KB kitni quality par banega.
        # NAYA UPDATE: Maine Binary Search ke steps 8 se badhakar 12 kar diye hain! Isse size accuracy bohut zyada precision me aayegi (jaise 66 KB target pe 65.9 KB result milega).
        # ======================================================================
        
        # IF CONDITION: Check if Target KB is demanded
        if target_kb:
            target_bytes = target_kb * 1024 # Target size ko Bytes me badla kyunki disk size Bytes me napa jata hai (1 KB = 1024 Bytes).
            working_img = img          
            best_io = None

            # IF CONDITION: Lossless to Lossy conversion check
            # KYA/KYUN: PNG, BMP, GIF formats "Lossless" hote hain - inme "Quality=80%" jaisa parameter hota hi nahi. Aap inko seedha size me kam nahi kar sakte.
            # EFFECT: Majboori mein humein in file types ko pehle internal system me JPEG jaisa behave karwana padta hai, taaki quality compression (size kam karna) kaam kare. Aur phir wahi JPEG user ko wapas diya jayega (target achieve karne ki shart hoti hai).
            if save_format in ['PNG', 'BMP', 'GIF', 'TIFF']: 
                if working_img.mode in ("RGBA", "P"):
                    bg = Image.new('RGB', working_img.size, (255, 255, 255))
                    if working_img.mode == 'RGBA':
                        bg.paste(working_img, mask=working_img.split()[3])
                    else:
                        bg.paste(working_img)
                    working_img = bg
                # Force format to JPEG taaki quality parameter se compression possible ho sake.
                active_format = 'JPEG'
                active_mimetype = 'image/jpeg'
                download_filename = download_filename.rsplit('.', 1)[0] + '.jpg'
            else:
                # Agar pehle se WEBP ya JPEG (lossy formats) hai toh bina ruke waise hi chalne do.
                active_format = save_format
                active_mimetype = export_mimetype

            MAX_SHRINK_ATTEMPTS = 6  # Image ki width/height max kitni baar kam karna (resize) allow hai agar target bahut chota ho.
            MIN_DIMENSION = 10       # Image minimum 10 pixel se chhoti nahi karenge warna gayab (invisible) ho jayegi.

            # ------------------------------------------------------------------
            # OUTER FOR LOOP: Image ke dimensions ko chhota karne ka loop
            # KYUN: Agar sabse ghatiya quality (1) par bhi photo Target (e.g. 5KB) se badi ban rahi hai, toh ek hi rasta bachta hai - photo ki width/height ko lambaai aur chaudai dono se physically chhota kar do.
            # ------------------------------------------------------------------
            for shrink_attempt in range(MAX_SHRINK_ATTEMPTS):
                min_q = 1 # Sabse ghatiya quality (lowest size)
                max_q = 100 # Sabse best quality (highest size)
                best_io = None

                # ------------------------------------------------------------------
                # INNER FOR LOOP: Binary Search for Quality (12 tries for extreme accuracy)
                # KYA: Ek guess-game (Anumaan lagana). Phele 50 quality try karo. Agar size zyada bada hai, toh 50 se niche (25) try karo. Agar size chota hai, toh 50 se upar (75) try karo.
                # EFFECT: 12 baar yahi adha-adhi range divide hoti hai, jisse aakhir me wo 1 single exact quality point (jaise 83%) nikal aati hai jo target size ke exactly barabar ho.
                # ------------------------------------------------------------------
                for _ in range(12):  
                    mid_q = (min_q + max_q) // 2
                    temp_io = io.BytesIO() # Naya khali virtual file (RAM)
                    
                    # Working image ko us quality par virtual file (temp_io) me save kiya
                    working_img.save(temp_io, active_format, quality=mid_q, dpi=(target_dpi, target_dpi))
                    
                    # File me kitne Bytes likhe gaye (tell) usko map kar 'size' banaya.
                    size = temp_io.tell()

                    # IF-ELSE: Agar test file target bytes se chhoti ya barabar hai, to quality aur badhane ki gunzaish hai (taaki bilkul border line par size aye).
                    if size <= target_bytes:
                        best_io = temp_io # Ye test pass hua, ye humara sabse best backup hai filhal ke liye.
                        min_q = mid_q + 1  
                    else:
                        # Agar target se badh gaya, toh agle cycle me range choti karni hogi.
                        max_q = mid_q - 1  

                # IF CONDITION: Agar upar ke 12 loops me ek baar bhi successful 'best_io' mil gaya (mtlb koi na koi quality level ne target meet kar liya), toh outer loop (Shrink loop) ko aage chalne mat do, yahin se bahar aa jao 'break' lagake.
                if best_io:
                    break

                # Yahan aane ka matlab 'best_io' nahi mila. Image itni badi hai ki Quality=1 par bhi file target limit tod rahi hai.
                # Toh ab photo ki Lambaai (width) aur Chaudai (height) ko 15% (0.85 multiplier) chota karo.
                new_w = max(MIN_DIMENSION, int(working_img.width * 0.85))
                new_h = max(MIN_DIMENSION, int(working_img.height * 0.85))

                # IF CONDITION: Agar resize karte-karte dimensions 10 pixel hit kar gaye aur size change nahi hua, toh usse zyada chota mat karo warna file corrupt ho jayegi. Loop tod do.
                if new_w == working_img.width and new_h == working_img.height:
                    break

                # Purani image ko over-write kar do is choti 85% wali nayi image se, aur agli baar for loop ke shuruat me jaakar fir se binary search chalne do is nayi choti image par.
                working_img = working_img.resize((new_w, new_h), Image.Resampling.LANCZOS)

            # Aakhri final check for target KB after everything is done.
            # IF CONDITION: Agar humare loop me ek pass hua file (best_io) store hua tha.
            if best_io:
                img_io = best_io # To main file output 'img_io' ko isi passed version ke sath replace kar do.
            else:
                # ELSE: Agar kisi bhi extreme case me Target fail ho jaye (jo rare hai), to sabse absolute minimum size (Quality=1) par photo create karke export kar do as fallback fail-safe.
                img_io = io.BytesIO()
                working_img.save(img_io, active_format, quality=1, dpi=(target_dpi, target_dpi))

            # Export format MIME (browser indicator) update kardo.
            export_mimetype = active_mimetype

        else:
            # ======================================================================
            # ELSE BLOCK: NORMAL SAVE (NO TARGET KB PROVIDED)
            # KYA KAR RAHA HAIN: Agar user ne target KB input me khali chhod diya tha, toh iska matlab use normal save format chahiye jo fast hota hai.
            # KYUN: Ye website ka default fast mode hai jisme loop ki zaroorat nahi.
            # ======================================================================
            
            # IF-ELIF-ELSE Block for normal format saving decisions:
            if save_format in ['PNG', 'GIF']:
                # PNG aur GIF par 'quality' rule lagu nahi hota. 'optimize=True' compress level enhance karta hai without losing data details.
                img.save(img_io, save_format, optimize=True, dpi=(target_dpi, target_dpi))
                
            elif save_format in ['WEBP', 'JPEG']:
                # WEBP aur JPEG standard lossy format hote hain unhe specific numerical quality (e.g. 80) chahiye hoti hai jo form se extract ki gayi thi.
                img.save(img_io, save_format, quality=quality, dpi=(target_dpi, target_dpi))
                
            else:
                # BMP ya TIFF formats uncompressed heavy file hote hain jinhe specific quality ya optimization command skip karni hoti hai.
                img.save(img_io, save_format, dpi=(target_dpi, target_dpi))
            
        # File cursor stream pointer (RAM pointer) ko bilkul shuruati position (0 byte) me lao.
        # KYUN: Agar cursor last me hoga, toh download karte samay browser ko sirf khali byte read hoge. Seek(0) karne se puri memory end tak theek se frontend par aayegi.
        img_io.seek(0) 

        # RETURN STATEMENT: Image user ko wapas download karne k liye 'send_file' framework module ke through bheji ja rahi hai.
        # as_attachment=True: Isse browser usse sidha download folder me download kar lega, browser ke tab (screen) me khol kar nahi dikhayega!
        return send_file(img_io, mimetype=export_mimetype, as_attachment=True, download_name=download_filename)

    except Exception as e:
        # EXCEPT BLOCK: Final safety net. Agar Pillow library koi undefined error throw karti hai jo try block fail karta hai, toh server stop na ho balki JSON format 500 error dikhaye.
        return jsonify({'error': f'Image Process me backend error aaya: {str(e)}'}), 500

# ======================================================================
# ERROR HANDLERS (SYSTEM ERROR MANAGEMENT)
# KYA KAR RAHA HAIN: Jab server par framework level HTTP protocol error aati hai (jaise user ne rate limit tod di), toh us built-in error message ko sundar text me translate karta hai.
# KYUN KAR RAHA HAIN: Custom errors user experience (UX) accha karte hain, warna backend standard raw code dikhakar user ko dara sakta hai.
# ======================================================================

@app.errorhandler(429)
def ratelimit_handler(e):
    # 429 Status Code: "Too Many Requests". 
    # Jab user upar limiter @limiter.limit("5 per minute") ko tod kar 6th request marta hai.
    return jsonify({'error': 'Rate limit exceeded! Aapne lagatar bahut zyada requests bhej di hain. Kripya server cool-down ke liye thoda rukiye.'}), 429

@app.errorhandler(413)
def request_entity_too_large(error):
    # 413 Status Code: "Payload Too Large". 
    # Jab uploaded image app.config['MAX_CONTENT_LENGTH'] (100MB default setup) se upar size ki limit tod deti hai.
    return jsonify({'error': 'File is too large! Maximum allowed server input memory buffer size is 100MB.'}), 413

# ======================================================================
# MAIN EXECUTION BLOCK (SERVER STARTER)
# KYA KAR RAHA HAIN: Ye ek conditional check hai jo ensure karta hai ki ye Python script direct chalai ja rahi hai (python app.py), na ki import module karke kisi aur file ke andar ghusai gai hai.
# KYUN KAR RAHA HAIN: Python ka yeh traditional command framework hai server on karne ka. 
# EFFECT KYA HOGA: 'debug=True' isme developer command line/terminal (cmd/vs code console) par errors clearly list kar dega code break par point karte huye. Product launching deployment environment me 'debug=False' mandatory hota hai security purpose ki vajah se.
# ======================================================================
if __name__ == '__main__':
    # Flask local internal server default localhost (127.0.0.1) ke under Port (gate) 5000 activate / host ho raha hai is line se. 
    app.run(debug=True, port=5000)