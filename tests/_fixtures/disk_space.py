"""Decouple state.db repair tests from the host volume's real free space."""

import shutil


def ample_disk_usage(path, _real=shutil.disk_usage):
    """``shutil.disk_usage`` for *path*'s real volume, reported as entirely free.

    The repair/forensic-backup disk guards keep ``max(min bytes, fraction of total)`` headroom, so on a
    nearly-full developer volume they refuse every repair these tests exercise.
    """
    usage = _real(path)
    return type(usage)(usage.total, 0, usage.total)


def report_ample_disk_space(monkeypatch):
    """Install :func:`ample_disk_usage`; tests of the guard itself patch ``shutil.disk_usage`` again, overriding it."""
    monkeypatch.setattr(shutil, "disk_usage", ample_disk_usage)
