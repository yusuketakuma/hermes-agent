"""Tests for the Finance Core PDF renderer boundary."""

from __future__ import annotations

import subprocess
import sys
import os
from pathlib import Path


def test_renderer_falls_back_to_local_chromium_cli(
    tmp_path: Path,
    monkeypatch,
):
    from finance_core.renderer import ChromiumPdfRenderer

    chromium = tmp_path / "chromium"
    chromium.write_text("test binary placeholder", encoding="utf-8")
    chromium.chmod(0o700)
    monkeypatch.setenv("FINANCE_CORE_CHROMIUM", str(chromium))
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)

    calls: list[tuple[list[str], dict[str, object]]] = []

    class FakeProcess:
        pid = 12345

        def __init__(self, command, **kwargs):
            calls.append((command, kwargs))
            self.returncode = 0
            pdf_path = next(
                Path(argument.split("=", 1)[1])
                for argument in command
                if argument.startswith("--print-to-pdf=")
            )
            pdf_path.write_bytes(b"%PDF-1.7\ncli-fallback\n%%EOF\n")

        def poll(self):
            return self.returncode

        def wait(self, **_kwargs):
            return self.returncode

    monkeypatch.setattr(subprocess, "Popen", FakeProcess)

    pdf = ChromiumPdfRenderer().render("<!doctype html><p>preview</p>")

    assert pdf.startswith(b"%PDF-1.7")
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[0] == str(chromium)
    assert "--headless=new" in command
    assert "--no-pdf-header-footer" in command
    assert any(argument.startswith("--print-to-pdf=") for argument in command)
    assert any(argument.startswith("file://") for argument in command)
    assert kwargs["start_new_session"] is True


def test_renderer_stops_chromium_after_pdf_is_ready(tmp_path: Path, monkeypatch):
    from finance_core.renderer import ChromiumPdfRenderer

    chromium = tmp_path / "chromium"
    chromium.write_text("test binary placeholder", encoding="utf-8")
    chromium.chmod(0o700)
    monkeypatch.setenv("FINANCE_CORE_CHROMIUM", str(chromium))
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)

    class FakeProcess:
        pid = 12345

        def __init__(self, command, **kwargs):
            self.command = command
            self.kwargs = kwargs
            self.returncode = None
            pdf_path = next(
                Path(argument.split("=", 1)[1])
                for argument in command
                if argument.startswith("--print-to-pdf=")
            )
            pdf_path.write_bytes(b"%PDF-1.7\nready\n%%EOF\n")

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def wait(self, **_kwargs):
            self.returncode = 0
            return self.returncode

    process_holder: list[FakeProcess] = []

    def fake_popen(command, **kwargs):
        process = FakeProcess(command, **kwargs)
        process_holder.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(os, "killpg", lambda *_args: None)

    pdf = ChromiumPdfRenderer().render("<!doctype html><p>preview</p>")

    assert pdf.startswith(b"%PDF-1.7")
    assert len(process_holder) == 1
    assert process_holder[0].kwargs["start_new_session"] is True
