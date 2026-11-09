"""JPEG encoding helpers (Pillow based) and ffmpeg quality mapping."""

from __future__ import annotations

import io

from PIL import Image



def encode_jpeg(img: Image.Image, quality: int = 70, subsampling: int = 2) -> bytes:
    """Encode a Pillow image as baseline JPEG.

    subsampling: 0 = 4:4:4 (sharpest text), 1 = 4:2:2, 2 = 4:2:0 (default,
    smallest). Textbook JPEG quality semantics: 70 is a good screen tradeoff.
    """
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=int(quality), subsampling=subsampling,
             optimize=False, progressive=False)
    return buf.getvalue()


def qscale_for_quality(quality: int) -> int:
    """Map a libjpeg-style quality (1-100) onto ffmpeg's mjpeg ``-q:v`` scale.

    ffmpeg's mjpeg encoder uses MPEG-style qscale 2..31 where 2 is best.
    These points were measured on this project against Pillow output sizes
    (1280x720 desktop content, 4:2:0):

        quality 95 -> q2 (~250 kB), 85 -> q4 (~190 kB), 80 -> q5 (~170 kB),
        70 -> q6-7 (~140-160 kB), 60 -> q9 (~115 kB), 50 -> q11 (~60 kB)
    """
    q = max(1, min(100, int(quality)))
    if q >= 95:
        return 2
    if q >= 90:
        return 3
    if q >= 85:
        return 4
    if q >= 78:
        return 5
    if q >= 72:
        return 6
    if q >= 65:
        return 8
    if q >= 58:
        return 10
    if q >= 50:
        return 12
    return max(2, min(31, int(round(31 - q * 0.3))))
