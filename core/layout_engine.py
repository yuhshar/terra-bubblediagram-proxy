"""
core/layout_engine.py
---------------------
Step 2: AI Layout Engine

Claude analyzes the plan image + parsed geometry and returns two kinds of markups:

  RECONFIGURE markups  (change_type: enlarge | relocate | reconfigure | add | remove | swap)
    -> Colored semi-transparent bubble drawn over the room
    -> Colored leader line + callout text routed OUTSIDE the plan boundary
    -> Appear on Sheet 1 (annotated plan) and Sheet 2 (reconfiguration study)

  COMMENT markups  (change_type: comment)
    -> NO bubble fill — red leader line and red callout text only
    -> Leader line routes OUTSIDE the plan boundary
    -> Appear on Sheet 1 only
"""

import json
import math
import re
import httpx
from dataclasses import dataclass, field
from typing import Optional

from core.parser import PlanGeometry, RoomZone


# ---- Data Structures ---------------------------------------------------------

@dataclass
class RoomMarkup:
    """A single room markup: either a reconfigure bubble or a comment annotation."""
    room_name: str
    change_type: str        # enlarge | relocate | reconfigure | add | remove | swap | comment
    fill_color: tuple
    fill_opacity: float
    stroke_color: tuple
    inside_label: str       # short text inside bubble (reconfigure only)
    callout_text: str       # outside callout text
    is_comment: bool = False        # True -> red text/line only, no bubble
    bbox: Optional[tuple] = None    # (x0, y0, x1, y1) PDF pts
    cx: float = 0.0
    cy: float = 0.0
    # Leader line start (on room bbox edge) and end (outside plan boundary)
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
    plan_bbox: tuple = (0, 0, 612, 792)   # (x0, y0, x1, y1) of drawing area


# ---- Change Type Color Palette -----------------------------------------------

CHANGE_COLORS = {
    "enlarge":     ((0.0,  0.52, 0.78), (0.0,  0.38, 0.65)),   # blue
    "relocate":    ((0.93, 0.42, 0.01), (0.80, 0.30, 0.0)),    # orange
    "reconfigure": ((0.04, 0.55, 0.40), (0.02, 0.40, 0.28)),   # teal
    "add":         ((0.20, 0.65, 0.20), (0.10, 0.50, 0.10)),   # green
    "remove":      ((0.78, 0.15, 0.15), (0.65, 0.05, 0.05)),   # red-fill
    "swap":        ((0.55, 0.20, 0.70), (0.40, 0.10, 0.55)),   # purple
    "comment":     ((0.82, 0.08, 0.08), (0.72, 0.04, 0.04)),   # red (unused for fill)
    "default":     ((0.25, 0.25, 0.25), (0.15, 0.15, 0.15)),
}

FILL_OPACITY = 0.28
COMMENT_RED  = (0.82, 0.08, 0.08)   # unified red for comment lines/text

RECONFIGURE_TYPES = {"enlarge", "relocate", "reconfigure", "add", "remove", "swap"}


def _get_change_colors(change_type: str):
    entry = CHANGE_COLORS.get(change_type.lower(), CHANGE_COLORS["default"])
    return entry[0], entry[1]


# ---- System Prompt -----------------------------------------------------------

MARKUP_SYSTEM_PROMPT = """You are a senior architect and development advisor for Terra, a Miami-based luxury real estate developer. You are reviewing a unit floor plan and producing a schematic markup showing recommended improvements.

[TERRA PROJECT-SPECIFIC RECONFIGURATION STANDARDS - INSERT HERE BEFORE DEPLOYMENT]

Your task: analyze ALL rooms in the plan and identify every room that could be improved.

You must classify each markup as one of two types:

TYPE 1 - RECONFIGURE (change_type: enlarge | relocate | reconfigure | add | remove | swap)
  Use ONLY when you are recommending a specific physical layout change: resize a room, move it,
  add a missing element, remove something, or swap two rooms.
  These will be drawn as colored bubble diagrams overlaid on the room.

TYPE 2 - COMMENT (change_type: comment)
  Use for any concern, observation, or verification note that does NOT require a specific physical
  reconfiguration. Examples: "VERIFY EGRESS WINDOW SIZE", "CONFIRM ACOUSTIC SEPARATION",
  "CHECK CLEARANCE AT ENTRY", "REVIEW BALCONY DEPTH".
  These will be drawn as red annotation lines with NO bubble — just a red leader line and red text.

For EVERY room that could be improved, return a markup entry with:
- room_name: EXACT name as it appears in the geometry data
- change_type: one of the values above
- inside_label: short room name for bubble label (used on reconfigure types only)
- callout_text: specific, uppercase, actionable note (max ~8 words)
- callout_side: "top" | "bottom" | "left" | "right"
  Choose the side of the OVERALL PLAN that has the most open margin space.
  Actively spread callouts across all four sides to avoid crowding any one side.

Respond ONLY with valid JSON, no preamble or markdown:
{
  "summary": "2-3 sentence overall assessment",
  "markups": [
    {
      "room_name": "BEDROOM 1",
      "change_type": "enlarge",
      "inside_label": "BEDROOM 1",
      "callout_text": "INCREASE TO MIN 11FT CLEAR",
      "callout_side": "top"
    },
    {
      "room_name": "MASTER BATHROOM",
      "change_type": "comment",
      "inside_label": "",
      "callout_text": "VERIFY SOAKING TUB CLEARANCE",
      "callout_side": "right"
    }
  ]
}

Be thorough. Spread callouts across all four plan edges. Focus on luxury residential quality."""


# ---- Callout Routing ---------------------------------------------------------

MARGIN_OUTSIDE = 52.0    # pts of clearance beyond plan bbox edge
HEADER_RESERVE = 22.0    # pts to keep clear at top for the header banner
FOOTER_RESERVE = 14.0    # pts to keep clear at bottom


def _route_callout(
    room_cx: float,
    room_cy: float,
    room_bbox: tuple,
    callout_side: str,
    plan_bbox: tuple,
    page_width: float,
    page_height: float,
) -> tuple:
    """
    Returns (lx0, ly0, lx1, ly1):
      lx0/ly0 = leader line start on the room bbox edge
      lx1/ly1 = callout endpoint placed OUTSIDE the plan bounding box

    The endpoint is always beyond plan_bbox by MARGIN_OUTSIDE pts on the
    requested side, keeping the text in the page margin area.
    """
    px0, py0, px1, py1 = plan_bbox
    rx0, ry0, rx1, ry1 = room_bbox

    if callout_side == "top":
        lx0, ly0 = room_cx, ry1
        lx1 = _clamp(room_cx, 10, page_width - 10)
        ly1 = min(py1 + MARGIN_OUTSIDE, page_height - HEADER_RESERVE - 4)
    elif callout_side == "bottom":
        lx0, ly0 = room_cx, ry0
        lx1 = _clamp(room_cx, 10, page_width - 10)
        ly1 = max(py0 - MARGIN_OUTSIDE, FOOTER_RESERVE + 4)
    elif callout_side == "left":
        lx0, ly0 = rx0, room_cy
        lx1 = max(px0 - MARGIN_OUTSIDE, 10)
        ly1 = _clamp(room_cy, FOOTER_RESERVE, page_height - HEADER_RESERVE)
    else:  # right
        lx0, ly0 = rx1, room_cy
        lx1 = min(px1 + MARGIN_OUTSIDE, page_width - 10)
        ly1 = _clamp(room_cy, FOOTER_RESERVE, page_height - HEADER_RESERVE)

    return lx0, ly0, lx1, ly1


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


# ---- Geometry context builder ------------------------------------------------

CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL   = "claude-sonnet-4-20250514"


def _geometry_to_context(geo: PlanGeometry) -> str:
    lines = [
        f"PAGE: {geo.page_width:.1f} x {geo.page_height:.1f} pts",
        f"PLAN_BBOX: x0={geo.plan_bbox[0]:.0f} y0={geo.plan_bbox[1]:.0f} "
        f"x1={geo.plan_bbox[2]:.0f} y1={geo.plan_bbox[3]:.0f}",
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
    """
    Call Claude API -> get room markup proposals -> match to parsed geometry
    -> route all callout endpoints outside the plan boundary.
    Returns MarkupProposal with fully populated RoomMarkup objects.
    """
    system  = custom_system_prompt or MARKUP_SYSTEM_PROMPT
    context = _geometry_to_context(geo)

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": plan_image_b64,
                    },
                },
                {
                    "type": "text",
                    "text": (
                        f"Unit type: {unit_label}\n\n"
                        f"GEOMETRY DATA:\n{context}\n\n"
                        "Classify each markup as 'reconfigure' type (physical layout change -> bubble) "
                        "or 'comment' type (observation/note -> red text line only, NO bubble). "
                        "Spread callout_side across all four plan edges to avoid crowding. "
                        "Use EXACT room names from geometry data. Return JSON only."
                    ),
                },
            ],
        }
    ]

    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(
            CLAUDE_API_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 2000,
                "system": system,
                "messages": messages,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    raw = "".join(
        block.get("text", "") for block in data.get("content", [])
        if block.get("type") == "text"
    )
    raw = re.sub(r"```json|```", "", raw).strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Claude returned invalid JSON: {e}\nRaw: {raw[:500]}")

    # ---- Match markups to parsed geometry ------------------------------------
    markups: list[RoomMarkup] = []
    room_lookup = {r.name.upper().strip(): r for r in geo.rooms}

    for item in result.get("markups", []):
        room_name    = item.get("room_name", "").strip()
        change_type  = item.get("change_type", "comment").lower()
        inside_label = item.get("inside_label", room_name)
        callout_text = item.get("callout_text", "")
        callout_side = item.get("callout_side", "top")

        is_comment  = (change_type == "comment")
        fill_rgb, stroke_rgb = _get_change_colors(change_type)

        # Exact match first, then fuzzy
        parsed = room_lookup.get(room_name.upper())
        if not parsed:
            for key, r in room_lookup.items():
                if any(w in key for w in room_name.upper().split() if len(w) > 2):
                    parsed = r
                    break

        if not parsed:
            continue

        lx0, ly0, lx1, ly1 = _route_callout(
            room_cx=parsed.cx,
            room_cy=parsed.cy,
            room_bbox=parsed.bbox,
            callout_side=callout_side,
            plan_bbox=geo.plan_bbox,
            page_width=geo.page_width,
            page_height=geo.page_height,
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
            cx=parsed.cx,
            cy=parsed.cy,
            leader_x0=lx0,
            leader_y0=ly0,
            callout_x=lx1,
            callout_y=ly1,
            callout_side=callout_side,
        ))

    return MarkupProposal(
        summary=result.get("summary", ""),
        markups=markups,
        plan_angle_deg=geo.plan_angle_deg,
        plan_bbox=geo.plan_bbox,
    )
