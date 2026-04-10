"""
core/renderer.py
----------------
Step 3: PDF Markup Renderer

SHEET 1 — Annotated Plan (original PDF + overlay):
  - ALL markups (reconfigure + comment): connector line + callout text ONLY
  - NO bubble fills on this sheet — clean annotation layer over the original plan
  - Reconfigure annotations: colored line + colored text
  - Comment annotations: red line + red text
  - Terra header banner + legend

SHEET 2 — Reconfiguration Study (blank white page, only if reconfigs exist):
  - ONLY reconfigure-type markups, drawn as colored bubble fills inside the unit
  - NO connector lines, NO callout text — just the room fills as a visual study
  - Terra header banner
"""

import io
from typing import Optional

from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.colors import Color

from core.layout_engine import (
    MarkupProposal, RoomMarkup,
    CHANGE_COLORS, COMMENT_RED, RECONFIGURE_TYPES,
)


# ---- Color helpers -----------------------------------------------------------

def _rgb(t: tuple, alpha: float = 1.0) -> Color:
    return Color(*t, alpha=alpha)

RED_LINE = _rgb(COMMENT_RED, 0.88)
RED_TEXT = _rgb(COMMENT_RED, 1.0)


# ---- Shared: elbow leader line -----------------------------------------------

def _draw_leader(
    c: canvas.Canvas,
    x0: float, y0: float,
    x1: float, y1: float,
    color: Color,
    line_width: float = 0.75,
    dot_radius: float = 2.0,
):
    """
    Two-segment elbow from (x0,y0) on room edge to (x1,y1) in sheet margin.
    Travels perpendicular to the exit side first, then parallel.
    """
    c.saveState()
    c.setStrokeColor(color)
    c.setLineWidth(line_width)

    dx = abs(x1 - x0)
    dy = abs(y1 - y0)

    if dx < 2 or dy < 2:
        c.line(x0, y0, x1, y1)
    else:
        # Vertical-dominant exit: go vertical first, then horizontal
        if dy >= dx:
            mid_x, mid_y = x0, y1
        else:
            mid_x, mid_y = x1, y0
        p = c.beginPath()
        p.moveTo(x0, y0)
        p.lineTo(mid_x, mid_y)
        p.lineTo(x1, y1)
        c.drawPath(p, stroke=1, fill=0)

    # Dot at room anchor
    c.setFillColor(color)
    c.circle(x0, y0, dot_radius, fill=1, stroke=0)
    c.restoreState()


# ---- Shared: callout text ----------------------------------------------------

def _draw_callout_text(
    c: canvas.Canvas,
    text: str,
    x1: float, y1: float,
    callout_side: str,
    color: Color,
    font_size: float = 6.5,
):
    c.saveState()
    c.setFont("Helvetica-Bold", font_size)
    c.setFillColor(color)
    tw = c.stringWidth(text, "Helvetica-Bold", font_size)
    gap = 3.5

    if callout_side == "top":
        tx, ty = x1 - tw / 2, y1 + gap
    elif callout_side == "bottom":
        tx, ty = x1 - tw / 2, y1 - font_size - gap
    elif callout_side == "left":
        tx, ty = x1 - tw - gap, y1 - font_size / 3
    else:  # right
        tx, ty = x1 + gap, y1 - font_size / 3

    c.drawString(tx, ty, text)
    c.restoreState()


# ---- Sheet 1: annotation layer (lines + text, NO bubble fills) ---------------

def _render_sheet1_overlay(
    c: canvas.Canvas,
    proposal: MarkupProposal,
    pw: float, ph: float,
):
    """
    Draws ONLY connector lines and callout text.
    No bubble fills on this sheet at all — keeps the original plan readable.
    """
    for m in proposal.markups:
        if m.is_comment:
            line_color = RED_LINE
            text_color = RED_TEXT
        else:
            _, stroke_rgb = CHANGE_COLORS.get(m.change_type, CHANGE_COLORS["default"])
            line_color = _rgb(stroke_rgb, 0.88)
            text_color = _rgb(stroke_rgb, 1.0)

        _draw_leader(c, m.leader_x0, m.leader_y0, m.callout_x, m.callout_y, line_color)
        _draw_callout_text(c, m.callout_text.upper(), m.callout_x, m.callout_y, m.callout_side, text_color)

    _draw_legend_sheet1(c, proposal, pw, ph)
    _draw_header(c, "TERRA — SCHEMATIC MARKUP  |  SHEET 1 — ANNOTATED PLAN", proposal.summary, pw, ph)


# ---- Sheet 2: bubble study (fills only, NO lines or text) --------------------

def _render_sheet2(
    c: canvas.Canvas,
    proposal: MarkupProposal,
    pw: float, ph: float,
):
    """
    Blank white page. Draws ONLY colored room fills (bubble diagrams).
    No connector lines, no callout text — pure visual reconfiguration study.
    """
    # White background
    c.setFillColor(Color(1, 1, 1))
    c.rect(0, 0, pw, ph, fill=1, stroke=0)

    for m in proposal.markups:
        if m.is_comment or not m.bbox:
            continue

        x0, y0, x1, y1 = m.bbox
        w, h = x1 - x0, y1 - y0

        fill_rgb, stroke_rgb = CHANGE_COLORS.get(m.change_type, CHANGE_COLORS["default"])
        c.saveState()
        c.setFillColor(_rgb(fill_rgb, 0.35))
        c.setStrokeColor(_rgb(stroke_rgb, 0.90))
        c.setLineWidth(1.5)
        c.rect(x0, y0, w, h, fill=1, stroke=1)

        # Room label inside bubble
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        fs = max(7.0, min(11.0, min(abs(w), abs(h)) * 0.20))
        text = m.inside_label.upper()
        text_color = Color(
            stroke_rgb[0] * 0.45,
            stroke_rgb[1] * 0.45,
            stroke_rgb[2] * 0.45,
            alpha=0.95,
        )
        c.translate(cx, cy)
        c.rotate(proposal.plan_angle_deg)
        c.setFont("Helvetica-Bold", fs)
        c.setFillColor(text_color)
        tw = c.stringWidth(text, "Helvetica-Bold", fs)
        c.drawString(-tw / 2, -fs / 3, text)
        c.restoreState()

    _draw_header(
        c,
        "TERRA — SCHEMATIC MARKUP  |  SHEET 2 — RECONFIGURATION STUDY",
        "Proposed room reconfigurations shown as colored overlays. See Sheet 1 for full annotation.",
        pw, ph,
    )


# ---- Legend (Sheet 1 only) ---------------------------------------------------

def _draw_legend_sheet1(
    c: canvas.Canvas,
    proposal: MarkupProposal,
    pw: float, ph: float,
):
    reconfig_types = list(dict.fromkeys(
        m.change_type for m in proposal.markups if m.change_type in RECONFIGURE_TYPES
    ))
    has_comments = any(m.is_comment for m in proposal.markups)
    rows = reconfig_types + (["comment"] if has_comments else [])
    if not rows:
        return

    entry_h = 12.0
    pad, swatch = 7.0, 8.0
    legend_w = 145.0
    legend_h = pad * 2 + len(rows) * entry_h + 16

    lx = pw - legend_w - 10
    ly = 10.0

    c.saveState()
    c.setFillColor(Color(1, 1, 1, alpha=0.90))
    c.setStrokeColor(Color(0.7, 0.7, 0.7, alpha=0.5))
    c.setLineWidth(0.4)
    c.roundRect(lx, ly, legend_w, legend_h, 3, fill=1, stroke=1)

    c.setFont("Helvetica-Bold", 7.0)
    c.setFillColor(Color(0.15, 0.15, 0.15))
    c.drawString(lx + pad, ly + legend_h - pad - 7, "MARKUP LEGEND")
    c.setStrokeColor(Color(0.8, 0.8, 0.8))
    c.setLineWidth(0.3)
    c.line(lx + pad, ly + legend_h - pad - 11, lx + legend_w - pad, ly + legend_h - pad - 11)

    for i, ct in enumerate(rows):
        ey = ly + legend_h - pad - 20 - i * entry_h
        if ct == "comment":
            c.setStrokeColor(_rgb(COMMENT_RED, 0.88))
            c.setLineWidth(1.0)
            c.line(lx + pad, ey + swatch / 2, lx + pad + swatch, ey + swatch / 2)
            c.setFillColor(_rgb(COMMENT_RED, 1.0))
            c.circle(lx + pad, ey + swatch / 2, 2.0, fill=1, stroke=0)
            label = "COMMENT / VERIFY"
        else:
            fill_rgb, stroke_rgb = CHANGE_COLORS.get(ct, CHANGE_COLORS["default"])
            c.setStrokeColor(_rgb(stroke_rgb, 0.88))
            c.setLineWidth(1.5)
            cx_s = lx + pad + swatch / 2
            cy_s = ey + swatch / 2
            c.line(cx_s - swatch / 2, cy_s, cx_s + swatch / 2, cy_s)
            c.setFillColor(_rgb(stroke_rgb, 1.0))
            c.circle(cx_s - swatch / 2, cy_s, 2.0, fill=1, stroke=0)
            label = ct.upper()

        c.setFont("Helvetica", 6.5)
        c.setFillColor(Color(0.15, 0.15, 0.15))
        c.drawString(lx + pad + swatch + 4, ey + 1.5, label)

    c.restoreState()


# ---- Header banner -----------------------------------------------------------

def _draw_header(
    c: canvas.Canvas,
    title: str, subtitle: str,
    pw: float, ph: float,
):
    bh = 20.0
    by = ph - bh
    c.saveState()
    c.setFillColor(Color(0.059, 0.314, 0.255, alpha=0.93))
    c.rect(0, by, pw, bh, fill=1, stroke=0)
    c.setFont("Helvetica-Bold", 7.5)
    c.setFillColor(Color(1, 1, 1, alpha=0.95))
    c.drawString(10, by + 7, title)
    sub = (subtitle[:160] + "...") if len(subtitle) > 160 else subtitle
    c.setFont("Helvetica", 6.0)
    c.setFillColor(Color(1, 1, 1, alpha=0.70))
    c.drawString(10, by + 1, sub)
    c.restoreState()


# ---- Main renderer -----------------------------------------------------------

def render_markup_overlay(
    original_pdf_bytes: bytes,
    proposal: MarkupProposal,
    page_index: int = 0,
    page_height_pts: Optional[float] = None,
    page_width_pts: Optional[float] = None,
) -> bytes:
    original_reader = PdfReader(io.BytesIO(original_pdf_bytes))
    original_page   = original_reader.pages[page_index]

    if page_width_pts is None or page_height_pts is None:
        mb = original_page.mediabox
        page_width_pts  = float(mb.width)
        page_height_pts = float(mb.height)

    pw, ph = page_width_pts, page_height_pts

    # Sheet 1: overlay annotation lines + text onto original plan
    s1_buf = io.BytesIO()
    c1 = canvas.Canvas(s1_buf, pagesize=(pw, ph))
    _render_sheet1_overlay(c1, proposal, pw, ph)
    c1.save()
    s1_buf.seek(0)
    original_page.merge_page(PdfReader(s1_buf).pages[0])

    writer = PdfWriter()
    writer.add_page(original_page)

    # Sheet 2: blank page with bubble fills only (if any reconfigs)
    has_reconfigs = any(not m.is_comment for m in proposal.markups)
    if has_reconfigs:
        s2_buf = io.BytesIO()
        c2 = canvas.Canvas(s2_buf, pagesize=(pw, ph))
        _render_sheet2(c2, proposal, pw, ph)
        c2.save()
        s2_buf.seek(0)
        writer.add_page(PdfReader(s2_buf).pages[0])

    writer.add_metadata({
        "/Creator": "Terra Unit Plan Reviewer",
        "/Producer": "Terra — Schematic Markup Engine v3",
        "/Subject": proposal.summary[:80],
    })

    out_buf = io.BytesIO()
    writer.write(out_buf)
    out_buf.seek(0)
    return out_buf.read()
