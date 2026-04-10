"""
core/layout_engine.py
---------------------
Step 2: AI Layout Engine

Claude analyzes the plan image + parsed geometry and returns a list of
room markups. Each markup has:
  - Room name + position (matched to parsed geometry)
  - Fill color (by change type)
  - Inside label (short room name)
  - Outside callout text (what needs to change)
  - Callout anchor direction (which side to place the leader line)
"""

import json
import math
import re
import httpx
from dataclasses import dataclass, field
from typing import Optional

from core.parser import PlanGeometry, RoomZone


# ─── Data Structures ──────────────────────────────────────────────────────────

@dataclass
class RoomMarkup:
    """A single room markup with fill, inside label, and outside callout."""
    room_name: str
    change_type: str        # enlarge | relocate | reconfigure | add | remove | swap
    fill_color: tuple
    fill_opacity: float
    stroke_color: tuple
    inside_label: str       # short text inside the room fill
    callout_text: str       # outside callout text
    bbox: Optional[tuple] = None        # (x0, y0, x1, y1) PDF pts
    cx: float = 0.0
    cy: float = 0.0
    callout_dx: float = 60.0
    callout_dy: float = 60.0


@dataclass
class MarkupProposal:
    summary: str
    markups: list[RoomMarkup] = field(default_factory=list)
    plan_angle_deg: float = 0.0


# ─── Change Type Color Palette ────────────────────────────────────────────────

CHANGE_COLORS = {
    "enlarge":     ((0.0,  0.52, 0.78), (0.0,  0.38, 0.65)),   # blue
    "relocate":    ((0.93, 0.42, 0.01), (0.80, 0.30, 0.0)),    # orange
    "reconfigure": ((0.04, 0.55, 0.40), (0.02, 0.40, 0.28)),   # teal
    "add":         ((0.20, 0.65, 0.20), (0.10, 0.50, 0.10)),   # green
    "remove":      ((0.78, 0.15, 0.15), (0.65, 0.05, 0.05)),   # red
    "swap":        ((0.55, 0.20, 0.70), (0.40, 0.10, 0.55)),   # purple
    "default":     ((0.25, 0.25, 0.25), (0.15, 0.15, 0.15)),
}

FILL_OPACITY = 0.30


def _get_change_colors(change_type: str):
    entry = CHANGE_COLORS.get(change_type.lower(), CHANGE_COLORS["default"])
    return entry[0], entry[1]


# ─── System Prompt ────────────────────────────────────────────────────────────

MARKUP_SYSTEM_PROMPT = """You are a senior architect and development advisor for Terra, a Miami-based luxury real estate developer. You are reviewing a unit floor plan and producing a schematic markup showing recommended improvements.

[TERRA PROJECT-SPECIFIC RECONFIGURATION STANDARDS — INSERT HERE BEFORE DEPLOYMENT]

Your task: analyze ALL rooms in the plan and identify every room that could be improved. For each room that needs a change, return a markup entry.

You will receive:
1. A rendered image of the floor plan
2. Structured data listing all detected rooms with their positions

For EVERY room that could be improved, return a markup with:
- The EXACT room name as it appears in the geometry data
- change_type: enlarge | relocate | reconfigure | add | remove | swap
- inside_label: room name only (e.g. "BEDROOM 1")
- callout_text: specific and actionable (e.g. "INCREASE SIZE OF BEDROOM 1", "SWITCH A/C AND W/D LOCATIONS")
- callout_direction: "top" | "bottom" | "left" | "right" — side with most open space for the leader line

Respond ONLY with valid JSON, no preamble or markdown:
{
  "summary": "2-3 sentence overall assessment",
  "markups": [
    {
      "room_name": "BEDROOM 1",
      "change_type": "enlarge",
      "inside_label": "BEDROOM 1",
      "callout_text": "INCREASE SIZE OF BEDROOM 1",
      "callout_direction": "top"
    }
  ]
}

Be thorough — flag every room with a meaningful improvement opportunity. Focus on luxury residential quality: room proportions, adjacencies, privacy, circulation, natural light, and storage."""


# ─── Callout Direction Offsets ────────────────────────────────────────────────

CALLOUT_OFFSETS = {
    "top":    (0,    90),
    "bottom": (0,   -90),
    "left":   (-110, 0),
    "right":  (110,  0),
}


# ─── API Call ─────────────────────────────────────────────────────────────────

CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-sonnet-4-20250514"


def _geometry_to_context(geo: PlanGeometry) -> str:
    lines = [
        f"PAGE: {geo.page_width:.1f} x {geo.page_height:.1f} pts",
        f"PLAN_ANGLE_DEG: {geo.plan_angle_deg:.1f}",
        f"UNIT_TYPE: {geo.unit_type or 'Unknown'}",
        f"UNIT_AREA_SQFT: {geo.unit_area_sqft or 'Unknown'}",
        "",
        "DETECTED_ROOMS (use EXACT names):",
    ]
    for r in geo.rooms:
        lines.append(
            f"  - \"{r.name}\" cx={r.cx:.0f} cy={r.cy:.0f} "
            f"dim={r.dimension_text or 'unlabeled'}"
        )
    return "\n".join(lines)


async def generate_markups(
    geo: PlanGeometry,
    plan_image_b64: str,
    api_key: str,
    unit_label: str = "",
    custom_system_prompt: Optional[str] = None,
) -> MarkupProposal:
    """
    Call Claude API → get room markup proposals → match to parsed geometry.
    Returns MarkupProposal with fully populated RoomMarkup objects.
    """
    system = custom_system_prompt or MARKUP_SYSTEM_PROMPT
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
                        "Identify ALL rooms that could be improved. "
                        "Use EXACT room names from the geometry data. "
                        "Return JSON only."
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

    # ── Match markups to parsed geometry ──────────────────────────────────
    markups: list[RoomMarkup] = []
    room_lookup = {r.name.upper().strip(): r for r in geo.rooms}

    for item in result.get("markups", []):
        room_name = item.get("room_name", "").strip()
        change_type = item.get("change_type", "reconfigure").lower()
        inside_label = item.get("inside_label", room_name)
        callout_text = item.get("callout_text", "")
        callout_dir = item.get("callout_direction", "top")

        fill_rgb, stroke_rgb = _get_change_colors(change_type)

        # Exact match first
        parsed = room_lookup.get(room_name.upper())

        # Fuzzy match fallback
        if not parsed:
            for key, r in room_lookup.items():
                if any(w in key for w in room_name.upper().split() if len(w) > 2):
                    parsed = r
                    break

        if not parsed:
            continue

        dx, dy = CALLOUT_OFFSETS.get(callout_dir, (0, 90))

        markups.append(RoomMarkup(
            room_name=room_name,
            change_type=change_type,
            fill_color=fill_rgb,
            fill_opacity=FILL_OPACITY,
            stroke_color=stroke_rgb,
            inside_label=inside_label,
            callout_text=callout_text,
            bbox=parsed.bbox,
            cx=parsed.cx,
            cy=parsed.cy,
            callout_dx=dx,
            callout_dy=dy,
        ))

    return MarkupProposal(
        summary=result.get("summary", ""),
        markups=markups,
        plan_angle_deg=geo.plan_angle_deg,
    )
