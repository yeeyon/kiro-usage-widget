"""
Cross-platform tests for the Kiro Usage Widget.
Run anywhere (no Kiro install needed) thanks to the fixture DB.

    python tests/make_fixture.py
    python -m pytest -q
"""
import os
import sys
import importlib

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURE = os.path.join(HERE, "fixtures", "state.vscdb")
sys.path.insert(0, ROOT)


def ensure_fixture():
    if not os.path.exists(FIXTURE):
        import make_fixture  # noqa
        sys.path.insert(0, HERE)
        importlib.import_module("make_fixture").build()


@pytest.fixture()
def widget(monkeypatch):
    ensure_fixture()
    monkeypatch.setenv("KIRO_DB_PATH", FIXTURE)
    import kiro_usage_widget as w
    importlib.reload(w)        # re-resolve DB_PATH from the env override
    return w


# ---- path resolution per OS --------------------------------------------
@pytest.mark.parametrize("plat,marker", [
    ("win32", "Kiro"),
    ("darwin", "Library/Application Support"),
    ("linux", ".config"),
])
def test_path_resolution(monkeypatch, plat, marker):
    monkeypatch.delenv("KIRO_DB_PATH", raising=False)
    import kiro_usage_widget as w
    importlib.reload(w)
    monkeypatch.setattr(w, "IS_WIN", plat == "win32")
    monkeypatch.setattr(w, "IS_MAC", plat == "darwin")
    if plat == "linux":
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    p = w.default_db_path().replace("\\", "/")
    assert "state.vscdb" in p
    assert marker in p


def test_env_override(monkeypatch):
    monkeypatch.setenv("KIRO_DB_PATH", "/tmp/x/state.vscdb")
    import kiro_usage_widget as w
    importlib.reload(w)
    assert w.default_db_path() == "/tmp/x/state.vscdb"


# ---- usage read --------------------------------------------------------
def test_read_usage(widget):
    u = widget.read_usage()
    assert u is not None
    assert u["limit"] == 2000.0
    assert 0 <= u["pct"] <= 100
    assert u["used"] == round(2000.0 * u["pct"] / 100.0, 2)
    assert len(u["reset"]) == 10  # YYYY-MM-DD


# ---- zone colors -------------------------------------------------------
def test_zone_color_boundaries(widget):
    assert widget.zone_color(0) == widget.EMERALD
    assert widget.zone_color(49.9) == widget.EMERALD
    assert widget.zone_color(50) == widget.AMBER
    assert widget.zone_color(89.9) == widget.AMBER
    assert widget.zone_color(90) == widget.ROSE
    assert widget.zone_color(100) == widget.ROSE


# ---- threshold fire-once logic -----------------------------------------
def test_thresholds_fire_once(widget, tmp_path, monkeypatch):
    monkeypatch.setattr(widget, "STATE_FILE", str(tmp_path / "alert_state.json"))
    fired = []
    u = {"pct": 95.0, "reset": "2026-07-01"}
    widget.check_thresholds(u, fired.append)
    assert sorted(fired) == [50, 90]
    fired.clear()
    widget.check_thresholds(u, fired.append)   # same cycle -> no repeat
    assert fired == []


def test_thresholds_reset_new_cycle(widget, tmp_path, monkeypatch):
    monkeypatch.setattr(widget, "STATE_FILE", str(tmp_path / "alert_state.json"))
    fired = []
    widget.check_thresholds({"pct": 60.0, "reset": "2026-07-01"}, fired.append)
    assert fired == [50]
    fired.clear()
    # new billing cycle -> fires again
    widget.check_thresholds({"pct": 60.0, "reset": "2026-08-01"}, fired.append)
    assert fired == [50]


# ---- gauge rendering ---------------------------------------------------
def test_gauge_renders(widget):
    img = widget.make_icon(42)
    assert img.size == (64, 64)
    assert img.mode == "RGBA"
    # gauge must draw *something* (non-transparent pixels)
    assert img.getbbox() is not None


def test_screenshot_mode(widget, tmp_path):
    rc = widget.run_screenshot(str(tmp_path))
    assert rc == 0
    for name in ("icon_14.png", "icon_65.png", "icon_95.png", "states.png"):
        assert (tmp_path / name).exists()


# ---- token refresh -----------------------------------------------------
import json as _json
import time as _time


def _write_token_pair(tmp_path, expires_at):
    """Write a kiro-auth-token.json + its clientIdHash registration file,
    mirroring the real AWS SSO cache layout."""
    reg_hash = "deadbeef"
    (tmp_path / f"{reg_hash}.json").write_text(_json.dumps(
        {"clientId": "cid", "clientSecret": "secret", "expiresAt": expires_at}))
    tok = tmp_path / "kiro-auth-token.json"
    tok.write_text(_json.dumps({
        "accessToken": "OLD", "refreshToken": "R1", "region": "ap-southeast-1",
        "clientIdHash": reg_hash, "expiresAt": expires_at,
    }))
    return tok


def test_parse_expiry(widget):
    assert widget._parse_expiry("2026-07-02T02:39:04.326Z") is not None
    assert widget._parse_expiry("") is None
    assert widget._parse_expiry("not-a-date") is None


def test_access_token_fresh_skips_network(widget, tmp_path, monkeypatch):
    future = _time.strftime("%Y-%m-%dT%H:%M:%SZ",
                            _time.gmtime(_time.time() + 3600))
    tok = _write_token_pair(tmp_path, future)
    monkeypatch.setenv("KIRO_TOKEN_PATH", str(tok))
    monkeypatch.setattr(widget.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not hit network")))
    assert widget._access_token() == "OLD"


def test_access_token_expired_refreshes_and_persists(widget, tmp_path, monkeypatch):
    past = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(_time.time() - 10))
    tok = _write_token_pair(tmp_path, past)
    monkeypatch.setenv("KIRO_TOKEN_PATH", str(tok))

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return _json.dumps({"accessToken": "NEW", "refreshToken": "R2",
                                "expiresIn": 3600}).encode()

    monkeypatch.setattr(widget.urllib.request, "urlopen",
                        lambda *a, **k: _Resp())
    assert widget._access_token() == "NEW"
    # rewritten to disk so the next poll / Kiro reuse the fresh token
    saved = _json.loads(tok.read_text())
    assert saved["accessToken"] == "NEW"
    assert saved["refreshToken"] == "R2"


def test_access_token_no_refresh_env(widget, tmp_path, monkeypatch):
    past = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(_time.time() - 10))
    tok = _write_token_pair(tmp_path, past)
    monkeypatch.setenv("KIRO_TOKEN_PATH", str(tok))
    monkeypatch.setenv("KIRO_NO_REFRESH", "1")
    monkeypatch.setattr(widget.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not hit network")))
    assert widget._access_token() == "OLD"   # stale, but no refresh attempted

