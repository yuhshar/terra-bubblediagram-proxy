"""
core/layout_engine.py
---------------------
Step 2: AI Layout Engine

Two markup types:

  RECONFIGURE  (change_type: enlarge | relocate | reconfigure | add | remove | swap)
    Sheet 1: connector line + text only (NO bubble fill)
    Sheet 2: bubble fill only (NO connector lines or text)

  COMMENT  (change_type: comment)
    Sheet 1: red connector line + red text only
    Sheet 2: not shown

All callout endpoints are placed in the SHEET MARGIN ZONES — the white space
between the unit floor plan and the drawing border/title block. The AI picks
which margin zone (top/bottom/left/right) has the most open space, and the
routing clips the endpoint so it always stays within the printed page.
"""

import json
import re
import httpx
from dataclasses import dataclass, field
from typing import Optional

from core.parser import PlanGeometry


# ---- Data Structures ---------------------------------------------------------

@dataclass
class RoomMarkup:
    room_name: str
    change_type: str        # enlarge|relocate|reconfigure|add|remove|swap|comment
    fill_color: tuple
    fill_opacity: float
    stroke_color: tuple
    inside_label: str
    callout_text: str
    is_comment: bool = False
    bbox: Optional[tuple] = None    # (x0, y0, x1, y1) PDF pts — room location
    cx: float = 0.0
    cy: float = 0.0
    # Leader: start on room bbox edge, end in sheet margin
    leader_x0: float = 0.0
    leader_y0: float = 0.0
    callout_x: float = 0.0
    callout_y: float = 0.0
    callout_side: str = "top"


@dataclass
class MarkupProposal:
    summary: str
    markups: list[RoomMarkup] = field(default_factory=list)
    plan_angle_deg: float = 0.0
    plan_bbox: tuple = (0, 0, 612, 792)


# ---- Color palette -----------------------------------------------------------

CHANGE_COLORS = {
    "enlarge":     ((0.0,  0.52, 0.78), (0.0,  0.38, 0.65)),
    "relocate":    ((0.93, 0.42, 0.01), (0.80, 0.30, 0.0)),
    "reconfigure": ((0.04, 0.55, 0.40), (0.02, 0.40, 0.28)),
    "add":         ((0.20, 0.65, 0.20), (0.10, 0.50, 0.10)),
    "remove":      ((0.78, 0.15, 0.15), (0.65, 0.05, 0.05)),
    "swap":        ((0.55, 0.20, 0.70), (0.40, 0.10, 0.55)),
    "comment":     ((0.82, 0.08, 0.08), (0.72, 0.04, 0.04)),
    "default":     ((0.25, 0.25, 0.25), (0.15, 0.15, 0.15)),
}

FILL_OPACITY    = 0.28
COMMENT_RED     = (0.82, 0.08, 0.08)
RECONFIGURE_TYPES = {"enlarge", "relocate", "reconfigure", "add", "remove", "swap"}


def _get_change_colors(change_type: str):
    return CHANGE_COLORS.get(change_type.lower(), CHANGE_COLORS["default"])


# ---- System Prompt -----------------------------------------------------------

MARKUP_SYSTEM_PROMPT = """You are a senior architect and development advisor for Terra, a Miami-based luxury real estate developer. You are reviewing a unit floor plan and producing a schematic markup.

[TERRA PROJECT-SPECIFIC RECONFIGURATION STANDARDS - INSERT HERE BEFORE DEPLOYMENT]

Classify every markup as one of two types:

TYPE 1 - RECONFIGURE (change_type: enlarge | relocate | reconfigure | add | remove | swap)
  A specific physical layout change. Will show as a bubble diagram on the reconfiguration sheet.
  On the annotated plan sheet it appears as a connector line + text only (no fill).

TYPE 2 - COMMENT (change_type: comment)
  An observation, concern, or verification note. Red connector line + red text only. No bubble ever.

Rules for callout_side:
  Look at the OVERALL DRAWING SHEET. The floor plan sits somewhere on it, surrounded by white margin
  space on all sides (and a title block at the bottom). Assign callout_side based on which margin
  zone around the unit has clear space for this annotation:
  - "top"    = white space above the unit on the sheet
  - "bottom" = white space below the unit (above title block)
  - "left"   = white space to the left of the unit
  - "right"  = white space to the right of the unit (key plan area counts as occupied)
  Spread callouts across all four sides. Never put more than 4-5 on the same side.

Respond ONLY with valid JSON:
{
  "summary": "2-3 sentence assessment",
  "markups": [
    {
      "room_name": "BEDROOM 1",
      "change_type": "enlarge",
      "inside_label": "BEDROOM 1",
      "callout_text": "INCREASE TO MIN 11FT CLEAR",
      "callout_side": "top"
    }
  ]
}"""


# ---- Callout routing: endpoint in sheet margin, never off-page ---------------

# How far into the margin to place the callout endpoint (from plan bbox edge)
MARGIN_DEPTH = 45.0

# Safe insets from page edges (pts) — keeps text away from crop marks / binding
PAGE_INSET_H = 14.0   # horizontal
PAGE_INSET_V = 24.0   # vertical (top: clears header banner; bottom: clears title block)
TITLE_BLOCK_H = 55.0  # estimated height of title block at bottom of sheet


def _route_callout(
    room_cx: float, room_cy: float,
    room_bbox: tuple,
    callout_side: str,
    plan_bbox: tuple,
    page_width: float, page_height: float,
) -> tuple:
    """
    Returns (lx0, ly0, lx1, ly1):
      lx0/ly0 = leader start, on the room bbox edge facing callout_side
      lx1/ly1 = callout endpoint, in the sheet margin on that side

    Endpoint is clamped so it stays within the printable area of the sheet.
    """
    px0, py0, px1, py1 = plan_bbox
    rx0, ry0, rx1, ry1 = room_bbox

    # Safe page bounds (avoids header at top, title block at bottom, crop at sides)
    safe_x0 = PAGE_INSET_H
    safe_x1 = page_width - PAGE_INSET_H
    safe_y0 = TITLE_BLOCK_H
    safe_y1 = page_height - PAGE_INSET_V

    def cx(v): return max(safe_x0, min(safe_x1, v))
    def cy(v): return max(safe_y0, min(safe_y1, v))

    if callout_side == "top":
        lx0, ly0 = room_cx, ry1
        lx1 = cx(room_cx)
        ly1 = cy(py1 + MARGIN_DEPTH)
    elif callout_side == "bottom":
        lx0, ly0 = room_cx, ry0
        lx1 = cx(room_cx)
        ly1 = cy(py0 - MARGIN_DEPTH)
    elif callout_side == "left":
        lx0, ly0 = rx0, room_cy
        lx1 = cx(px0 - MARGIN_DEPTH)
        ly1 = cy(room_cy)
    else:  # right
        lx0, ly0 = rx1, room_cy
        lx1 = cx(px1 + MARGIN_DEPTH)
        ly1 = cy(room_cy)

    return lx0, ly0, lx1, ly1


# ---- API helpers -------------------------------------------------------------

CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL   = "claude-sonnet-4-20250514"


def _geometry_to_context(geo: PlanGeometry) -> str:
    lines = [
        f"PAGE: {geo.page_width:.1f} x {geo.page_height:.1f} pts",
        f"PLAN_BBOX (unit drawing area): x0={geo.plan_bbox[0]:.0f} y0={geo.plan_bbox[1]:.0f} "
        f"x1={geo.plan_bbox[2]:.0f} y1={geo.plan_bbox[3]:.0f}",
        f"  -> margin zones: top={geo.page_height - geo.plan_bbox[3]:.0f}pts  "
        f"bottom={geo.plan_bbox[1]:.0f}pts  "
        f"left={geo.plan_bbox[0]:.0f}pts  "
        f"right={geo.page_width - geo.plan_bbox[2]:.0f}pts",
        f"PLAN_ANGLE_DEG: {geo.plan_angle_deg:.1f}",
        f"UNIT_TYPE: {geo.unit_type or 'Unknown'}",
        f"UNIT_AREA_SQFT: {geo.unit_area_sqft or 'Unknown'}",
        "",
        "DETECTED_ROOMS (use EXACT names):",
    ]
    for r in geo.rooms:
        lines.append(
            f"  - \"{r.name}\"  cx={r.cx:.0f} cy={r.cy:.0f}  "
            f"bbox=[{r.bbox[0]:.0f},{r.bbox[1]:.0f},{r.bbox[2]:.0f},{r.bbox[3]:.0f}]  "
            f"dim={r.dimension_text or 'unlabeled'}"
        )
    return "\n".join(lines)


# ---- Main entry point --------------------------------------------------------

async def generate_markups(
    geo: PlanGeometry,
    plan_image_b64: str,
    api_key: str,
    unit_label: str = "",
    custom_system_prompt: Optional[str] = None,
) -> MarkupProposal:
    system  = custom_system_prompt or MARKUP_SYSTEM_PROMPT
    context = _geometry_to_context(geo)

    messages = [{
        "role": "user",
        "content": [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": plan_image_b64},
            },
            {
                "type": "text",
                "text": (
                    f"Unit type: {unit_label}\n\n"
                    f"GEOMETRY DATA:\n{context}\n\n"
                    "Use EXACT room names from geometry data. "
                    "Spread callout_side across all four sides. "
                    "Return JSON only."
                ),
            },
        ],
    }]

    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(
            CLAUDE_API_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={"model": CLAUDE_MODEL, "max_tokens": 2000, "system": system, "messages": messages},
        )
        resp.raise_for_status()
        data = resp.json()

    raw = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    raw = re.sub(r"```json|```", "", raw).strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Claude returned invalid JSON: {e}\nRaw: {raw[:500]}")

    room_lookup = {r.name.upper().strip(): r for r in geo.rooms}
    markups: list[RoomMarkup] = []

    for item in result.get("markups", []):
        room_name    = item.get("room_name", "").strip()
        change_type  = item.get("change_type", "comment").lower()
        inside_label = item.get("inside_label", room_name)
        callout_text = item.get("callout_text", "")
        callout_side = item.get("callout_side", "top")

        is_comment = (change_type == "comment")
        fill_rgb, stroke_rgb = _get_change_colors(change_type)

        parsed = room_lookup.get(room_name.upper())
        if not parsed:
            for key, r in room_lookup.items():
                if any(w in key for w in room_name.upper().split() if len(w) > 2):
                    parsed = r
                    break
        if not parsed:
            continue

        lx0, ly0, lx1, ly1 = _route_callout(
            room_cx=parsed.cx, room_cy=parsed.cy,
            room_bbox=parsed.bbox,
            callout_side=callout_side,
            plan_bbox=geo.plan_bbox,
            page_width=geo.page_width, page_height=geo.page_height,
        )

        markups.append(RoomMarkup(
            room_name=room_name,
            change_type=change_type,
            fill_color=fill_rgb,
            fill_opacity=FILL_OPACITY,
            stroke_color=stroke_rgb,
            inside_label=inside_label,
            callout_text=callout_text,
            is_comment=is_comment,
            bbox=parsed.bbox,
            cx=parsed.cx, cy=parsed.cy,
            leader_x0=lx0, leader_y0=ly0,
            callout_x=lx1, callout_y=ly1,
            callout_side=callout_side,
        ))

    return MarkupProposal(
        summary=result.get("summary", ""),
        markups=markups,
        plan_angle_deg=geo.plan_angle_deg,
        plan_bbox=geo.plan_bbox,
    )
