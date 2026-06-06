"""
detector.py  -  Kill detection via OCR  (Splatoon 2/3)

Primary:  easyocr (pure Python, no external tools)
          pip install easyocr
          ~200MB model downloaded on first use.
          Fires only when "をたおした" is found.

Fallback: pixel-based when easyocr is not installed.

Two-stage pipeline:
  1. Fast pixel pre-filter (<1ms)  -- skip OCR if no dark banner
  2. OCR                           -- only when pre-filter passes
"""

import cv2
import numpy as np
from dataclasses import dataclass
from typing import Callable

# ---------------------------------------------------------------------------
# OCR backend  (lazy-loaded on first use to keep startup fast)
# ---------------------------------------------------------------------------
_ocr_reader  = None   # easyocr.Reader, initialised on first OCR call
_EASYOCR     = False  # True once easyocr import succeeded
_OCR_READY   = False  # True once reader is loaded and ready

try:
    import easyocr as _easyocr_mod
    _EASYOCR = True
except ImportError:
    _easyocr_mod = None

KILL_PHRASES = ("をたおした", "たおした")


def _ensure_reader(use_gpu: bool = False):
    """Lazy-init the EasyOCR reader (downloads model on first call)."""
    global _ocr_reader, _OCR_READY
    if _OCR_READY:
        return _ocr_reader
    if not _EASYOCR:
        return None
    try:
        _ocr_reader = _easyocr_mod.Reader(
            ["ja"], gpu=use_gpu, verbose=False,
            model_storage_directory=None,   # use easyocr default cache
        )
        _OCR_READY = True
    except Exception:
        pass
    return _ocr_reader


def ocr_available() -> bool:
    return _EASYOCR


def ocr_ready() -> bool:
    return _OCR_READY


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class MarkerEvent:
    time_sec: float
    event_type: str
    confidence: float = 1.0
    label: str = ""

    def __post_init__(self):
        if not self.label:
            self.label = self.event_type


# Kill banner ROI (normalised 0-1): bottom-centre strip
KILL_ROI = (0.22, 0.88, 0.54, 0.09)


def _crop(frame, roi):
    h, w = frame.shape[:2]
    x  = int(roi[0] * w);  y  = int(roi[1] * h)
    rw = int(roi[2] * w);  rh = int(roi[3] * h)
    return frame[y:y+rh, x:x+rw]


def _gray(frame):
    return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------

class SplatoonDetector:
    """
    Kill detection from the 'XX wo taoshita!' banner.

    Pipeline:
      stage 1 (fast):  dark-ratio pre-filter on thumbnail (<1 ms)
      stage 2 (OCR):   easyocr on upscaled ROI (fires only on text match)
      fallback:        pixel bright-text ratio when easyocr unavailable
    """

    def __init__(
        self,
        kill_dark_ratio:   float = 0.45,
        kill_bright_ratio: float = 0.03,
        cooldown_sec:      float = 3.0,
        use_gpu:           bool  = False,
    ):
        self.kill_dark_ratio   = kill_dark_ratio
        self.kill_bright_ratio = kill_bright_ratio
        self.cooldown_sec      = cooldown_sec
        self.use_gpu           = use_gpu

        self._banner_visible = False
        self._last_kill_t    = -999.0

    # -- stage 1: fast pre-filter ------------------------------------------
    def _prefilter(self, gray_roi) -> bool:
        thumb = cv2.resize(gray_roi, (100, 14), interpolation=cv2.INTER_AREA)
        dark  = float((thumb < 60).sum()) / thumb.size
        return dark >= self.kill_dark_ratio

    # -- stage 2a: easyocr -------------------------------------------------
    def _ocr_has_kill_text(self, roi) -> bool:
        reader = _ensure_reader(self.use_gpu)
        if reader is None:
            return False
        # upscale for better recognition
        gray = _gray(roi)
        h, w = gray.shape
        big  = cv2.resize(gray, (w * 3, h * 3), interpolation=cv2.INTER_CUBIC)
        _, thresh = cv2.threshold(big, 120, 255, cv2.THRESH_BINARY)
        # easyocr accepts numpy arrays
        try:
            results = reader.readtext(thresh, detail=0, paragraph=True)
            text    = " ".join(results)
        except Exception:
            return False
        return any(p in text for p in KILL_PHRASES)

    # -- stage 2b: pixel fallback ------------------------------------------
    def _pixel_has_kill_text(self, gray_roi) -> bool:
        thumb  = cv2.resize(gray_roi, (200, 28), interpolation=cv2.INTER_AREA)
        bright = float((thumb > 160).sum()) / thumb.size
        return bright >= self.kill_bright_ratio

    # -- main entry --------------------------------------------------------
    def feed(self, frame, time_sec: float) -> list:
        roi  = _crop(frame, KILL_ROI)
        gray = _gray(roi)

        if not self._prefilter(gray):
            self._banner_visible = False
            return []

        if _EASYOCR:
            banner_now = self._ocr_has_kill_text(roi)
        else:
            banner_now = self._pixel_has_kill_text(gray)

        result = []
        if banner_now and not self._banner_visible:
            if time_sec - self._last_kill_t > self.cooldown_sec:
                self._last_kill_t = time_sec
                result.append(MarkerEvent(time_sec, "kill", 1.0))

        self._banner_visible = banner_now
        return result


# ---------------------------------------------------------------------------
# GPU helpers
# ---------------------------------------------------------------------------

def check_gpu() -> dict:
    info = {"cuda": False, "opencl": False, "label": "CPU only"}
    try:
        if cv2.cuda.getCudaEnabledDeviceCount() > 0:
            info["cuda"]  = True
            info["label"] = "CUDA ({})".format(cv2.cuda.DeviceInfo(0).name())
            return info
    except Exception:
        pass
    try:
        if cv2.ocl.haveOpenCL():
            cv2.ocl.setUseOpenCL(True)
            info["opencl"] = True
            info["label"]  = "OpenCL ({})".format(cv2.ocl.Device.getDefault().name())
    except Exception:
        pass
    return info


# ---------------------------------------------------------------------------
# Analysis loop
# ---------------------------------------------------------------------------

def analyse_video(path, detector, sample_fps=10.0, progress_cb=None, use_gpu=False):
    if use_gpu:
        info = check_gpu()
        if info["cuda"]:
            try:
                return _analyse_cuda(path, detector, sample_fps, progress_cb)
            except Exception:
                pass
        if info["opencl"]:
            cv2.ocl.setUseOpenCL(True)

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise IOError("Cannot open: " + path)

    fps    = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total  = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    step   = max(1, int(fps / sample_fps))
    idx    = 0
    events = []

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if idx % step == 0:
            events.extend(detector.feed(frame, idx / fps))
            if progress_cb and total > 0:
                progress_cb(idx / total)
        idx += 1

    cap.release()
    if progress_cb:
        progress_cb(1.0)
    return events


def _analyse_cuda(path, detector, sample_fps, progress_cb):
    reader  = cv2.cudacodec.createVideoReader(path)
    fmt     = reader.format()
    fps     = fmt.fps if fmt.fps > 0 else 30.0
    total   = getattr(fmt, "nFrames", 0)
    step    = max(1, int(fps / sample_fps))
    idx     = 0
    events  = []
    gpu_mat = cv2.cuda_GpuMat()

    while True:
        ok, gpu_mat = reader.nextFrame(gpu_mat)
        if not ok:
            break
        if idx % step == 0:
            frame = gpu_mat.download()
            events.extend(detector.feed(frame, idx / fps))
            if progress_cb and total > 0:
                progress_cb(idx / total)
        idx += 1

    if progress_cb:
        progress_cb(1.0)
    return events
