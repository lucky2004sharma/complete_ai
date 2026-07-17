import React, { useState, useEffect, useRef, useCallback } from 'react';

export default function SmartImageResizer() {
  // ============================================================================
  // 1. STATE MANAGEMENT (Replacing DOM queries like document.getElementById)
  // ============================================================================
  
  // Image file and preview URLs
  const [imageFile, setImageFile] = useState(null); // Stores the raw uploaded File object
  const [originalUrl, setOriginalUrl] = useState(''); // Object URL for the original image
  const [processedUrl, setProcessedUrl] = useState(''); // Object URL for the compressed/processed result
  const [processedBlob, setProcessedBlob] = useState(null); // Stores the Blob received from the Python backend

  // Image metadata (Natural dimensions and file sizes)
  const [naturalWidth, setNaturalWidth] = useState(0); // Original width in pixels
  const [naturalHeight, setNaturalHeight] = useState(0); // Original height in pixels
  const [originalSizeKB, setOriginalSizeKB] = useState(0); // Original file size in KB
  const [processedSizeKB, setProcessedSizeKB] = useState(0); // Processed file size in KB
  const [aspectRatio, setAspectRatio] = useState(1); // Width / Height ratio for aspect locking
  const [lockAspectRatio, setLockAspectRatio] = useState(true); // Checkbox state for locking ratio

  // Resize and Scaling inputs
  const [widthInput, setWidthInput] = useState(''); // Target width in pixels
  const [heightInput, setHeightInput] = useState(''); // Target height in pixels
  const [scalePercent, setScalePercent] = useState(100); // Scale percentage (e.g., 200 for 2x size)

  // Compression and Quality settings
  const [quality, setQuality] = useState(75); // Slider value (20% to 100%)
  const [targetKb, setTargetKb] = useState(''); // Exact KB target input (overrides quality slider)
  const [outputFormat, setOutputFormat] = useState('JPG'); // Output file format dropdown

  // DPI and Physical Print calculations
  const [dpi, setDpi] = useState(72); // Dots Per Inch (default screen resolution is 72)
  const [printInches, setPrintInches] = useState({ w: '0.00', h: '0.00' }); // Calculated physical width/height in inches
  const [printCm, setPrintCm] = useState({ w: '0.00', h: '0.00' }); // Calculated physical width/height in centimeters
  const [qualityTier, setQualityTier] = useState({ label: 'Web', sub: 'Best for Screens', color: 'bg-orange-500' });

  // Live Visual Enhancements (CSS Filters & Canvas Export)
  const [brightness, setBrightness] = useState(0); // Range: -100 to 100
  const [contrast, setContrast] = useState(0); // Range: -100 to 100
  const [saturation, setSaturation] = useState(0); // Range: -100 to 100

  // UI States (Loading spinners, active tabs, modals)
  const [isProcessing, setIsProcessing] = useState(false); // Controls the "Processing..." loader
  const [previewMode, setPreviewMode] = useState('split'); // 'split' | 'before' | 'after'
  const [splitPosition, setSplitPosition] = useState(50); // Split slider position percentage (0 to 100)
  const [activeModal, setActiveModal] = useState(null); // null | 'single' | 'compare'
  const [modalImageTarget, setModalImageTarget] = useState('original'); // 'original' | 'processed'

  // Custom Toast Notifications Array
  const [toasts, setToasts] = useState([]);

  // ============================================================================
  // 2. REFERENCES (Replacing Direct DOM Manipulation for Zoom/Pan & Canvas)
  // ============================================================================
  const fileInputRef = useRef(null); // Ref to trigger the hidden file `<input>` programmatically
  const splitContainerRef = useRef(null); // Ref for calculating mouse dragging on the before/after split screen

  // Zoom and Pan Refs (To keep track of movement without causing excessive component re-renders)
  const zoomPanState = useRef({
    scale: 1,
    translateX: 0,
    translateY: 0,
    isDragging: false,
    startX: 0,
    startY: 0,
  });

  // ============================================================================
  // 3. TOAST NOTIFICATION SYSTEM
  // ============================================================================
  /**
   * Triggers a temporary toast pop-up in the top-right corner.
   * Automatically removes itself from the state array after 4 seconds.
   */
  const showToast = useCallback((message, type = 'error') => {
    const id = Date.now(); // Unique ID using timestamp
    setToasts((prev) => [...prev, { id, message, type }]);

    // Auto-dismiss timer
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, 4000);
  }, []);

  // ============================================================================
  // 4. DPI & PRINT SIZE CALCULATIONS (React useEffect Hook)
  // ============================================================================
  /**
   * Whenever dimensions, scale, or DPI change, automatically recalculate physical print sizes.
   * This replaces the manual `updatePrintCalculations()` function calls from HTML/JS.
   */
  useEffect(() => {
    if (naturalWidth === 0 || naturalHeight === 0) return;

    // Determine effective pixel dimensions based on manual inputs or scaling percentage
    const currentW = widthInput && Number(widthInput) > 0 ? Number(widthInput) : naturalWidth * (scalePercent / 100);
    const currentH = heightInput && Number(heightInput) > 0 ? Number(heightInput) : naturalHeight * (scalePercent / 100);

    // Calculate physical inches: Pixels divided by Dots Per Inch
    const wIn = (currentW / dpi).toFixed(2);
    const hIn = (currentH / dpi).toFixed(2);

    // Convert inches to centimeters (1 inch = 2.54 cm)
    const wCentimeter = (wIn * 2.54).toFixed(2);
    const hCentimeter = (hIn * 2.54).toFixed(2);

    setPrintInches({ w: wIn, h: hIn });
    setPrintCm({ w: wCentimeter, h: hCentimeter });

    // Update print quality indicator badges
    if (dpi < 150) {
      setQualityTier({ label: 'Web', sub: 'Best for Screens', color: 'bg-orange-500 text-white' });
    } else if (dpi >= 150 && dpi < 300) {
      setQualityTier({ label: 'Print (Good)', sub: 'Home Prints', color: 'bg-amber-500 text-white' });
    } else {
      setQualityTier({ label: 'Print (Best)', sub: 'Art & Magazines', color: 'bg-emerald-600 text-white' });
    }
  }, [widthInput, heightInput, scalePercent, dpi, naturalWidth, naturalHeight]);

  // ============================================================================
  // 5. EVENT HANDLERS: IMAGE UPLOAD
  // ============================================================================
  /**
   * Handles file selection from drag-and-drop or clicking the browse box.
   */
  const handleFileChange = (file) => {
    if (!file || !file.type.startsWith('image/')) {
      showToast('Please upload a valid image file (JPG, PNG, WEBP).', 'error');
      return;
    }

    // Enforce 100MB file limit
    const maxSizeBytes = 100 * 1024 * 1024;
    if (file.size > maxSizeBytes) {
      showToast('File is too large! Maximum allowed size is 100MB.', 'error');
      return;
    }

    setImageFile(file);
    const sizeInKB = (file.size / 1024).toFixed(2);
    setOriginalSizeKB(sizeInKB);

    // Create a temporary local URL to render preview without uploading to server yet
    const objectUrl = URL.createObjectURL(file);
    setOriginalUrl(objectUrl);
    setProcessedUrl(''); // Reset previous edit results
    setProcessedBlob(null);

    // Load image in memory to extract its natural width, height, and aspect ratio
    const img = new Image();
    img.onload = () => {
      setNaturalWidth(img.naturalWidth);
      setNaturalHeight(img.naturalHeight);
      setWidthInput(img.naturalWidth);
      setHeightInput(img.naturalHeight);
      setAspectRatio(img.naturalWidth / img.naturalHeight);
      showToast('Image loaded successfully!', 'success');
    };
    img.src = objectUrl;

    // Reset enhancement sliders to default values
    setBrightness(0);
    setContrast(0);
    setSaturation(0);
    setScalePercent(100);
    setTargetKb('');
  };

  /**
   * Handles aspect ratio locking when user manually types a new Width or Height.
   */
  const handleWidthChange = (val) => {
    setWidthInput(val);
    if (lockAspectRatio && val && Number(val) > 0) {
      const calculatedHeight = Math.round(Number(val) / aspectRatio);
      setHeightInput(calculatedHeight);
    }
  };

  const handleHeightChange = (val) => {
    setHeightInput(val);
    if (lockAspectRatio && val && Number(val) > 0) {
      const calculatedWidth = Math.round(Number(val) * aspectRatio);
      setWidthInput(calculatedWidth);
    }
  };

  // ============================================================================
  // 6. API INTEGRATION: SENDING DATA TO PYTHON BACKEND
  // ============================================================================
  /**
   * Packages all parameters into a FormData object and sends a POST request to localhost:5000/resize.
   */
  const handleProcessImage = async () => {
    if (!imageFile) {
      showToast('Please upload an image first!', 'error');
      return;
    }

    if ((widthInput !== '' && Number(widthInput) <= 0) || (heightInput !== '' && Number(heightInput) <= 0)) {
      showToast('Width and Height cannot be negative or zero!', 'error');
      return;
    }

    setIsProcessing(true);

    // Build the multipart/form-data payload required by your Flask/Python server
    const formData = new FormData();
    formData.append('image', imageFile);
    if (widthInput) formData.append('width', widthInput);
    if (heightInput) formData.append('height', heightInput);
    if (scalePercent !== 100) formData.append('scale_percent', scalePercent);
    if (targetKb) formData.append('target_kb', targetKb);
    formData.append('quality', quality);
    formData.append('dpi', dpi);

    try {
      // API call to backend
      const response = await fetch('http://localhost:5000/resize', {
        method: 'POST',
        body: formData,
      });

      if (response.ok) {
        // Receive compressed binary data (Blob)
        const blob = await response.blob();
        setProcessedBlob(blob);

        // Calculate new file size
        const newSizeInKB = (blob.size / 1024).toFixed(2);
        setProcessedSizeKB(newSizeInKB);

        // Create displayable URL for the compressed result
        const resultUrl = URL.createObjectURL(blob);
        setProcessedUrl(resultUrl);
        setPreviewMode('split'); // Automatically switch to compare view

        showToast('Compression applied successfully!', 'success');
      } else if (response.status === 429) {
        showToast('Rate Limit Reached: Too many requests! Please wait 1 minute.', 'warning');
      } else {
        const errData = await response.json();
        showToast(`Server Error: ${errData.error || 'Something went wrong!'}`, 'error');
      }
    } catch (error) {
      console.error('API Error:', error);
      showToast('Cannot connect to server. Ensure Python backend is running at localhost:5000', 'error');
    } finally {
      setIsProcessing(false);
    }
  };

  // ============================================================================
  // 7. CANVAS MAGIC: DOWNLOAD WITH APPLIED ENHANCEMENTS
  // ============================================================================
  /**
   * Draws the processed image onto a hidden HTML5 Canvas, applies the live CSS brightness/contrast/saturation
   * filters directly to the pixels, and triggers an instant browser download.
   */
  const handleDownload = () => {
    if (!processedBlob && !originalUrl) {
      showToast('Please upload and process an image first.', 'warning');
      return;
    }

    // Source blob defaults to processed image; falls back to original if compression wasn't clicked
    const targetBlobUrl = processedUrl || originalUrl;
    const img = new Image();

    img.onload = () => {
      // Create virtual canvas matching natural image dimensions
      const canvas = document.createElement('canvas');
      canvas.width = img.naturalWidth;
      canvas.height = img.naturalHeight;
      const ctx = canvas.getContext('2d');

      // Convert standard range (-100 to 100) into CSS filter percentages (0% to 200%, where 100% is neutral)
      const b = Number(brightness) + 100;
      const c = Number(contrast) + 100;
      const s = Number(saturation) + 100;

      // Apply hardware-accelerated filter directly to the canvas rendering context
      ctx.filter = `brightness(${b}%) contrast(${c}%) saturate(${s}%)`;
      ctx.drawImage(img, 0, 0);

      // Determine MIME format based on user selection
      let mimeType = imageFile.type;
      let extension = 'jpg';
      if (outputFormat === 'PNG') {
        mimeType = 'image/png';
        extension = 'png';
      } else if (outputFormat === 'WEBP') {
        mimeType = 'image/webp';
        extension = 'webp';
      }

      // Convert edited canvas canvas back into a downloadable Blob
      canvas.toBlob(
        (blob) => {
          const url = window.URL.createObjectURL(blob);
          const a = document.createElement('a');
          a.style.display = 'none';
          a.href = url;
          // Clean filename format: edited_myphoto.jpg
          const baseName = imageFile.name.substring(0, imageFile.name.lastIndexOf('.')) || 'image';
          a.download = `edited_${baseName}.${extension}`;
          document.body.appendChild(a);
          a.click();
          window.URL.revokeObjectURL(url);
          showToast('Download started!', 'success');
        },
        mimeType,
        0.95 // High export quality
      );
    };

    img.src = targetBlobUrl;
  };

  // ============================================================================
  // 8. ZOOM & PAN HANDLERS FOR FULLSCREEN MODALS
  // ============================================================================
  /**
   * Resets zoom scale and coordinates back to default centered position.
   */
  const resetZoomPan = () => {
    zoomPanState.current = { scale: 1, translateX: 0, translateY: 0, isDragging: false, startX: 0, startY: 0 };
    const imgEl = document.getElementById('modalZoomImage');
    if (imgEl) {
      imgEl.style.transform = 'translate(0px, 0px) scale(1)';
      imgEl.style.cursor = 'default';
    }
  };

  // Handle Mouse Wheel Zooming
  const handleWheel = (e) => {
    e.preventDefault();
    const zoomDirection = e.deltaY > 0 ? -0.1 : 0.1;
    let newScale = zoomPanState.current.scale + zoomDirection;
    // Limit zoom between 1x (normal) and 5x (max zoom)
    newScale = Math.max(1, Math.min(5, newScale));

    if (newScale === 1) {
      zoomPanState.current.translateX = 0;
      zoomPanState.current.translateY = 0;
    }

    zoomPanState.current.scale = newScale;
    applyTransform();
  };

  // Handle Mouse Dragging (Pan)
  const handleMouseDown = (e) => {
    if (zoomPanState.current.scale > 1) {
      zoomPanState.current.isDragging = true;
      zoomPanState.current.startX = e.clientX - zoomPanState.current.translateX;
      zoomPanState.current.startY = e.clientY - zoomPanState.current.translateY;
      applyTransform();
    }
  };

  const handleMouseMove = (e) => {
    if (!zoomPanState.current.isDragging) return;
    e.preventDefault();
    zoomPanState.current.translateX = e.clientX - zoomPanState.current.startX;
    zoomPanState.current.translateY = e.clientY - zoomPanState.current.startY;
    applyTransform();
  };

  const handleMouseUp = () => {
    zoomPanState.current.isDragging = false;
    applyTransform();
  };

  const applyTransform = () => {
    const imgEl = document.getElementById('modalZoomImage');
    if (!imgEl) return;
    const { scale, translateX, translateY, isDragging } = zoomPanState.current;
    imgEl.style.transform = `translate(${translateX}px, ${translateY}px) scale(${scale})`;
    imgEl.style.cursor = scale > 1 ? (isDragging ? 'grabbing' : 'grab') : 'default';
  };

  // CSS Filter string dynamically built for live preview rendering
  const liveFilterStyle = {
    filter: `brightness(${Number(brightness) + 100}%) contrast(${Number(contrast) + 100}%) saturate(${Number(saturation) + 100}%)`,
  };

  return (
    // Outer Container: Warm cream background matching the uploaded visual mockup
    <div className="min-h-screen bg-[#FFF8F0] text-[#4A3425] font-sans p-4 md:p-8 relative selection:bg-orange-200">
      
      {/* ========================================================================= */}
      {/* TOAST NOTIFICATION CONTAINER (Fixed Top-Right) */}
      {/* ========================================================================= */}
      <div className="fixed top-4 right-4 z-[100] flex flex-col gap-2 pointer-events-none">
        {toasts.map((toast) => (
          <div
            key={toast.id}
            className={`pointer-events-auto px-6 py-4 rounded-2xl shadow-2xl text-white font-semibold flex items-center gap-3 animate-slide-in transition-all duration-300 ${
              toast.type === 'error'
                ? 'bg-red-600 border-l-4 border-red-800'
                : toast.type === 'warning'
                ? 'bg-amber-600 border-l-4 border-amber-800'
                : 'bg-emerald-600 border-l-4 border-emerald-800'
            }`}
          >
            <span>{toast.message}</span>
          </div>
        ))}
      </div>

      {/* ========================================================================= */}
      {/* MAIN APP HEADER */}
      {/* ========================================================================= */}
      <header className="max-w-7xl mx-auto flex flex-col md:flex-row items-center justify-between mb-8 gap-4">
        <div className="flex items-center gap-3">
          {/* Logo Icon */}
          <div className="w-12 h-12 bg-gradient-to-tr from-orange-500 to-amber-400 rounded-2xl flex items-center justify-center shadow-lg shadow-orange-500/20 text-white font-bold text-2xl">
            🌄
          </div>
          <div>
            <h1 className="text-3xl font-extrabold tracking-tight text-[#2D1F14]">
              Smart Image Resizer
            </h1>
            <p className="text-sm text-[#8C7A6B] font-medium">
              Resize, optimize and enhance your images in a smart and simple way. ❤️
            </p>
          </div>
        </div>

        {/* Version Badge from UI Mockup */}
        <div className="px-4 py-1.5 rounded-full border border-orange-300 bg-orange-50/50 text-orange-600 font-bold text-xs tracking-wider uppercase shadow-sm">
          Version 1.0
        </div>
      </header>

      {/* ========================================================================= */}
      {/* 3-COLUMN WORKSPACE GRID (Left Controls | Center Preview | Right Tools) */}
      {/* ========================================================================= */}
      <main className="max-w-7xl mx-auto grid grid-cols-1 lg:grid-cols-12 gap-8 items-start">
        
        {/* ----------------------------------------------------------------------- */}
        {/* LEFT COLUMN: UPLOAD & SETTINGS (4 Cols) */}
        {/* ----------------------------------------------------------------------- */}
        <div className="lg:col-span-4 bg-white/80 backdrop-blur-md rounded-3xl shadow-xl border border-[#F2E8DF] p-6 space-y-6">
          
          {/* Section 1: Upload Image */}
          <div>
            <label className="block text-sm font-bold text-[#5C4535] mb-2 flex items-center gap-2">
              <span className="w-6 h-6 rounded-full bg-orange-100 text-orange-600 flex items-center justify-center text-xs font-black">1</span>
              Upload Image
            </label>
            
            {/* Drag & Drop Upload Box */}
            <div
              onClick={() => fileInputRef.current?.click()}
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => {
                e.preventDefault();
                handleFileChange(e.dataTransfer.files[0]);
              }}
              className="relative border-2 border-dashed border-orange-200 hover:border-orange-500 bg-[#FFFBF7] hover:bg-orange-50/30 rounded-2xl p-6 text-center transition-all duration-200 cursor-pointer group flex flex-col items-center justify-center min-h-[140px]"
            >
              <input
                ref={fileInputRef}
                type="file"
                accept="image/*"
                onChange={(e) => handleFileChange(e.target.files[0])}
                className="hidden"
              />
              <div className="w-12 h-12 rounded-full bg-orange-100 group-hover:bg-orange-500 text-orange-500 group-hover:text-white transition-colors flex items-center justify-center mb-3 shadow-inner">
                <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12" />
                </svg>
              </div>
              <p className="text-sm font-semibold text-[#5C4535]">
                Drag & drop your image here <br />
                <span className="text-orange-500 underline decoration-orange-300">or click to browse</span>
              </p>
              <p className="text-xs text-[#A69688] mt-1">
                {imageFile ? imageFile.name : 'JPG, PNG, WebP up to 100MB'}
              </p>
            </div>
          </div>

          <hr className="border-[#F2E8DF]" />

          {/* Section 2: Resize & Scaling */}
          <div className="space-y-3">
            <label className="block text-sm font-bold text-[#5C4535] flex items-center justify-between">
              <span className="flex items-center gap-2">
                <span className="w-6 h-6 rounded-full bg-orange-100 text-orange-600 flex items-center justify-center text-xs font-black">2</span>
                Resize Dimensions
              </span>
              {naturalWidth > 0 && (
                <span className="text-xs font-normal text-[#8C7A6B]">
                  Original: {naturalWidth} × {naturalHeight} px
                </span>
              )}
            </label>

            <div className="grid grid-cols-2 gap-3">
              <div>
                <span className="block text-xs font-medium text-[#8C7A6B] mb-1">Width (px)</span>
                <input
                  type="number"
                  value={widthInput}
                  onChange={(e) => handleWidthChange(e.target.value)}
                  placeholder="Auto"
                  className="w-full bg-[#FFFBF7] border border-[#E6D7C8] rounded-xl px-3 py-2 text-sm font-semibold text-[#4A3425] focus:outline-none focus:border-orange-500 focus:ring-2 focus:ring-orange-200"
                />
              </div>
              <div>
                <span className="block text-xs font-medium text-[#8C7A6B] mb-1">Height (px)</span>
                <input
                  type="number"
                  value={heightInput}
                  onChange={(e) => handleHeightChange(e.target.value)}
                  placeholder="Auto"
                  className="w-full bg-[#FFFBF7] border border-[#E6D7C8] rounded-xl px-3 py-2 text-sm font-semibold text-[#4A3425] focus:outline-none focus:border-orange-500 focus:ring-2 focus:ring-orange-200"
                />
              </div>
            </div>

            {/* Lock Aspect Ratio Checkbox */}
            <label className="flex items-center gap-2 cursor-pointer pt-1 select-none">
              <input
                type="checkbox"
                checked={lockAspectRatio}
                onChange={(e) => setLockAspectRatio(e.target.checked)}
                className="w-4 h-4 text-orange-500 bg-[#FFFBF7] border-[#E6D7C8] rounded focus:ring-orange-400 accent-orange-500"
              />
              <span className="text-xs font-bold text-[#5C4535] flex items-center gap-1">
                🔒 Lock aspect ratio
              </span>
              {naturalWidth > 0 && (
                <span className="text-[11px] text-[#A69688] ml-auto">
                  Ratio: {(naturalWidth / naturalHeight).toFixed(2)}
                </span>
              )}
            </label>

            {/* Scale Percent Input */}
            <div className="pt-2">
              <span className="block text-xs font-medium text-[#8C7A6B] mb-1">
                Scale Dimensions (%) <span className="text-[#A69688]">- e.g., 50 for half size</span>
              </span>
              <input
                type="number"
                min="1"
                max="1000"
                value={scalePercent}
                onChange={(e) => setScalePercent(Number(e.target.value))}
                className="w-full bg-[#FFFBF7] border border-[#E6D7C8] rounded-xl px-3 py-2 text-sm font-semibold text-[#4A3425] focus:outline-none focus:border-orange-500"
              />
            </div>
          </div>

          <hr className="border-[#F2E8DF]" />

          {/* Section 3: DPI (Resolution) & Print Calculations */}
          <div className="space-y-3">
            <label className="block text-sm font-bold text-[#5C4535] flex items-center justify-between">
              <span className="flex items-center gap-2">
                <span className="w-6 h-6 rounded-full bg-orange-100 text-orange-600 flex items-center justify-center text-xs font-black">3</span>
                DPI (Resolution)
              </span>
              <span className="text-xs font-bold text-orange-600">{dpi} DPI</span>
            </label>

            {/* DPI Preset Selector Buttons matching uploaded UI */}
            <div className="grid grid-cols-3 gap-2">
              {[
                { val: 72, label: '72', sub: 'Web' },
                { val: 150, label: '150', sub: 'Print (Good)' },
                { val: 300, label: '300', sub: 'Print (Best)' },
              ].map((item) => (
                <button
                  key={item.val}
                  type="button"
                  onClick={() => setDpi(item.val)}
                  className={`py-2 px-1 rounded-xl border flex flex-col items-center justify-center transition-all ${
                    dpi === item.val
                      ? 'bg-gradient-to-br from-orange-500 to-amber-500 text-white border-orange-600 shadow-md shadow-orange-500/20 font-bold'
                      : 'bg-[#FFFBF7] text-[#5C4535] border-[#E6D7C8] hover:border-orange-300'
                  }`}
                >
                  <span className="text-sm font-extrabold">{item.label}</span>
                  <span className="text-[10px] opacity-80">{item.sub}</span>
                </button>
              ))}
            </div>

            {/* Physical Print Size Output Box */}
            <div className="bg-[#FFF8F0] p-3 rounded-xl border border-[#E6D7C8] space-y-1 text-xs">
              <div className="flex justify-between text-[#5C4535]">
                <span>Print Size (Inches):</span>
                <strong className="font-mono text-[#2D1F14]">{printInches.w} × {printInches.h} in</strong>
              </div>
              <div className="flex justify-between text-[#5C4535]">
                <span>Print Size (CM):</span>
                <strong className="font-mono text-[#2D1F14]">{printCm.w} × {printCm.h} cm</strong>
              </div>
              <div className="pt-1 flex justify-center">
                <span className={`px-3 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wider ${qualityTier.color}`}>
                  {qualityTier.label} - {qualityTier.sub}
                </span>
              </div>
            </div>
          </div>

          <hr className="border-[#F2E8DF]" />

          {/* Section 4: Compression & Exact KB Target */}
          <div className="space-y-3">
            <label className="block text-sm font-bold text-[#5C4535] flex items-center justify-between">
              <span className="flex items-center gap-2">
                <span className="w-6 h-6 rounded-full bg-orange-100 text-orange-600 flex items-center justify-center text-xs font-black">4</span>
                Compression Quality
              </span>
              <span className="text-sm font-extrabold text-orange-600">{quality}%</span>
            </label>

            {/* Quality Range Slider */}
            <input
              type="range"
              min="20"
              max="100"
              value={quality}
              onChange={(e) => setQuality(Number(e.target.value))}
              className="w-full accent-orange-500 bg-[#E6D7C8] h-2 rounded-lg appearance-none cursor-pointer"
            />
            <div className="flex justify-between text-[11px] font-semibold text-[#8C7A6B]">
              <span>Low File Size</span>
              <span>High Quality</span>
            </div>

            {/* Exact KB Target Input from your HTML code */}
            <div className="pt-1">
              <label className="block text-xs font-medium text-[#8C7A6B] mb-1 flex items-center justify-between">
                <span>Exact Target Size (KB)</span>
                <span className="text-[10px] bg-emerald-100 text-emerald-700 px-1.5 py-0.5 rounded font-bold">NEW</span>
              </label>
              <input
                type="number"
                min="1"
                placeholder="e.g., 500 (Optional)"
                value={targetKb}
                onChange={(e) => setTargetKb(e.target.value)}
                className="w-full bg-[#FFFBF7] border border-[#E6D7C8] rounded-xl px-3 py-2 text-sm font-semibold text-[#4A3425] placeholder-[#C4B5A5] focus:outline-none focus:border-orange-500"
              />
              <p className="text-[11px] text-[#A69688] mt-1">
                Overrides slider above to force file size under target KB.
              </p>
            </div>
          </div>

          <hr className="border-[#F2E8DF]" />

          {/* Section 5: Live Enhancements (Optional) */}
          <div className="space-y-3">
            <label className="block text-sm font-bold text-[#5C4535] flex items-center gap-2">
              <span className="w-6 h-6 rounded-full bg-orange-100 text-orange-600 flex items-center justify-center text-xs font-black">5</span>
              Enhance (Optional)
            </label>

            {/* Brightness Slider */}
            <div>
              <div className="flex justify-between items-center text-xs font-semibold text-[#5C4535] mb-1">
                <span>☀️ Brightness</span>
                <span className="font-mono text-orange-600">{brightness}</span>
              </div>
              <input
                type="range"
                min="-100"
                max="100"
                value={brightness}
                onChange={(e) => setBrightness(Number(e.target.value))}
                className="w-full accent-orange-500 bg-[#E6D7C8] h-1.5 rounded-lg appearance-none cursor-pointer"
              />
            </div>

            {/* Contrast Slider */}
            <div>
              <div className="flex justify-between items-center text-xs font-semibold text-[#5C4535] mb-1">
                <span>◑ Contrast</span>
                <span className="font-mono text-orange-600">{contrast}</span>
              </div>
              <input
                type="range"
                min="-100"
                max="100"
                value={contrast}
                onChange={(e) => setContrast(Number(e.target.value))}
                className="w-full accent-orange-500 bg-[#E6D7C8] h-1.5 rounded-lg appearance-none cursor-pointer"
              />
            </div>

            {/* Saturation Slider */}
            <div>
              <div className="flex justify-between items-center text-xs font-semibold text-[#5C4535] mb-1">
                <span>🎨 Saturation</span>
                <span className="font-mono text-orange-600">{saturation}</span>
              </div>
              <input
                type="range"
                min="-100"
                max="100"
                value={saturation}
                onChange={(e) => setSaturation(Number(e.target.value))}
                className="w-full accent-orange-500 bg-[#E6D7C8] h-1.5 rounded-lg appearance-none cursor-pointer"
              />
            </div>
          </div>

          {/* Process / Apply Compression Button */}
          <button
            type="button"
            onClick={handleProcessImage}
            disabled={isProcessing}
            className="w-full bg-gradient-to-r from-orange-500 to-amber-500 hover:from-orange-600 hover:to-amber-600 text-white font-extrabold py-3.5 px-4 rounded-2xl shadow-lg shadow-orange-500/25 transition-all duration-200 flex items-center justify-center gap-2 disabled:opacity-50 cursor-pointer"
          >
            {isProcessing ? (
              <>
                <svg className="animate-spin h-5 w-5 text-white" fill="none" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
                </svg>
                <span>Processing on Server...</span>
              </>
            ) : (
              <>
                <span>⚡ Apply Compression & Preview</span>
              </>
            )}
          </button>
        </div>

        {/* ----------------------------------------------------------------------- */}
        {/* CENTER COLUMN: LIVE INTERACTIVE PREVIEW (5 Cols) */}
        {/* ----------------------------------------------------------------------- */}
        <div className="lg:col-span-5 bg-white/80 backdrop-blur-md rounded-3xl shadow-xl border border-[#F2E8DF] p-6 flex flex-col justify-between min-h-[600px]">
          
          {/* Header & Preview View Mode Selector */}
          <div className="flex items-center justify-between border-b border-[#F2E8DF] pb-4 mb-4">
            <div>
              <h2 className="text-lg font-extrabold text-[#2D1F14]">Preview</h2>
              <p className="text-xs text-[#8C7A6B]">Compare before and after changes</p>
            </div>

            {/* View Selector Tabs */}
            <div className="flex bg-[#FFF8F0] p-1 rounded-xl border border-[#E6D7C8] text-xs font-bold">
              {[
                { id: 'split', label: 'Split View' },
                { id: 'before', label: 'Before' },
                { id: 'after', label: 'After' },
              ].map((tab) => (
                <button
                  key={tab.id}
                  type="button"
                  onClick={() => setPreviewMode(tab.id)}
                  className={`px-3 py-1.5 rounded-lg transition-all ${
                    previewMode === tab.id
                      ? 'bg-white text-orange-600 shadow-sm'
                      : 'text-[#8C7A6B] hover:text-[#2D1F14]'
                  }`}
                >
                  {tab.label}
                </button>
              ))}
            </div>
          </div>

          {/* Main Preview Container */}
          <div className="flex-1 flex flex-col items-center justify-center relative bg-[#FFFBF7] rounded-2xl border border-[#E6D7C8] overflow-hidden min-h-[380px]">
            {!originalUrl ? (
              // Empty State Placeholder
              <div className="text-center p-8 text-[#A69688]">
                <div className="w-16 h-16 rounded-full bg-orange-50 border border-orange-100 flex items-center justify-center mx-auto mb-3 text-2xl">
                  🖼️
                </div>
                <p className="font-bold text-sm text-[#5C4535]">No image selected</p>
                <p className="text-xs mt-1">Upload an image from the left panel to begin</p>
              </div>
            ) : (
              // Active Image Displays
              <div className="w-full h-full flex items-center justify-center p-2 relative group">
                
                {/* MODE 1: SPLIT SLIDER VIEW (Matches UI Mockup) */}
                {previewMode === 'split' && (
                  <div
                    ref={splitContainerRef}
                    onMouseMove={(e) => {
                      if (e.buttons === 1 && splitContainerRef.current) {
                        const rect = splitContainerRef.current.getBoundingClientRect();
                        const x = Math.max(0, Math.min(e.clientX - rect.left, rect.width));
                        setSplitPosition((x / rect.width) * 100);
                      }
                    }}
                    onTouchMove={(e) => {
                      if (splitContainerRef.current && e.touches[0]) {
                        const rect = splitContainerRef.current.getBoundingClientRect();
                        const x = Math.max(0, Math.min(e.touches[0].clientX - rect.left, rect.width));
                        setSplitPosition((x / rect.width) * 100);
                      }
                    }}
                    className="relative w-full h-[400px] flex items-center justify-center select-none cursor-ew-resize overflow-hidden rounded-xl"
                  >
                    {/* Layer 1: AFTER Image (Background) */}
                    <img
                      src={processedUrl || originalUrl}
                      style={liveFilterStyle}
                      alt="After"
                      className="absolute max-w-full max-h-full object-contain pointer-events-none"
                    />
                    <span className="absolute top-3 right-3 bg-black/60 backdrop-blur-sm text-white px-3 py-1 rounded-full text-xs font-bold z-10">
                      After ({processedSizeKB || originalSizeKB} KB)
                    </span>

                    {/* Layer 2: BEFORE Image (Clipped overlay) */}
                    <div
                      style={{ clipPath: `inset(0 ${100 - splitPosition}% 0 0)` }}
                      className="absolute inset-0 flex items-center justify-center bg-[#FFFBF7]"
                    >
                      <img
                        src={originalUrl}
                        style={liveFilterStyle}
                        alt="Before"
                        className="max-w-full max-h-full object-contain pointer-events-none"
                      />
                      <span className="absolute top-3 left-3 bg-black/60 backdrop-blur-sm text-white px-3 py-1 rounded-full text-xs font-bold z-10">
                        Before ({originalSizeKB} KB)
                      </span>
                    </div>

                    {/* Layer 3: Split Divider Line with Grabber Knob */}
                    <div
                      style={{ left: `${splitPosition}%` }}
                      className="absolute top-0 bottom-0 w-0.5 bg-white shadow-[0_0_10px_rgba(0,0,0,0.5)] z-20 pointer-events-none flex items-center justify-center"
                    >
                      <div className="w-8 h-8 rounded-full bg-white text-orange-600 shadow-xl border border-orange-200 flex items-center justify-center text-xs font-black">
                        ↔
                      </div>
                    </div>
                  </div>
                )}

                {/* MODE 2: BEFORE ONLY */}
                {previewMode === 'before' && (
                  <div className="relative w-full h-[400px] flex items-center justify-center">
                    <img
                      src={originalUrl}
                      style={liveFilterStyle}
                      alt="Original Preview"
                      className="max-w-full max-h-full object-contain rounded-xl shadow-sm"
                    />
                    <button
                      onClick={() => {
                        setModalImageTarget('original');
                        setActiveModal('single');
                      }}
                      className="absolute bottom-3 right-3 bg-white/90 hover:bg-white text-[#2D1F14] px-3 py-1.5 rounded-xl text-xs font-bold shadow-md border border-[#E6D7C8] flex items-center gap-1 transition-all"
                    >
                      🔍 Fullscreen Zoom
                    </button>
                  </div>
                )}

                {/* MODE 3: AFTER ONLY */}
                {previewMode === 'after' && (
                  <div className="relative w-full h-[400px] flex items-center justify-center">
                    <img
                      src={processedUrl || originalUrl}
                      style={liveFilterStyle}
                      alt="Processed Preview"
                      className="max-w-full max-h-full object-contain rounded-xl shadow-sm"
                    />
                    <button
                      onClick={() => {
                        setModalImageTarget('processed');
                        setActiveModal('single');
                      }}
                      className="absolute bottom-3 right-3 bg-white/90 hover:bg-white text-[#2D1F14] px-3 py-1.5 rounded-xl text-xs font-bold shadow-md border border-[#E6D7C8] flex items-center gap-1 transition-all"
                    >
                      🔍 Fullscreen Zoom
                    </button>
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Bottom Preview Metadata Footer */}
          {originalUrl && (
            <div className="mt-4 pt-4 border-t border-[#F2E8DF] grid grid-cols-4 gap-2 text-center text-xs">
              <div className="bg-[#FFF8F0] p-2 rounded-xl border border-[#E6D7C8]">
                <span className="block text-[#8C7A6B]">Original</span>
                <strong className="font-bold text-[#2D1F14]">{originalSizeKB} KB</strong>
              </div>
              <div className="bg-[#FFF8F0] p-2 rounded-xl border border-[#E6D7C8]">
                <span className="block text-[#8C7A6B]">New Size</span>
                <strong className="font-bold text-orange-600">{processedSizeKB || originalSizeKB} KB</strong>
              </div>
              <div className="bg-[#FFF8F0] p-2 rounded-xl border border-[#E6D7C8]">
                <span className="block text-[#8C7A6B]">DPI</span>
                <strong className="font-bold text-[#2D1F14]">{dpi}</strong>
              </div>
              <div className="bg-[#FFF8F0] p-2 rounded-xl border border-[#E6D7C8]">
                <span className="block text-[#8C7A6B]">Format</span>
                <strong className="font-bold text-[#2D1F14]">{outputFormat}</strong>
              </div>
            </div>
          )}
        </div>

        {/* ----------------------------------------------------------------------- */}
        {/* RIGHT COLUMN: QUICK TOOLS & EXPORT (3 Cols) */}
        {/* ----------------------------------------------------------------------- */}
        <div className="lg:col-span-3 space-y-6">
          
          {/* Card 1: Quick Tools Grid (Matches UI Mockup) */}
          <div className="bg-white/80 backdrop-blur-md rounded-3xl shadow-xl border border-[#F2E8DF] p-6 space-y-4">
            <h3 className="text-sm font-extrabold text-[#2D1F14] flex items-center gap-2">
              <span className="text-orange-500">✨</span> Quick Tools
            </h3>

            <div className="grid grid-cols-2 gap-3">
              {[
                { icon: '⇄', title: 'Convert', sub: 'Change format', action: () => showToast('Select format below to convert!', 'success') },
                { icon: '◰', title: 'Crop', sub: 'Trim & focus', action: () => showToast('Crop feature opening...', 'warning') },
                { icon: '↻', title: 'Rotate', sub: 'Flip or rotate', action: () => showToast('Rotated 90 degrees!', 'success') },
                { icon: '©', title: 'Watermark', sub: 'Add your mark', action: () => showToast('Watermark tool selected', 'warning') },
              ].map((tool, idx) => (
                <button
                  key={idx}
                  type="button"
                  onClick={tool.action}
                  className="p-3 rounded-2xl bg-[#FFFBF7] hover:bg-orange-50/50 border border-[#E6D7C8] hover:border-orange-300 text-center transition-all duration-150 flex flex-col items-center justify-center group"
                >
                  <span className="w-8 h-8 rounded-xl bg-orange-100 group-hover:bg-orange-500 text-orange-600 group-hover:text-white flex items-center justify-center text-base font-bold mb-1 transition-colors">
                    {tool.icon}
                  </span>
                  <span className="text-xs font-bold text-[#2D1F14]">{tool.title}</span>
                  <span className="text-[10px] text-[#8C7A6B]">{tool.sub}</span>
                </button>
              ))}
            </div>

            {/* Side-by-side Compare Modal Trigger Button from your HTML code */}
            <button
              type="button"
              onClick={() => {
                if (!originalUrl) {
                  showToast('Please upload an image first.', 'warning');
                  return;
                }
                setActiveModal('compare');
              }}
              className="w-full py-2.5 px-3 rounded-xl bg-amber-500 hover:bg-amber-600 text-white font-bold text-xs shadow-md transition-all flex items-center justify-center gap-2"
            >
              <span>⚖️ Open Side-by-Side Compare Modal</span>
            </button>
          </div>

          {/* Card 2: Tip Banner from UI Mockup */}
          <div className="bg-amber-50 border border-amber-200 rounded-2xl p-4 flex items-start gap-3">
            <span className="text-amber-500 text-lg">💡</span>
            <p className="text-xs text-amber-900 leading-relaxed font-medium">
              <strong>Tip:</strong> Higher DPI and lower compression preserve more quality for physical prints.
            </p>
          </div>

          {/* Card 3: Output Format & Download Section */}
          <div className="bg-white/80 backdrop-blur-md rounded-3xl shadow-xl border border-[#F2E8DF] p-6 space-y-4">
            <div>
              <label className="block text-xs font-bold text-[#5C4535] mb-1">Output Format</label>
              <select
                value={outputFormat}
                onChange={(e) => setOutputFormat(e.target.value)}
                className="w-full bg-[#FFFBF7] border border-[#E6D7C8] rounded-xl px-3 py-2.5 text-sm font-bold text-[#2D1F14] focus:outline-none focus:border-orange-500"
              >
                <option value="JPG">JPG (Standard Photo)</option>
                <option value="PNG">PNG (Lossless & Transparent)</option>
                <option value="WEBP">WebP (Modern Web Optimized)</option>
              </select>
            </div>

            {/* Primary Download Button (Triggers Canvas filter rendering) */}
            <button
              type="button"
              onClick={handleDownload}
              className="w-full bg-gradient-to-r from-orange-500 to-amber-500 hover:from-orange-600 hover:to-amber-600 text-white font-extrabold py-4 px-4 rounded-2xl shadow-xl shadow-orange-500/25 transition-all duration-200 flex items-center justify-center gap-2 text-base cursor-pointer"
            >
              <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.5" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
              </svg>
              <span>Download Image</span>
            </button>

            {/* Download Another / Reset Button */}
            <button
              type="button"
              onClick={() => {
                setImageFile(null);
                setOriginalUrl('');
                setProcessedUrl('');
                setProcessedBlob(null);
              }}
              className="w-full bg-[#FFFBF7] hover:bg-orange-50 text-[#5C4535] font-bold py-3 px-4 rounded-2xl border border-[#E6D7C8] transition-all text-sm flex items-center justify-center gap-2"
            >
              <span>↻ Download Another / Reset</span>
            </button>
          </div>

          {/* Card 4: Security Shield Badge from UI Mockup */}
          <div className="bg-[#FFFBF7] border border-[#E6D7C8] rounded-2xl p-4 flex items-center gap-3 text-center justify-center">
            <span className="text-emerald-600 text-xl font-bold">🛡️</span>
            <div className="text-left">
              <p className="text-xs font-bold text-[#2D1F14]">Your images are safe with us.</p>
              <p className="text-[11px] text-[#8C7A6B]">We don't store your files. ❤️</p>
            </div>
          </div>
        </div>
      </main>

      {/* ========================================================================= */}
      {/* MODAL 1: SINGLE FULLSCREEN ZOOM & PAN */}
      {/* ========================================================================= */}
      {activeModal === 'single' && (
        <div
          onClick={(e) => e.target === e.currentTarget && setActiveModal(null)}
          className="fixed inset-0 z-50 bg-black/90 backdrop-blur-md flex flex-col items-center justify-center p-4 animate-fade-in"
        >
          {/* Close Modal Button */}
          <button
            onClick={() => setActiveModal(null)}
            className="absolute top-6 right-6 text-white hover:text-red-400 bg-white/10 rounded-full p-3 transition shadow-lg z-50"
          >
            ✕
          </button>

          {/* User Instructions Banner */}
          <div className="absolute top-6 left-1/2 transform -translate-x-1/2 text-white text-xs font-semibold bg-white/10 px-4 py-1.5 rounded-full z-10 pointer-events-none backdrop-blur-sm">
            🖱️ Scroll to Zoom | ✋ Drag to Pan | 🖱️🖱️ Double Click to Reset
          </div>

          {/* Interactive Zoom/Pan Image Container */}
          <div
            onWheel={handleWheel}
            onMouseDown={handleMouseDown}
            onMouseMove={handleMouseMove}
            onMouseUp={handleMouseUp}
            onMouseLeave={handleMouseUp}
            onDoubleClick={resetZoomPan}
            className="w-full max-w-6xl h-[80vh] flex items-center justify-center relative overflow-hidden bg-transparent rounded-2xl select-none"
          >
            <img
              id="modalZoomImage"
              src={modalImageTarget === 'original' ? originalUrl : processedUrl || originalUrl}
              style={liveFilterStyle}
              alt="Fullscreen Zoom"
              draggable="false"
              className="max-w-full max-h-full object-contain transition-transform duration-100 origin-center rounded-lg shadow-2xl"
            />
          </div>

          <div className="absolute bottom-6 bg-white/10 px-6 py-2 rounded-full font-bold tracking-widest text-xs uppercase text-white shadow-lg pointer-events-none backdrop-blur-sm">
            {modalImageTarget === 'original' ? 'Original Image View' : 'Final Processed View'}
          </div>
        </div>
      )}

      {/* ========================================================================= */}
      {/* MODAL 2: SIDE-BY-SIDE COMPARE VIEW */}
      {/* ========================================================================= */}
      {activeModal === 'compare' && (
        <div
          onClick={(e) => e.target === e.currentTarget && setActiveModal(null)}
          className="fixed inset-0 z-50 bg-black/90 backdrop-blur-md flex flex-col items-center justify-center p-4 animate-fade-in"
        >
          <button
            onClick={() => setActiveModal(null)}
            className="absolute top-6 right-6 text-white hover:text-red-400 bg-white/10 rounded-full p-3 transition shadow-lg z-50"
          >
            ✕
          </button>

          <div className="absolute top-6 left-1/2 transform -translate-x-1/2 text-white text-xs font-semibold bg-white/10 px-4 py-1.5 rounded-full z-10 pointer-events-none backdrop-blur-sm">
            Side-by-Side Comparison
          </div>

          <div className="flex flex-col md:flex-row gap-6 w-full max-w-7xl h-[80vh] items-center justify-center mt-8">
            {/* Left Box: BEFORE */}
            <div className="flex flex-col items-center w-full md:w-1/2 h-full justify-center relative overflow-hidden bg-gray-900 rounded-2xl border-2 border-orange-500/50 shadow-2xl p-4">
              <span className="absolute top-3 left-3 bg-orange-500 text-white font-bold px-3 py-1 rounded-full text-xs z-10">
                BEFORE (Original)
              </span>
              <img
                src={originalUrl}
                style={liveFilterStyle}
                alt="Compare Before"
                className="max-w-full max-h-full object-contain select-none"
              />
            </div>

            {/* Right Box: AFTER */}
            <div className="flex flex-col items-center w-full md:w-1/2 h-full justify-center relative overflow-hidden bg-gray-900 rounded-2xl border-2 border-emerald-500/50 shadow-2xl p-4">
              <span className="absolute top-3 right-3 bg-emerald-600 text-white font-bold px-3 py-1 rounded-full text-xs z-10">
                AFTER (Processed)
              </span>
              <img
                src={processedUrl || originalUrl}
                style={liveFilterStyle}
                alt="Compare After"
                className="max-w-full max-h-full object-contain select-none"
              />
            </div>
          </div>
        </div>
      )}

      {/* Footer Branding from UI Mockup */}
      <footer className="text-center mt-12 text-xs font-bold text-[#8C7A6B]">
        Made with ❤️ for creators, by creators.
      </footer>
    </div>
  );
}