"""Loss-aware parsing of numbered translation results shared by both batch paths."""
import re


def parse_numbered_translations(text, count):
    """Map output blocks to input indexes without truncating multi-line translations.

    Args:
        text: Raw numbered model output.
        count: Expected number of source segments.

    Returns:
        Valid non-empty blocks by index. Duplicate indexes are omitted so they
        can be retried individually. Out-of-range numbering invalidates the
        batch, since it may indicate a one-based/shifted index convention.
    """
    if not isinstance(text, str) or not text.strip():
        return {}
    blocks = {}
    duplicates = set()
    current = None
    lines = []

    def store():
        """Store the current unambiguous block. Args: None. Returns: None."""
        if current is None:
            return
        if current in blocks:
            duplicates.add(current)
        blocks[current] = "\n".join(lines).strip()

    for line in text.splitlines():
        marker = re.match(r"^\[(\d+)\][ \t]*(.*)$", line)
        if marker:
            store()
            index = int(marker.group(1))
            if not 0 <= index < count:
                return {}
            current = index
            lines = [marker.group(2)]
        elif current is not None:
            lines.append(line)
    store()
    return {index: value for index, value in blocks.items() if value and index not in duplicates}


def require_translation(text):
    """Reject missing model results instead of returning an empty successful translation.

    Args:
        text: Result from a direct translation attempt.

    Returns:
        Original non-empty text; raises RuntimeError for invalid/empty results.
    """
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("translation returned empty output")
    return text


def split_translation_batches(segments, max_segments, max_chars, overhead_chars=0):
    """Split by segment count and Python-character budget without altering sources.

    Args:
        segments: Iterable of original strings, including multiline/empty strings.
        max_segments: Positive maximum items per batch.
        max_chars: Positive character budget, not an estimated token count.
        overhead_chars: Nonnegative reserved instruction/wrapper characters.

    Returns:
        Ordered lists of the original strings. Each numbered segment reserves
        its index, brackets, space and newline. A single oversized segment is
        kept intact in its own batch; it is never truncated or joined with the
        next segment. Over-budget instructions similarly force singletons.
    """
    for name, value, minimum in (("max_segments", max_segments, 1), ("max_chars", max_chars, 1),
                                 ("overhead_chars", overhead_chars, 0)):
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            raise ValueError(f"{name} must be an integer >= {minimum}")
    if isinstance(segments, (str, bytes)):
        raise ValueError("translation segments must be an iterable of strings")
    result = []
    batch = []
    used = overhead_chars
    for segment in segments:
        if not isinstance(segment, str):
            raise ValueError("translation segments must be strings")
        cost = len(segment) + len(str(len(batch))) + 4
        if batch and (len(batch) >= max_segments or used + cost > max_chars):
            result.append(batch)
            batch = []
            used = overhead_chars
            cost = len(segment) + 5
        if not batch and used + cost > max_chars:
            result.append([segment])
            continue
        batch.append(segment)
        used += cost
    if batch:
        result.append(batch)
    return result
