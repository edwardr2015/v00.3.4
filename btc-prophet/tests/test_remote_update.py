import asyncio, hashlib, io, json, zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from prophet import remote, updates
from prophet.app import create_app

H = {"X-Prophet-Control": "local-dashboard"}


def release(root="ai-btc-prophet-0.3.23-dev99"):
    files = {"README.md": b"x", "Start Prophet.command": b"#!/bin/bash\n",
             "pyproject.toml": b'[project]\nname="ai-btc-prophet"\nversion="0.3.23"\n', "prophet/__init__.py": b""}
    manifest = [dict(path=k, sha256=hashlib.sha256(v).hexdigest(), bytes=len(v)) for k, v in files.items()]
    files["MANIFEST.json"] = json.dumps(manifest).encode()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for k, v in files.items():
            z.writestr(root + "/" + k, v)
    return out.getvalue()


def client(tmp_path):
    return create_app(str(tmp_path / "p.sqlite3"), collect=False, auth_required=False)


def fake_feed(build, data):
    digest = hashlib.sha256(data).hexdigest()
    feed = json.dumps(dict(build=build, file="ai-btc-prophet-0.3.23-dev99.zip", sha256=digest)).encode()
    async def fetch(url, limit):
        return feed if "LATEST.json" in url else data
    return fetch, digest


def test_nothing_runs_on_its_own(tmp_path, monkeypatch):
    monkeypatch.delenv("PROPHET_AUTO_UPDATE", raising=False)
    app = client(tmp_path)
    assert app.state.updater.auto() is False
    app.state.updater.start()
    assert app.state.updater.task is None


def test_check_then_install_on_request(tmp_path, monkeypatch):
    monkeypatch.delenv("PROPHET_SUPERVISED", raising=False)
    app = client(tmp_path)
    data = release()
    app.state.updater.fetch, digest = fake_feed(remote.BUILD + 1, data)
    with TestClient(app) as c:
        assert c.post("/api/remote/install", headers=H).status_code == 409  # nothing checked yet
        state = c.post("/api/remote/check", headers=H).json()
        assert state["pending"]["sha256"] == digest
        app.state.store.put("mode", "SIMULATE")
        result = c.post("/api/remote/install", headers=H).json()
        assert "Quit and reopen" in result["message"]
        pointer = json.loads((updates.inbox(app.state.store) / "next-release.json").read_text())
        assert pointer["sha256"] == digest
        assert list(updates.inbox(app.state.store).glob("before-*.sqlite3"))  # backup made first
        assert app.state.store.get("resume_after_update") == "SIMULATE"


def test_up_to_date_and_bad_checksum(tmp_path):
    app = client(tmp_path)
    up = app.state.updater
    up.fetch, _ = fake_feed(remote.BUILD, release())
    assert "Up to date" in asyncio.run(up.check())["message"]
    data = release()
    async def bad(url, limit):
        if "LATEST.json" in url:
            return json.dumps(dict(build=remote.BUILD + 1, file="ai-btc-prophet-0.3.23-dev99.zip", sha256="0" * 64)).encode()
        return data
    up.fetch = bad
    state = asyncio.run(up.check())
    assert state["pending"] is None and "failed" in state["message"]


def test_restart_needs_pause_and_launcher(tmp_path, monkeypatch):
    app = client(tmp_path)
    with TestClient(app) as c:
        monkeypatch.delenv("PROPHET_SUPERVISED", raising=False)
        assert c.post("/api/remote/restart", headers=H).status_code == 409
        monkeypatch.setenv("PROPHET_SUPERVISED", "1")
        app.state.store.put("mode", "SIMULATE")
        assert c.post("/api/remote/restart", headers=H).status_code == 409


def test_resume_after_restart(tmp_path):
    app = client(tmp_path)
    store = app.state.store
    store.put("resume_after_update", "SIMULATE")
    app.state.updater.resume_after_restart()
    assert store.get("mode") == "SIMULATE" and store.get("resume_after_update") is None


def test_remote_page_needs_password_off_this_mac(tmp_path):
    app = create_app(str(tmp_path / "p.sqlite3"), collect=False, auth_required=True)
    with TestClient(app) as c:
        r = c.get("/remote", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login?next=/remote"
        assert c.get("/api/remote/status").status_code == 401


def test_feed_rejects_odd_file_names():
    import pytest
    with pytest.raises(ValueError):
        remote.parse_feed(json.dumps(dict(build=99, file="../evil.zip", sha256="a" * 64)), "https://x/LATEST.json")
