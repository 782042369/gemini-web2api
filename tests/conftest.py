"""Apply the external-network guard to pytest as well as the unittest runner."""
import pytest

from tests.network_guard import offline_network


@pytest.fixture(autouse=True)
def forbid_external_network():
    """Permit only fake/loopback network calls. Args: None. Yields: None."""
    with offline_network():
        yield
