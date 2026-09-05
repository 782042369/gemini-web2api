"""wrb.fr response parsing: candidate texts, conversation ids, cleanup."""
import json
import re


def clean_text(text: str, strip: bool = True) -> str:
    text = re.sub(
        r'```(?:python|javascript|text)\?code_(?:reference|stdout)&code_event_index=\d+\n.*?```\n?',
        '', text, flags=re.DOTALL
    )
    text = re.sub(r'http://googleusercontent\.com/card_content/\d+\n?', '', text)
    return text.strip() if strip else text


def _extract_texts_from_line(line: str) -> list:
    """Parse a single wrb.fr line and return list of text strings found."""
    if '"wrb.fr"' not in line or len(line) < 200:
        return []
    try:
        arr = json.loads(line)
        inner_str = arr[0][2]
        if not inner_str or len(inner_str) < 50:
            return []
        inner = json.loads(inner_str)
        if not (isinstance(inner, list) and len(inner) > 4 and inner[4]):
            return []
        texts = []
        for part in inner[4]:
            if isinstance(part, list) and len(part) > 1 and part[1] and isinstance(part[1], list):
                for t in part[1]:
                    if isinstance(t, str) and t:
                        texts.append(t)
        return texts
    except (json.JSONDecodeError, IndexError, TypeError):
        return []


def extract_response_text(raw: str) -> str:
    """Parse full response to get final text."""
    bard_err = re.search(r'BardErrorInfo(?:\s*"?\s*,)?\s*\[\s*(\d+)\s*\]', raw)
    if bard_err:
        raise RuntimeError(f"Gemini upstream rejected request: BardErrorInfo [{bard_err.group(1)}]")
    last_text = ""
    for line in raw.split("\n"):
        for t in _extract_texts_from_line(line):
            if len(t) > len(last_text):
                last_text = t
    return clean_text(last_text)


def _extract_conversation_id(line: str):
    """Parse a single wrb.fr line and return the conversation id (inner[1][0])."""
    if '"wrb.fr"' not in line or len(line) < 200:
        return None
    try:
        arr = json.loads(line)
        inner_str = arr[0][2]
        if not inner_str:
            return None
        inner = json.loads(inner_str)
        if isinstance(inner, list) and len(inner) > 1 and isinstance(inner[1], list) and inner[1]:
            cid = inner[1][0]
            if isinstance(cid, str) and cid:
                return cid
    except (json.JSONDecodeError, IndexError, TypeError):
        return None
    return None


def extract_conversation_id(raw: str) -> str:
    """Extract conversation id from a full (non-streaming) response."""
    for line in raw.split("\n"):
        cid = _extract_conversation_id(line)
        if cid:
            return cid
    return None
