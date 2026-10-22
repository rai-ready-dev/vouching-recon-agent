"""Make two Windows behaviours visible on every platform: "file is in use" and the legacy default text encoding.

Windows refuses to delete a file that this process still has open; Linux and macOS allow it, so such leaks pass
silently here and then fail on a Windows laptop. On Linux this fixture reproduces the Windows behaviour: deleting a
file that is still open raises PermissionError, so a leaked handle fails the test run instead of surprising a user.
"""
import os
import pathlib
import sys

import pytest


def _open_paths() -> set[str]:
    found = set()
    for fd in os.listdir("/proc/self/fd"):
        try:
            found.add(os.readlink(f"/proc/self/fd/{fd}"))
        except OSError:
            pass
    return found


@pytest.fixture(autouse=True)
def windows_style_open_file_guard(monkeypatch):
    if not sys.platform.startswith("linux"):
        return  # on Windows the real rule applies
    real_unlink, real_remove = os.unlink, os.remove

    def guard(real):
        def inner(path, *a, **k):
            if k.get("dir_fd") is None and isinstance(path, (str, os.PathLike)):
                full = os.path.realpath(os.fspath(path))
                if full in _open_paths():
                    raise PermissionError(13, f"[simulated WinError 32] The process cannot access the file because it is being used by another process: {full}")
            return real(path, *a, **k)
        return inner

    monkeypatch.setattr(os, "unlink", guard(real_unlink))
    monkeypatch.setattr(os, "remove", guard(real_remove))


@pytest.fixture(autouse=True)
def windows_style_default_text_encoding(monkeypatch):
    """Windows opens text files as cp1252 unless told otherwise. pathlib asks io.text_encoding for the default, so
    making that return cp1252 turns every text read/write without an explicit encoding into a test failure when the
    content is not representable (Arabic names, arrows, ...)."""
    import io

    monkeypatch.setattr(io, "text_encoding", lambda encoding, stacklevel=2: encoding or "cp1252")
