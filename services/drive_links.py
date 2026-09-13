"""Google Drive/Docs links found in text.

Shared because two places need the same answer and must agree: the mailer,
which grants recipients access to whatever a message links, and the approval
gate, which has to name those documents in the question and fold them into the
fingerprint. Two copies of the regex would eventually disagree, and the way
they would disagree is a mail approved for one set of documents going out
sharing another.
"""

import re
from typing import List

# The common Google Docs/Sheets/Slides/Drive link forms.
_PATTERNS = [
    re.compile(
        r"https?://docs\.google\.com/"
        r"(?:document|spreadsheets|presentation)/d/([a-zA-Z0-9_-]+)"
    ),
    re.compile(r"https?://drive\.google\.com/file/d/([a-zA-Z0-9_-]+)"),
    re.compile(r"https?://drive\.google\.com/open\?id=([a-zA-Z0-9_-]+)"),
]


def extract_file_ids(text: str) -> List[str]:
    """De-duplicated Drive/Docs file ids linked in *text*, in order."""
    ids: List[str] = []
    if not text:
        return ids
    for pattern in _PATTERNS:
        for file_id in pattern.findall(text):
            if file_id not in ids:
                ids.append(file_id)
    return ids
