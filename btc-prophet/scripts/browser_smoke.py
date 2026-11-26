"""Local browser smoke test against an isolated empty database (never user accounts)."""

import hashlib
import json
import sqlite3
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import urllib.request
from zipfile import ZipFile
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
SERVER_PYTHON = os.getenv("PROPHET_BROWSER_PYTHON", str(ROOT / ".venv/bin/python"))
with tempfile.TemporaryDirectory(prefix="prophet-ui-") as folder:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    database = str(Path(folder) / "browser.sqlite")
    env = {
        **os.environ,
        "PROPHET_DB": database,
        "PROPHET_AUTH_FILE": str(Path(folder) / "auth.json"),
    }
    subprocess.run(
        [
            SERVER_PYTHON,
            "-c",
            "import os;from prophet.auth import configure;configure(os.environ['PROPHET_AUTH_FILE'],'synthetic-browser-password','synthetic-viewer-password')",
        ],
        env=env,
        cwd=ROOT,
        check=True,
    )
    code = f'from prophet.app import create_app;import uvicorn;app=create_app({database!r},collect=False);app.state.paper.recover_startup();uvicorn.run(app,host="127.0.0.1",port={port})' 
    log = open(Path(folder) / "server.log", "w")
    process = subprocess.Popen(
        [SERVER_PYTHON, "-c", code], cwd=ROOT, env=env, stdout=log, stderr=log
    )
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                urllib.request.urlopen(base + "/login", timeout=1)
                break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError("Test web server did not start")
        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path="/usr/bin/chromium", headless=True, args=["--no-sandbox"]
            )
            page = browser.new_page(viewport={"width": 1440, "height": 1100})
            page.set_default_timeout(8000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base)
            page.locator("#username").fill("admin")
            page.locator("#password").fill("synthetic-browser-password")
            page.locator("#login-form button").click()
            expect(page.locator("#research-results-dialog")).to_be_hidden()
            expect(page.locator("#competition-cards article")).to_have_count(0)
            expect(page.locator("#ai-decision")).to_have_text("Waiting")
            expect(page.locator("#selective-90-latest")).to_contain_text("90-second shadow")
            expect(page.locator("#toggle-paper")).to_be_visible()
            expect(page.locator("#ai-reason")).to_contain_text("Waiting for a qualifying entry")
            signal = dict(kind="ENTRY",side="UP",model="entry-fixture",up=.72,stake=5)
            def show_test_signal(route):
                response=route.fetch()
                state=response.json()
                state["primary_signal"]=signal
                route.fulfill(response=response,json=state)
            page.route("**/api/state",show_test_signal)
            with page.expect_response(lambda response: response.url.endswith("/api/state") and response.ok):
                page.reload()
            expect(page.locator("#ai-decision")).to_have_text("UP")
            expect(page.locator("#ai-reason")).to_have_text("$5.00 paper · entry-fixture")
            signal.update(kind="ENTRY",side="DOWN",model="entry-model-fixture")
            with page.expect_response(lambda response: response.url.endswith("/api/state") and response.ok):
                page.reload()
            expect(page.locator("#ai-decision")).to_have_text("DOWN")
            expect(page.locator("#ai-reason")).to_have_text("$5.00 paper · entry-model-fixture")
            page.unroute("**/api/state")

            page.locator("#open-research-results").click()
            expect(page.locator("#research-results-dialog")).to_be_visible()
            expect(page.locator("#competition-cards article")).to_have_count(9)
            expect(page.locator("#corrective-comparison")).to_contain_text("Reference Gap Agreement 40–70¢")
            expect(page.locator("#autonomous-accounts tr")).to_have_count(12)
            expect(page.locator("#autonomous-accounts")).to_contain_text("Reference Gap + Market Agreement")
            expect(page.locator("#pooled-consensus-result")).to_contain_text("0 settled")
            expect(page.locator("#pooled-comparison tr")).to_have_count(3)
            for index in range(9):
                details=page.locator("#competition-cards article details").nth(index)
                toggle=details.locator("summary")
                toggle.click()
                expect(details).to_have_attribute("open","")
                toggle.click()
                expect(details).not_to_have_attribute("open","")
            page.keyboard.press("Escape")
            expect(page.locator("#research-results-dialog")).to_be_hidden()
            page.reload()
            expect(page.locator("#research-results-dialog")).to_be_hidden()
            with sqlite3.connect(database) as db:
                for i in range(28):
                    up=.7 if i%2 else .3
                    db.execute("""INSERT INTO adaptive_decisions(slug,account,time,model,up,decision,reason,observation,details)
                        VALUES (?,'adaptive-1',?,'synthetic',?,'WAIT','BROWSER_FIXTURE','synthetic','{}')""",(f"browser-{i}",time.time()-1000+i,up))
            page.reload()
            page.locator("#open-research-results").click()
            expect(page.locator("#prediction-tiles button")).to_have_count(25)
            assert page.locator("#prediction-tiles button.UP").count()>0 and page.locator("#prediction-tiles button.DOWN").count()>0
            for index in range(25):
                page.locator("#prediction-tiles button").nth(index).click()
                expect(page.locator("#prediction-detail")).to_contain_text(f"browser-{27-index}")
                expect(page.locator("#prediction-detail")).to_contain_text("BROWSER_FIXTURE")
            page.goto(base+"/research-dashboard")
            expect(page).to_have_url(base+"/")
            page.wait_for_selector("#toggle-paper")
            page.get_by_role("link",name="Prophet home").click()
            expect(page).to_have_url(base+"/")
            expect(page.get_by_text("Archived adaptive paper accounts",exact=True)).to_be_hidden()
            page.locator("#open-research-results").click()
            for caption in ("Historical data audit · earlier display-price results withdrawn",
                            "Risk shutdowns and recent skipped opportunities", "Collector activity"):
                summary = page.locator("summary").filter(has_text=caption)
                summary.click()
                expect(summary.locator("..")).to_have_attribute("open","")
                summary.click()
                expect(summary.locator("..")).not_to_have_attribute("open","")
            page.get_by_role("button",name="Close research results").click()
            expect(page.locator("#research-results-dialog")).to_be_hidden()
            page.context.route("https://www.tradingview.com/",lambda route:route.fulfill(body="Attribution navigation fixture"))
            with page.expect_popup() as attribution:
                page.locator('a[href="https://www.tradingview.com/"]').click()
            expect(attribution.value).to_have_url("https://www.tradingview.com/")
            attribution.value.close()
            expect(page.locator("#games-streak")).to_contain_text("first completed paper trade")
            page.locator("#open-research-results").click()
            page.get_by_text("Why accounts skipped",exact=True).click()
            expect(page.locator("#skip-diagnostics")).to_contain_text("Waiting for completed adaptive rounds")
            page.get_by_text("Why accounts skipped",exact=True).click()
            page.get_by_role("button",name="Close research results").click()
            for interval in (30,60,15):
                with page.expect_response(f"**/api/candles?interval={interval}") as chart:
                    page.locator(f'[data-interval="{interval}"]').click()
                assert chart.value.ok
                assert "selected" in page.locator(f'[data-interval="{interval}"]').get_attribute("class")
            page.locator("#open-research-results").click()
            history = page.locator("#strategy-history")
            page.locator("#strategy-history-button").click()
            expect(history).to_be_visible()
            page.locator("#strategy-history-button").click()
            expect(history).to_be_hidden()
            page.locator("#analyze-trends").click()
            expect(page.locator("#trend-result")).to_contain_text("Insufficient Data")
            page.locator("#compare-models").click()
            page.wait_for_selector("#leaderboard tr:nth-child(162)")
            assert page.locator("#leaderboard").inner_text().count("UNKNOWN") >= 162
            page.get_by_text("Promote / roll back archived comparison",exact=True).click()
            page.locator("#promotion-config").select_option("timing/v2/momentum-v2/90/HOLD/1")
            page.locator("#promotion-reason").fill("Synthetic browser promotion check")
            page.locator("#promote-primary").click()
            expect(page.locator("#promotion-status")).to_contain_text("fresh out-of-sample evidence")
            page.locator("#rollback-primary").click()
            expect(page.locator("#promotion-status")).to_contain_text("Select a previously active strategy")
            page.get_by_role("button",name="Close research results").click()
            expect(page.locator("#toggle-paper")).to_have_text("Pause paper")
            page.locator("#toggle-paper").click()
            expect(page.locator("#notice")).to_have_text("Paper paused")
            page.locator("#toggle-paper").click()
            expect(page.locator("#toggle-paper")).to_have_text("Pause paper")
            page.locator("#toggle-paper").click()
            expect(page.locator("#notice")).to_have_text("Paper paused")
            page.locator("#emergency").click()
            expect(page.locator("#notice")).to_contain_text("Emergency stop")
            page.locator("#settings").click()
            expect(page.locator("#settings-dialog")).to_be_visible()
            for group in ("Advanced diagnostics","Software updates","Safety and learning"):
                page.get_by_text(group,exact=True).click()
            expect(page.locator("#settings-dialog")).to_contain_text("Challenger training runs automatically")
            expect(page.locator("#sms-test")).to_be_disabled()
            expect(page.locator("#rollback")).to_be_disabled()
            page.locator("#request-training").click()
            expect(page.locator("#training-result")).to_contain_text("Switch to Learn")
            page.locator("#diagnose").click()
            expect(page.locator("#diagnostic-result")).to_contain_text("Database ok")
            page.locator("#repair-feed").click()
            expect(page.locator("#diagnostic-result")).to_contain_text("Feed workers are not running")
            page.locator("#alternate-repair").click()
            expect(page.locator("#diagnostic-result")).to_contain_text("Keep the safety lock active")
            page.locator("#update-history").click()
            expect(page.locator("#update-result")).to_contain_text("Installed 0.3.23")
            page.locator("#test-update").click()
            expect(page.locator("#update-result")).to_contain_text("Choose a release ZIP")
            from io import BytesIO
            code_files = {"pyproject.toml":b'[project]\nname="ai-btc-prophet"\nversion="0.3.8"\n',
                          "Start Prophet.command":b"#!/bin/sh\nexit 0\n"}
            archive=BytesIO()
            with ZipFile(archive,"w") as z:
                for f,payload in code_files.items():
                    z.writestr("ai-btc-prophet-browser/"+f,payload)
                z.writestr("ai-btc-prophet-browser/MANIFEST.json",json.dumps(
                    [dict(path=f,bytes=len(payload),sha256=hashlib.sha256(payload).hexdigest())
                     for f,payload in code_files.items()]))
            page.locator("#update-file").set_input_files(dict(name="browser.zip",mimeType="application/zip",buffer=archive.getvalue()))
            page.locator("#update-checksum").fill(hashlib.sha256(archive.getvalue()).hexdigest())
            page.locator("#test-update").click()
            expect(page.locator("#update-result")).to_contain_text("Verified 0.3.8")
            page.locator("#stage-update").click()
            expect(page.locator("#update-result")).to_contain_text("Backup saved")
            page.locator("#activate-update").click()
            expect(page.locator("#update-result")).to_contain_text("Approved version selected")
            page.locator("#rollback-update").click()
            expect(page.locator("#update-result")).to_contain_text("current ledger is preserved")
            # This server runs with collect=False, so no alert worker sends the test.
            (Path(folder)/"sms.json").write_text(json.dumps(
                dict(sid="AC"+"a"*32,token="synthetic",to="+12025550101",sender="+12025550102")
            ))
            artifact = Path(folder)/"models"/"synthetic-browser-shadow.json"
            artifact.write_text("{}")
            with sqlite3.connect(database) as db:
                db.execute("INSERT INTO model_runs VALUES (NULL,?,?,?,?,?,?,?,?,?)",
                           ("synthetic-browser-shadow",time.time(),1,2,.25,.25,str(artifact),
                            hashlib.sha256(artifact.read_bytes()).hexdigest(),"{}"))
            page.evaluate("settingsStatus()")
            expect(page.locator("#sms-test")).to_be_enabled()
            page.once("dialog", lambda dialog: dialog.accept())
            with page.expect_response("**/api/alerts/test") as queued:
                page.locator("#sms-test").click()
            assert queued.value.json()["status"]=="PENDING"
            page.locator("#shadow-version").select_option("synthetic-browser-shadow")
            with page.expect_response("**/api/shadow-rollback/synthetic-browser-shadow") as rolled:
                page.locator("#rollback").click()
            assert rolled.value.ok
            with page.expect_response("**/api/control/review-reset") as denied:
                page.locator("#reset").click()
            assert denied.value.status==409
            page.get_by_role("button",name="Deny",exact=True).click()
            expect(page.locator("#interventions")).to_contain_text("still unresolved")
            page.get_by_role("button",name="Approve",exact=True).click()
            expect(page.locator("#intervention-feedback")).to_contain_text("Recorded")
            with page.expect_response("**/api/control/review-reset") as cleared:
                page.locator("#reset").click()
            assert cleared.value.ok
            with page.expect_response("**/api/control/formal-start") as preflight:
                page.locator("#formal-start").click()
            assert preflight.value.status == 409
            expect(page.locator("#formal-start-result")).to_contain_text("48 hours")
            with page.expect_response("**/api/control/collector-pause") as collector_off:
                page.locator("#collector-pause").click()
            assert collector_off.value.ok
            with page.expect_response("**/api/control/collector-resume") as collector_on:
                page.locator("#collector-resume").click()
            assert collector_on.value.ok
            page.locator('[data-mode="LEARN"]').click()
            page.wait_for_selector("[data-mode=LEARN].active")
            page.locator("#request-training").click()
            expect(page.locator("#training-result")).to_contain_text("Training requested")
            page.locator('[aria-label="Close settings"]').click()
            expect(page.locator("#settings-dialog")).to_be_hidden()
            url_before_export = page.url
            pending_export = []
            page.context.route("**/api/export/quick.xlsx", lambda route: pending_export.append(route))
            with page.context.expect_page() as report_tab:
                page.locator("#export").click()
            report = report_tab.value
            report.on("pageerror", lambda error: errors.append(str(error)))
            expect(report.locator("#export-status")).to_contain_text("Preparing the quick report")
            assert report.url.endswith("/reports/excel")
            assert page.url == url_before_export
            with page.expect_response(lambda response: response.url.endswith("/api/state") and response.ok):
                page.reload()
            expect(page.locator("#research-results-dialog")).to_be_hidden()
            assert len(pending_export) == 1
            pending_export[0].fulfill(status=500, json={"detail": "Synthetic export failure"})
            expect(report.locator("#export-status")).to_have_text("Synthetic export failure")
            expect(report.locator("#export-retry")).to_be_enabled()
            page.context.unroute("**/api/export/quick.xlsx")
            with report.expect_download() as download:
                report.locator("#export-retry").click()
            expect(report.locator("#export-status")).to_contain_text("Download started")
            assert page.url == url_before_export
            assert download.value.suggested_filename == "BTC-Prophet-Quick-Report.xlsx"
            with ZipFile(download.value.path()) as quick_workbook:
                xml=quick_workbook.read("xl/workbook.xml")
                assert b"Recent Performance" in xml and b"Selective 90 Summary" in xml and b"Unified Trades" in xml and b"Forward Research" in xml and b"Selective 90 Blockers" in xml and b"Entry Pricing Audit" in xml
            with report.expect_download() as full_download:
                report.locator("#export-full").click()
            assert full_download.value.suggested_filename == "BTC-Prophet-Paper-Report.xlsx"
            file = full_download.value.path()
            with ZipFile(file) as workbook:
                sheets = workbook.read("xl/workbook.xml")
                assert all(name in sheets for name in (b"Hold vs Flip Summary", b"Hold vs Flip Rounds", b"Challenger Outcomes", b"Matched Exit Results", b"Matched Exit Audit", b"Model Vote Audit", b"Model Vote Trends", b"Autonomous Model Accounts", b"Autonomous Model Decisions", b"Autonomous Model Trades", b"Agreement Price Cap Results", b"Market Change Audit", b"Timing and Market Conditions", b"Model Trade Review", b"Agreement Forward Results", b"Pooled 100 Round Audit", b"100 Agreement Comparisons"))
            output = ROOT / "data/v02-browser"
            output.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output / "desktop.png"), full_page=True)
            desktop_overflow = page.evaluate("document.documentElement.scrollWidth>innerWidth")
            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_timeout(200)
            mobile_overflow = page.evaluate("document.documentElement.scrollWidth>innerWidth")
            page.screenshot(path=str(output / "mobile.png"), full_page=True)
            assert not desktop_overflow and not mobile_overflow
            page.locator("#open-research-results").click()
            expect(page.locator("#research-results-dialog")).to_be_visible()
            expect(page.locator("#competition-cards article")).to_have_count(9)
            assert page.locator("#research-results-dialog").evaluate("(node) => node.scrollWidth <= node.clientWidth + 1")
            page.get_by_role("button",name="Close research results").click()
            page.locator("#logout").click()
            expect(page.locator("#login-form")).to_be_visible()
            page.locator("#username").fill("viewer")
            page.locator("#password").fill("synthetic-viewer-password")
            page.locator("#login-form button").click()
            expect(page.locator("#toggle-paper")).to_be_disabled()
            expect(page.locator("#emergency")).to_be_disabled()
            expect(page.locator("#formal-start")).to_be_disabled()
            assert page.request.post(base+"/api/control/formal-start",
                    headers={"X-Prophet-Control":"local-dashboard"}).status == 403
            assert not errors, errors
            result = dict(
                scope="ISOLATED EMPTY DATABASE / automated Chromium, not native Safari or macOS",
                desktop="1440x1100",
                mobile="390x844",
                javascript_errors=errors,
                horizontal_overflow=False,
                checks=[
                    "admin login, logout and viewer restrictions",
                    "one dashboard, 25 colored tiles, tile and nine model details",
                    "update validation and backup staging",
                    "diagnostic controls and collector pause/resume",
                    "chart intervals, strategy history and skip diagnostics",
                    "promotion and rollback validation",
                    "SMS queue and shadow rollback with isolated fixtures",
                    "trend button",
                    "162-row comparison with UNKNOWN P&L",
                    "LEARN, SIMULATE, PAUSE, RESUME and OFF",
                    "LIVE locked",
                    "emergency and review",
                    "formal experiment preflight, banner feedback, viewer authorization",
                    "home navigation and account, history, risk, collector disclosures",
                    "Excel export tab, dashboard refresh during export, retry and workbook validation",
                    "desktop and mobile layout",
                ],
            )
            (output / "report.json").write_text(json.dumps(result, indent=2))
            print(json.dumps(result, indent=2))
            browser.close()
    finally:
        process.terminate()
        process.wait(timeout=10)
        log.close()
