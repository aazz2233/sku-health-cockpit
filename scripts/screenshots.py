"""Capture dashboard screenshots for the README.

Needs a running Streamlit instance and Playwright:

    uv pip install playwright
    uv run streamlit run src/sku_cockpit/app/dashboard.py
    uv run python scripts/screenshots.py

Uses the locally installed Edge/Chrome (channel), so no browser download is needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://localhost:8501"
OUT = Path(__file__).resolve().parents[1] / "docs" / "images"

PAGES: list[tuple[str, str, str]] = [
    ("", "01-overview.png", "SKU 经营总览"),
    ("health", "02-health.png", "SKU 健康列表"),
    ("drilldown", "03-drilldown.png", "SKU 下钻"),
    ("review", "04-review.png", "人审队列"),
]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="msedge", headless=True)
        except Exception:
            browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1500, "height": 1000})
        page = context.new_page()
        for slug, filename, marker in PAGES:
            url = f"{BASE}/{slug}"
            page.goto(url, wait_until="domcontentloaded", timeout=90_000)
            try:
                page.wait_for_selector(f"text={marker}", timeout=90_000)
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] marker not found on {slug}: {exc}")
            # charts render asynchronously; wait for the plotly canvas to exist
            try:
                page.wait_for_selector(".js-plotly-plot", timeout=30_000)
            except Exception:  # noqa: BLE001
                pass
            page.wait_for_timeout(5000)
            target = OUT / filename
            page.screenshot(path=str(target), full_page=True)
            print(f"[ok] {slug} -> {target} ({target.stat().st_size // 1024} KB)")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
