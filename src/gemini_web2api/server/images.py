"""Image upload helper shared by every API protocol handler."""
from ..config import CONFIG
from ..budget import RequestControlError, check_budget
from ..multimodal import detect_image_mime, fetch_image_bytes, upload_image


class UploadedFileRef(str):
    """String-compatible upstream file reference carrying its MIME type."""

    def __new__(cls, value, mime_type):
        """Create a reference that serializes as a normal string.

        Args:
            value: Upstream file reference path.
            mime_type: Detected media type.

        Returns:
            A string-compatible reference instance.
        """
        obj = str.__new__(cls, value)
        obj.mime_type = mime_type
        return obj


def _upload_images(images: list) -> list:
    """Upload images and return list of file references. Returns None if no images."""
    if not images:
        return None
    file_refs = []
    for item in images:
        check_budget("image processing")
        if not (isinstance(item, tuple) and len(item) == 2):
            continue
        data, mime = item
        if isinstance(data, str):
            data = fetch_image_bytes(data)
            mime = mime or "image/png"
        if not data:
            raise RuntimeError("image fetch failed")
        max_bytes = int(CONFIG.get("max_image_bytes") or 0)
        if max_bytes > 0 and len(data) > max_bytes:
            raise RuntimeError(f"image exceeds {max_bytes} bytes")
        mime = detect_image_mime(data, mime or "image/png")
        try:
            ref = upload_image(data, "image.png", mime or "image/png")
            check_budget("image upload")
            file_refs.append(UploadedFileRef(ref, mime or "image/png"))
        except RequestControlError:
            raise
        except Exception as e:
            check_budget("image upload")
            raise RuntimeError(f"image upload failed: {e}") from e
    return file_refs if file_refs else None
