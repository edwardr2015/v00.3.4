"""Local password hashes and revocable, opaque, HttpOnly sessions."""

import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import time


def password_record(password, role="admin"):
    if len(password) < 12:
        raise ValueError("Use a password with at least 12 characters")
    salt = secrets.token_hex(24)
    return dict(
        salt=salt,
        hash=hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600000).hex(),
        role=role,
    )


def configure(path, password, viewer_password=None):
    users = {"admin": password_record(password)}
    if viewer_password:
        users["viewer"] = password_record(viewer_password, "viewer")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(users, f)


class Auth:
    def __init__(self, store):
        self.store = store
        self.path = Path(os.getenv("PROPHET_AUTH_FILE", str(Path(store.path).parent / "auth.json")))
        with store.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS web_sessions (hash TEXT PRIMARY KEY, role TEXT, expires REAL);
            CREATE TABLE IF NOT EXISTS login_attempts (time REAL, address TEXT);
            """)

    def configured(self):
        return self.path.is_file()

    def login(self, user, password, address, now=None):
        now = time.time() if now is None else now
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM login_attempts WHERE time<?", (now - 900,))
            if (
                db.execute(
                    "SELECT count(*) FROM login_attempts WHERE address=?", (address,)
                ).fetchone()[0]
                >= 5
            ):
                raise ValueError("Too many attempts; wait 15 minutes")
            db.execute("INSERT INTO login_attempts VALUES (?,?)", (now, address))
        try:
            record = json.loads(self.path.read_text()).get(user)
        except (OSError, ValueError):
            record = None
        salt = record["salt"] if record else "invalid-user-salt"
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600000).hex()
        if not record or not hmac.compare_digest(candidate, record["hash"]):
            raise ValueError("Invalid username or password")
        token = secrets.token_urlsafe(48)
        with self.store.connect() as db:
            db.execute("DELETE FROM web_sessions WHERE expires<?", (now,))
            db.execute(
                "INSERT INTO web_sessions VALUES (?,?,?)",
                (hashlib.sha256(token.encode()).hexdigest(), record["role"], now + 12 * 3600),
            )
        return token, record["role"]

    def role(self, token):
        if not token:
            return None
        with self.store.connect() as db:
            row = db.execute(
                "SELECT role FROM web_sessions WHERE hash=? AND expires>?",
                (hashlib.sha256(token.encode()).hexdigest(), time.time()),
            ).fetchone()
        return row[0] if row else None

    def logout(self, token):
        with self.store.connect() as db:
            db.execute(
                "DELETE FROM web_sessions WHERE hash=?",
                (hashlib.sha256((token or "").encode()).hexdigest(),),
            )
