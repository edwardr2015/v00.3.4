"""Optional browser check: pip install playwright; python scripts/smoke_browser.py.

Run the local server first. Uses system Chromium when available; otherwise run
`playwright install chromium`. Captures stay in ignored data/.
"""

import json
import os
import shutil
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

output = Path("data")
output.mkdir(exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch(
        executable_path=shutil.which("chromium"),
        headless=True,
        args=["--no-sandbox"] if shutil.which("chromium") else [],
    )
    page = browser.new_page(viewport={"width": 1440, "height": 1100})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(os.getenv("PROPHET_URL", "http://127.0.0.1:8000"), wait_until="networkidle")
    expect(page.locator("#reference-price")).not_to_have_text("—", timeout=20000)
    page.locator('[data-interval="30"]').click()
    expect(page.locator('[data-interval="30"]')).to_have_class("selected")
    page.locator('[data-mode="LEARN"]').click()
    page.locator("#session-controls summary").click()
    expect(page.locator("#stop")).to_be_enabled()
    page.locator("#stop").click()
    expect(page.locator('[data-mode="OFF"]')).to_have_class("mode active")
    page.locator("#emergency").click()
    expect(page.locator("#notice")).to_contain_text("EMERGENCY STOP LATCHED")
    page.locator("#settings").click()
    expect(page.locator("#settings-dialog")).to_be_visible()
    page.locator("#reset").click()
    expect(page.locator("#notice")).not_to_contain_text("EMERGENCY STOP LATCHED")
    page.get_by_role("button", name="Close settings").click()
    pending_export = []
    page.context.route("**/api/export/quick.xlsx", lambda route: pending_export.append(route))
    with page.context.expect_page() as opened, page.context.expect_event("request",
            predicate=lambda request: request.url.endswith("/api/export/quick.xlsx")):
        page.locator("#export").click()
    report = opened.value
    expect(report.locator("#export-status")).to_contain_text("Preparing the quick report")
    with report.expect_download() as download:
        pending_export[0].continue_()
    exported = download.value
    page.context.unroute("**/api/export/quick.xlsx")
    exported.save_as(str(output / "browser-export.xlsx"))
    from openpyxl import load_workbook

    assert "Recent Paper Trades" in load_workbook(output / "browser-export.xlsx").sheetnames
    assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
    page.screenshot(path=str(output / "dashboard-desktop.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator("#settings")).to_be_visible()
    assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
    page.screenshot(path=str(output / "dashboard-mobile.png"), full_page=True)
    # Verify local failures become an explicit warning while preserving observations.
    previous = page.locator("#reference-price").inner_text()
    page.route("**/api/state", lambda route: route.abort())
    expect(page.locator("#notice")).to_contain_text("Cannot reach the local service", timeout=8000)
    assert page.locator("#reference-price").inner_text() == previous
    assert not errors, errors
    result = dict(
        desktop="passed",
        mobile="passed",
        controls="passed",
        disconnect_warning="passed",
        javascript_errors=errors,
    )
    (output / "browser-check.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))
    browser.close()
