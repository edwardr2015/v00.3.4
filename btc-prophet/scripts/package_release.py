"""Build a private Mac trial archive from an allowlist; exclude credentials and user ledgers."""

import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT.parent / "ai-btc-prophet-v0.2-mac.zip"
STAGE = ROOT / "build/mac/ai-btc-prophet-v0.2"


def build():
    if STAGE.exists():
        if not STAGE.resolve().is_relative_to((ROOT / "build").resolve()):
            raise RuntimeError("Unsafe package staging path")
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True, exist_ok=True)
    tracked = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT, text=True
    ).splitlines()
    allow = ("prophet/", "scripts/", "tests/", "docs/", "ops/")
    for name in tracked:
        if not (
            name.startswith(allow)
            or name in ("README.md", "pyproject.toml", "Start Prophet.command", ".gitignore")
        ):
            continue
        src = ROOT / name
        if not src.is_file() or "__pycache__" in src.parts:
            continue
        target = STAGE / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
    research = STAGE / "research"
    research.mkdir(exist_ok=True)
    history = ROOT / "data/history.sqlite3"
    if history.exists():
        with sqlite3.connect(history) as src, sqlite3.connect(research / "history.sqlite3") as dst:
            src.backup(dst)
    cache = ROOT / "data/history-cache"
    if cache.exists():
        shutil.copytree(cache, research / "history-cache", dirs_exist_ok=True)
    depth = ROOT / "data/depth-samples"
    if depth.exists():
        shutil.copytree(depth, research / "depth-samples", dirs_exist_ok=True)
    rows = []
    with sqlite3.connect(ROOT / "data/prophet.sqlite3") as db:
        db.row_factory = sqlite3.Row
        for row in db.execute("SELECT * FROM model_registry"):
            r = dict(row)
            path = Path(r.pop("path"))
            if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != r["sha256"]:
                continue
            (research / "models").mkdir(exist_ok=True)
            shutil.copy2(path, research / "models" / path.name)
            r["filename"] = path.name
            rows.append(r)
    (research / "model-registry.json").write_text(json.dumps(rows, indent=2))
    with sqlite3.connect(ROOT / "data/verified-bootstrap.sqlite3") as db:
        db.row_factory = sqlite3.Row
        rows = [
            dict(r) for r in db.execute("SELECT * FROM verified_markets ORDER BY end DESC LIMIT 50")
        ]
    (research / "verified-markets.json").write_text(json.dumps(rows, indent=2))
    (research / "README.md").write_text(
        "Private research copy: official historical data, provenance cache, sampled US depth, model artifacts and verified settlements. Earlier display-price P&L is withdrawn. Artifacts are research only; no active experiment or credentials are included. Do not publish data to GitHub. Installation preserves existing database rows.\n"
    )
    manifest = []
    for path in sorted(STAGE.rglob("*")):
        if path.is_file():
            manifest.append(
                dict(
                    path=str(path.relative_to(STAGE)),
                    bytes=path.stat().st_size,
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            )
    (STAGE / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))
    with zipfile.ZipFile(OUTPUT, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(STAGE.rglob("*")):
            if path.is_file():
                archive.write(path, str(path.relative_to(STAGE.parent)))
        assert archive.testzip() is None
    print(
        json.dumps(
            dict(
                file=str(OUTPUT),
                bytes=OUTPUT.stat().st_size,
                sha256=hashlib.sha256(OUTPUT.read_bytes()).hexdigest(),
                files=len(manifest) + 1,
            )
        )
    )


if __name__ == "__main__":
    build()
