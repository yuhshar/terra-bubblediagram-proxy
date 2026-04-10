"""
utils/pdf_to_image.py
---------------------
Converts a single PDF page to a base64 JPEG string for the Claude vision API.

Uses pdf2image (wraps poppler's pdftoppm) at high DPI for crisp floor plan
detail. Falls back to PyMuPDF (fitz) if pdf2image is unavailable.
"""

import base64
import io
from typing import Optional

DPI = 200          # High enough for floor plan label legibility
MAX_EDGE_PX = 2400  # Cap to keep base64 payload reasonable


def render_page_to_jpeg_b64(
    pdf_bytes: bytes,
    page_index: int = 0,
    dpi: int = DPI,
) -> str:
    """
    Render a single PDF page to a JPEG and return as base64 string.

    Tries pdf2image first (poppler-based, best quality for vector PDFs),
    then falls back to PyMuPDF (fitz).

    Args:
        pdf_bytes: Raw PDF bytes
        page_index: 0-based page index
        dpi: Render resolution

    Returns:
        Base64-encoded JPEG string (no data: prefix)
    """
    try:
        return _render_pdf2image(pdf_bytes, page_index, dpi)
    except ImportError:
        pass
    except Exception:
        pass

    try:
        return _render_pymupdf(pdf_bytes, page_index, dpi)
    except ImportError:
        raise RuntimeError(
            "Neither pdf2image nor PyMuPDF (fitz) is installed. "
            "Install one: pip install pdf2image  OR  pip install pymupdf"
        )


def _render_pdf2image(pdf_bytes: bytes, page_index: int, dpi: int) -> str:
    from pdf2image import convert_from_bytes

    images = convert_from_bytes(
        pdf_bytes,
        dpi=dpi,
        first_page=page_index + 1,
        last_page=page_index + 1,
        fmt="jpeg",
        jpegopt={"quality": 92, "optimize": True},
    )
    if not images:
        raise ValueError(f"pdf2image returned no images for page {page_index}")

    img = images[0]
    img = _cap_image_size(img)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _render_pymupdf(pdf_bytes: bytes, page_index: int, dpi: int) -> str:
    import fitz  # PyMuPDF

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    if page_index >= len(doc):
        raise ValueError(f"Page {page_index} not in PDF ({len(doc)} pages)")

    page = doc[page_index]
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)

    img_bytes = pix.tobytes("jpeg")
    doc.close()

    # Cap size
    from PIL import Image
    img = Image.open(io.BytesIO(img_bytes))
    img = _cap_image_size(img)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _cap_image_size(img):
    """Resize image so longest edge <= MAX_EDGE_PX."""
    from PIL import Image
    w, h = img.size
    longest = max(w, h)
    if longest > MAX_EDGE_PX:
        scale = MAX_EDGE_PX / longest
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    return img
