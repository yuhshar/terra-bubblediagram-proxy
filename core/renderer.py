"""
core/renderer.py
----------------
Step 3: PDF Markup Renderer

Takes the original vector PDF + ReconfigurationProposal and produces
a new PDF with semi-transparent polygonal bubble diagrams baked in as
a vector overlay layer.

Rendering approach:
  - Uses reportlab to draw bubbles onto a transparent overlay PDF page
  - Uses pypdf to merge the overlay onto the original plan page
  - Preserves all original vector geometry underneath

Bubble visual style:
  - Semi-transparent filled polygons (angular vertices, NOT ellipses)
  - Colored stroke outline at higher opacity
  - Rotated room name label centered in each bubble
  - Optional note text below label in smaller font
  - A legend panel in the margin listing all room types
"""

import io
import math
from typing import Optional

from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.colors import Color
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont


from core.layout_engine import ReconfigurationProposal, BubblePolygon


# ─── Font Setup ───────────────────────────────────────────────────────────────

# Use Helvetica (built-in, always available) — clean sans-serif for diagrams
LABEL_FONT = "Helvetica-Bold"
NOTE_FONT = "Helvetica"
LEGEND_FONT = "Helvetica"


# ─── Drawing Helpers ──────────────────────────────────────────────────────────

def _draw_angular_polygon(
    c: canvas.Canvas,
    points: list[tuple[float, float]],
    fill_rgb: tuple,
    fill_opacity: float,
    stroke_rgb: tuple,
    stroke_opacity: float = 0.85,
    stroke_width: float = 1.4,
):
    """
    Draw a filled + stroked polygon with angular vertices.
    Uses reportlab's path API for crisp vector output.
    """
    if len(points) < 3:
        return

    # Fill pass
    fill_color = Color(fill_rgb[0], fill_rgb[1], fill_rgb[2], alpha=fill_opacity)
    stroke_color = Color(stroke_rgb[0], stroke_rgb[1], stroke_rgb[2], alpha=stroke_opacity)

    p = c.beginPath()
    p.moveTo(points[0][0], points[0][1])
    for px, py in points[1:]:
        p.lineTo(px, py)
    p.close()

    c.saveState()
    c.setFillColor(fill_color)
    c.setStrokeColor(stroke_color)
    c.setLineWidth(stroke_width)
    c.setLineJoin(0)   # miter join — sharp angular corners
    c.drawPath(p, fill=1, stroke=1)
    c.restoreState()


def _draw_bubble_label(
    c: canvas.Canvas,
    bubble: BubblePolygon,
    page_height: float,
):
    """
    Draw room name label centered in the bubble, rotated to plan angle.
    Optionally draws a smaller note below the room name.
    """
    lx = bubble.label_x
    ly = bubble.label_y
    angle = bubble.label_angle

    # Compute font size relative to bubble area
    xs = [p[0] for p in bubble.points]
    ys = [p[1] for p in bubble.points]
    approx_w = max(xs) - min(xs)
    approx_h = max(ys) - min(ys)
    approx_diag = math.hypot(approx_w, approx_h)
    label_fs = max(6.5, min(11.0, approx_diag * 0.095))
    note_fs = max(5.5, label_fs * 0.72)

    c.saveState()
    c.translate(lx, ly)
    c.rotate(angle)

    # Room name
    c.setFont(LABEL_FONT, label_fs)
    stroke_rgb = bubble.stroke_color
    # Dark version of stroke for readable text
    text_color = Color(
        stroke_rgb[0] * 0.55,
        stroke_rgb[1] * 0.55,
        stroke_rgb[2] * 0.55,
        alpha=0.92,
    )
    c.setFillColor(text_color)
    c.setStrokeColor(text_color)

    label_text = bubble.room_name.upper()
    tw = c.stringWidth(label_text, LABEL_FONT, label_fs)

    # Slight offset up if note present
    y_offset = note_fs * 0.7 if bubble.note else 0

    c.drawString(-tw / 2, y_offset, label_text)

    # Note text
    if bubble.note:
        c.setFont(NOTE_FONT, note_fs)
        note_color = Color(
            stroke_rgb[0] * 0.45,
            stroke_rgb[1] * 0.45,
            stroke_rgb[2] * 0.45,
            alpha=0.75,
        )
        c.setFillColor(note_color)
        nw = c.stringWidth(bubble.note, NOTE_FONT, note_fs)
        c.drawString(-nw / 2, y_offset - label_fs * 1.2, bubble.note)

    c.restoreState()


def _draw_legend(
    c: canvas.Canvas,
    proposal: ReconfigurationProposal,
    page_width: float,
    page_height: float,
):
    """
    Draw a compact legend panel in the bottom-right corner of the page.
    Lists each bubble room type with its color swatch.
    """
    if not proposal.bubbles:
        return

    # Deduplicate by room_type
    seen = {}
    for b in proposal.bubbles:
        if b.room_type not in seen:
            seen[b.room_type] = b

    entries = list(seen.values())
    entry_h = 13.0
    pad = 8.0
    swatch_size = 8.0
    legend_w = 140.0
    legend_h = pad * 2 + len(entries) * entry_h + 18

    # Position: bottom-right margin
    lx = page_width - legend_w - 12
    ly = 12.0

    # Background panel
    c.saveState()
    bg = Color(1, 1, 1, alpha=0.82)
    border = Color(0.7, 0.7, 0.7, alpha=0.6)
    c.setFillColor(bg)
    c.setStrokeColor(border)
    c.setLineWidth(0.5)
    c.roundRect(lx, ly, legend_w, legend_h, 4, fill=1, stroke=1)

    # Title
    c.setFont("Helvetica-Bold", 7.5)
    title_color = Color(0.25, 0.25, 0.25, alpha=0.9)
    c.setFillColor(title_color)
    c.drawString(lx + pad, ly + legend_h - pad - 8, "RECONFIGURATION DIAGRAM")

    # Divider
    c.setStrokeColor(Color(0.8, 0.8, 0.8, alpha=0.7))
    c.setLineWidth(0.4)
    c.line(lx + pad, ly + legend_h - pad - 12,
           lx + legend_w - pad, ly + legend_h - pad - 12)

    # Entries
    for i, bubble in enumerate(entries):
        ey = ly + legend_h - pad - 22 - i * entry_h
        # Swatch
        fill_c = Color(*bubble.fill_color, alpha=0.55)
        stroke_c = Color(*bubble.stroke_color, alpha=0.85)
        c.setFillColor(fill_c)
        c.setStrokeColor(stroke_c)
        c.setLineWidth(0.8)
        c.rect(lx + pad, ey, swatch_size, swatch_size, fill=1, stroke=1)
        # Label
        c.setFont(LEGEND_FONT, 7.0)
        c.setFillColor(Color(0.2, 0.2, 0.2, alpha=0.9))
        display_name = bubble.room_name.upper()
        c.drawString(lx + pad + swatch_size + 5, ey + 1, display_name)

    c.restoreState()


def _draw_summary_header(
    c: canvas.Canvas,
    proposal: ReconfigurationProposal,
    page_width: float,
    page_height: float,
):
    """
    Draw a small summary banner at the top of the page.
    """
    banner_h = 22.0
    by = page_height - banner_h

    c.saveState()
    bg = Color(0.059, 0.314, 0.255, alpha=0.88)   # Terra teal
    c.setFillColor(bg)
    c.rect(0, by, page_width, banner_h, fill=1, stroke=0)

    c.setFont("Helvetica-Bold", 7.5)
    c.setFillColor(Color(1, 1, 1, alpha=0.95))
    c.drawString(10, by + 8, "TERRA — SCHEMATIC RECONFIGURATION PROPOSAL")

    # Summary text truncated to fit
    summary_short = proposal.summary[:140] + "…" if len(proposal.summary) > 140 else proposal.summary
    c.setFont("Helvetica", 6.5)
    c.setFillColor(Color(1, 1, 1, alpha=0.78))
    c.drawString(10, by + 1, summary_short)

    c.restoreState()


# ─── Main Renderer ────────────────────────────────────────────────────────────

def render_bubble_overlay(
    original_pdf_bytes: bytes,
    proposal: ReconfigurationProposal,
    page_index: int = 0,
    page_height_pts: Optional[float] = None,
    page_width_pts: Optional[float] = None,
) -> bytes:
    """
    Render bubble diagram overlay onto original PDF page.

    Args:
        original_pdf_bytes: Raw bytes of the source vector PDF
        proposal: ReconfigurationProposal from layout_engine.py
        page_index: Which page to overlay (0-based)
        page_height_pts: Page height in PDF points (from parser)
        page_width_pts: Page width in PDF points (from parser)

    Returns:
        Bytes of the new PDF with bubble overlay baked in
    """
    # ── Read original PDF ──────────────────────────────────────────────────
    original_reader = PdfReader(io.BytesIO(original_pdf_bytes))
    original_page = original_reader.pages[page_index]

    # Get page dimensions from original if not provided
    if page_width_pts is None or page_height_pts is None:
        media_box = original_page.mediabox
        page_width_pts = float(media_box.width)
        page_height_pts = float(media_box.height)

    pw = page_width_pts
    ph = page_height_pts

    # ── Build overlay PDF in memory ────────────────────────────────────────
    overlay_buffer = io.BytesIO()
    c = canvas.Canvas(overlay_buffer, pagesize=(pw, ph))

    # Draw each bubble polygon
    for bubble in proposal.bubbles:
        _draw_angular_polygon(
            c,
            bubble.points,
            fill_rgb=bubble.fill_color,
            fill_opacity=bubble.fill_opacity,
            stroke_rgb=bubble.stroke_color,
            stroke_opacity=0.88,
            stroke_width=1.5,
        )

    # Draw labels on top of all fills
    for bubble in proposal.bubbles:
        _draw_bubble_label(c, bubble, ph)

    # Draw legend
    _draw_legend(c, proposal, pw, ph)

    # Draw summary banner
    _draw_summary_header(c, proposal, pw, ph)

    c.save()
    overlay_buffer.seek(0)

    # ── Merge overlay onto original ────────────────────────────────────────
    overlay_reader = PdfReader(overlay_buffer)
    overlay_page = overlay_reader.pages[0]

    # Merge: original stays as base, overlay drawn on top
    original_page.merge_page(overlay_page)

    # ── Build output PDF ───────────────────────────────────────────────────
    writer = PdfWriter()

    # Add all pages; only the selected page gets the overlay
    for i, page in enumerate(original_reader.pages):
        if i == page_index:
            writer.add_page(original_page)
        else:
            writer.add_page(page)

    # Preserve metadata
    writer.add_metadata({
        "/Creator": "Terra Unit Plan Reviewer",
        "/Producer": "Terra — Bubble Diagram Engine v1",
        "/Subject": f"Schematic Reconfiguration — {proposal.summary[:80]}",
    })

    out_buffer = io.BytesIO()
    writer.write(out_buffer)
    out_buffer.seek(0)
    return out_buffer.read()
