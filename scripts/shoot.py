#!/usr/bin/env python
"""Screenshot a page in both themes and report console errors.

The Chrome extension is not available on this machine, so this headless
Playwright run is how UI work actually gets looked at rather than assumed.

    .venv/bin/python scripts/shoot.py http://127.0.0.1:8700 /tmp/ui
"""

from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright


def main(url: str, prefix: str, width: int = 1440, height: int = 950) -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}")
                if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        page.on("requestfailed", lambda r: problems.append(f"requestfailed: {r.url}"))

        page.goto(url, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(1200)
        page.screenshot(path=f"{prefix}-light.png", full_page=True)

        page.emulate_media(color_scheme="dark")
        page.wait_for_timeout(400)
        page.screenshot(path=f"{prefix}-dark.png", full_page=True)

        # A page that scrolls sideways is a layout bug, not a preference.
        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
        if overflow > 2:
            problems.append(f"horizontal overflow: {overflow}px")

        browser.close()

    print(f"{prefix}-light.png\n{prefix}-dark.png")
    print("\n".join(problems) if problems else "no console errors, no overflow")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "/tmp/shot"))
