"""
core/renderer.py
----------------
Step 3: PDF Markup Renderer

Sheet 1 — Annotated Plan:
  - RECONFIGURE markups: colored semi-transparent fill + inside label + leader line + callout text
  - COMMENT markups: red leader line + red callout text ONLY (no fill, no bubble)
  - All callout endpoints are outside the plan bounding box (pre-computed in layout_engine)
  - Leader lines use an elbow (two segments) when needed to avoid overlapping the plan
  - Terra teal header banner + legend

Sheet 2 — Reconfiguration Study (only added if there are any reconfigure-type markups):
  - Clean white page with only the reconfigure bubbles drawn, larger and more legible
  - Used as a schematic study sheet for design team discussion

Merges overlay onto original vector PDF page using pypdf.
"""

import io
import math
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

RED        = Color(*COMMENT_RED, alpha=1.0)
RED_LIGHT  = Color(*COMMENT_RED, alpha=0.15)
RED_LINE   = Color(*COMMENT_RED, alpha=0.90)


# ---- Leader line drawing -----------------------------------------------------

def _draw_leader(
    c: canvas.Canvas,
    x0: float, y0: float,   # start: on room bbox edge
    x1: float, y1: float,   # end: outside plan boundary
    color: Color,
    line_width: float = 0.8,
    dot_radius: float = 2.2,
    dot_color: Optional[Color] = None,
):
    """
    Draw a two-segment elbow leader line from (x0,y0) to (x1,y1).
    The elbow mid-point is chosen to first travel perpendicular to the
    callout direction, then parallel, keeping the line clean.
    """
    c.saveState()
    c.setStrokeColor(color)
    c.setLineWidth(line_width)

    # Determine if the leader is predominantly horizontal or vertical
    dx = abs(x1 - x0)
    dy = abs(y1 - y0)

    if dx < 2 or dy < 2:
        # Essentially straight — draw single segment
        c.line(x0, y0, x1, y1)
    else:
        # Elbow: go vertically first, then horizontally (or vice versa)
        # Choose based on which direction the leader exits the plan
        if dy >= dx:
            # Vertical dominant: go vertical to y1 level, then horizontal
            mid_x, mid_y = x0, y1
        else:
            # Horizontal dominant: go horizontal to x1, then vertical
            mid_x, mid_y = x1, y0

        p = c.beginPath()
        p.moveTo(x0, y0)
        p.lineTo(mid_x, mid_y)
        p.lineTo(x1, y1)
        c.drawPath(p, stroke=1, fill=0)

    # Dot at room anchor point
    c.setFillColor(dot_color or color)
    c.circle(x0, y0, dot_radius, fill=1, stroke=0)

    c.restoreState()


# ---- Callout text drawing ----------------------------------------------------

def _draw_callout_text(
    c: canvas.Canvas,
    text: str,
    x1: float, y1: float,
    callout_side: str,
    color: Color,
    font_size: float = 7.0,
):
    """
    Draw callout text near (x1, y1) offset away from the leader endpoint.
    Text is positioned so it never overlaps the leader line.
    """
    c.saveState()
    c.setFont("Helvetica-Bold", font_size)
    c.setFillColor(color)

    tw = c.stringWidth(text, "Helvetica-Bold", font_size)
    gap = 4.0

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


# ---- Reconfigure bubble drawing ----------------------------------------------

def _draw_reconfig_bubble(
    c: canvas.Canvas,
    markup: RoomMarkup,
    plan_angle_deg: float,
    label_font_size: Optional[float] = None,
):
    """Draw semi-transparent fill + stroke + inside label for a reconfigure markup."""
    if not markup.bbox:
        return

    x0, y0, x1, y1 = markup.bbox
    w, h = x1 - x0, y1 - y0

    fill   = _rgb(markup.fill_color, markup.fill_opacity)
    stroke = _rgb(markup.stroke_color, 0.88)

    c.saveState()
    c.setFillColor(fill)
    c.setStrokeColor(stroke)
    c.setLineWidth(1.5)
    c.rect(x0, y0, w, h, fill=1, stroke=1)

    # Inside label
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    fs = label_font_size or max(6.0, min(10.0, min(abs(w), abs(h)) * 0.18))
    text = markup.inside_label.upper()
    text_color = Color(
        markup.stroke_color[0] * 0.5,
        markup.stroke_color[1] * 0.5,
        markup.stroke_color[2] * 0.5,
        alpha=0.95,
    )
    c.translate(cx, cy)
    c.rotate(plan_angle_deg)
    c.setFont("Helvetica-Bold", fs)
    c.setFillColor(text_color)
    tw = c.stringWidth(text, "Helvetica-Bold", fs)
    c.drawString(-tw / 2, -fs / 3, text)
    c.restoreState()


# ---- Legend ------------------------------------------------------------------

def _draw_legend(
    c: canvas.Canvas,
    proposal: MarkupProposal,
    page_width: float,
    page_height: float,
):
    """Compact legend — shows reconfigure change types + comment indicator."""
    reconfig_types = list(dict.fromkeys(
        m.change_type for m in proposal.markups
        if m.change_type in RECONFIGURE_TYPES
    ))
    has_comments = any(m.is_comment for m in proposal.markups)

    rows = reconfig_types + (["comment"] if has_comments else [])
    if not rows:
        return

    entry_h = 12.0
    pad = 7.0
    swatch = 8.0
    legend_w = 140.0
    legend_h = pad * 2 + len(rows) * entry_h + 16

    lx = page_width - legend_w - 10
    ly = 10.0

    c.saveState()
    c.setFillColor(Color(1, 1, 1, alpha=0.88))
    c.setStrokeColor(Color(0.7, 0.7, 0.7, alpha=0.5))
    c.setLineWidth(0.4)
    c.roundRect(lx, ly, legend_w, legend_h, 3, fill=1, stroke=1)

    c.setFont("Helvetica-Bold", 7.0)
    c.setFillColor(Color(0.2, 0.2, 0.2, alpha=0.9))
    c.drawString(lx + pad, ly + legend_h - pad - 7, "MARKUP LEGEND")

    c.setStrokeColor(Color(0.8, 0.8, 0.8, alpha=0.5))
    c.setLineWidth(0.3)
    c.line(lx + pad, ly + legend_h - pad - 11,
           lx + legend_w - pad, ly + legend_h - pad - 11)

    for i, ct in enumerate(rows):
        ey = ly + legend_h - pad - 20 - i * entry_h
        if ct == "comment":
            # Red line swatch
            c.setStrokeColor(RED_LINE)
            c.setLineWidth(1.0)
            c.line(lx + pad, ey + swatch / 2, lx + pad + swatch, ey + swatch / 2)
            c.setFillColor(RED)
            c.circle(lx + pad, ey + swatch / 2, 2.0, fill=1, stroke=0)
            c.setFont("Helvetica", 6.5)
            c.setFillColor(Color(0.2, 0.2, 0.2, alpha=0.9))
            c.drawString(lx + pad + swatch + 4, ey + 1.5, "COMMENT / VERIFY")
        else:
            colors = CHANGE_COLORS.get(ct, CHANGE_COLORS["default"])
            fill_rgb, stroke_rgb = colors[0], colors[1]
            c.setFillColor(Color(*fill_rgb, alpha=0.55))
            c.setStrokeColor(Color(*stroke_rgb, alpha=0.85))
            c.setLineWidth(0.7)
            c.rect(lx + pad, ey, swatch, swatch, fill=1, stroke=1)
            c.setFont("Helvetica", 6.5)
            c.setFillColor(Color(0.2, 0.2, 0.2, alpha=0.9))
            c.drawString(lx + pad + swatch + 4, ey + 1.5, ct.upper())

    c.restoreState()


# ---- Header banner -----------------------------------------------------------

def _draw_header_banner(
    c: canvas.Canvas,
    title: str,
    subtitle: str,
    page_width: float,
    page_height: float,
):
    bh = 20.0
    by = page_height - bh

    c.saveState()
    c.setFillColor(Color(0.059, 0.314, 0.255, alpha=0.92))
    c.rect(0, by, page_width, bh, fill=1, stroke=0)

    c.setFont("Helvetica-Bold", 7.5)
    c.setFillColor(Color(1, 1, 1, alpha=0.95))
    c.drawString(10, by + 7, title)

    sub = subtitle[:160] + "..." if len(subtitle) > 160 else subtitle
    c.setFont("Helvetica", 6.0)
    c.setFillColor(Color(1, 1, 1, alpha=0.72))
    c.drawString(10, by + 1, sub)
    c.restoreState()


# ---- Sheet 1: annotated plan overlay -----------------------------------------

def _render_sheet1(
    c: canvas.Canvas,
    proposal: MarkupProposal,
    pw: float,
    ph: float,
):
    """
    Draw all markup annotations onto an overlay canvas (same size as original page).
    - Reconfigure markups: bubble fill + inside label + elbow leader + callout text
    - Comment markups: red elbow leader + red callout text only
    """
    # Layer 1: reconfigure fills (drawn first so they sit below leaders)
    for m in proposal.markups:
        if not m.is_comment:
            _draw_reconfig_bubble(c, m, proposal.plan_angle_deg)

    # Layer 2: all leader lines
    for m in proposal.markups:
        if m.is_comment:
            line_color = RED_LINE
            dot_color  = RED
        else:
            line_color = _rgb(m.stroke_color, 0.90)
            dot_color  = _rgb(m.fill_color, 0.95)

        _draw_leader(
            c,
            m.leader_x0, m.leader_y0,
            m.callout_x, m.callout_y,
            color=line_color,
            dot_color=dot_color,
        )

    # Layer 3: all callout texts
    for m in proposal.markups:
        text_color = RED if m.is_comment else _rgb(m.stroke_color, 0.92)
        _draw_callout_text(
            c,
            m.callout_text.upper(),
            m.callout_x, m.callout_y,
            m.callout_side,
            text_color,
        )

    _draw_legend(c, proposal, pw, ph)
    _draw_header_banner(
        c,
        "TERRA — SCHEMATIC MARKUP  |  SHEET 1 OF 2  — ANNOTATED PLAN",
        proposal.summary,
        pw, ph,
    )


# ---- Sheet 2: reconfiguration study ------------------------------------------

def _render_sheet2(
    c: canvas.Canvas,
    proposal: MarkupProposal,
    pw: float,
    ph: float,
):
    """
    Blank white sheet showing only reconfigure bubbles, drawn larger.
    Used as a schematic reconfiguration study page.
    """
    reconfig = [m for m in proposal.markups if not m.is_comment and m.bbox]
    if not reconfig:
        return

    # White background
    c.setFillColor(Color(1, 1, 1, alpha=1))
    c.rect(0, 0, pw, ph, fill=1, stroke=0)

    # Draw each reconfigure bubble with a larger inside label
    for m in reconfig:
        _draw_reconfig_bubble(c, m, proposal.plan_angle_deg, label_font_size=9.0)

        # Leader line + callout on this sheet too
        line_color = _rgb(m.stroke_color, 0.90)
        dot_color  = _rgb(m.fill_color, 0.95)
        _draw_leader(
            c,
            m.leader_x0, m.leader_y0,
            m.callout_x, m.callout_y,
            color=line_color,
            dot_color=dot_color,
            line_width=1.0,
        )
        _draw_callout_text(
            c,
            m.callout_text.upper(),
            m.callout_x, m.callout_y,
            m.callout_side,
            _rgb(m.stroke_color, 0.92),
            font_size=8.0,
        )

    _draw_header_banner(
        c,
        "TERRA — SCHEMATIC MARKUP  |  SHEET 2 OF 2  — RECONFIGURATION STUDY",
        "Reconfigure-type markups only. Comments and verifications on Sheet 1.",
        pw, ph,
    )


# ---- Main Renderer -----------------------------------------------------------

def render_markup_overlay(
    original_pdf_bytes: bytes,
    proposal: MarkupProposal,
    page_index: int = 0,
    page_height_pts: Optional[float] = None,
    page_width_pts: Optional[float] = None,
) -> bytes:
    """
    Render markup overlay onto original PDF.

    Output:
      Sheet 1: original plan + all annotations (comments + reconfig bubbles)
      Sheet 2: white page with reconfigure bubbles only (if any reconfigs present)

    Returns bytes of the new marked-up PDF.
    """
    original_reader = PdfReader(io.BytesIO(original_pdf_bytes))
    original_page   = original_reader.pages[page_index]

    if page_width_pts is None or page_height_pts is None:
        media_box      = original_page.mediabox
        page_width_pts = float(media_box.width)
        page_height_pts = float(media_box.height)

    pw = page_width_pts
    ph = page_height_pts

    # ---- Build Sheet 1 overlay -----------------------------------------------
    s1_buf = io.BytesIO()
    c1 = canvas.Canvas(s1_buf, pagesize=(pw, ph))
    _render_sheet1(c1, proposal, pw, ph)
    c1.save()
    s1_buf.seek(0)

    s1_overlay_page = PdfReader(s1_buf).pages[0]
    original_page.merge_page(s1_overlay_page)

    # ---- Build Sheet 2 (reconfiguration study) --------------------------------
    has_reconfigs = any(not m.is_comment for m in proposal.markups)

    writer = PdfWriter()
    writer.add_page(original_page)   # Sheet 1

    if has_reconfigs:
        s2_buf = io.BytesIO()
        c2 = canvas.Canvas(s2_buf, pagesize=(pw, ph))
        _render_sheet2(c2, proposal, pw, ph)
        c2.save()
        s2_buf.seek(0)
        s2_page = PdfReader(s2_buf).pages[0]
        writer.add_page(s2_page)     # Sheet 2

    writer.add_metadata({
        "/Creator": "Terra Unit Plan Reviewer",
        "/Producer": "Terra — Schematic Markup Engine v2",
        "/Subject": proposal.summary[:80],
    })

    out_buf = io.BytesIO()
    writer.write(out_buf)
    out_buf.seek(0)
    return out_buf.read()
