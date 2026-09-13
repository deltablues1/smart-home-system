"""
Vertex AI Tool Implementation

Implements tools for generating visual assets using Google's Vertex AI models:
- Imagen 4 for images (via google-genai SDK)
- Google Veo for videos (via google-genai SDK)
"""

import os
import logging
import time
from typing import Dict, Any, Optional, List
from google.cloud import storage

logger = logging.getLogger(__name__)

# Constants
PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT")
# Imagen/Veo need regional endpoint, not global
LOCATION = os.getenv("VERTEX_AI_LOCATION", "us-central1")
BUCKET_NAME = os.getenv("GOOGLE_CLOUD_STORAGE_BUCKET")

def _get_storage_bucket():
    """Gets or creates the Cloud Storage bucket"""
    if not BUCKET_NAME:
        raise ValueError("GOOGLE_CLOUD_STORAGE_BUCKET environment variable not set")
    
    storage_client = storage.Client(project=PROJECT_ID)
    try:
        bucket = storage_client.get_bucket(BUCKET_NAME)
        return bucket
    except Exception:
        # Try creating if it doesn't exist (might fail on permissions)
        try:
            bucket = storage_client.create_bucket(BUCKET_NAME, location=LOCATION)
            return bucket
        except Exception as e:
            logger.error(f"Failed to access/create bucket {BUCKET_NAME}: {e}")
            raise

def save_to_gcs(content: bytes, filename: str, content_type: str, make_public: bool = True) -> dict:
    """
    Saves binary content to GCS and returns URIs.

    Args:
        content: Binary content to upload
        filename: Name for the file
        content_type: MIME type (e.g., "image/png")
        make_public: If True, makes the object publicly readable

    Returns:
        Dictionary with gs_uri and public_url
    """
    bucket = _get_storage_bucket()
    blob = bucket.blob(f"generated_assets/{filename}")
    blob.upload_from_string(content, content_type=content_type)

    # Make publicly accessible
    public_url = None
    if make_public:
        try:
            blob.make_public()
            public_url = blob.public_url
            logger.info(f"Asset made public: {public_url}")
        except Exception as e:
            logger.warning(f"Could not make blob public: {e}")

    return {
        "gs_uri": f"gs://{BUCKET_NAME}/generated_assets/{filename}",
        "public_url": public_url or f"https://storage.googleapis.com/{BUCKET_NAME}/generated_assets/{filename}"
    }

def generate_visual_asset(
    credentials: Any,
    prompt: str,
    asset_type: str,
    aspect_ratio: str = "1:1"
) -> Dict[str, Any]:
    """
    Generates an image or video using Vertex AI.

    Args:
        credentials: OAuth credentials (passed automatically)
        prompt: Description of the asset to generate
        asset_type: 'IMAGE' or 'VIDEO'
        aspect_ratio: Aspect ratio (e.g., '1:1', '16:9')

    Returns:
        Dictionary with status and asset URI
    """
    if not PROJECT_ID:
        return {"error": "GOOGLE_CLOUD_PROJECT not set"}

    try:
        timestamp = int(time.time())

        # Cost tracking
        try:
            from web.app import _track_imagen, _track_veo
            _cost_hooks = {"imagen": _track_imagen, "veo": _track_veo}
        except Exception:
            _cost_hooks = {}

        if asset_type.upper() == 'IMAGE':
            logger.info(f"Generating image with prompt: {prompt}")
            if "imagen" in _cost_hooks:
                _cost_hooks["imagen"]()
            
            # Imagen 4 (GA) via the google-genai SDK. The classic
            # vertexai.preview.vision_models SDK is deprecated (removal
            # ~2026-06-24), so we use the go-forward genai client here.
            # Imagen uses a regional endpoint (LOCATION), not global.
            from google.genai import Client as GenaiClient
            from google.genai.types import GenerateImagesConfig

            img_client = GenaiClient(vertexai=True, project=PROJECT_ID, location=LOCATION)
            response = img_client.models.generate_images(
                model="imagen-4.0-generate-001",
                prompt=prompt,
                config=GenerateImagesConfig(
                    number_of_images=1,
                    aspect_ratio=aspect_ratio,
                ),
            )

            generated = getattr(response, "generated_images", None) or []
            if not generated:
                return {"error": "No images generated"}

            image_data = generated[0].image.image_bytes
            filename = f"image_{timestamp}.png"

            # Save to GCS (best-effort, may fail on permissions)
            gcs_result = {"gs_uri": "", "public_url": ""}
            try:
                gcs_result = save_to_gcs(image_data, filename, "image/png", make_public=True)
            except Exception as gcs_err:
                logger.warning(f"GCS upload failed (non-critical): {gcs_err}")

            # Also save locally for web dashboard via media service
            local_url = None
            try:
                from services.media_service import save_generated_image
                saved = save_generated_image(image_data, prefix="imagen")
                local_url = saved["url_path"]
                logger.info(f"Image saved locally: {local_url}")
            except Exception as local_err:
                logger.warning(f"Local save failed: {local_err}")

            return {
                "status": "SUCCESS",
                "type": "IMAGE",
                "uri": gcs_result.get("gs_uri", ""),
                "gcs_path": gcs_result.get("gs_uri", ""),
                "public_url": gcs_result.get("public_url", ""),
                "local_url": local_url,
                "image_bytes": image_data,
            }
            
        elif asset_type.upper() == 'VIDEO':
            # Veo requires location=global via google-genai SDK
            # Models: veo-3.1-generate-001 (GA), veo-2.0-generate-001 (fallback)
            VEO_MODELS = [
                "veo-3.1-generate-001",
                "veo-3.1-fast-generate-001",
                "veo-2.0-generate-001",
            ]

            from google.genai import Client as GenaiClient
            from google.genai.types import GenerateVideosConfig

            # Use global location for Veo (different from Imagen which uses regional)
            # Must explicitly set location=global, since VERTEX_AI_LOCATION may be us-west1
            veo_client = GenaiClient(
                vertexai=True,
                project=PROJECT_ID,
                location="global",
            )

            # GCS output URI (required for Veo - videos are saved to GCS)
            output_gcs = None
            if BUCKET_NAME:
                output_gcs = f"gs://{BUCKET_NAME}/generated_assets/video_{timestamp}/"

            if not output_gcs:
                return {"error": "GOOGLE_CLOUD_STORAGE_BUCKET not set. Video generation requires GCS bucket.", "status": "error"}

            last_error = None
            for model_id in VEO_MODELS:
                try:
                    logger.info(f"Generating video with {model_id}: {prompt[:80]}...")
                    if "veo" in _cost_hooks:
                        _cost_hooks["veo"]()

                    operation = veo_client.models.generate_videos(
                        model=model_id,
                        prompt=prompt,
                        config=GenerateVideosConfig(
                            aspect_ratio=aspect_ratio or "16:9",
                            output_gcs_uri=output_gcs,
                        ),
                    )

                    # Poll for completion (video generation takes 1-5 minutes)
                    logger.info(f"Veo operation started with {model_id}, polling...")
                    max_polls = 40  # ~10 minutes max
                    poll_count = 0
                    while not operation.done and poll_count < max_polls:
                        time.sleep(15)
                        operation = veo_client.operations.get(operation)
                        poll_count += 1
                        if poll_count % 4 == 0:
                            logger.info(f"Veo poll {poll_count}/{max_polls}: still generating...")

                    if not operation.done:
                        last_error = "Video generation timed out (>10 minutes). Try a simpler prompt."
                        continue

                    if not operation.response or not operation.result:
                        last_error = f"Video generation failed: {getattr(operation, 'error', 'Unknown error')}"
                        continue

                    generated_videos = operation.result.generated_videos
                    if not generated_videos:
                        last_error = "No videos in result"
                        continue

                    video = generated_videos[0].video
                    video_uri = getattr(video, 'uri', None)
                    logger.info(f"Video generated: {video_uri}")

                    # Download from GCS and save locally for web dashboard
                    local_url = None
                    try:
                        if video_uri and video_uri.startswith("gs://"):
                            # Parse gs://bucket/path
                            gcs_parts = video_uri.replace("gs://", "").split("/", 1)
                            gcs_bucket = gcs_parts[0]
                            gcs_blob_path = gcs_parts[1] if len(gcs_parts) > 1 else ""

                            storage_client = storage.Client(project=PROJECT_ID)
                            bucket = storage_client.bucket(gcs_bucket)
                            blob = bucket.blob(gcs_blob_path)
                            video_data = blob.download_as_bytes()

                            # Save locally as .mp4
                            from services.media_service import UPLOAD_DIR
                            import uuid
                            file_id = uuid.uuid4().hex[:12]
                            mp4_filename = f"veo_{file_id}.mp4"
                            mp4_path = os.path.join(UPLOAD_DIR, mp4_filename)
                            with open(mp4_path, "wb") as f:
                                f.write(video_data)
                            local_url = f"/api/media/{file_id}"
                            logger.info(f"Video saved locally: {local_url} ({len(video_data)} bytes)")
                    except Exception as dl_err:
                        logger.warning(f"Could not download video from GCS: {dl_err}")

                    return {
                        "status": "SUCCESS",
                        "type": "VIDEO",
                        "local_url": local_url,
                        "video_bytes": None,  # Already saved locally
                        "message": f"Video generated successfully with {model_id}"
                    }

                except Exception as sdk_err:
                    last_error = str(sdk_err)
                    logger.warning(f"Veo model {model_id} failed: {sdk_err}")
                    continue

            return {"error": f"All Veo models failed. Last error: {last_error}", "status": "error"}

    except Exception as e:
        logger.error(f"Vertex AI generation failed: {e}")
        return {"error": str(e)}

def register_vertex_ai_tools(tool_registry) -> None:
    """
    Register Vertex AI tools in the tool registry
    """
    tool_registry.register_tool(
        name="generate_visual_asset",
        function=generate_visual_asset,
        description="Generates an image or video using Vertex AI (Gemini/Veo).",
        parameters={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Detailed description of the asset to generate"
                },
                "asset_type": {
                    "type": "string",
                    "enum": ["IMAGE", "VIDEO"],
                    "description": "Type of asset to generate"
                },
                "aspect_ratio": {
                    "type": "string",
                    "description": "Aspect ratio (e.g. '1:1', '16:9')",
                    "default": "1:1"
                }
            },
            "required": ["prompt", "asset_type"]
        }
    )
    logger.info("Vertex AI tools registered successfully")
