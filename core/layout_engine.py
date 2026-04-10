"""
core/layout_engine.py
---------------------
Step 2: AI Layout Engine

Takes a PlanGeometry + base64 plan image and calls the Claude API.
Claude returns a reconfiguration proposal as structured JSON containing
polygon points in PDF coordinate space, aligned to the plan's rotation angle.

The system prompt placeholder is where Terra will inject project-specific
reconfiguration standards.
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
class BubblePolygon:
    """
    A single reconfiguration bubble.
    Points are in PDF coordinate space (origin bottom-left, units = points).
    Shape has angular vertices (not ellipses) — typically 5-8 sided polygons
    that follow the plan's rotation geometry.
    """
    room_name: str
    room_type: str          # living, bedroom, primary_bedroom, kitchen, etc.
    points: list[tuple[float, float]]  # [(x, y), ...] closed polygon
    fill_color: tuple[float, float, float]  # RGB 0-1
    fill_opacity: float     # 0-1
    stroke_color: tuple[float, float, float]
    label_x: float          # label anchor point
    label_y: float
    label_angle: float      # text rotation to match plan angle
    note: Optional[str] = None   # brief annotation e.g. "Expand 2ft N"


@dataclass
class ReconfigurationProposal:
    summary: str
    rationale: str
    bubbles: list[BubblePolygon] = field(default_factory=list)
    overall_angle_deg: float = 0.0   # dominant plan rotation used


# ─── Room Type Color Palette ──────────────────────────────────────────────────
# Semi-transparent Terra brand-adjacent colors per room type

ROOM_COLORS = {
    "living":            ((0.059, 0.314, 0.255), (0.055, 0.431, 0.337)),   # teal
    "dining":            ((0.059, 0.314, 0.255), (0.055, 0.431, 0.337)),
    "kitchen":           ((0.522, 0.310, 0.067), (0.937, 0.624, 0.153)),   # amber
    "bedroom":           ((0.176, 0.306, 0.663), (0.282, 0.451, 0.839)),   # blue
    "primary_bedroom":   ((0.384, 0.114, 0.494), (0.573, 0.216, 0.678)),   # purple
    "bathroom":          ((0.043, 0.396, 0.514), (0.129, 0.604, 0.718)),   # cyan
    "primary_bathroom":  ((0.043, 0.396, 0.514), (0.129, 0.604, 0.718)),
    "den":               ((0.310, 0.404, 0.114), (0.490, 0.604, 0.235)),   # olive
    "office":            ((0.310, 0.404, 0.114), (0.490, 0.604, 0.235)),
    "balcony":           ((0.114, 0.420, 0.404), (0.200, 0.631, 0.604)),   # seafoam
    "entry":             ((0.400, 0.388, 0.369), (0.600, 0.588, 0.565)),   # gray
    "storage":           ((0.400, 0.388, 0.369), (0.600, 0.588, 0.565)),
    "laundry":           ((0.400, 0.388, 0.369), (0.600, 0.588, 0.565)),
    "mechanical":        ((0.400, 0.388, 0.369), (0.600, 0.588, 0.565)),
    "default":           ((0.200, 0.200, 0.200), (0.400, 0.400, 0.400)),
}

FILL_OPACITY = 0.28
STROKE_OPACITY = 0.85


def _get_room_colors(room_type: str):
    entry = ROOM_COLORS.get(room_type.lower(), ROOM_COLORS["default"])
    return entry[0], entry[1]  # fill_rgb, stroke_rgb


# ─── Polygon Geometry Helpers ─────────────────────────────────────────────────

def _rotate_point(x, y, cx, cy, angle_rad):
    """Rotate point (x,y) around center (cx,cy) by angle_rad."""
    cos_a = math.cos(angle_rad)
    sin_a = math.sin(angle_rad)
    dx, dy = x - cx, y - cy
    return (cx + dx * cos_a - dy * sin_a,
            cy + dx * sin_a + dy * cos_a)


def _make_angular_polygon(
    cx: float, cy: float,
    width: float, height: float,
    angle_deg: float,
    num_points: int = 6,
    jitter_factor: float = 0.12,
) -> list[tuple[float, float]]:
    """
    Generate a schematic-style angular polygon (bubble diagram shape).
    - NOT a smooth ellipse — has distinct vertices
    - Rotated to follow the plan geometry angle
    - Slight irregular jitter on vertex radii for hand-drawn feel
    - num_points: 5-8 recommended for organic-but-angular look
    """
    import random
    rng = random.Random(hash((cx, cy, width, height)))

    angle_rad = math.radians(angle_deg)
    half_w = width / 2
    half_h = height / 2
    points = []

    for i in range(num_points):
        # Distribute vertices evenly around ellipse, then snap to angular
        theta = (2 * math.pi * i / num_points)
        # Base point on ellipse
        bx = half_w * math.cos(theta)
        by = half_h * math.sin(theta)
        # Apply jitter for angular/irregular vertex feel
        jitter = 1.0 + rng.uniform(-jitter_factor, jitter_factor)
        bx *= jitter
        by *= jitter
        # Rotate to plan angle
        rx, ry = _rotate_point(bx, by, 0, 0, angle_rad)
        points.append((cx + rx, cy + ry))

    return points


def _bbox_to_polygon_dims(
    bbox: tuple, plan_angle_deg: float
) -> tuple[float, float, float, float, float]:
    """
    Given a room bbox (x0,y0,x1,y1) and plan angle,
    return (cx, cy, width, height, angle_deg) for polygon generation.
    The polygon will be axis-aligned to the plan geometry.
    """
    x0, y0, x1, y1 = bbox
    cx = (x0 + x1) / 2
    cy = (y0 + y1) / 2
    # Shrink slightly so bubbles don't perfectly fill room box
    w = abs(x1 - x0) * 0.82
    h = abs(y1 - y0) * 0.82
    return cx, cy, w, h, plan_angle_deg


# ─── System Prompt ────────────────────────────────────────────────────────────

BUBBLE_SYSTEM_PROMPT = """You are a senior architect and development advisor for Terra, a Miami-based luxury real estate developer. You are reviewing a unit floor plan to propose an optimal programmatic reconfiguration for schematic design.

[TERRA PROJECT-SPECIFIC RECONFIGURATION STANDARDS — INSERT HERE BEFORE DEPLOYMENT]

Your task is to analyze the existing room layout and propose an improved configuration. You will receive:
1. A rendered image of the floor plan
2. Structured data about existing rooms (names, positions, dimensions) in PDF coordinate space
3. The plan's dominant rotation angle

Your reconfiguration proposal must:
- Respect the unit's perimeter boundary (do not move exterior walls)
- Propose new room shapes as POLYGONS in the SAME PDF coordinate space provided
- All polygon points must be rotated to match the plan_angle_deg provided
- Each polygon should be 5-8 vertices — angular shapes, NOT circles or ellipses
- Optimize for: luxury residential quality, light/view access, circulation efficiency, privacy hierarchy
- Provide a brief rationale for each room change

Respond ONLY with valid JSON, no preamble or markdown:
{
  "summary": "2-3 sentence overall reconfiguration rationale",
  "rationale": "detailed explanation of key moves",
  "rooms": [
    {
      "room_name": "Primary Suite",
      "room_type": "primary_bedroom",
      "note": "Expanded 2ft toward balcony for better proportions",
      "bbox": [x0, y0, x1, y1],
      "polygon_points": [[x1,y1],[x2,y2],[x3,y3],[x4,y4],[x5,y5],[x6,y6]]
    }
  ]
}

CRITICAL: All coordinates must be in PDF point units matching the coordinate space of the existing rooms data provided. The plan_angle_deg indicates the rotation — all polygons must follow this angle so bubbles align with the floor plan geometry."""


# ─── API Call ─────────────────────────────────────────────────────────────────

CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-sonnet-4-20250514"


def _geometry_to_prompt_context(geo: PlanGeometry) -> str:
    """Serialise PlanGeometry to a concise context string for Claude."""
    lines = [
        f"PAGE: {geo.page_width:.1f} x {geo.page_height:.1f} pts",
        f"PLAN_ANGLE_DEG: {geo.plan_angle_deg:.1f}",
        f"DRAWING_BBOX: {[round(v,1) for v in geo.plan_bbox]}",
        f"UNIT_TYPE: {geo.unit_type or 'Unknown'}",
        f"UNIT_AREA_SQFT: {geo.unit_area_sqft or 'Unknown'}",
        "",
        "EXISTING_ROOMS:",
    ]
    for r in geo.rooms:
        bbox_str = [round(v, 1) for v in r.bbox]
        lines.append(
            f"  - name={r.name!r} cx={r.cx:.1f} cy={r.cy:.1f} "
            f"bbox={bbox_str} dim={r.dimension_text!r} sqft={r.area_sqft}"
        )
    return "\n".join(lines)


async def generate_reconfiguration(
    geo: PlanGeometry,
    plan_image_b64: str,
    api_key: str,
    unit_label: str = "",
    custom_system_prompt: Optional[str] = None,
) -> ReconfigurationProposal:
    """
    Call Claude API with plan image + geometry context.
    Returns a ReconfigurationProposal with BubblePolygon objects
    in PDF coordinate space, aligned to plan_angle_deg.

    Args:
        geo: Parsed plan geometry from parser.py
        plan_image_b64: Base64 JPEG of the rendered plan page
        api_key: Anthropic API key
        unit_label: e.g. "3BR / 3.5BA"
        custom_system_prompt: Override default prompt (for Terra project standards)
    """
    system = custom_system_prompt or BUBBLE_SYSTEM_PROMPT
    context = _geometry_to_prompt_context(geo)

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
                        "Propose the optimal reconfiguration. "
                        "Return polygon_points in the SAME coordinate space as the geometry data above. "
                        "All polygons must follow the PLAN_ANGLE_DEG. Return JSON only."
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
                "max_tokens": 3000,
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

    # Strip markdown fences if present
    raw = re.sub(r"```json|```", "", raw).strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Claude returned invalid JSON: {e}\nRaw: {raw[:500]}")

    # ── Build BubblePolygon objects ────────────────────────────────────────
    bubbles: list[BubblePolygon] = []
    plan_angle = geo.plan_angle_deg

    for room_data in result.get("rooms", []):
        room_name = room_data.get("room_name", "Room")
        room_type = room_data.get("room_type", "default")
        note = room_data.get("note")
        fill_rgb, stroke_rgb = _get_room_colors(room_type)

        # Use Claude-provided polygon points if valid, else generate from bbox
        raw_points = room_data.get("polygon_points", [])
        bbox = room_data.get("bbox")

        if raw_points and len(raw_points) >= 4:
            points = [(float(p[0]), float(p[1])) for p in raw_points]
        elif bbox and len(bbox) == 4:
            cx, cy, w, h, angle = _bbox_to_polygon_dims(bbox, plan_angle)
            num_pts = 6
            points = _make_angular_polygon(cx, cy, w, h, angle, num_pts)
        else:
            # Fallback: find matching existing room and use its bbox
            matching = next(
                (r for r in geo.rooms if room_name.lower() in r.name.lower()),
                None
            )
            if matching:
                cx, cy, w, h, angle = _bbox_to_polygon_dims(matching.bbox, plan_angle)
                points = _make_angular_polygon(cx, cy, w, h, angle, 6)
            else:
                continue

        # Centroid for label placement
        lx = sum(p[0] for p in points) / len(points)
        ly = sum(p[1] for p in points) / len(points)

        bubbles.append(BubblePolygon(
            room_name=room_name,
            room_type=room_type,
            points=points,
            fill_color=fill_rgb,
            fill_opacity=FILL_OPACITY,
            stroke_color=stroke_rgb,
            label_x=lx,
            label_y=ly,
            label_angle=plan_angle,
            note=note,
        ))

    return ReconfigurationProposal(
        summary=result.get("summary", ""),
        rationale=result.get("rationale", ""),
        bubbles=bubbles,
        overall_angle_deg=plan_angle,
    )
