"""
api/bubble_endpoint.py
----------------------
FastAPI route: POST /api/bubble-diagram

Wires together Steps 1-3:
  1. parser.parse_pdf_page()       — extract vector geometry
  2. layout_engine.generate_reconfiguration()  — AI layout proposal
  3. renderer.render_bubble_overlay()          — bake bubbles into PDF

Designed to be mounted on the existing terra-unitplanreview-proxy server
(Railway / Express) or run standalone.

Request:  multipart/form-data
  - file:         PDF file upload
  - page_index:   int (default 0)
  - unit_label:   str  e.g. "3BR / 3.5BA"
  - system_prompt: str (optional — Terra project-specific standards)

Response: PDF file download (application/pdf)
"""

import base64
import io
import os
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from core.parser import parse_pdf_page
from core.layout_engine import generate_reconfiguration
from core.renderer import render_bubble_overlay
from utils.pdf_to_image import render_page_to_jpeg_b64

app = FastAPI(
    title="Terra Bubble Diagram Service",
    description="Schematic reconfiguration bubble diagram generator",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Tighten for production
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["*"],
)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


@app.post("/api/bubble-diagram")
async def bubble_diagram(
    file: UploadFile = File(..., description="Vector PDF floor plan"),
    page_index: int = Form(0, description="0-based page index"),
    unit_label: str = Form("", description="Unit type label e.g. '3BR / 3.5BA'"),
    system_prompt: Optional[str] = Form(
        None,
        description="Terra project-specific reconfiguration standards (optional override)"
    ),
):
    """
    Full pipeline: parse → AI layout → render → return marked-up PDF.
    """
    if not ANTHROPIC_API_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY not configured on server")

    # ── Read uploaded file ─────────────────────────────────────────────────
    pdf_bytes = await file.read()

    if not pdf_bytes:
        raise HTTPException(400, "Empty file received")

    if not pdf_bytes.startswith(b"%PDF"):
        raise HTTPException(400, "File does not appear to be a valid PDF")

    try:
        # ── Step 1: Parse vector geometry ──────────────────────────────────
        geo = parse_pdf_page(pdf_bytes, page_index=page_index)

        if not geo.rooms:
            raise HTTPException(
                422,
                "No room labels detected in the PDF. "
                "Ensure the PDF is a vector export with text layers intact."
            )

        # ── Step 2: Render plan page to JPEG for AI vision ─────────────────
        plan_image_b64 = render_page_to_jpeg_b64(pdf_bytes, page_index=page_index)

        # ── Step 3: AI reconfiguration proposal ────────────────────────────
        proposal = await generate_reconfiguration(
            geo=geo,
            plan_image_b64=plan_image_b64,
            api_key=ANTHROPIC_API_KEY,
            unit_label=unit_label,
            custom_system_prompt=system_prompt or None,
        )

        if not proposal.bubbles:
            raise HTTPException(
                422,
                "AI returned no reconfiguration bubbles. "
                "Check that the plan image and geometry data are consistent."
            )

        # ── Step 4: Render bubble overlay onto PDF ─────────────────────────
        output_pdf = render_bubble_overlay(
            original_pdf_bytes=pdf_bytes,
            proposal=proposal,
            page_index=page_index,
            page_height_pts=geo.page_height,
            page_width_pts=geo.page_width,
        )

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        raise HTTPException(500, f"Pipeline error: {type(e).__name__}: {e}")

    # ── Return marked-up PDF ───────────────────────────────────────────────
    filename = f"terra_reconfig_{unit_label.replace('/', '-').replace(' ', '_') or 'unit'}.pdf"
    return Response(
        content=output_pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Terra-Rooms-Count": str(len(proposal.bubbles)),
            "X-Terra-Plan-Angle": str(round(proposal.overall_angle_deg, 1)),
            "X-Terra-Summary": proposal.summary[:200],
        },
    )


@app.post("/api/bubble-diagram/parse-only")
async def parse_only(
    file: UploadFile = File(...),
    page_index: int = Form(0),
):
    """
    Debug endpoint: returns parsed geometry JSON without calling AI.
    Useful for verifying Step 1 is reading the PDF correctly.
    """
    pdf_bytes = await file.read()

    try:
        geo = parse_pdf_page(pdf_bytes, page_index=page_index)
    except Exception as e:
        raise HTTPException(500, f"Parse error: {e}")

    return {
        "page_width": geo.page_width,
        "page_height": geo.page_height,
        "plan_angle_deg": geo.plan_angle_deg,
        "plan_bbox": geo.plan_bbox,
        "unit_type": geo.unit_type,
        "unit_area_sqft": geo.unit_area_sqft,
        "rooms_detected": len(geo.rooms),
        "rooms": [
            {
                "name": r.name,
                "cx": round(r.cx, 1),
                "cy": round(r.cy, 1),
                "bbox": [round(v, 1) for v in r.bbox],
                "dimension_text": r.dimension_text,
                "area_sqft": r.area_sqft,
            }
            for r in geo.rooms
        ],
        "segments_detected": len(geo.all_segments),
    }


@app.get("/health")
async def health():
    return {"status": "ok", "service": "terra-bubble-diagram"}
