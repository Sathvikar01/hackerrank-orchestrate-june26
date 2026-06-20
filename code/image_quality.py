"""Classical image quality analysis using OpenCV.

Produces a deterministic set of quality flags that map onto the project risk_flags.
"""

import logging
from pathlib import Path
from typing import Dict, List

import cv2
import numpy as np
from PIL import Image


BLUR_THRESHOLD = 50.0
LOW_LIGHT_THRESHOLD = 25.0
GLARE_BRIGHT_THRESHOLD = 245
GLARE_FRACTION_THRESHOLD = 0.30
DARK_THRESHOLD = 20.0
OBSTRUCTION_BLACK_FRACTION = 0.35


logger = logging.getLogger(__name__)


def _to_bgr(path: Path, max_dim: int = 768) -> np.ndarray:
    with Image.open(path) as img:
        img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        arr = np.array(img)
    return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)


def analyze_image(path: Path) -> Dict[str, bool]:
    """Return a dict of detected quality flags for a single image."""
    bgr = _to_bgr(path)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

    # Blur via Laplacian variance
    lap_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    blurry = lap_var < BLUR_THRESHOLD

    # Brightness (mean of grayscale)
    mean_brightness = float(gray.mean())
    low_light = mean_brightness < LOW_LIGHT_THRESHOLD

    # Glare: fraction of pixels above bright threshold
    bright_fraction = float((gray > GLARE_BRIGHT_THRESHOLD).mean())
    glare = bright_fraction > GLARE_FRACTION_THRESHOLD

    # Crop / obstruction heuristic: very dark border regions or mostly dark image
    h, w = gray.shape
    border = np.concatenate(
        [
            gray[0:max(2, h // 20), :].flatten(),
            gray[-max(2, h // 20):, :].flatten(),
            gray[:, 0:max(2, w // 20)].flatten(),
            gray[:, -max(2, w // 20):].flatten(),
        ]
    )
    border_mean = float(border.mean()) if border.size else 255.0
    black_fraction = float((gray < DARK_THRESHOLD).mean())
    cropped = border_mean < DARK_THRESHOLD or black_fraction > OBSTRUCTION_BLACK_FRACTION

    return {
        "blurry_image": blurry,
        "low_light_or_glare": low_light or glare,
        "cropped_or_obstructed": cropped,
    }


def analyze_images(paths: List[Path]) -> Dict[str, bool]:
    """Aggregate quality flags across all images in a claim."""
    flags = {"blurry_image": False, "low_light_or_glare": False, "cropped_or_obstructed": False}
    if not paths:
        return flags
    for p in paths:
        try:
            result = analyze_image(p)
            for k, v in result.items():
                flags[k] = flags[k] or v
        except Exception as exc:
            logger.warning("OpenCV quality analysis failed for %s: %s", p, exc)
            flags["cropped_or_obstructed"] = True
    return flags
