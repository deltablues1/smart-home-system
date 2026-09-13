"""
YouTube API Implementation

Real implementation for YouTube transcript extraction
Uses youtube-transcript-api (open source, no API key needed!)
"""

import logging
import re
import os
from typing import Dict, Any, Optional, List
from urllib.parse import urlparse, parse_qs
from googleapiclient.http import MediaFileUpload
from tools.google_api_client import GoogleAPIClient

logger = logging.getLogger(__name__)


def extract_video_id(url: str) -> Optional[str]:
    """
    Extract YouTube video ID from various URL formats

    Supports:
    - https://www.youtube.com/watch?v=VIDEO_ID
    - https://youtu.be/VIDEO_ID
    - https://www.youtube.com/embed/VIDEO_ID
    - VIDEO_ID (direct ID)

    Args:
        url: YouTube URL or video ID

    Returns:
        Video ID or None if invalid
    """
    # If it's already just an ID (11 characters, alphanumeric + _-)
    if re.match(r'^[a-zA-Z0-9_-]{11}$', url):
        return url

    # Parse URL
    try:
        parsed = urlparse(url)

        # youtube.com/watch?v=VIDEO_ID
        if 'youtube.com' in parsed.netloc and parsed.path == '/watch':
            query_params = parse_qs(parsed.query)
            return query_params.get('v', [None])[0]

        # youtu.be/VIDEO_ID
        if 'youtu.be' in parsed.netloc:
            return parsed.path.lstrip('/')

        # youtube.com/embed/VIDEO_ID
        if 'youtube.com' in parsed.netloc and parsed.path.startswith('/embed/'):
            return parsed.path.split('/')[2]

    except Exception as e:
        logger.error(f"Failed to parse YouTube URL: {e}")

    return None


async def youtube_get_transcript(
    credentials,
    url: str,
    languages: Optional[List[str]] = None,
    preserve_formatting: bool = False
) -> Dict[str, Any]:
    """
    Get transcript (captions/subtitles) from a YouTube video

    Uses youtube-transcript-api library which is FREE and doesn't require API key!
    It works by scraping YouTube's caption data.

    Args:
        credentials: Not used (kept for consistency with other tools)
        url: YouTube video URL or video ID
        languages: Preferred languages for transcript (default: ['hr', 'en'])
        preserve_formatting: Keep timestamps and formatting (default: False)

    Returns:
        Dictionary with:
        - video_id: YouTube video ID
        - transcript: Full transcript text
        - language: Language code of transcript
        - segments: List of transcript segments with timestamps (if preserve_formatting=True)

    Example:
        result = await youtube_get_transcript(
            credentials=None,
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        )
        # Returns: {"video_id": "dQw4w9WgXcQ", "transcript": "...", "language": "en"}
    """
    try:
        # Import youtube_transcript_api
        try:
            from youtube_transcript_api import YouTubeTranscriptApi
            from youtube_transcript_api._errors import (
                TranscriptsDisabled,
                NoTranscriptFound,
                VideoUnavailable
            )
        except ImportError:
            return {
                "error": "youtube-transcript-api not installed. Run: pip install youtube-transcript-api",
                "video_id": None,
                "transcript": None
            }

        # Extract video ID
        video_id = extract_video_id(url)
        if not video_id:
            return {
                "error": f"Invalid YouTube URL or video ID: {url}",
                "video_id": None,
                "transcript": None
            }

        logger.info(f"Fetching YouTube transcript for video: {video_id}")

        # Set default languages
        if not languages:
            languages = ['hr', 'en']  # Croatian first, then English

        # Try to get transcript using new API (youtube-transcript-api 1.2.3+)
        try:
            # Create API instance
            api = YouTubeTranscriptApi()

            # Try to fetch transcript with preferred languages
            try:
                fetched = api.fetch(video_id, languages=languages, preserve_formatting=False)
                transcript_data = fetched.to_raw_data()  # Get the transcript data as list of dicts
                language_used = fetched.language_code
                logger.info(f"Found transcript in {language_used}")
            except Exception as e:
                # If no transcript in preferred languages, try with default (English)
                logger.warning(f"Could not fetch transcript in {languages}: {e}")
                fetched = api.fetch(video_id, languages=['en'], preserve_formatting=False)
                transcript_data = fetched.to_raw_data()  # Get the transcript data
                language_used = fetched.language_code
                logger.info(f"Using transcript in {language_used}")

            # Format transcript
            if preserve_formatting:
                # Keep timestamps and structure
                segments = [
                    {
                        "text": segment['text'],
                        "start": segment['start'],
                        "duration": segment['duration']
                    }
                    for segment in transcript_data
                ]
                full_text = "\n".join([
                    f"[{segment['start']:.1f}s] {segment['text']}"
                    for segment in transcript_data
                ])
            else:
                # Just concatenate text
                segments = []
                full_text = " ".join([segment['text'] for segment in transcript_data])

            result = {
                "video_id": video_id,
                "transcript": full_text,
                "language": language_used,
                "word_count": len(full_text.split()),
                "segment_count": len(transcript_data),
                "segments": segments if preserve_formatting else None
            }

            logger.info(f"Transcript fetched: {result['word_count']} words in {language_used}")
            return result

        except TranscriptsDisabled:
            return {
                "error": "Transcripts are disabled for this video",
                "video_id": video_id,
                "transcript": None
            }
        except NoTranscriptFound:
            return {
                "error": f"No transcript found in languages: {languages}",
                "video_id": video_id,
                "transcript": None
            }
        except VideoUnavailable:
            return {
                "error": "Video is unavailable",
                "video_id": video_id,
                "transcript": None
            }

    except Exception as e:
        logger.error(f"YouTube transcript extraction failed: {e}")
        return {
            "error": str(e),
            "video_id": extract_video_id(url) if url else None,
            "transcript": None
        }


async def youtube_search_and_transcript(
    credentials,
    search_query: str,
    max_results: int = 3
) -> Dict[str, Any]:
    """
    Search YouTube and get transcripts from top results

    Note: This requires YouTube Data API key for search functionality.
    For now, this is a placeholder that returns instructions.

    Args:
        credentials: Google credentials
        search_query: Search query
        max_results: Number of results to fetch transcripts for

    Returns:
        Dictionary with search results and transcripts
    """
    return {
        "error": "YouTube search requires YouTube Data API key. Use youtube_get_transcript with direct URL instead.",
        "suggestion": "Use Google Search to find YouTube videos, then use youtube_get_transcript on the URLs",
        "query": search_query
    }


def upload_to_youtube(
    credentials: Any,
    video_path: str,
    title: str,
    description: str,
    privacy_status: str = "unlisted"
) -> Dict[str, Any]:
    """
    Uploads a video to YouTube.
    
    Args:
        credentials: OAuth credentials
        video_path: Local path to the video file
        title: Video title
        description: Video description
        privacy_status: 'private', 'public', or 'unlisted'
        
    Returns:
        Dictionary with status and video ID
    """
    if not os.path.exists(video_path):
        return {"error": f"Video file not found: {video_path}"}

    try:
        client = GoogleAPIClient(credentials=credentials)
        youtube = client.get_service("youtube", "v3")
        
        body = {
            'snippet': {
                'title': title,
                'description': description,
                'tags': ['generated', 'ad'],
                'categoryId': '22' # People & Blogs
            },
            'status': {
                'privacyStatus': privacy_status
            }
        }
        
        # MediaFileUpload handles large uploads
        media = MediaFileUpload(video_path, chunksize=-1, resumable=True)
        
        request = youtube.videos().insert(
            part=','.join(body.keys()),
            body=body,
            media_body=media
        )
        
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                logger.info(f"Uploaded {int(status.progress() * 100)}%")
        
        logger.info(f"Upload complete: {response['id']}")
        
        return {
            "status": "SUCCESS",
            "video_id": response['id'],
            "video_url": f"https://youtu.be/{response['id']}"
        }

    except Exception as e:
        logger.error(f"YouTube upload failed: {e}")
        return {"error": str(e)}



def register_youtube_tools(tool_registry) -> None:
    """
    Register YouTube tools in the tool registry

    Args:
        tool_registry: ToolRegistry instance
    """
    # Register transcript tool
    tool_registry.register_tool(
        name="youtube_get_transcript",
        function=youtube_get_transcript,
        description="Get transcript (captions/subtitles) from a YouTube video. Works with Croatian and English videos. No API key required!",
        parameters={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "YouTube video URL or video ID"
                },
                "languages": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Preferred languages for transcript (default: ['hr', 'en'])",
                    "default": ["hr", "en"]
                },
                "preserve_formatting": {
                    "type": "boolean",
                    "description": "Keep timestamps and segment structure (default: false)",
                    "default": False
                }
            },
            "required": ["url"]
        },
        requires_auth=False,
        auth_type="none"
    )

    # Register upload tool
    tool_registry.register_tool(
        name="upload_to_youtube",
        function=upload_to_youtube,
        description="Uploads a video to YouTube (default: unlisted).",
        parameters={
            "type": "object",
            "properties": {
                "video_path": {
                    "type": "string",
                    "description": "Local path to the video file"
                },
                "title": {
                    "type": "string",
                    "description": "Video title"
                },
                "description": {
                    "type": "string",
                    "description": "Video description"
                },
                "privacy_status": {
                    "type": "string",
                    "enum": ["private", "public", "unlisted"],
                    "description": "Privacy status (default: unlisted)",
                    "default": "unlisted"
                }
            },
            "required": ["video_path", "title", "description"]
        }
    )

    logger.info("YouTube tools registered successfully")
