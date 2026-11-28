"""Loopback-only dashboard service. This application has no trading endpoints."""

import asyncio
import logging
from ipaddress import ip_address
import json
import os
import sqlite3
import shutil
import sys
import time
from tempfile import TemporaryDirectory
from contextlib import asynccontextmanager, closing
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .collector import Collector
from .agreement import AgreementResearch
from . import observer, research_audit
from .adaptive_entry import BOOK_CONFIRM_MAX_AGE, REFERENCE_MAX_AGE, get_state
from .providers import timestamp, validate_market
from .store import Store
from .v2paper import Engine as PaperEngine
from .auth import Auth
from . import analytics, competition, intervention, operations, remote, updates
from .learning import Learning, matched_forecasts
from . import model_paper, pooled_consensus, selective_forecast, unified, corrective
from .lab import Lab
from .runtime import Runtime
from .alerts import Alerts

STATIC = Path(__file__).parent / "static"
EXPORT_TIMEOUT = 600
QUICK_EXPORT_TIMEOUT = 30


async def build_excel(database, destination, *, quick=False, kind=None):
    options = [kind] if kind else (["quick"] if quick else [])
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "prophet.v2reporting", str(Path(database).resolve()), str(destination), *options,
        env={**os.environ, "TMPDIR": str(destination.parent)},
    )
    try:
        return await asyncio.wait_for(process.wait(), timeout=QUICK_EXPORT_TIMEOUT if quick else EXPORT_TIMEOUT)
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


def create_app(db_path=None, collect=True, auth_required=True):
    store = Store(db_path or os.getenv("PROPHET_DB", "data/prophet.sqlite3"))
    collector = Collector(store)
    collector.enabled = not store.get("collection_paused",False)
    paper = PaperEngine(store)
    learning = Learning(store, os.getenv("PROPHET_HISTORY_DB", "data/history.sqlite3"))
    # Legacy SmartResearch remains available for audit; v2 lab has independent positions.
    auth = Auth(store)
    lab = Lab(store, paper)
    agreement = AgreementResearch(store, lab)
    lean = os.getenv("PROPHET_LEAN") == "1"
    # Lean mode (2.3 default): the research models that never beat the simple formula stop running.
    paper.smart = None if lean else agreement
    runtime = Runtime(store, collector, paper)
    alerts = Alerts(store)
    corrective_research = corrective.Research(store,paper)
    updater = remote.Updater(store, paper)
    from .hourly import Hourly
    hourly = Hourly(store, collector)
    from .feeds import Referee
    referee = Referee(store)
    from .live import LiveTrader
    live_trader = LiveTrader(store)
    from .watchdog import Watchdog
    dog = Watchdog(store, paper)
    export_lock = asyncio.Lock()
    quick_export_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
        )
        logging.getLogger("httpx").setLevel(logging.WARNING)
        store.event("INFO", "Read-only research service started")
        if collect:
            await runtime.start()
        try:
            if collect:
                paper.recover_startup()
                updater.resume_after_restart()
                collector.enabled = True
                await collector.start()
                await paper.start()
                await learning.start()
                await alerts.start()
                if not lean:
                    corrective_research.start()
                updater.start()
                hourly.start()
                referee.start()
                live_trader.start()
                dog.start()
            yield
        finally:
            await runtime.close(release=False)
            try:
                await updater.close()
                await hourly.close()
                await referee.close()
                await live_trader.close()
                await dog.close()
                await corrective_research.close()
                await alerts.close()
                await learning.close()
                await paper.close()
                await collector.close()
            finally:
                runtime.release()

    app = FastAPI(title="AI BTC Prophet Trader", version="2.5.0", lifespan=lifespan)
    app.state.store = store
    app.state.collector = collector
    app.state.paper = paper
    app.state.agreement = agreement
    app.state.learning = learning
    app.state.auth = auth
    app.state.updater = updater
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"]
        + [h.strip() for h in os.getenv("PROPHET_HOSTS", "").split(",") if h.strip()],
    )

    @app.middleware("http")
    async def security(request: Request, call_next):
        from fastapi.responses import JSONResponse, RedirectResponse

        if not auth_required:
            client = request.client.host if request.client else ""
            try:
                local = ip_address(client).is_loopback
            except ValueError:
                local = client == "testclient" and request.url.hostname == "testserver"
            if not local or request.url.hostname not in ("127.0.0.1", "localhost", "::1", "testserver"):
                return JSONResponse({"detail": "Password-free access is local to this computer"}, status_code=403)
            if request.url.path == "/login":
                return RedirectResponse("/", status_code=303)

        public = request.url.path in ("/login", "/api/login") or request.url.path.startswith(
            "/static/"
        )
        display = os.getenv("PROPHET_PUBLIC_DISPLAY") == "1" and request.url.path in (
            "/display",
            "/api/display",
        )
        role = auth.role(request.cookies.get("prophet_session")) if auth_required else "admin"
        if auth_required and not public and not display and not role:
            if request.url.path == "/":
                return RedirectResponse("/login", status_code=303)
            if request.url.path == "/remote":
                return RedirectResponse("/login?next=/remote", status_code=303)
            return JSONResponse({"detail": "Sign in required"}, status_code=401)
        if (
            role == "viewer"
            and request.method not in ("GET", "HEAD")
            and request.url.path != "/api/logout"
        ):
            return JSONResponse({"detail": "Read-only account"}, status_code=403)
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                raise_error = {"detail": "Cross-origin control requests are forbidden"}
                from fastapi.responses import JSONResponse

                return JSONResponse(raise_error, status_code=403)
            if request.headers.get("x-prophet-control") != "local-dashboard":
                from fastapi.responses import JSONResponse

                return JSONResponse({"detail": "Missing dashboard control header"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        )
        return response

    @app.get("/api/session")
    def session(request: Request):
        return dict(
            role=auth.role(request.cookies.get("prophet_session")) if auth_required else "admin",
            auth_required=auth_required,
        )

    @app.get("/login")
    def login_page():
        return FileResponse(STATIC / "login.html")

    @app.post("/api/login")
    async def login(request: Request):
        if not auth.configured():
            raise HTTPException(503, "Run scripts/setup_auth.py locally first")
        body = await request.json()
        try:
            token, role = auth.login(
                str(body.get("username", "")), str(body.get("password", "")), request.client.host
            )
        except ValueError as exc:
            raise HTTPException(401, str(exc)) from exc
        from fastapi.responses import JSONResponse

        response = JSONResponse(dict(role=role))
        response.set_cookie(
            "prophet_session",
            token,
            httponly=True,
            samesite="strict",
            max_age=43200,
            secure=os.getenv("PROPHET_SECURE_COOKIES") == "1",
        )
        return response

    @app.post("/api/logout")
    def logout(request: Request):
        auth.logout(request.cookies.get("prophet_session"))
        response = Response(status_code=204)
        response.delete_cookie("prophet_session")
        return response

    @app.get("/api/models")
    def compare_models():
        return lab.leaderboard()

    @app.get("/api/model-performance")
    def model_performance():
        return lab.model_performance()

    @app.get("/api/adaptive")
    def adaptive_status():
        with store.connect() as db:
            rows = [
                dict(r)
                for r in db.execute("SELECT * FROM adaptive_selections ORDER BY time DESC LIMIT 50")
            ]
        summary = paper.summary()
        return dict(enabled=store.get("mode") == "SIMULATE",
                    policy="CONTINUOUS AFTER-COST EV", latest=summary["adaptive_decision"],
                    selections=rows)

    @app.post("/api/adaptive/{action}")
    def adaptive_control(action: str):
        if action not in ("shadow", "approved-simulation"):
            raise HTTPException(404, "Unknown adaptive action")
        if action == "approved-simulation":
            raise HTTPException(409, "Use SIMULATE to run continuous entries and independent timing shadows")
        try:
            lab.adaptive.enable(False)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return dict(enabled=action == "approved-simulation")

    @app.get("/api/strategies")
    def strategy_history():
        summary = paper.summary()
        return dict(
            active=summary["strategy"],
            history=summary["strategy_history"],
            performance=summary["strategy_performance"],
        )

    @app.post("/api/promote")
    async def promote_strategy(request: Request):
        from .promotion import promote, rollback_strategy

        body = await request.json()
        if not isinstance(body,dict):
            raise HTTPException(422,"Expected a promotion request object")
        try:
            if body.get("rollback"):
                result = rollback_strategy(
                    store, int(body.get("strategy_id", 0)), str(body.get("reason", ""))
                )
            else:
                result = promote(store,str(body.get("configuration","")),str(body.get("reason","")))
        except (OSError, TypeError, ValueError) as exc:
            store.event("WARNING", "Model switch request rejected: "+str(exc))
            raise HTTPException(409, str(exc)) from exc
        store.event("INFO", "Owner approved and completed model switch: "+str(result.get("model","rollback")))
        return result

    def decision_display(market, now):
        active = market and validate_market(market, now)
        mode = store.get("mode", "OFF")
        with store.connect() as db:
            predicate = """decision IN ('UP','DOWN') AND up IS NOT NULL AND time<=?
                            AND json_extract(details,'$.protocol')='fixed-timing-v1'"""
            recent = db.execute(
                f"SELECT decision FROM v2_decisions WHERE {predicate} ORDER BY time DESC,checkpoint DESC LIMIT 20",
                (now,),
            ).fetchall()
            call = db.execute(
                f"""SELECT decision,checkpoint,time FROM v2_decisions
                    WHERE {predicate} AND slug=? ORDER BY time DESC,checkpoint DESC LIMIT 1""",
                (now,market["slug"]),
            ).fetchone() if active and mode in ("LEARN", "SIMULATE") and not store.get("emergency", False) else None
        up = sum(row["decision"] == "UP" for row in recent)
        count = len(recent)
        return dict(call=dict(call) if call else None, count=count, up_count=up, down_count=count-up,
                    up_percent=100*up/count if count else None,
                    down_percent=100*(count-up)/count if count else None)

    def primary_signal(slug, now, mode, quotes_ready, emergency, baseline):
        waiting = dict(kind="WAITING", side=None, model=None, baseline=baseline)
        if not slug:
            return waiting
        with store.connect() as db:
            db.execute("BEGIN")
            if paper.unified_enabled:
                position = db.execute("""SELECT side,model,up,time,stake FROM unified_trades
                    WHERE slug=? AND status='OPEN' AND time<=?""", (slug,now)).fetchone()
                return dict(position,kind="ENTRY",baseline=baseline) if position else waiting
            position = db.execute("""SELECT side,model,probability,time FROM v2_orders
                WHERE account='adaptive-1' AND slug=? AND status='OPEN' AND time<=?""", (slug,now)).fetchone()
            if position:
                return dict(position, kind="ENTRY", baseline=baseline, up=position["probability"])
            if mode == "OFF" or emergency or not quotes_ready:
                return waiting
            forecast = db.execute("""SELECT model,up,time FROM adaptive_decisions
                WHERE account='adaptive-1' AND slug=? AND time BETWEEN ? AND ?
                  AND decision IN ('WAIT','OBSERVE','UP','DOWN')
                ORDER BY time DESC,id DESC LIMIT 1""", (slug,now-REFERENCE_MAX_AGE,now)).fetchone()
        if not forecast or forecast["up"] in (None,.5):
            return waiting
        return dict(forecast, kind="FORECAST", baseline=baseline,
                    side="UP" if forecast["up"]>.5 else "DOWN")

    def benchmark_results(now):
        experiment = store.get("v2_experiment") or {}
        start = experiment.get("start", store.get("paper_pilot_started"))
        with store.connect() as db:
            rows = db.execute("""SELECT o.pnl FROM lab_orders o JOIN lab_configs c ON c.id=o.config
                WHERE c.id LIKE 'timing/%' AND c.version='momentum-v2' AND c.checkpoint=90
                AND c.exit='HOLD' AND c.stake=1 AND o.status='SETTLED' AND o.time>=? AND o.settled<=?
                ORDER BY o.settled,o.id""",(start,now)).fetchall() if start is not None else []
        pnl = [row["pnl"] for row in rows]
        values = ["WIN" if item>0 else "LOSS" if item<0 else "FLAT" for item in pnl]
        runs = analytics.streaks(values,"WIN","LOSS")
        return dict(start=start, wins=values.count("WIN"), losses=values.count("LOSS"),
                    flats=values.count("FLAT"), pnl=sum(pnl), current=runs["current"],
                    streak=runs["length"], longest_win=runs["longest"]["WIN"],
                    longest_loss=runs["longest"]["LOSS"])

    @app.get("/api/display")
    def public_data():
        if os.getenv("PROPHET_PUBLIC_DISPLAY") != "1":
            raise HTTPException(404, "Public display is disabled")
        with store.connect() as db:
            timing = [dict(row) for row in db.execute("""SELECT c.checkpoint, count(o.id) AS trades,
                sum(o.pnl) AS pnl FROM lab_configs c LEFT JOIN lab_orders o
                ON o.config=c.id AND o.status='SETTLED'
                WHERE c.id LIKE 'timing/%' AND c.family='momentum' AND c.exit='HOLD' AND c.stake=1
                GROUP BY c.checkpoint ORDER BY c.checkpoint""")]
        now = time.time()
        market = store.get("market")
        return dict(
            reference=store.get("reference"),
            spot=store.get("spot"),
            market=market,
            server_time=now,
            decision_display=decision_display(market, now),
            benchmark_results=benchmark_results(now),
            live_locked=True,
            timing=timing,
        )

    @app.get("/display")
    def public_display():
        if os.getenv("PROPHET_PUBLIC_DISPLAY") != "1":
            raise HTTPException(404, "Public display is disabled")
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/",status_code=307)

    @app.get("/api/history")
    def history_board():
        return analytics.history(store)

    @app.post("/api/analyze-trends")
    def analyze_trends():
        return analytics.analyze(store)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/research-dashboard")
    def research_dashboard():
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/",status_code=307)

    @app.get("/api/competition")
    def model_competition():
        catalog = lab.models.catalog()
        with store.connect() as db:
            return competition.standings(db,time.time(),catalog)

    @app.get("/api/model-paper")
    def autonomous_accounts():
        with store.connect() as db:
            db.execute("BEGIN")
            return model_paper.standings(db)

    @app.get("/api/selective-forecast")
    def selective_status():
        with store.connect() as db:
            db.execute("BEGIN")
            return selective_forecast.summary(db)

    @app.get("/api/pooled-consensus")
    def pooled_status():
        with store.connect() as db:
            db.execute("BEGIN")
            return dict(pooled_consensus.summary(db), comparisons=pooled_consensus.comparisons(db))

    @app.get("/api/pick-history")
    def pick_history():
        with store.connect() as db:
            return [dict(r) for r in db.execute("""WITH latest AS (
                SELECT *,row_number() OVER (PARTITION BY slug ORDER BY time DESC,id DESC) AS number
                FROM adaptive_decisions WHERE account='adaptive-1' AND up IS NOT NULL AND up<>.5
                AND time<=? AND decision NOT IN ('SKIP')
            )
            SELECT d.slug,d.time,d.model,d.up,d.decision,d.reason,v.outcome,
                CASE WHEN d.up>.5 THEN 'UP' ELSE 'DOWN' END AS side
            FROM latest d LEFT JOIN verified_markets v ON v.slug=d.slug
            WHERE d.number=1 ORDER BY d.time DESC LIMIT 25""",(time.time(),))]

    @app.get("/api/diagnostics")
    def diagnostics():
        with store.connect() as db:
            integrity=db.execute("PRAGMA quick_check").fetchone()[0]
        return dict(integrity=integrity,free_disk_bytes=shutil.disk_usage(Path(store.path).parent).free,
                    runtime=store.get("runtime_health"),backup=store.get("last_backup"),mode=store.get("mode","OFF"),
                    emergency=store.get("emergency",False),events=store.events())

    @app.post("/api/diagnostics/{action}")
    async def repair(action: str):
        if action=="diagnose":
            return operations.sample(store,check_integrity=True)
        if action=="reconnect":
            ok,message=await operations.reconnect(store,collector,started=collect)
            if not ok:
                raise HTTPException(409,message)
            return dict(message=message)
        if action=="alternative":
            now=time.time()
            with store.connect() as db:
                operations.journal(db,now,"alternative","PROPOSED",
                    dict(message="Keep the safety lock while reviewing the feed and latest backup."))
            return dict(message="Keep the safety lock active. Review feed diagnostics and the latest backup before trying a repair.")
        raise HTTPException(404,"Unknown repair action")

    @app.get("/api/updates")
    def update_history():
        with store.connect() as db:
            return dict(current=app.version,history=[dict(r) for r in db.execute("""
                SELECT time,action,state,details FROM repair_history
                WHERE action IN ('test-update','stage-update','activate-update','rollback-update') ORDER BY id DESC LIMIT 20""")])

    @app.post("/api/updates/test")
    async def test_update(request: Request):
        data=bytearray()
        async for chunk in request.stream():
            if len(data)+len(chunk)>updates.MAX_ARCHIVE:
                raise HTTPException(413,"The update ZIP is too large.")
            data.extend(chunk)
        try:
            return updates.test_update(store,bytes(data),request.headers.get("x-prophet-checksum"))
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from exc

    @app.post("/api/updates/stage/{digest}")
    def stage_update(digest: str):
        if store.get("mode","OFF")!="OFF":
            raise HTTPException(409,"Pause before staging an update.")
        try:
            return updates.stage(store,digest)
        except (OSError,ValueError) as exc:
            raise HTTPException(409,str(exc)) from exc

    @app.post("/api/updates/activate/{digest}")
    def activate_update(digest: str):
        if store.get("mode","OFF")!="OFF":
            raise HTTPException(409,"Pause before selecting a release.")
        try:
            return updates.select_release(store,None if digest=="rollback" else digest)
        except (OSError,ValueError) as exc:
            raise HTTPException(409,str(exc)) from exc

    @app.get("/remote")
    def remote_page():
        return FileResponse(STATIC / "remote.html")

    @app.get("/api/remote/status")
    def remote_status():
        return updater.status()

    @app.post("/api/remote/check")
    async def remote_check():
        if updater.busy.locked():
            raise HTTPException(409, "An update check is already running.")
        async with updater.busy:
            return await updater.check()

    @app.post("/api/remote/install")
    def remote_install():
        state = store.get("auto_update_status", None) or {}
        if not state.get("pending"):
            raise HTTPException(409, "No verified update is waiting. Press Check for update first.")
        if store.get("emergency", False):
            raise HTTPException(409, "Clear the safety lock before installing an update.")
        return updater.install(state["pending"])

    @app.post("/api/remote/main-model/{value}")
    def main_model(value: str):
        """Owner switch: main account forecast = official settlement-average rule (off by default)."""
        if value not in ("settlement", "settlement_live", "selective", "standard"):
            raise HTTPException(404, "Use standard, settlement, settlement_live or selective")
        store.put("main_model", value)
        store.put("main_model_settlement", value != "standard")
        store.event("INFO", "Main account forecast set to " + value)
        return dict(main_model=value)

    @app.get("/api/export/recorder.csv.gz")
    def export_recorder(hours: float = Query(24, gt=0, le=72)):
        """Second-by-second recorder rows (order book, BRTI, spot) for research, gzip CSV."""
        import csv, gzip, io
        buffer = io.BytesIO()
        with gzip.GzipFile(fileobj=buffer, mode="wb") as gz, io.TextIOWrapper(gz, encoding="utf-8", newline="") as text:
            writer = csv.writer(text)
            writer.writerow(["time","slug","elapsed","book","book_timestamp","reference_price",
                             "reference_timestamp","spot_price","spot_timestamp","spot_source"])
            with store.connect() as db:
                for row in db.execute("SELECT * FROM fresh_recorder WHERE time>=? ORDER BY time",
                                      (time.time()-hours*3600,)):
                    writer.writerow(list(row))
        return Response(buffer.getvalue(), media_type="application/gzip",
                        headers={"Content-Disposition": "attachment; filename=BTC-Prophet-Recorder.csv.gz"})

    @app.get("/api/audit")
    def system_audit():
        from . import audit, feeds, fresh, hourly, shadow_rules, selective50
        with store.connect() as db:
            for module in (feeds, fresh, hourly):
                module.initialize(db)
            return audit.run(db, states=referee.states)

    @app.get("/api/brain")
    def brain_view():
        from . import brain
        with store.connect() as db:
            return brain.thinking(db)

    @app.get("/api/live")
    def live_status():
        from . import live
        return live.status(store)

    @app.post("/api/live/mode/{value}")
    def live_mode(value: str, request: Request):
        """Real-money switch. Only from this computer; LIVE needs the confirmation header."""
        from . import live
        client = request.client.host if request.client else ""
        try:
            local = ip_address(client).is_loopback
        except ValueError:
            local = client == "testclient"
        if not local:
            raise HTTPException(403, "The real-money switch only works on the Mac running the bot.")
        if value.lower() == "live" and request.headers.get("x-prophet-confirm") != "real-money":
            raise HTTPException(428, "Add the header X-Prophet-Confirm: real-money to turn on real trading.")
        try:
            return dict(mode=live.set_mode(store, value, "owner request"))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/feeds")
    def feed_referee():
        from . import feeds
        with store.connect() as db:
            return feeds.summary(db, referee.states)

    @app.post("/api/remote/restart")
    def remote_restart():
        if not remote.supervised():
            raise HTTPException(409, "Restart from the phone needs the dev13 launcher. Quit and reopen Start Prophet.")
        if store.get("mode", "OFF") != "OFF":
            raise HTTPException(409, "Pause before restarting.")
        updater.restart()
        return dict(ok=True, message="Restarting. This page reconnects in about a minute.")

    @app.get("/api/pick")
    def pick():
        now = time.time()
        market = store.get("market")
        active = bool(market and validate_market(market, now))
        with store.connect() as db:
            current = observer.status(db, observer.MAIN, now)
            current.update(observer.scoreboard(db, observer.MAIN))
            mode = get_state(db, "mode") or "OFF"
            enabled = bool(get_state(db, "observer_enabled"))
            emergency = bool(get_state(db, "emergency"))
            call = None
            if active and enabled and not emergency and mode == "LEARN":
                call = db.execute("""SELECT r.side AS decision,r.time,d.checkpoint
                    FROM observer_rounds r JOIN v2_decisions d ON d.slug=r.slug AND d.time=r.time
                    WHERE r.account=? AND r.slug=? AND r.time<=? AND d.decision=r.side
                    ORDER BY d.checkpoint LIMIT 1""",(observer.MAIN,market["slug"],now)).fetchone()
        return dict(call=dict(call) if call else None, mode=mode, observer=current,
                    enabled=enabled, emergency=emergency, server_time=now,
                    close=timestamp(market["assetPriceTerms"]["windowEnd"]) if active else None)

    @app.get("/api/research-diagnostics")
    def research_diagnostics():
        with store.connect() as db:
            return research_audit.skip_diagnostics(db)

    @app.get("/api/agreement-research")
    def agreement_research():
        return agreement.results()

    @app.post("/api/observer/{action}")
    def observer_control(action: str):
        if action not in ("start", "stop"):
            raise HTTPException(404, "Unknown observer control")
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if action == "start" and (get_state(db, "emergency") or db.execute(
                "SELECT 1 FROM v2_accounts WHERE halted IS NOT NULL LIMIT 1"
            ).fetchone()):
                raise HTTPException(409, "Complete the safety review in research settings first.")
            paper._state(db,"observer_enabled",action=="start")
            paper._state(db,"mode","LEARN" if action=="start" else "OFF")
        store.event("INFO", "Observer started" if action=="start" else "Observer stopped")
        return dict(enabled=action=="start")

    @app.get("/api/state")
    def state():
        now = time.time()
        market = store.get("market")
        reference = store.get("reference")
        book = store.get("book")
        spot = store.get("spot")
        active = bool(market and validate_market(market, now))
        availability = store.get("availability") or {}
        no_open_market = (availability.get("available") is False
                          and 0 <= now-availability.get("checked",0) <= 10)
        active_book = bool(active and book and book["slug"] == market["slug"]
                           and book.get("state") == "MARKET_STATE_OPEN")
        health = {}
        for name, value in [("reference", reference), ("market", book), ("spot", spot)]:
            h = store.get("health_" + name, {"ok": False, "checked": None, "error": None})
            age = now - value["timestamp"] if value else None
            checked = h.get("checked")
            last_success = h.get("last_success", checked if h.get("ok") else None)
            if not collector.enabled:
                status = "disconnected"
            elif name == "market" and no_open_market:
                status = "refreshing"
            elif checked is not None and now-checked>30:
                status = "disconnected"
            elif checked is None:
                status = "refreshing"
            elif not h.get("ok"):
                status = "refreshing" if last_success is not None and now-last_success<=30 else "disconnected"
            elif age is None or (name == "market" and not active_book):
                status = "refreshing"
            elif (age < 0 or (name == "market" and now-checked > BOOK_CONFIRM_MAX_AGE)
                  or (name != "market" and age > REFERENCE_MAX_AGE)):
                status = "delayed"
            else:
                status = "connected"
            h.update(age_seconds=age, status=status, last_success=last_success,
                     source_timestamp=value["timestamp"] if value else None)
            health[name] = h
        if not active_book:
            book = None
        if paper.unified_enabled:
            with store.connect() as db:
                account_lock = bool(db.execute("SELECT 1 FROM v2_accounts WHERE halted IS NOT NULL").fetchone())
                prediction = db.execute("SELECT * FROM v2_decisions ORDER BY time DESC,checkpoint DESC LIMIT 1").fetchone()
                decision = db.execute("SELECT * FROM adaptive_decisions ORDER BY id DESC LIMIT 1").fetchone()
                summary = dict(model=db.execute("SELECT model FROM strategy_changes ORDER BY id DESC LIMIT 1").fetchone()[0],
                               prediction=dict(prediction) if prediction else None,
                               adaptive_decision=dict(decision) if decision else None)
        else:
            summary = paper.summary()
            account_lock = any(a["halted"] for a in summary["accounts"])
        feed_block = paper.data_block(now)
        quote_eligible = bool(active_book and book and book.get("bids") and book.get("asks") and not feed_block)
        execution_state = (
            "EXECUTION BLOCKED" if store.get("emergency") or account_lock
            else "FRESH AND ELIGIBLE" if quote_eligible
            else "STALE" if health["market"]["status"] == "delayed" or health["reference"]["status"] == "delayed"
            else "UNAVAILABLE"
        )
        mode = store.get("mode", "OFF")
        emergency = store.get("emergency", False)
        return dict(
            version=app.version+" · paper research",
            server_time=now,
            collecting=collector.enabled,
            emergency=emergency,
            mode=mode,
            live_locked=True,
            lean=os.getenv("PROPHET_LEAN") == "1",
            prediction=summary["prediction"],
            primary_prediction=summary["adaptive_decision"],
            primary_signal=primary_signal(market["slug"] if active else None, now, mode,
                                          quote_eligible, emergency, summary["model"]),
            next_evaluation=store.get("next_evaluation"),
            next_market_start=(market or {}).get("assetPriceTerms",{}).get("nextWindowStart"),
            decision_display=decision_display(market, now),
            benchmark_results=benchmark_results(now),
            last_outcome=store.get("last_outcome"),
            model_version=summary["model"],
            reference=reference,
            spot=spot,
            market=market,
            market_active=active,
            seconds_remaining=max(0, timestamp(market["assetPriceTerms"]["windowEnd"]) - now)
            if market
            else None,
            book=book,
            health=health,
            execution_state=execution_state,
            execution_data_block=feed_block,
            availability=store.get("availability"),
            events=store.events(),
        )

    @app.get("/api/candles")
    def candles(interval: int = Query(15)):
        if interval not in (15, 30, 60):
            raise HTTPException(422, "Choose 15, 30 or 60 seconds")
        return dict(
            source="CF Benchmarks BRTI via Polymarket US",
            interval=interval,
            candles=store.candles(interval),
        )

    @app.get("/api/health")
    def health():
        s = state()
        return dict(
            service="running",
            providers=s["health"],
            live_trading=False,
            runtime=store.get("runtime_health"),
            last_backup=store.get("last_backup"),
        )

    @app.get("/api/research")
    def research():
        root = Path(os.getenv("PROPHET_HISTORY_DB", "data/history.sqlite3"))
        result = dict(available=False, active_model_changed=False, live_trading=False)
        if not root.exists():
            return result
        try:
            with closing(
                sqlite3.connect("file:" + str(root.resolve()) + "?mode=ro", uri=True)
            ) as db:
                result.update(
                    available=True,
                    reference_candles=db.execute("SELECT count(*) FROM reference15").fetchone()[0],
                    resolved_contracts=db.execute("SELECT count(*) FROM markets").fetchone()[0],
                    contracts_with_quotes=db.execute(
                        "SELECT count(DISTINCT slug) FROM quotes"
                    ).fetchone()[0],
                    quarantined_quotes=db.execute("SELECT count(*) FROM quote_issues").fetchone()[
                        0
                    ],
                )
        except sqlite3.Error:
            return dict(available=False, message="Historical dataset is being initialized")
        for name, filename in [
            ("coverage", "history-coverage.json"),
            ("baseline", "baseline-report.json"),
            ("challenger", "challenger-report.json"),
        ]:
            try:
                result[name] = json.loads((root.parent / filename).read_text())
            except (OSError, ValueError):
                result[name] = None
        try:
            result["coverage"] = json.loads(
                (root.parent / "history-14day-coverage.json").read_text()
            )
        except (OSError, ValueError):
            pass
        try:
            result["backtest"] = json.loads((root.parent / "backtest14/report.json").read_text())
        except (OSError, ValueError):
            result["backtest"] = None
        return result

    @app.post("/api/control/{action}")
    def control(action: str):
        if action in ("start", "formal-start"):
            try:
                paper.mode("SIMULATE", formal=action == "formal-start")
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        elif action == "stop":
            paper.mode("OFF")
            store.put("paused_mode",None)
        elif action == "pause":
            prior = store.get("mode","OFF")
            if prior != "OFF":
                paper.mode("OFF")
                store.put("paused_mode",prior)
        elif action == "resume":
            paused = store.get("paused_mode")
            if paused not in ("LEARN","SIMULATE") or store.get("emergency",False):
                raise HTTPException(409,"There is no resumable session or a safety lock is active.")
            try:
                paper.mode(paused)
            except ValueError as exc:
                raise HTTPException(409,str(exc)) from exc
            store.put("paused_mode",None)
        elif action in ("collector-pause","collector-resume"):
            if action=="collector-pause" and store.get("emergency",False):
                raise HTTPException(409,"Collection remains on during emergency review.")
            enabled = action=="collector-resume"
            with store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if enabled:
                    db.execute("DELETE FROM state WHERE key IN ('health_market','health_reference')")
                paper._state(db,"collection_paused",not enabled)
            collector.enabled = enabled
        elif action == "emergency":
            from . import live
            live.set_mode(store, "OFF", "emergency stop")
            store.put("paused_mode",None)
            store.put("collection_paused",False)
            store.put("emergency", True)
            paper.halt("EMERGENCY_STOP")
            collector.enabled = True  # Observation continues under the emergency latch.
        elif action == "review-reset":
            with store.connect() as db:
                unresolved = db.execute("""SELECT 1 FROM intervention_requests r
                    JOIN risk_events e ON r.issue=e.reason AND r.account IS e.account
                    WHERE e.reviewed IS NULL AND r.status IN ('PENDING','DENIED')
                    AND r.id=(SELECT max(id) FROM intervention_requests WHERE issue=r.issue AND account IS r.account)
                    LIMIT 1""").fetchone()
            if unresolved:
                raise HTTPException(409, "Review the request in Settings and approve it before clearing the lock.")
            paper.review()
            store.put("emergency", False)
        else:
            raise HTTPException(404, "Unknown control")
        from . import watchdog
        watchdog.remember(store, action)
        store.event("INFO", "User control: " + action)
        return dict(ok=True, action=action)

    @app.get("/api/paper")
    def paper_state():
        return paper.summary()

    @app.get("/api/unified")
    def unified_state():
        with store.connect() as db:
            db.execute("BEGIN")
            return unified.summary(db,time.time())

    WIN_SOUND_TYPES = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac", ".wav": "audio/wav"}

    BUILT_IN_SOUND = Path(__file__).resolve().parent / "static" / "sounds" / "jackpot.mp3"

    def win_sound():
        """The user's own song (win-sound.mp3/.m4a/.aac/.wav next to the database) if present,
        otherwise the built-in casino jackpot recording (Pixabay, free licence)."""
        folder = Path(store.path).resolve().parent
        for ext in WIN_SOUND_TYPES:
            path = folder / ("win-sound" + ext)
            if path.is_file() and 0 < path.stat().st_size <= 50 * 1024 * 1024:
                return path
        return BUILT_IN_SOUND if BUILT_IN_SOUND.is_file() else None

    @app.get("/api/win-sound/status")
    def win_sound_status():
        path = win_sound()
        return dict(available=bool(path), file=path.name if path else None,
                    built_in=bool(path) and path == BUILT_IN_SOUND,
                    folder=str(Path(store.path).resolve().parent))

    @app.get("/api/win-sound")
    def win_sound_file():
        path = win_sound()
        if not path:
            raise HTTPException(404, "Put your song in the data folder as win-sound.mp3 or win-sound.m4a")
        return FileResponse(path, media_type=WIN_SOUND_TYPES[path.suffix])

    @app.post("/api/unified/reset-scoreboard")
    def unified_reset_scoreboard():
        """Restart the dashboard counters only; trades, cash and exports keep full history."""
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            since = unified.reset_scoreboard(db,time.time())
        store.event("INFO","Paper scoreboard counter reset (history kept)")
        return dict(counting_since=since)

    @app.get("/api/corrective-research")
    def corrective_state():
        with store.connect() as db:
            db.execute("BEGIN")
            return corrective.summary(db,time.time())

    @app.get("/api/research-comparison")
    def research_comparison():
        now = time.time()
        with store.connect() as db:
            db.execute("BEGIN")
            return dict(challenger=matched_forecasts(db,now),large=unified.matched_large(db,now))

    @app.get("/api/competition/preflight")
    def competition_preflight():
        with store.connect() as db:
            return competition.preflight(db, time.time())

    MODEL_LEARNING = {
        "logistic": "Retrains every hour", "random_forest": "Retrains every hour",
        "gradient_boosting": "Retrains every hour", "ensemble": "Retrains every hour",
        "xgboost": "Retrains every hour", "lightgbm": "Retrains every hour",
        "pattern_odds": "Learns from settled rounds",
        "momentum": "Fixed rule, does not learn", "model_vote": "Fixed rule, does not learn",
        "market_odds_adjustment": "Fixed rule, does not learn",
        "volatility_reference_gap": "Fixed rule, does not learn",
        "reference_gap_agreement": "Fixed rule, does not learn",
    }

    @app.get("/api/models-overview")
    def models_overview():
        """One read-only view of every model account, whether it learns, and the learning record."""
        with store.connect() as db:
            db.execute("BEGIN")
            models = []
            for m in model_paper.standings(db):
                closed = m.get("closed") or 0
                models.append(dict(family=m["family"], label=m["label"],
                    learning=MODEL_LEARNING.get(m["family"], "Fixed rule, does not learn"),
                    version=(m.get("latest") or {}).get("version"), trades=closed,
                    wins=m.get("wins", 0), losses=m.get("losses", 0), pnl=m.get("pnl", 0),
                    cash=m.get("cash"), win_rate=(m.get("wins", 0)/closed) if closed else None))
            runs = [dict(r) for r in db.execute(
                "SELECT time,brier,baseline_brier FROM model_runs ORDER BY id DESC LIMIT 200")]
            day = time.time()-86400
            minute = db.execute("""SELECT f.minute, count(*) FROM unified_minute_forecasts f
                JOIN unified_rounds r ON r.slug=f.slug JOIN verified_markets v ON v.slug=f.slug AND v.end=r.end
                GROUP BY f.minute ORDER BY f.minute""").fetchall()
            latest = db.execute("SELECT details FROM unified_rounds WHERE reason='ENTERED' ORDER BY start DESC LIMIT 1").fetchone()
            source = json.loads(latest[0]).get("calibration_source", "first_forecast") if latest else None
        status = store.get("learning_status") or {}
        return dict(
            primary=dict(model="Volatility Reference Gap", exit=unified.primary_exit(),
                         learning="Adjusts its confidence", calibration_source=source,
                         minute_labels={str(k): n for k, n in minute}, minute_labels_needed=unified.MINUTE_LABELS),
            learning=dict(last_trained=runs[0]["time"] if runs else None,
                          trainings_last_24h=sum(r["time"] >= day for r in runs),
                          examples=status.get("examples"), brier=runs[0]["brier"] if runs else None,
                          baseline_brier=runs[0]["baseline_brier"] if runs else None, coin_flip_brier=.25,
                          first_brier=runs[-1]["brier"] if runs else None, trades_main_account=False),
            models=models)

    @app.get("/api/learning")
    def learning_state():
        with store.connect() as db:
            versions = [
                dict(r)
                for r in db.execute(
                    "SELECT version,time,brier,baseline_brier FROM model_runs ORDER BY id DESC LIMIT 30"
                )
            ]
            shadows = [
                dict(r)
                for r in db.execute(
                    "SELECT version,count(*) predictions,sum(outcome IS NOT NULL) evaluated,avg(brier) brier FROM model_shadows GROUP BY version"
                )
            ]
        return dict(
            enabled=store.get("learning_enabled", False),
            status=store.get("learning_status"),
            shadow_version=store.get("shadow_version"),
            versions=versions,
            shadows=shadows,
            active_model_unchanged=True,
        )

    @app.post("/api/learning/request")
    def queue_learning():
        now = time.time()
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if get_state(db,"mode") not in ("LEARN","SIMULATE") or get_state(db,"emergency"):
                raise HTTPException(409,"Switch to Learn or Simulate before requesting training.")
            previous = get_state(db,"last_manual_learning_request") or 0
            if now-previous < 300:
                raise HTTPException(409,"A training batch was already requested in the last five minutes.")
            paper._state(db,"last_manual_learning_request",now)
            paper._state(db,"learning_last_attempt",0)
            operations.journal(db,now,"training-request","QUEUED",dict(active_model_unchanged=True))
        return dict(message="Training requested. Results appear in Learning History.")
    
    @app.post("/api/learning/{action}")
    def learning_control(action: str):
        if action not in ("start", "stop"):
            raise HTTPException(404, "Unknown learning control")
        store.put("learning_enabled", action == "start")
        return dict(enabled=action == "start")

    @app.post("/api/shadow-rollback/{version}")
    def rollback(version: str):
        try:
            learning.rollback(version)
        except (OSError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return dict(shadow_version=version, active_model_unchanged=True)

    @app.get("/api/backtest")
    def backtest():
        try:
            return json.loads((Path(store.path).parent / "backtest14/report.json").read_text())
        except (OSError, ValueError):
            return dict(status="NOT_RUN", scenarios=[])

    @app.get("/api/interventions")
    def interventions():
        with store.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM intervention_requests ORDER BY id DESC LIMIT 50")]

    @app.post("/api/interventions/{request_id}/{action}")
    async def decide_intervention(request_id: int, action: str, request: Request):
        if action not in ("approve", "deny"):
            raise HTTPException(404, "Unknown decision")
        body = await request.json()
        note = str(body.get("note", ""))[:500] if isinstance(body,dict) else ""
        status = "APPROVED" if action == "approve" else "DENIED"
        if not intervention.resolve(store,request_id,status,note):
            raise HTTPException(409, "Request is missing or already decided")
        return dict(status=status, message="Recorded. Safety controls remain in place.")

    @app.get("/api/alerts")
    def alert_state():
        return alerts.summary()

    @app.post("/api/alerts/test")
    def alert_test():
        if not alerts.configured():
            raise HTTPException(409, "SMS provider is not configured")
        import uuid

        with store.connect() as db:
            db.execute(
                "INSERT INTO alert_outbox(event_key,time,reason,body,status,updated) VALUES (?,?,?,?,?,?)",
                (
                    "test:" + str(uuid.uuid4()),
                    time.time(),
                    "TEST",
                    "BTC Prophet PAPER: this is your requested SMS test. No real-money trading is enabled.",
                    "PENDING",
                    time.time(),
                ),
            )
        return dict(status="PENDING", message="Queued test; check provider delivery status")

    @app.get("/reports/excel")
    def export_page():
        return FileResponse(STATIC / "excel.html")

    async def send_report(*, quick, kind=None):
        lock = quick_export_lock if quick else export_lock
        if lock.locked():
            raise HTTPException(409, "An Excel report is already being prepared. Try again when it finishes.")

        async with lock:
            folder = TemporaryDirectory(prefix="prophet-excel-")
            report_ready = False
            try:
                path = Path(folder.name) / "report.xlsx"
                options = {"quick": True} if quick else {}
                if kind:
                    options = {"quick": True, "kind": kind}
                if await build_excel(store.path, path, **options) != 0:
                    raise HTTPException(500, "Excel export failed. Check the Prophet Terminal for the error and try again.")
                response = FileResponse(
                    path,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    filename=("BTC-Prophet-Research-Report.xlsx" if kind == "research" else
                              "BTC-Prophet-Quick-Report.xlsx" if quick else "BTC-Prophet-Paper-Report.xlsx"),
                    headers={"Cache-Control": "no-store", "Accept-Ranges": "none"},
                    background=BackgroundTask(folder.cleanup),
                )
                report_ready = True
                return response
            except TimeoutError as exc:
                limit = "30 seconds" if quick else "10 minutes"
                raise HTTPException(504, f"Excel export took longer than {limit}. Please try again.") from exc
            except OSError as exc:
                logging.getLogger(__name__).exception("Unable to prepare Excel report")
                raise HTTPException(500, "Could not write the Excel report. Check free disk space and try again.") from exc
            finally:
                if not report_ready:
                    folder.cleanup()

    @app.get("/api/export.xlsx")
    async def export():
        return await send_report(quick=False)

    @app.get("/api/export/quick.xlsx")
    async def quick_export():
        return await send_report(quick=True)

    @app.get("/api/export/research.xlsx")
    async def research_export():
        return await send_report(quick=True, kind="research")

    @app.post("/api/mode/{value}")
    def mode(value: str):
        try:
            paper.mode(value)
        except ValueError as exc:
            raise HTTPException(423 if value == "LIVE" else 409, str(exc)) from exc
        return dict(mode=value)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app(auth_required=os.getenv("PROPHET_AUTH_REQUIRED", "1") != "0")
