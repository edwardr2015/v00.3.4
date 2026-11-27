import wave

from fastapi.testclient import TestClient

from prophet.app import create_app
from prophet.store import Store


def tone(path, seconds=1):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 8000 * seconds)


def test_win_song_served_only_from_the_data_folder(tmp_path):
    store = Store(str(tmp_path / "prophet.sqlite3"))
    app = create_app(store.path, collect=False, auth_required=False)
    with TestClient(app) as client:
        built_in = client.get("/api/win-sound/status").json()
        assert built_in["available"] and built_in["built_in"] and built_in["file"] == "jackpot.mp3"
        default = client.get("/api/win-sound")
        assert default.status_code == 200 and default.headers["content-type"] == "audio/mpeg"
        assert len(default.content) > 100_000                       # the built-in casino jackpot recording
        tone(tmp_path / "win-sound.wav")                            # the user's own file wins
        status = client.get("/api/win-sound/status").json()
        assert status == dict(available=True, file="win-sound.wav", built_in=False, folder=str(tmp_path.resolve()))
        song = client.get("/api/win-sound")
        assert song.status_code == 200 and song.headers["content-type"] == "audio/wav"
        assert song.content == (tmp_path / "win-sound.wav").read_bytes()
        csp = client.get("/").headers["content-security-policy"]
        assert "youtube" not in csp and "frame-src" not in csp
