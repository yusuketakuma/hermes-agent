"""PDF rendering boundary for Finance Core."""

from __future__ import annotations

import os
import signal
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Protocol

from finance_core.errors import FinanceError


class PdfRenderer(Protocol):
    def render(self, html: str) -> bytes:
        """Render complete HTML to PDF bytes without network access."""


class ChromiumPdfRenderer:
    """Render HTML through the deployment-pinned headless Chromium runtime."""

    def render(self, html: str) -> bytes:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return self._render_with_chromium_cli(html)

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    page.set_content(html, wait_until="load")
                    return page.pdf(
                        format="A4",
                        print_background=True,
                        prefer_css_page_size=True,
                    )
                finally:
                    browser.close()
        except Exception as exc:
            try:
                return self._render_with_chromium_cli(html)
            except FinanceError as fallback_exc:
                raise FinanceError(
                    f"Chromium PDF rendering failed: {type(exc).__name__}"
                ) from fallback_exc

    def _render_with_chromium_cli(self, html: str) -> bytes:
        chromium = self._find_chromium()
        if chromium is None:
            raise FinanceError(
                "Chromium PDF rendering requires Playwright or a local Chromium executable"
            )

        with tempfile.TemporaryDirectory(prefix="finance-core-render-") as directory:
            render_dir = Path(directory)
            os.chmod(render_dir, 0o700)
            html_path = render_dir / "document.html"
            pdf_path = render_dir / "document.pdf"
            profile_path = render_dir / "profile"
            profile_path.mkdir(mode=0o700)
            html_path.write_text(html, encoding="utf-8")
            os.chmod(html_path, 0o600)

            command = [
                chromium,
                "--headless=new",
                "--disable-gpu",
                "--disable-javascript",
                "--disable-background-networking",
                "--host-resolver-rules=MAP * ~NOTFOUND",
                "--disable-extensions",
                "--disable-default-apps",
                "--disable-sync",
                "--no-first-run",
                "--no-default-browser-check",
                "--no-pdf-header-footer",
                f"--user-data-dir={profile_path}",
                f"--print-to-pdf={pdf_path}",
                html_path.as_uri(),
            ]

            process: subprocess.Popen[bytes] | None = None
            try:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    try:
                        rendered = pdf_path.read_bytes()
                    except OSError:
                        rendered = b""
                    if rendered.startswith(b"%PDF-"):
                        return rendered
                    if process.poll() is not None:
                        raise FinanceError("Chromium CLI did not produce a PDF")
                    time.sleep(0.05)
                raise FinanceError("Chromium CLI rendering timed out")
            except FinanceError:
                raise
            except Exception as exc:
                raise FinanceError(
                    f"Chromium CLI rendering failed: {type(exc).__name__}"
                ) from exc
            finally:
                if process is not None:
                    self._stop_process(process)

    @staticmethod
    def _stop_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return

        kill_group = getattr(os, "killpg", None)
        if kill_group is not None:
            try:
                kill_group(process.pid, signal.SIGTERM)
            except OSError:
                process.terminate()
        else:
            process.terminate()

        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if kill_group is not None:
                try:
                    kill_group(process.pid, signal.SIGKILL)
                except OSError:
                    process.kill()
            else:
                process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    @staticmethod
    def _find_chromium() -> str | None:
        configured = os.environ.get("FINANCE_CORE_CHROMIUM")
        if configured:
            configured_path = Path(configured)
            if configured_path.is_file() and os.access(configured_path, os.X_OK):
                return str(configured_path)
            return None

        for candidate in (
            "chromium",
            "chromium-browser",
            "google-chrome",
            "google-chrome-stable",
            "chrome",
        ):
            found = shutil.which(candidate)
            if found:
                return found

        for candidate in (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ):
            if Path(candidate).is_file() and os.access(candidate, os.X_OK):
                return candidate
        return None
