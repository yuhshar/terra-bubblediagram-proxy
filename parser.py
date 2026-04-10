"""
core/parser.py
--------------
Step 1: PDF Vector Parser

Extracts from a vector PDF:
  - Page dimensions (MediaBox)
  - All text elements with positions (room labels, dimensions)
  - All vector paths/lines (wall geometry)
  - Plan rotation angle (derived from dominant line angles)
  - Inferred room bounding boxes from label clusters + surrounding geometry

Returns a structured PlanGeometry object consumed by the AI layout engine.
"""

import math
import re
from dataclasses import dataclass, field
from typing import Optional
import pdfplumber
import pdfplumber.utils


# ─── Data Structures ─────────────────────────────────────────────────────────

@dataclass
class TextElement:
    text: str
    x: float          # PDF coordinate space (origin bottom-left)
    y: float
    size: float       # font size pt
    is_room_label: bool = False
    is_dimension: bool = False


@dataclass
class PathSegment:
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def length(self) -> float:
        return math.hypot(self.x1 - self.x0, self.y1 - self.y0)

    @property
    def angle_deg(self) -> float:
        """Angle of segment in degrees, normalised to [0, 180)."""
        a = math.degrees(math.atan2(self.y1 - self.y0, self.x1 - self.x0))
        return a % 180


@dataclass
class RoomZone:
    name: str
    cx: float          # centroid x (PDF coords)
    cy: float          # centroid y
    bbox: tuple        # (x0, y0, x1, y1) approximate bounding box
    dimension_text: Optional[str] = None   # e.g. "14'-0\" x 16'-0\""
    area_sqft: Optional[float] = None


@dataclass
class PlanGeometry:
    page_width: float
    page_height: float
    plan_angle_deg: float          # dominant rotation of the floor plan
    plan_bbox: tuple               # (x0, y0, x1, y1) tight bbox of drawing area
    rooms: list[RoomZone] = field(default_factory=list)
    all_text: list[TextElement] = field(default_factory=list)
    all_segments: list[PathSegment] = field(default_factory=list)
    unit_type: Optional[str] = None      # e.g. "3 BEDROOM 3.5 BATHS"
    unit_area_sqft: Optional[float] = None


# ─── Room Label Heuristics ────────────────────────────────────────────────────

ROOM_KEYWORDS = {
    "living", "dining", "kitchen", "bedroom", "master", "bath",
    "balcony", "office", "den", "foyer", "entry", "laundry",
    "powder", "closet", "wic", "mechanical", "a/c", "w/d",
    "great", "room", "corridor", "hall", "storage", "pantry",
    "utility", "linen", "study", "library", "terrace", "patio",
    "garage", "lobby", "vestibule",
}

DIMENSION_RE = re.compile(r"^\d{1,3}['\-][\-\d\s\"½¼¾]+")
AREA_RE = re.compile(r"(\d[\d,]*\.?\d*)\s*(?:sq\.?\s*ft|sf|sqft)", re.IGNORECASE)
UNIT_TYPE_RE = re.compile(
    r"(\d+)\s*bedroom[s]?\s*[\d.]+\s*bath[s]?", re.IGNORECASE
)
APARTMENT_AREA_RE = re.compile(
    r"apartment\s*area[:\s]+(\d[\d,]*\.?\d*)\s*sq\.?\s*ft", re.IGNORECASE
)


def _is_room_label(text: str) -> bool:
    t = text.lower().strip()
    if len(t) < 2:
        return False
    return any(kw in t for kw in ROOM_KEYWORDS)


def _is_dimension(text: str) -> bool:
    return bool(DIMENSION_RE.match(text.strip()))


def _parse_sqft(dim_text: str) -> Optional[float]:
    """Convert '14\'-0\" x 16\'-0\"' to approximate sqft."""
    parts = re.findall(r"(\d+)'\s*-?\s*(\d+)\"", dim_text)
    if len(parts) == 2:
        w = int(parts[0][0]) + int(parts[0][1]) / 12
        h = int(parts[1][0]) + int(parts[1][1]) / 12
        return round(w * h, 1)
    return None


# ─── Geometry Helpers ─────────────────────────────────────────────────────────

def _dominant_angle(segments: list[PathSegment], min_length: float = 20.0) -> float:
    """
    Find the dominant line angle in the plan (the floor plan rotation).
    Uses a weighted histogram of segment angles (weighted by length).
    Returns angle in degrees [0, 180).
    """
    bins = {}
    resolution = 5  # degrees

    for seg in segments:
        if seg.length < min_length:
            continue
        bucket = round(seg.angle_deg / resolution) * resolution % 180
        bins[bucket] = bins.get(bucket, 0.0) + seg.length

    if not bins:
        return 0.0

    # Two dominant perpendicular angles expected — pick the one NOT near 0/90/180
    # (those are axis-aligned; we want the rotation offset)
    sorted_bins = sorted(bins.items(), key=lambda x: -x[1])

    for angle, _ in sorted_bins:
        # Prefer non-axis-aligned angles as the plan rotation indicator
        if angle not in (0, 90, 180):
            return float(angle)

    return float(sorted_bins[0][0])


def _drawing_bbox(segments: list[PathSegment], margin: float = 10.0) -> tuple:
    """Tight bounding box around all meaningful vector paths."""
    if not segments:
        return (0, 0, 100, 100)
    xs = [s.x0 for s in segments] + [s.x1 for s in segments]
    ys = [s.y0 for s in segments] + [s.y1 for s in segments]
    return (
        min(xs) - margin,
        min(ys) - margin,
        max(xs) + margin,
        max(ys) + margin,
    )


# ─── Room Association ─────────────────────────────────────────────────────────

def _associate_dimensions(
    labels: list[TextElement],
    dims: list[TextElement],
    proximity_threshold: float = 60.0,
) -> dict[int, str]:
    """
    For each room label, find the nearest dimension text within threshold.
    Returns {label_index: dimension_string}.
    """
    associations = {}
    for i, label in enumerate(labels):
        best_dist = float("inf")
        best_dim = None
        for dim in dims:
            dist = math.hypot(label.x - dim.x, label.y - dim.y)
            if dist < best_dist and dist < proximity_threshold:
                best_dist = dist
                best_dim = dim.text
        if best_dim:
            associations[i] = best_dim
    return associations


def _build_room_zones(
    labels: list[TextElement],
    dim_associations: dict[int, str],
    page_height: float,
) -> list[RoomZone]:
    """
    Build RoomZone objects from label positions.
    Bounding box is estimated from label position + dimension span.
    Coordinates are converted to PDF space (origin bottom-left).
    """
    zones = []
    for i, label in enumerate(labels):
        dim_text = dim_associations.get(i)
        area = _parse_sqft(dim_text) if dim_text else None

        # Rough bbox: expand from centroid by half-dimension sizes
        pad = 30.0
        if dim_text:
            parts = re.findall(r"(\d+)'\s*-?\s*(\d+)\"", dim_text)
            if len(parts) == 2:
                # Convert ft to ~PDF points at 1/8"=1'-0" scale: 1ft ≈ 9pt
                scale = 9.0
                w_ft = int(parts[0][0]) + int(parts[0][1]) / 12
                h_ft = int(parts[1][0]) + int(parts[1][1]) / 12
                pad_x = (w_ft * scale) / 2
                pad_y = (h_ft * scale) / 2
            else:
                pad_x = pad_y = pad
        else:
            pad_x = pad_y = pad

        zones.append(RoomZone(
            name=label.text.strip(),
            cx=label.x,
            cy=label.y,
            bbox=(
                label.x - pad_x,
                label.y - pad_y,
                label.x + pad_x,
                label.y + pad_y,
            ),
            dimension_text=dim_text,
            area_sqft=area,
        ))
    return zones


# ─── Main Parser ──────────────────────────────────────────────────────────────

def parse_pdf_page(pdf_bytes: bytes, page_index: int = 0) -> PlanGeometry:
    """
    Parse a single page of a vector PDF and return PlanGeometry.

    Args:
        pdf_bytes: Raw PDF file bytes
        page_index: 0-based page index to parse

    Returns:
        PlanGeometry with all extracted data
    """
    import io

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        if page_index >= len(pdf.pages):
            raise ValueError(
                f"Page {page_index} not found. PDF has {len(pdf.pages)} pages."
            )

        page = pdf.pages[page_index]
        pw = float(page.width)
        ph = float(page.height)

        # ── Extract all text elements ──────────────────────────────────────
        words = page.extract_words(
            x_tolerance=3,
            y_tolerance=3,
            keep_blank_chars=False,
            use_text_flow=False,
        )

        all_text: list[TextElement] = []
        for w in words:
            # pdfplumber y is from top; convert to bottom-left origin
            y_bl = ph - float(w["top"])
            te = TextElement(
                text=w["text"],
                x=float(w["x0"]),
                y=y_bl,
                size=float(w.get("size", 8)),
            )
            te.is_room_label = _is_room_label(te.text)
            te.is_dimension = _is_dimension(te.text)
            all_text.append(te)

        # ── Merge adjacent room label words (e.g. "MASTER" + "BEDROOM") ───
        # Group words that are vertically close and horizontally sequential
        room_label_groups: list[list[TextElement]] = []
        used = set()
        label_words = [t for t in all_text if t.is_room_label]

        for i, te in enumerate(label_words):
            if i in used:
                continue
            group = [te]
            used.add(i)
            for j, other in enumerate(label_words):
                if j in used:
                    continue
                if (
                    abs(other.y - te.y) < 12
                    and abs(other.x - te.x) < 120
                    and other.x > te.x
                ):
                    group.append(other)
                    used.add(j)
            room_label_groups.append(sorted(group, key=lambda t: t.x))

        merged_labels: list[TextElement] = []
        for group in room_label_groups:
            merged_text = " ".join(g.text for g in group)
            cx = sum(g.x for g in group) / len(group)
            cy = sum(g.y for g in group) / len(group)
            sz = max(g.size for g in group)
            te = TextElement(
                text=merged_text, x=cx, y=cy, size=sz,
                is_room_label=True
            )
            merged_labels.append(te)

        dim_words = [t for t in all_text if t.is_dimension]

        # ── Extract vector paths ───────────────────────────────────────────
        segments: list[PathSegment] = []
        for obj in page.objects.get("line", []):
            seg = PathSegment(
                x0=float(obj["x0"]),
                y0=ph - float(obj["y0"]),
                x1=float(obj["x1"]),
                y1=ph - float(obj["y1"]),
            )
            if seg.length > 5:
                segments.append(seg)

        # Also extract from curves/paths (rect objects = wall boundaries)
        for obj in page.objects.get("rect", []):
            x0, y0 = float(obj["x0"]), ph - float(obj["y0"])
            x1, y1 = float(obj["x1"]), ph - float(obj["y1"])
            # Decompose rect into 4 segments
            for seg in [
                PathSegment(x0, y0, x1, y0),
                PathSegment(x1, y0, x1, y1),
                PathSegment(x1, y1, x0, y1),
                PathSegment(x0, y1, x0, y0),
            ]:
                if seg.length > 5:
                    segments.append(seg)

        # ── Unit type & area from title block text ─────────────────────────
        full_text = " ".join(t.text for t in all_text)
        unit_type = None
        unit_area = None

        m = UNIT_TYPE_RE.search(full_text)
        if m:
            unit_type = m.group(0).strip()

        m2 = APARTMENT_AREA_RE.search(full_text)
        if m2:
            try:
                unit_area = float(m2.group(1).replace(",", ""))
            except ValueError:
                pass

        # ── Plan rotation angle ────────────────────────────────────────────
        plan_angle = _dominant_angle(segments)

        # ── Drawing bounding box ───────────────────────────────────────────
        plan_bbox = _drawing_bbox(segments)

        # ── Associate dimensions → room labels ─────────────────────────────
        dim_assoc = _associate_dimensions(merged_labels, dim_words)

        # ── Build room zones ───────────────────────────────────────────────
        rooms = _build_room_zones(merged_labels, dim_assoc, ph)

        return PlanGeometry(
            page_width=pw,
            page_height=ph,
            plan_angle_deg=plan_angle,
            plan_bbox=plan_bbox,
            rooms=rooms,
            all_text=all_text,
            all_segments=segments,
            unit_type=unit_type,
            unit_area_sqft=unit_area,
        )
