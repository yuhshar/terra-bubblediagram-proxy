"""
core/renderer.py
----------------
Step 3: PDF Markup Renderer

Draws on each marked-up room:
  1. Semi-transparent colored fill over the room bbox
  2. Stroke border (same color, higher opacity)
  3. Inside label — room name centered in the fill, rotated to plan angle
  4. Leader line — from room centroid toward callout direction
  5. Callout text — at end of leader line, uppercase, colored

Merges overlay onto original vector PDF page using pypdf.
"""

import io
import math
from typing import Optional

from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.colors import Color

from core.layout_engine import MarkupProposal, RoomMarkup


# ─── Drawing Helpers ──────────────────────────────────────────────────────────

def _draw_room_fill(
    c: canvas.Canvas,
    markup: RoomMarkup,
):
    """Draw semi-transparent filled rectangle over the room."""
    if not markup.bbox:
        return
    x0, y0, x1, y1 = markup.bbox
    w = x1 - x0
    h = y1 - y0

    fill = Color(*markup.fill_color, alpha=markup.fill_opacity)
    stroke = Color(*markup.stroke_color, alpha=0.88)

    c.saveState()
    c.setFillColor(fill)
    c.setStrokeColor(stroke)
    c.setLineWidth(1.5)
    c.rect(x0, y0, w, h, fill=1, stroke=1)
    c.restoreState()


def _draw_inside_label(
    c: canvas.Canvas,
    markup: RoomMarkup,
    plan_angle_deg: float,
):
    """Draw room name centered inside the fill, rotated to plan angle."""
    if not markup.bbox:
        return

    x0, y0, x1, y1 = markup.bbox
    cx = (x0 + x1) / 2
    cy = (y0 + y1) / 2
    box_w = abs(x1 - x0)
    box_h = abs(y1 - y0)

    # Font size relative to box size
    fs = max(6.0, min(10.0, min(box_w, box_h) * 0.18))

    text = markup.inside_label.upper()
    text_color = Color(
        markup.stroke_color[0] * 0.5,
        markup.stroke_color[1] * 0.5,
        markup.stroke_color[2] * 0.5,
        alpha=0.95,
    )

    c.saveState()
    c.translate(cx, cy)
    c.rotate(plan_angle_deg)
    c.setFont("Helvetica-Bold", fs)
    c.setFillColor(text_color)
    tw = c.stringWidth(text, "Helvetica-Bold", fs)
    c.drawString(-tw / 2, -fs / 3, text)
    c.restoreState()


def _draw_leader_and_callout(
    c: canvas.Canvas,
    markup: RoomMarkup,
    plan_angle_deg: float,
):
    """
    Draw a leader line from room edge toward callout direction,
    then the callout text at the end.
    """
    if not markup.bbox:
        return

    x0, y0, x1, y1 = markup.bbox
    cx = (x0 + x1) / 2
    cy = (y0 + y1) / 2

    # Leader line start: edge of bbox in callout direction
    dx, dy = markup.callout_dx, markup.callout_dy

    # Start point on bbox edge
    if dx > 0:
        lx0 = x1
    elif dx < 0:
        lx0 = x0
    else:
        lx0 = cx

    if dy > 0:
        ly0 = y1
    elif dy < 0:
        ly0 = y0
    else:
        ly0 = cy

    # End point
    lx1 = lx0 + dx
    ly1 = ly0 + dy

    stroke = Color(*markup.stroke_color, alpha=0.90)
    dot_fill = Color(*markup.fill_color, alpha=0.95)

    c.saveState()

    # Leader line
    c.setStrokeColor(stroke)
    c.setLineWidth(0.8)
    c.line(lx0, ly0, lx1, ly1)

    # Dot at start
    c.setFillColor(dot_fill)
    c.circle(lx0, ly0, 2.5, fill=1, stroke=0)

    # Callout text
    callout = markup.callout_text.upper()
    fs = 7.0
    c.setFont("Helvetica-Bold", fs)
    c.setFillColor(stroke)
    tw = c.stringWidth(callout, "Helvetica-Bold", fs)

    # Position text at end of leader, offset so it doesn't overlap line
    if dx > 0:
        tx = lx1 + 3
        c.drawString(tx, ly1 - fs / 3, callout)
    elif dx < 0:
        tx = lx1 - tw - 3
        c.drawString(tx, ly1 - fs / 3, callout)
    else:
        # Vertical leader — center text
        tx = lx1 - tw / 2
        if dy > 0:
            c.drawString(tx, ly1 + 3, callout)
        else:
            c.drawString(tx, ly1 - fs - 3, callout)

    c.restoreState()


def _draw_legend(
    c: canvas.Canvas,
    proposal: MarkupProposal,
    page_width: float,
    page_height: float,
):
    """Compact legend in bottom-right showing change type color swatches."""
    from core.layout_engine import CHANGE_COLORS

    # Deduplicate change types used
    used_types = list(dict.fromkeys(m.change_type for m in proposal.markups))
    if not used_types:
        return

    entry_h = 12.0
    pad = 7.0
    swatch = 8.0
    legend_w = 130.0
    legend_h = pad * 2 + len(used_types) * entry_h + 16

    lx = page_width - legend_w - 10
    ly = 10.0

    c.saveState()
    c.setFillColor(Color(1, 1, 1, alpha=0.85))
    c.setStrokeColor(Color(0.7, 0.7, 0.7, alpha=0.6))
    c.setLineWidth(0.4)
    c.roundRect(lx, ly, legend_w, legend_h, 3, fill=1, stroke=1)

    c.setFont("Helvetica-Bold", 7.0)
    c.setFillColor(Color(0.2, 0.2, 0.2, alpha=0.9))
    c.drawString(lx + pad, ly + legend_h - pad - 7, "MARKUP LEGEND")

    c.setStrokeColor(Color(0.8, 0.8, 0.8, alpha=0.6))
    c.setLineWidth(0.3)
    c.line(lx + pad, ly + legend_h - pad - 11,
           lx + legend_w - pad, ly + legend_h - pad - 11)

    for i, ct in enumerate(used_types):
        colors = CHANGE_COLORS.get(ct, CHANGE_COLORS["default"])
        fill_rgb, stroke_rgb = colors[0], colors[1]
        ey = ly + legend_h - pad - 20 - i * entry_h

        c.setFillColor(Color(*fill_rgb, alpha=0.55))
        c.setStrokeColor(Color(*stroke_rgb, alpha=0.85))
        c.setLineWidth(0.7)
        c.rect(lx + pad, ey, swatch, swatch, fill=1, stroke=1)

        c.setFont("Helvetica", 6.5)
        c.setFillColor(Color(0.2, 0.2, 0.2, alpha=0.9))
        c.drawString(lx + pad + swatch + 4, ey + 1.5, ct.upper())

    c.restoreState()


def _draw_header_banner(
    c: canvas.Canvas,
    proposal: MarkupProposal,
    page_width: float,
    page_height: float,
):
    """Terra teal header strip at top of page."""
    bh = 20.0
    by = page_height - bh

    c.saveState()
    c.setFillColor(Color(0.059, 0.314, 0.255, alpha=0.90))
    c.rect(0, by, page_width, bh, fill=1, stroke=0)

    c.setFont("Helvetica-Bold", 7.5)
    c.setFillColor(Color(1, 1, 1, alpha=0.95))
    c.drawString(10, by + 7, "TERRA — SCHEMATIC MARKUP")

    summary_short = proposal.summary[:150] + "…" if len(proposal.summary) > 150 else proposal.summary
    c.setFont("Helvetica", 6.0)
    c.setFillColor(Color(1, 1, 1, alpha=0.75))
    c.drawString(10, by + 1, summary_short)
    c.restoreState()


# ─── Main Renderer ────────────────────────────────────────────────────────────

def render_markup_overlay(
    original_pdf_bytes: bytes,
    proposal: MarkupProposal,
    page_index: int = 0,
    page_height_pts: Optional[float] = None,
    page_width_pts: Optional[float] = None,
) -> bytes:
    """
    Render room markup overlay onto original PDF page.
    Returns bytes of the new marked-up PDF.
    """
    original_reader = PdfReader(io.BytesIO(original_pdf_bytes))
    original_page = original_reader.pages[page_index]

    if page_width_pts is None or page_height_pts is None:
        media_box = original_page.mediabox
        page_width_pts = float(media_box.width)
        page_height_pts = float(media_box.height)

    pw = page_width_pts
    ph = page_height_pts

    overlay_buffer = io.BytesIO()
    c = canvas.Canvas(overlay_buffer, pagesize=(pw, ph))

    # Draw fills first (bottom layer)
    for markup in proposal.markups:
        _draw_room_fill(c, markup)

    # Draw inside labels
    for markup in proposal.markups:
        _draw_inside_label(c, markup, proposal.plan_angle_deg)

    # Draw leader lines + callout text on top
    for markup in proposal.markups:
        _draw_leader_and_callout(c, markup, proposal.plan_angle_deg)

    # Legend and header
    _draw_legend(c, proposal, pw, ph)
    _draw_header_banner(c, proposal, pw, ph)

    c.save()
    overlay_buffer.seek(0)

    overlay_reader = PdfReader(overlay_buffer)
    overlay_page = overlay_reader.pages[0]
    original_page.merge_page(overlay_page)

    writer = PdfWriter()
    for i, page in enumerate(original_reader.pages):
        writer.add_page(original_page if i == page_index else page)

    writer.add_metadata({
        "/Creator": "Terra Unit Plan Reviewer",
        "/Producer": "Terra — Schematic Markup Engine v2",
        "/Subject": proposal.summary[:80],
    })

    out_buffer = io.BytesIO()
    writer.write(out_buffer)
    out_buffer.seek(0)
    return out_buffer.read()
