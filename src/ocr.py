"""OCR utilities for extracting overlay text from Snapchat media overlays.

Provides two engines:
- EasyOCR: lightweight, default; grayscale + autocontrast preprocessing
- PaddleOCR: more accurate; upscale x2 + adaptive thresholding preprocessing

Reader instances are cached to avoid repeated heavy initialization.
"""

import io
import importlib
from functools import lru_cache
from typing import List, Optional

import numpy as np
from PIL import Image, ImageOps

# Optional deps imported at module load; PaddleOCR imported lazily in function
import easyocr
import cv2


@lru_cache(maxsize=1)
def _get_easyocr_reader():
    """Return a cached EasyOCR Reader, using GPU if CUDA is available.

    On Apple MPS, fall back to CPU to avoid pin_memory warnings.
    """
    use_gpu = False
    try:
        torch = importlib.import_module("torch")
        if getattr(torch, "cuda", None) and torch.cuda.is_available():
            use_gpu = True
        elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            use_gpu = False
    except Exception:
        use_gpu = False
    return easyocr.Reader(["en"], gpu=use_gpu)


def extract_overlay_text_easy(overlay_bytes: bytes) -> Optional[str]:
    """Run OCR using EasyOCR on overlay image bytes (WebP/PNG).

    Light preprocessing: grayscale + autocontrast.
    Returns cleaned text or None if OCR fails or finds nothing.
    """
    try:
        img = Image.open(io.BytesIO(overlay_bytes))
        gray = ImageOps.autocontrast(img.convert("L"))
        reader = _get_easyocr_reader()
        arr = np.array(gray)
        results = reader.readtext(arr, detail=0)
        cleaned = "\n".join(line.strip() for line in results if str(line).strip())
        return cleaned or None
    except Exception:
        return None


def extract_overlay_text_paddle(overlay_bytes: bytes) -> Optional[str]:
    """Run OCR using PaddleOCR with stronger preprocessing.

    Preprocessing steps:
    - Upscale image x2 (bicubic)
    - Adaptive thresholding (mean) to binarize text
    """
    try:
        nparr = np.frombuffer(overlay_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_UNCHANGED)
        if img is None:
            print("PaddleOCR: cv2.imdecode returned None")
            return None

        # Normalize to grayscale for preprocessing
        if img.ndim == 3:
            # Handle BGR or BGRA
            if img.shape[2] == 4:
                # Convert BGRA to BGR first (drop alpha)
                img_bgr = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
            else:
                img_bgr = img
            gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
        else:
            gray = img

        upscaled = cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)

        th = cv2.adaptiveThreshold(
            upscaled,
            maxValue=255,
            adaptiveMethod=cv2.ADAPTIVE_THRESH_MEAN_C,
            thresholdType=cv2.THRESH_BINARY,
            blockSize=31,
            C=10,
        )

        # Paddle expects RGB ndarray
        th_rgb = cv2.cvtColor(th, cv2.COLOR_GRAY2RGB)

        # Lazy import + cache PaddleOCR instance
        from paddleocr import PaddleOCR
        ocr = _get_paddle_reader()
        result = ocr.ocr(th_rgb)

        lines: List[str] = []
        if isinstance(result, list):
            for page in result:
                if isinstance(page, list):
                    for det in page:
                        try:
                            txt = det[1][0]
                            if txt:
                                lines.append(str(txt).strip())
                        except Exception:
                            continue

        cleaned = "\n".join(s for s in lines if s)
        if not cleaned:
            # Try without thresholding as fallback (convert to RGB correctly)
            try:
                if img.ndim == 3:
                    if img.shape[2] == 4:
                        img_bgr2 = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
                    else:
                        img_bgr2 = img
                    rgb = cv2.cvtColor(img_bgr2, cv2.COLOR_BGR2RGB)
                else:
                    rgb = cv2.cvtColor(img, cv2.COLOR_GRAY2RGB)
                result2 = _get_paddle_reader().ocr(rgb)
                lines2: List[str] = []
                if isinstance(result2, list):
                    for page in result2:
                        if isinstance(page, list):
                            for det in page:
                                try:
                                    txt = det[1][0]
                                    if txt:
                                        lines2.append(str(txt).strip())
                                except Exception:
                                    continue
                cleaned2 = "\n".join(s for s in lines2 if s)
                return cleaned2 or None
            except Exception as e2:
                print(f"PaddleOCR fallback failed: {e2}")
        return cleaned or None
    except Exception as e:
        print(f"PaddleOCR error: {e}")
        return None


@lru_cache(maxsize=1)
def _get_paddle_reader():
    """Return a cached PaddleOCR reader with safe defaults (CPU)."""
    try:
        from paddleocr import PaddleOCR
        # Use only supported args; device selection handled internally
        return PaddleOCR(use_angle_cls=True, lang='en')
    except Exception as e:
        print(f"Failed to initialize PaddleOCR: {e}")
        raise
