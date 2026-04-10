"""
api/bubble_endpoint.py
----------------------
FastAPI route: POST /api/bubble-diagram

Pipeline:
  1. parse_pdf_page()        -- extract vector geometry + plan bounding box
  2. generate_markups()      -- AI markup proposals (reconfigure bubbles + comment annotations)
  3. render_markup_overlay() -- bake onto PDF as two sheets:
                                Sheet 1: annotated plan (all markups)
                                Sheet 2: reconfiguration study (reconfig bubbles only)

Request:  multipart/form-data
  - file:          PDF file upload
  - page_index:    int (default 0)
  - unit_label:    str e.g. "3BR / 3.5BA"
  - system_prompt: str (optional Terra project standards override)

Response: PDF file (application/pdf), 1 or 2 pages
"""

import os
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from core.parser import parse_pdf_page
from core.layout_engine import generate_markups
from core.renderer import render_markup_overlay
from utils.pdf_to_image import render_page_to_jpeg_b64

app = FastAPI(
    title="Terra Markup Service",
    description="Schematic markup PDF generator — annotated plan + reconfiguration study",
    version="3.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["*"],
)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


@app.post("/api/bubble-diagram")
async def bubble_diagram(
    file: UploadFile = File(...),
    page_index: int = Form(0),
    unit_label: str = Form(""),
    system_prompt: Optional[str] = Form(None),
):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY not configured")

    pdf_bytes = await file.read()

    if not pdf_bytes:
        raise HTTPException(400, "Empty file")
    if not pdf_bytes.startswith(b"%PDF"):
        raise HTTPException(400, "Not a valid PDF")

    try:
        # Step 1: Parse vector geometry (includes plan_bbox)
        geo = parse_pdf_page(pdf_bytes, page_index=page_index)

        if not geo.rooms:
            raise HTTPException(422, "No room labels detected in PDF")

        # Step 2: Render page to JPEG for AI vision
        plan_image_b64 = render_page_to_jpeg_b64(pdf_bytes, page_index=page_index)

        # Step 3: AI markup proposals (reconfigure + comment types)
        proposal = await generate_markups(
            geo=geo,
            plan_image_b64=plan_image_b64,
            api_key=ANTHROPIC_API_KEY,
            unit_label=unit_label,
            custom_system_prompt=system_prompt or None,
        )

        if not proposal.markups:
            raise HTTPException(422, "AI returned no markup proposals")

        # Step 4: Render overlay onto PDF (1 or 2 sheets)
        output_pdf = render_markup_overlay(
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

    reconfig_count = sum(1 for m in proposal.markups if not m.is_comment)
    comment_count  = sum(1 for m in proposal.markups if m.is_comment)
    sheet_count    = 2 if reconfig_count > 0 else 1

    filename = f"Terra_Markup_{unit_label.replace('/', '-').replace(' ', '_') or 'unit'}.pdf"
    return Response(
        content=output_pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Terra-Markups-Count": str(len(proposal.markups)),
            "X-Terra-Reconfig-Count": str(reconfig_count),
            "X-Terra-Comment-Count": str(comment_count),
            "X-Terra-Sheet-Count": str(sheet_count),
            "X-Terra-Summary": proposal.summary[:200],
        },
    )


@app.post("/api/bubble-diagram/parse-only")
async def parse_only(
    file: UploadFile = File(...),
    page_index: int = Form(0),
):
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
            }
            for r in geo.rooms
        ],
    }


@app.get("/health")
async def health():
    return {"status": "ok", "service": "terra-markup-v3"}
