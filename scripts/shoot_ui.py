"""Screenshot the running app, in slices that can actually be read.

Every visual decision in this project was reviewed through this script. It
exists because reasoning about a stylesheet is not the same as seeing the page:
a two-column hero rendered as one column with unstyled text below it, an
animation frame stood half empty, and labels sat on top of the route - none of
which any test could have noticed.

    python -m streamlit run app/main.py --server.port 8502 --server.headless true
    python scripts/shoot_ui.py --out build/ui

Requires playwright, which is a review tool rather than a runtime dependency:

    pip install playwright && playwright install chromium
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SLICE_HEIGHT = 900


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8502")
    parser.add_argument("--out", default="build/ui")
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument(
        "--settle",
        type=float,
        default=4.0,
        help="Seconds to wait after load, so animations reach a steady frame.",
    )
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit(
            "playwright is not installed. It is a review tool, not a runtime "
            "dependency: pip install playwright && playwright install chromium"
        ) from None

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": args.width, "height": SLICE_HEIGHT})
        try:
            page.goto(args.url, wait_until="networkidle", timeout=90_000)
            # The skeleton loader renders instantly; real content does not.
            page.wait_for_selector(".hero-band", timeout=90_000)
        except Exception as exc:  # noqa: BLE001 - report and exit, do not trace
            browser.close()
            raise SystemExit(
                f"Could not reach a rendered app at {args.url}: {exc}"
            ) from None
        page.wait_for_timeout(int(args.settle * 1000))

        # Streamlit scrolls an inner container rather than the document body.
        scroller = ".stMain" if page.locator(".stMain").count() else "section.main"
        total = page.evaluate(f"document.querySelector({scroller!r}).scrollHeight")
        print(f"page height: {total}px at {args.width}px wide")

        for index, top in enumerate(range(0, total, SLICE_HEIGHT)):
            page.evaluate(f"document.querySelector({scroller!r}).scrollTo(0, {top})")
            page.wait_for_timeout(600)
            path = out / f"page_{index:02d}.png"
            page.screenshot(path=str(path))
            print(f"  {path}")
        browser.close()


if __name__ == "__main__":
    sys.exit(main())
