# Terra — Bubble Diagram Service

Schematic reconfiguration bubble diagram generator.  
Wires together 3 steps to produce a marked-up vector PDF from any uploaded floor plan.

---

## Architecture

```
POST /api/bubble-diagram
        │
        ▼
┌───────────────────┐
│  Step 1: Parser   │  pdfplumber extracts vector geometry, room labels,
│  core/parser.py   │  dimensions, wall paths, plan rotation angle
└────────┬──────────┘
         │  PlanGeometry
         ▼
┌──────────────────────────┐
│  Step 2: Layout Engine   │  Claude API (vision + geometry context)
│  core/layout_engine.py   │  returns reconfiguration JSON with polygon
│                          │  points in PDF coordinate space
└────────┬─────────────────┘
         │  ReconfigurationProposal
         ▼
┌───────────────────────┐
│  Step 3: Renderer     │  reportlab draws semi-transparent angular
│  core/renderer.py     │  polygons + labels, pypdf merges onto
│                       │  original vector PDF
└────────┬──────────────┘
         │  marked-up PDF bytes
         ▼
    PDF download
```

---

## Running Standalone

```bash
# Install dependencies
pip install -r requirements.txt

# Install poppler (required for pdf2image)
# macOS:  brew install poppler
# Ubuntu: apt-get install poppler-utils
# Windows: https://github.com/oschwartz10612/poppler-windows

# Set API key
export ANTHROPIC_API_KEY=sk-ant-...

# Run server
uvicorn api.bubble_endpoint:app --host 0.0.0.0 --port 8001 --reload
```

---

## API Endpoints

### `POST /api/bubble-diagram`
Full pipeline — returns marked-up PDF.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `file` | PDF file | ✓ | Vector PDF floor plan |
| `page_index` | int | — | 0-based page (default: 0) |
| `unit_label` | string | — | e.g. `"3BR / 3.5BA"` |
| `system_prompt` | string | — | Terra project standards (overrides default) |

**Response:** `application/pdf` — original plan with bubble overlays baked in.

Response headers include:
- `X-Terra-Rooms-Count` — number of bubbles drawn
- `X-Terra-Plan-Angle` — detected plan rotation angle (degrees)
- `X-Terra-Summary` — AI reconfiguration summary (truncated)

### `POST /api/bubble-diagram/parse-only`
Debug endpoint — returns Step 1 geometry JSON without calling AI.

```json
{
  "page_width": 1224.0,
  "page_height": 792.0,
  "plan_angle_deg": 45.0,
  "rooms_detected": 12,
  "rooms": [
    { "name": "MASTER BEDROOM", "cx": 623.4, "cy": 412.1, 
      "bbox": [540, 340, 706, 484], "dimension_text": "14'-0\" x 16'-0\"" }
  ]
}
```

### `GET /health`
Health check. Returns `{"status": "ok"}`.

---

## Integration with Existing Terra Proxy (Railway)

The existing proxy handles:
```
POST /api/review  →  compliance review  →  JSON flags
```

Add the bubble service as a second route on the same server, OR deploy separately and call from the HTML frontend:

```javascript
// In Terra_UPR_4.html — add a second button alongside "Run compliance review"
async function runBubbleDiagram() {
  const formData = new FormData();
  formData.append('file', originalPdfFile);   // store the raw File object on upload
  formData.append('page_index', selectedPage);
  formData.append('unit_label', getUnitLabel());

  const resp = await fetch('https://YOUR_BUBBLE_SERVICE_URL/api/bubble-diagram', {
    method: 'POST',
    body: formData,
  });

  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  // Trigger download or open in new tab
  window.open(url, '_blank');
}
```

---

## Injecting Terra Project Standards

Replace the placeholder in `core/layout_engine.py`:

```python
BUBBLE_SYSTEM_PROMPT = """...
[TERRA PROJECT-SPECIFIC RECONFIGURATION STANDARDS — INSERT HERE BEFORE DEPLOYMENT]
...
```

Or pass `system_prompt` as a form field per-request — useful for testing 
multiple standard sets without redeploying.

---

## Bubble Visual Style

- **Shape:** 5–8 vertex angular polygons (NOT circles/ellipses)
- **Fill:** Semi-transparent (~28% opacity), color-coded by room type
- **Stroke:** Higher opacity outline (88%) with sharp mitered corners
- **Labels:** Room name rotated to match plan geometry angle
- **Legend:** Auto-generated bottom-right corner panel
- **Banner:** Summary header strip at top of page (Terra teal)

### Room Type Color Palette

| Room Type | Fill Color |
|-----------|-----------|
| Living / Dining | Terra teal |
| Kitchen | Amber |
| Bedroom | Blue |
| Primary Bedroom | Purple |
| Bathroom | Cyan |
| Den / Office | Olive |
| Balcony | Seafoam |
| Storage / Utility | Gray |

---

## File Structure

```
terra-bubble-service/
├── core/
│   ├── parser.py          # Step 1: PDF vector geometry extraction
│   ├── layout_engine.py   # Step 2: AI reconfiguration proposal
│   └── renderer.py        # Step 3: PDF bubble overlay rendering
├── api/
│   └── bubble_endpoint.py # FastAPI routes
├── utils/
│   └── pdf_to_image.py    # PDF page → JPEG for AI vision
├── requirements.txt
└── README.md
```
