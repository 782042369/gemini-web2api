"""Apply the external-network guard to pytest as well as the unittest runner."""
import pytest

from tests.network_guard import offline_network


@pytest.fixture(autouse=True)
def forbid_external_network():
    """Permit only fake/loopback network calls. Args: None. Yields: None."""
    with offline_network():
        yield


@pytest.fixture(autouse=True)
def reset_image_upload_cache():
    """Keep the upload reference cache out of cross-test coupling.

    Identical image bytes in two tests would otherwise reuse the first
    test's (mocked) upload reference, hiding later mock expectations.

    Args:
        None.

    Yields:
        None; the cache dict is cleared before and after each test.
    """
    from gemini_web2api.server import images
    images._ref_cache.clear()
    yield
    images._ref_cache.clear()
