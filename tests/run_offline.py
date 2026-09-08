"""Run the complete unittest-style and parameterized suite with network egress blocked."""
import sys

from tests.network_guard import offline_network


def main():
    """Run all tests including parameterized retry cases. Args: None. Returns: process status."""
    try:
        import pytest
    except ImportError:
        print("Test dependencies are missing; install the project's [dev] extra.", file=sys.stderr)
        return 2
    with offline_network():
        return int(pytest.main(["-q", "tests"]))


if __name__ == "__main__":
    sys.exit(main())
