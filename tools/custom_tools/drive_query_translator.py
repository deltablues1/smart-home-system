"""
Drive Query Translator - Custom Tool

Prevodi prirodni jezik u Google Drive Query Language (QPL).
Omogućava korisnicima da traže datoteke prirodnim jezikom umjesto
učenja složene QPL sintakse.
"""

from typing import Optional
from datetime import datetime, timedelta
import re
import logging

logger = logging.getLogger(__name__)


class DriveQueryTranslator:
    """
    Translator prirodnog jezika -> Google Drive Query Language

    Primjeri:
    - "find budget from last week" -> "name contains 'budget' and modifiedTime > '2024-01-01T00:00:00'"
    - "my presentations" -> "mimeType = 'application/vnd.google-apps.presentation' and 'me' in owners"
    - "shared with john" -> "'john@example.com' in readers"
    """

    # MIME tipovi za Google Workspace i Microsoft Office
    MIME_TYPES = {
        'document': 'application/vnd.google-apps.document',
        'doc': 'application/vnd.google-apps.document',
        'docs': 'application/vnd.google-apps.document',
        'spreadsheet': 'application/vnd.google-apps.spreadsheet',
        'sheet': 'application/vnd.google-apps.spreadsheet',
        'sheets': 'application/vnd.google-apps.spreadsheet',
        'excel': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'xls': 'application/vnd.ms-excel',
        'presentation': 'application/vnd.google-apps.presentation',
        'slide': 'application/vnd.google-apps.presentation',
        'slides': 'application/vnd.google-apps.presentation',
        'powerpoint': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        'pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        'word': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'folder': 'application/vnd.google-apps.folder',
        'pdf': 'application/pdf',
        'image': 'image/',
        'video': 'video/',
    }

    def translate(self, natural_query: str) -> str:
        """
        Prevodi prirodni jezik u Drive Query Language

        Args:
            natural_query: Prirodni jezik upit

        Returns:
            Drive QPL query string
        """
        # If the input is ALREADY valid Drive Query Language, return it
        # unchanged. Otherwise this NL translator mangles it (e.g. it would
        # turn "name = 'X' and trashed = false" into "trashed = true" because
        # it substring-matches "trash" and loses the name filter).
        if self._looks_like_qpl(natural_query):
            return natural_query.strip()

        query_parts = []

        # Normaliziraj query
        query = natural_query.lower().strip()

        # 1. File name search
        name_match = self._extract_name_pattern(query)
        if name_match:
            query_parts.append(f"name contains '{name_match}'")

        # 2. File type
        file_type = self._extract_file_type(query)
        if file_type:
            query_parts.append(f"mimeType = '{file_type}'")

        # 3. Time range
        time_query = self._extract_time_range(query)
        if time_query:
            query_parts.append(time_query)

        # 4. Ownership
        if 'my ' in query or 'mine' in query:
            query_parts.append("'me' in owners")

        # 5. Sharing
        shared_with = self._extract_shared_with(query)
        if shared_with:
            query_parts.append(f"'{shared_with}' in readers or '{shared_with}' in writers")

        # 6. Starred/Trashed
        if 'starred' in query:
            query_parts.append("starred = true")
        if 'trashed' in query or 'trash' in query:
            query_parts.append("trashed = true")
        else:
            # Default: exclude trashed
            query_parts.append("trashed = false")

        # Spoji sve dijelove
        if not query_parts:
            # Fallback: search u svim poljima
            query_parts.append(f"fullText contains '{query}'")

        return ' and '.join(query_parts)

    # Drive Query Language signatures: field operators / functions that only
    # appear in real QPL, never in plain natural-language search phrases.
    _QPL_SIGNATURES = (
        'contains', 'mimetype', 'modifiedtime', 'createdtime',
        'in owners', 'in readers', 'in writers', 'in parents',
        'trashed =', 'starred =', 'fulltext', 'sharedwithme', 'name =',
    )

    def _looks_like_qpl(self, query: str) -> bool:
        """Heuristic: is this string already valid Drive Query Language?"""
        q = query.lower()
        return any(sig in q for sig in self._QPL_SIGNATURES)

    def _extract_name_pattern(self, query: str) -> Optional[str]:
        """Ekstraktira pattern iz imena datoteke"""
        # Pattern: "find <name>", "search <name>", "<name> file"
        patterns = [
            r'find\s+(["\']?)(.+?)\1(?:\s+from|\s+in|\s*$)',
            r'search\s+(?:for\s+)?(["\']?)(.+?)\1(?:\s+from|\s+in|\s*$)',
            r'(["\']?)(.+?)\1\s+(?:file|document|spreadsheet|presentation)',
        ]

        for pattern in patterns:
            match = re.search(pattern, query)
            if match:
                # Group 2 holds the name; group 1 is the optional opening quote.
                name = match.group(2) if (match.lastindex or 0) >= 2 else match.group(1)
                name = (name or "").strip().strip("'\"").strip()
                if name:
                    return name

        return None

    def _extract_file_type(self, query: str) -> Optional[str]:
        """Ekstraktira tip datoteke"""
        for keyword, mime_type in self.MIME_TYPES.items():
            if keyword in query:
                return mime_type

        return None

    def _extract_time_range(self, query: str) -> Optional[str]:
        """Ekstraktira vremenski raspon"""
        now = datetime.now()

        # "last week"
        if 'last week' in query:
            start = now - timedelta(days=7)
            return f"modifiedTime > '{start.isoformat()}'"

        # "last month"
        if 'last month' in query:
            start = now - timedelta(days=30)
            return f"modifiedTime > '{start.isoformat()}'"

        # "today"
        if 'today' in query:
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            return f"modifiedTime > '{start.isoformat()}'"

        # "this week"
        if 'this week' in query:
            start = now - timedelta(days=now.weekday())
            start = start.replace(hour=0, minute=0, second=0, microsecond=0)
            return f"modifiedTime > '{start.isoformat()}'"

        # "after <date>" (e.g., "after 2024-01-01")
        match = re.search(r'after\s+(\d{4}-\d{2}-\d{2})', query)
        if match:
            return f"modifiedTime > '{match.group(1)}T00:00:00'"

        # "before <date>"
        match = re.search(r'before\s+(\d{4}-\d{2}-\d{2})', query)
        if match:
            return f"modifiedTime < '{match.group(1)}T23:59:59'"

        return None

    def _extract_shared_with(self, query: str) -> Optional[str]:
        """Ekstraktira email iz "shared with" patterna"""
        # "shared with john@example.com"
        match = re.search(r'shared\s+with\s+([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', query)
        if match:
            return match.group(1)

        # "shared with john" (bez domene - moramo pretpostaviti ili tražiti u kontaktima)
        match = re.search(r'shared\s+with\s+([a-zA-Z]+)', query)
        if match:
            name = match.group(1)
            logger.warning(f"Partial email detected: {name}. Consider using full email address.")
            return name

        return None


def translate_drive_query(natural_query: str) -> str:
    """
    Helper funkcija za prevođenje prirodnog jezika u Drive QPL

    Args:
        natural_query: Prirodni jezik upit

    Returns:
        Drive Query Language string
    """
    translator = DriveQueryTranslator()
    return translator.translate(natural_query)


# Function declaration za ADK
def get_drive_query_translator_tool():
    """
    Vraća Tool sa Drive Query Translator FunctionDeclaration

    Returns:
        Tool objekt
    """
    from google.genai.types import Tool, FunctionDeclaration

    return Tool(
        function_declarations=[
            FunctionDeclaration(
                name="translate_drive_query",
                description=(
                    "Translate natural language to Google Drive Query Language (QPL). "
                    "Supports queries like: 'find budget from last week', 'my presentations', "
                    "'shared with john@example.com', 'starred documents'."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "natural_query": {
                            "type": "string",
                            "description": "Natural language search query"
                        }
                    },
                    "required": ["natural_query"]
                }
            )
        ]
    )
