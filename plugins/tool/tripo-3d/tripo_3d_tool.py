# -*- coding: utf-8 -*-
# pylint: disable=too-many-return-statements,too-many-branches
# pylint: disable=too-many-statements,too-many-locals
"""Tripo 3D model generation tools.

Uses the DashScope async HTTP API to generate 3D models (GLB format)
via Tripo. Supports two models:
  - Tripo/Tripo-H3.1: Film-grade, ~2M faces
  - Tripo/Tripo-P1.0: Fast preview, ~20K faces

Three mutually exclusive input modes:
  - prompt: text-to-3D
  - image: single image to 3D
  - images: multi-image (2-4) to 3D

Async flow:
  1. POST /services/aigc/video-generation/3d-generation
     with X-DashScope-Async: enable → returns task_id
  2. GET  /tasks/{task_id}
     → poll until SUCCEEDED or FAILED
  3. Download the .glb model file
"""

import asyncio
import base64
import logging
import time
from pathlib import Path
from typing import Optional

import httpx
from agentscope.message import TextBlock
from agentscope.tool import ToolResponse
from qwenpaw.constant import DEFAULT_MEDIA_DIR
from qwenpaw.plugins import get_tool_config

logger = logging.getLogger(__name__)

_DEFAULT_ENDPOINT = "https://dashscope.aliyuncs.com/api/v1"
_DEFAULT_TIMEOUT = 600.0
_DEFAULT_MODEL = "Tripo/Tripo-P1.0"
_POLL_INTERVAL = 5  # seconds between status checks
_3D_GEN_PATH = "/services/aigc/video-generation/3d-generation"

_VALID_MODELS = ("Tripo/Tripo-P1.0", "Tripo/Tripo-H3.1")

_IMAGE_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def _resolve_image_url(path_or_url: str) -> str:
    """Resolve an image path or URL.

    HTTP/HTTPS URLs are returned as-is.
    Local files are converted to base64 data URLs.
    """
    if path_or_url.startswith(("http://", "https://")):
        return path_or_url

    path_obj = Path(path_or_url)
    if not path_obj.exists():
        raise FileNotFoundError(f"Image file not found: {path_or_url}")
    if not path_obj.is_file():
        raise ValueError(f"Not a file: {path_or_url}")

    ext = path_obj.suffix.lower()
    if ext not in _IMAGE_MIME_TYPES:
        raise ValueError(
            f"Unsupported image format: {ext}. "
            f"Supported: {', '.join(_IMAGE_MIME_TYPES.keys())}",
        )

    mime_type = _IMAGE_MIME_TYPES[ext]
    with open(path_obj, "rb") as f:
        image_data = base64.b64encode(f.read()).decode("utf-8")

    return f"data:{mime_type};base64,{image_data}"


def _extract_config(tool_config: dict) -> tuple[str, str, float, str, str]:
    """Extract api_key, endpoint, timeout, texture_quality, model."""
    api_key = tool_config.get("api_key", "")
    endpoint = tool_config.get("endpoint", "")
    if not endpoint or not endpoint.strip():
        endpoint = _DEFAULT_ENDPOINT

    timeout_raw = tool_config.get("timeout")
    if timeout_raw is None or float(timeout_raw) <= 0:
        timeout = _DEFAULT_TIMEOUT
    else:
        timeout = float(timeout_raw)

    texture_quality = tool_config.get("texture_quality", "standard")
    if texture_quality not in ("standard", "detailed"):
        texture_quality = "standard"

    model = tool_config.get("model", _DEFAULT_MODEL)
    if model not in _VALID_MODELS:
        model = _DEFAULT_MODEL

    return api_key, endpoint, timeout, texture_quality, model


async def _submit_3d_task(
    api_key: str,
    endpoint: str,
    body: dict,
    timeout: float,
) -> dict:
    """Submit an async 3D generation task to DashScope.

    Returns the full JSON response on success.
    Raises httpx.HTTPStatusError on failure.
    """
    url = endpoint.rstrip("/") + _3D_GEN_PATH
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-DashScope-Async": "enable",
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=body, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def _poll_task(
    api_key: str,
    endpoint: str,
    task_id: str,
    timeout: float,
) -> dict:
    """Poll a DashScope async task until completion.

    Returns the final task result dict.
    Raises TimeoutError if the task does not finish within timeout.
    Raises RuntimeError if the task fails.
    """
    url = endpoint.rstrip("/") + f"/tasks/{task_id}"
    headers = {
        "Authorization": f"Bearer {api_key}",
    }

    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=30) as client:
        while time.monotonic() < deadline:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()

            status = data.get("output", {}).get("task_status", "")
            logger.info(
                f"3D task {task_id} status: {status}",
            )

            if status == "SUCCEEDED":
                return data
            if status in ("FAILED", "CANCELED"):
                msg = data.get("output", {}).get("message", "Unknown error")
                raise RuntimeError(
                    f"3D generation task {status}: {msg}",
                )

            await asyncio.sleep(_POLL_INTERVAL)

    raise TimeoutError(
        f"3D generation task {task_id} did not complete within "
        f"{timeout}s",
    )


async def _download_file(
    file_url: str,
    save_dir: Path,
    prefix: str,
    ext: str,
    timeout: float,
) -> Path:
    """Download a file from URL and save locally."""
    save_dir.mkdir(parents=True, exist_ok=True)
    timestamp = int(time.time() * 1000)
    filename = f"{prefix}_{timestamp}{ext}"
    file_path = save_dir / filename

    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("GET", file_url) as response:
            response.raise_for_status()
            chunks = []
            async for chunk in response.aiter_bytes(
                chunk_size=1024 * 1024,
            ):
                chunks.append(chunk)
    await asyncio.to_thread(file_path.write_bytes, b"".join(chunks))

    logger.info(f"File saved to {file_path}")
    return file_path


def _error_response(msg: str) -> ToolResponse:
    """Create an error ToolResponse."""
    return ToolResponse(
        content=[TextBlock(type="text", text=f"Error: {msg}")],
    )


def _find_result_url(output: dict, ext: str) -> Optional[str]:
    """Search output dict for a URL ending with the given extension.

    DashScope async task results may nest URLs in a 'results' list.
    """
    results = output.get("results")
    if isinstance(results, list):
        for item in results:
            if isinstance(item, dict):
                url = item.get("url", "")
                if url and url.lower().endswith(ext):
                    return url
            elif isinstance(item, str) and item.lower().endswith(ext):
                return item
    # Flat scan of all string values
    for v in output.values():
        if isinstance(v, str) and v.startswith("http") and ext in v:
            return v
    return None


async def _run_3d_generation(
    tool_name: str,
    input_data: dict,
    model_override: str,
    texture_override: str,
    file_prefix: str,
    description: str,
) -> ToolResponse:
    """Shared 3D generation logic for all three tools.

    Args:
        tool_name: Config key for get_tool_config().
        input_data: The "input" dict (prompt/image/images).
        model_override: Model param from tool call (empty = use config).
        texture_override: Texture param from tool call (empty = use config).
        file_prefix: Prefix for saved files (e.g. "tripo_t2m").
        description: Human-readable description for the response.
    """
    try:
        tool_config = get_tool_config(tool_name)
        if not tool_config:
            return _error_response(
                "Tool not configured. "
                "Please set your API key in the tool settings.",
            )

        api_key, endpoint, timeout, cfg_quality, cfg_model = (
            _extract_config(tool_config)
        )
        if not api_key:
            return _error_response(
                "DashScope API key not configured. "
                "Please set your API key in the tool settings.",
            )

        model = model_override if model_override else cfg_model
        if model not in _VALID_MODELS:
            model = cfg_model
        quality = texture_override if texture_override else cfg_quality

        logger.info(
            f"Submitting 3D task: model={model}, "
            f"texture_quality={quality}",
        )

        body = {
            "model": model,
            "input": input_data,
            "parameters": {
                "texture_quality": quality,
            },
        }

        # Step 1: Submit async task
        submit_resp = await _submit_3d_task(
            api_key, endpoint, body, timeout,
        )

        task_id = submit_resp.get("output", {}).get("task_id")
        if not task_id:
            return _error_response(
                f"No task_id in response: {submit_resp}",
            )

        logger.info(f"3D generation task submitted: {task_id}")

        # Step 2: Poll until completion
        result = await _poll_task(api_key, endpoint, task_id, timeout)

        # Step 3: Extract model URL and download
        output = result.get("output", {})
        model_url = (
            output.get("model_url")
            or output.get("video_url")
            or _find_result_url(output, ".glb")
        )
        rendered_image_url = (
            output.get("rendered_image_url")
            or _find_result_url(output, ".png")
        )

        if not model_url:
            return _error_response(
                f"No model URL in task result. Output: {output}",
            )

        save_dir = DEFAULT_MEDIA_DIR / "tripo_3d"

        # Download GLB model
        model_path = await _download_file(
            model_url, save_dir, file_prefix, ".glb", timeout,
        )

        # Download preview image if available
        preview_path = None
        if rendered_image_url:
            try:
                preview_path = await _download_file(
                    rendered_image_url,
                    save_dir,
                    f"{file_prefix}_preview",
                    ".png",
                    timeout,
                )
            except Exception as e:
                logger.warning(
                    f"Failed to download preview image: {e}",
                )

        # Build response blocks
        content_blocks = []

        if preview_path:
            from agentscope.message import ImageBlock

            content_blocks.append(
                ImageBlock(
                    type="image",
                    source={"type": "url", "url": str(preview_path)},
                ),
            )

        content_blocks.append(
            TextBlock(
                type="text",
                text=(
                    f"3D model generated successfully\n"
                    f"Model: {model}\n"
                    f"{description}\n"
                    f"Texture quality: {quality}\n"
                    f"Model saved to: {model_path}\n"
                    f"Format: GLB (drag into any 3D viewer to "
                    f"inspect)\n"
                    f"Original URL (valid 24h): {model_url}"
                ),
            ),
        )

        return ToolResponse(content=content_blocks)

    except TimeoutError as e:
        logger.error(f"3D generation timed out: {e}")
        return _error_response(str(e))
    except Exception as e:
        logger.error(
            f"3D generation failed: {e}",
            exc_info=True,
        )
        return _error_response(f"3D generation failed - {str(e)}")


async def text_to_3d_tripo(
    prompt: str,
    model: str = "",
    texture_quality: str = "",
) -> ToolResponse:
    """Generate a 3D model from a text prompt using Tripo.

    Creates a 3D model (GLB format) from a natural language description.
    The generation is asynchronous and typically takes 2-10 minutes.

    Args:
        prompt (str):
            Text description of the 3D model to generate.
            Supports Chinese and English.
            Examples: "一只可爱的猫", "a futuristic spaceship"
        model (str, optional):
            Model to use: "Tripo/Tripo-P1.0" (fast preview, ~20K
            faces) or "Tripo/Tripo-H3.1" (film-grade, ~2M faces).
            If empty, uses the value from tool configuration.
        texture_quality (str, optional):
            Texture quality: "standard" or "detailed".
            If empty, uses the value from tool configuration.

    Returns:
        ToolResponse: Contains the local GLB file path, preview
        image, and generation metadata.
    """
    return await _run_3d_generation(
        tool_name="text_to_3d_tripo",
        input_data={"prompt": prompt},
        model_override=model,
        texture_override=texture_quality,
        file_prefix="tripo_t2m",
        description=f"Input: text prompt: {prompt}",
    )


async def image_to_3d_tripo(
    image_url: str,
    model: str = "",
    texture_quality: str = "",
) -> ToolResponse:
    """Generate a 3D model from an image using Tripo.

    Creates a 3D model (GLB format) from an input image.
    The generation is asynchronous and typically takes 2-10 minutes.

    Args:
        image_url (str):
            URL or local file path of the input image.
            Supports HTTP/HTTPS URLs and local files
            (.png/.jpg/.jpeg/.webp).
            The image should clearly show the object to be
            reconstructed in 3D.
        model (str, optional):
            Model to use: "Tripo/Tripo-P1.0" (fast preview, ~20K
            faces) or "Tripo/Tripo-H3.1" (film-grade, ~2M faces).
            If empty, uses the value from tool configuration.
        texture_quality (str, optional):
            Texture quality: "standard" or "detailed".
            If empty, uses the value from tool configuration.

    Returns:
        ToolResponse: Contains the local GLB file path, preview
        image, and generation metadata.
    """
    try:
        resolved_image = _resolve_image_url(image_url)
    except (FileNotFoundError, ValueError) as e:
        return _error_response(
            f"Invalid image_url '{image_url}' - {str(e)}",
        )

    return await _run_3d_generation(
        tool_name="image_to_3d_tripo",
        input_data={"image": resolved_image},
        model_override=model,
        texture_override=texture_quality,
        file_prefix="tripo_i2m",
        description="Input: single image",
    )


async def multi_images_to_3d_tripo(
    image_urls: list[str],
    model: str = "",
    texture_quality: str = "",
) -> ToolResponse:
    """Generate a 3D model from multiple images using Tripo.

    Creates a 3D model (GLB format) from 2-4 images showing
    different angles of the same object. The generation is
    asynchronous and typically takes 2-10 minutes.

    Args:
        image_urls (list[str]):
            List of 2-4 image URLs or local file paths.
            Each image should show the same object from a
            different angle. Supports HTTP/HTTPS URLs and
            local files (.png/.jpg/.jpeg/.webp).
        model (str, optional):
            Model to use: "Tripo/Tripo-P1.0" (fast preview, ~20K
            faces) or "Tripo/Tripo-H3.1" (film-grade, ~2M faces).
            If empty, uses the value from tool configuration.
        texture_quality (str, optional):
            Texture quality: "standard" or "detailed".
            If empty, uses the value from tool configuration.

    Returns:
        ToolResponse: Contains the local GLB file path, preview
        image, and generation metadata.
    """
    if not isinstance(image_urls, list) or not (2 <= len(image_urls) <= 4):
        return _error_response(
            "image_urls must be a list of 2-4 image URLs.",
        )

    resolved_images = []
    for i, url in enumerate(image_urls):
        try:
            resolved_images.append(_resolve_image_url(url))
        except (FileNotFoundError, ValueError) as e:
            return _error_response(
                f"Invalid image_urls[{i}] '{url}' - {str(e)}",
            )

    return await _run_3d_generation(
        tool_name="multi_images_to_3d_tripo",
        input_data={"images": resolved_images},
        model_override=model,
        texture_override=texture_quality,
        file_prefix="tripo_mi2m",
        description=f"Input: {len(resolved_images)} images (multi-angle)",
    )
