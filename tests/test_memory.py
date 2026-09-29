import sys

from RemoteWebControl import memory


def test_release_memory_never_fails():
    result = memory.release_memory()
    if sys.platform.startswith("linux"):
        assert isinstance(result, bool)
    else:
        assert result is False  # Only the garbage collector runs outside Linux.
