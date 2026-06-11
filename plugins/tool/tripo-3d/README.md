# Tripo 3D Generation Tool Plugin

Generate 3D models (GLB format) using Tripo via DashScope API.

## Models

| Model | Description | Faces | Speed |
|-------|-------------|-------|-------|
| `Tripo/Tripo-P1.0` | Fast preview | ~20K | Fast |
| `Tripo/Tripo-H3.1` | Film-grade quality | ~2M | Slower |

## Tools

### `text_to_3d_tripo` - Text to 3D

Generate 3D models from text descriptions.

**Parameters:**
- `prompt` (required): Description of the 3D model (Chinese/English)
- `model`: "Tripo/Tripo-P1.0" (default) or "Tripo/Tripo-H3.1"
- `texture_quality`: "standard" (default) or "detailed"

**Examples:**
```
prompt: "一只可爱的猫"
prompt: "a futuristic spaceship with metallic surface"
```

---

### `image_to_3d_tripo` - Image to 3D

Generate 3D models from a single reference image.

**Parameters:**
- `image_url` (required): URL or local file path of the input image
- `model`: "Tripo/Tripo-P1.0" (default) or "Tripo/Tripo-H3.1"
- `texture_quality`: "standard" (default) or "detailed"

**Image input support:**
- HTTP/HTTPS URLs (used directly)
- Local file paths: `.png`, `.jpg`, `.jpeg`, `.webp` (auto-converted to base64)

---

### `multi_images_to_3d_tripo` - Multi-Image to 3D

Generate 3D models from 2-4 images showing different angles of the same object.

**Parameters:**
- `image_urls` (required): List of 2-4 image URLs or local file paths
- `model`: "Tripo/Tripo-P1.0" (default) or "Tripo/Tripo-H3.1"
- `texture_quality`: "standard" (default) or "detailed"

---

## Input Modes

The three tools correspond to three mutually exclusive input modes:
- **prompt**: Text description only (`text_to_3d_tripo`)
- **image**: Single image URL (`image_to_3d_tripo`)
- **images**: 2-4 image URLs for multi-angle reconstruction (`multi_images_to_3d_tripo`)

## Configuration

| Field | Description | Default |
|-------|-------------|---------|
| `api_key` | DashScope API key (required) | — |
| `endpoint` | Regional API endpoint | `https://dashscope.aliyuncs.com/api/v1` |
| `model` | Tripo model | `Tripo/Tripo-P1.0` |
| `texture_quality` | Model texture quality | `standard` |
| `timeout` | Request timeout in seconds | 600 |

**Endpoints:**
- Beijing: `https://dashscope.aliyuncs.com/api/v1`
- Singapore: `https://dashscope-intl.aliyuncs.com/api/v1`

Get your API key at: https://bailian.console.aliyun.com/

## Output

- 3D models are saved to `{DEFAULT_MEDIA_DIR}/tripo_3d/` as `.glb` files
- Preview renders (when available) are saved as `.png` files
- GLB files can be viewed in any 3D viewer (Blender, three.js, Windows 3D Viewer, etc.)
- API-generated URLs are valid for 24 hours

## Technical Details

- **Models**: Tripo/Tripo-P1.0 (fast), Tripo/Tripo-H3.1 (film-grade)
- **API**: DashScope async HTTP API (`X-DashScope-Async: enable`)
- **Flow**: Submit task -> poll status -> download result
- **Generation time**: Typically 2-10 minutes
