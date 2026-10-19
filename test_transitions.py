"""Synthetic flip-logic tests: run python3 test_transitions.py (no network, no ntfy sends)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from app import compute_transitions

def cur(avail, quote=""):
    return {"R231|MK1H4LL/A": {"available": avail, "quote": quote, "store": "Example Store", "part": "MK1H4LL/A"}}

# 1) no baseline -> build baseline, no alert (even if available)
alerts, st = compute_transitions(None, cur(True, "Available Today"))
assert alerts == [] and st["R231|MK1H4LL/A"]["available"] is True; print("OK baseline: first run alerts nothing")
# 2) false->false (prelaunch default -> unavailable) -> no alert
alerts, st = compute_transitions({"R231|MK1H4LL/A": {"available": False, "quote": "Check back for availability on 10.24"}}, cur(False, "Unavailable for Pickup"))
assert alerts == []; print("OK false->false (prelaunch->unavailable): no alert")
# 3) false->true -> one available alert
alerts, st = compute_transitions(st, cur(True, "Available Tomorrow"))
assert len(alerts) == 1 and alerts[0]["kind"] == "available" and alerts[0]["prev_quote"] == "Unavailable for Pickup"; print("OK false->true: 1 available alert")
# 4) repeated true polls -> no alert
for _ in range(3):
    alerts, st = compute_transitions(st, cur(True, "Available Tomorrow")); assert alerts == []
print("OK true->true x3: no repeat alerts")
# 5) true->false -> one soldout alert with prev/current quote
alerts, st = compute_transitions(st, cur(False, "Unavailable for Pickup"))
assert len(alerts) == 1 and alerts[0]["kind"] == "soldout" and alerts[0]["prev_quote"] == "Available Tomorrow" and alerts[0]["quote"] == "Unavailable for Pickup"; print("OK true->false: 1 soldout alert")
# 6) repeated false -> no alert
alerts, st = compute_transitions(st, cur(False, "Unavailable for Pickup")); assert alerts == []; print("OK false->false after soldout: no alert")

# --- send_alert body format (dry-run: urlopen captured, nothing is sent) ---
import json as _json
import app as _app
_captured = {}
class _R:
    def read(self): return b"{}"
def _fake_urlopen(req, timeout=10):
    _captured["url"] = req.full_url; _captured["payload"] = _json.loads(req.data.decode("utf-8")); return _R()
_app.urlopen = _fake_urlopen; _app.WEBHOOK_URL = "https://ntfy.sh/dry-run-only"; _app.PUBLIC_URL = "https://tracker.example.com"
_base = {"product_title": "iPhone Duo 512GB Night Sky", "part": "MK1H4LL/A", "price": "$2,199",
         "buy_url": "https://www.apple.com/shop/buy-iphone/iphone-duo/7.6-inch-display-512gb-night-sky",
         "store": "Example Store", "city": "Example City", "store_state": "CA", "store_zip": "90210",
         "distance": "1.0 mi", "address": "1 Example Street", "phone": "555-0100",
         "hours": "Sun: 11:00 AM-7:00 PM", "quote": "Available Tomorrow"}
assert _app.send_alert(dict(_base, kind="available")) is True
_body = _captured["payload"]["message"]; print("DRY-RUN available body:\n" + _body)
assert _captured["payload"]["click"] == _base["buy_url"] and "Example Store" in _captured["payload"]["title"]
for _bad in ["Example Street", "555-01", "Example City", "90210", "1.0", "营业", "地址", "电话"]:
    assert _bad not in _body, _bad
print("OK dry-run available: no address/phone/city/zip/distance/hours, click=buyUrl")
assert _app.send_alert(dict(_base, kind="soldout", quote="Unavailable for Pickup", prev_quote="Available Tomorrow")) is True
_body2 = _captured["payload"]["message"]; print("DRY-RUN soldout body:\n" + _body2)
assert "已无货: Unavailable for Pickup（之前: Available Tomorrow）" in _body2 and _captured["payload"]["click"] == _base["buy_url"]
for _bad in ["Example Street", "555-01", "Example City", "90210", "1.0"]:
    assert _bad not in _body2, _bad
print("OK dry-run soldout: minimal body, click=buyUrl")

# --- email dry-run: captured SMTP (no real mail), flipped fan-out counts ---
class _FakeSMTP:
    instances = []
    def __init__(self, host, port, timeout=None): self.host, self.port = host, port; self.sent = []; _FakeSMTP.instances.append(self)
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def starttls(self): pass
    def login(self, u, p): self.user = u; return (235, b'OK')
    def send_message(self, em): self.sent.append(em)
_orig_smtp = _app.smtplib.SMTP
_app.smtplib.SMTP = _FakeSMTP
_app.ALERT_EMAIL = "alert@example.com"; _app.SMTP_HOST = "smtp.example.invalid"; _app.SMTP_USER = "sender@example.com"; _app.SMTP_PASSWORD = "dry-run-only"; _app.SMTP_FROM = "sender@example.com"
# graceful skip when unconfigured
_saved_pw = _app.SMTP_PASSWORD; _app.SMTP_PASSWORD = ""
assert _app.send_email_alert(dict(_base, kind="available")) is False; print("OK email skipped gracefully when SMTP_PASSWORD empty")
_app.SMTP_PASSWORD = _saved_pw
_web_before = []
_orig_send_alert = _app.send_alert
def _counting_send_alert(msg):
    _web_before.append(msg); return _orig_send_alert(msg)
_app.send_alert = _counting_send_alert
def _emails():
    return [em for inst in _FakeSMTP.instances for em in inst.sent]
def _drive(prev, avail, quote):
    alerts, newst = _app.compute_transitions(prev, {"R231|MK1H4LL/A": dict(_base, available=avail, quote=quote)})
    for a in alerts: _app.notify(a)
    return alerts, newst
alerts, st2 = _drive(None, False, "Check back for availability on 10.24")
assert alerts == [] and len(_web_before) == 0 and len(_emails()) == 0; print("OK baseline: 0 ntfy, 0 email")
alerts, st2 = _drive(st2, False, "Unavailable for Pickup")
assert alerts == [] and len(_web_before) == 0 and len(_emails()) == 0; print("OK false->false: 0 ntfy, 0 email")
alerts, st2 = _drive(st2, True, "Available Tomorrow")
assert len(alerts) == 1 and len(_web_before) == 1 and len(_emails()) == 1
_em = _emails()[0]; print("DRY-RUN email (available) Subject:", _em["Subject"]); print(_em.get_content())
assert _em["To"] == "alert@example.com" and "店: Example Store" in _em.get_content()
for _bad in ["Example Street", "555-01", "Example City", "90210", "1.0"]:
    assert _bad not in _em.get_content(), _bad
print("OK false->true: exactly 1 ntfy + 1 email, minimal body")
alerts, st2 = _drive(st2, True, "Available Tomorrow")
assert alerts == [] and len(_web_before) == 1 and len(_emails()) == 1; print("OK repeat available: still 1+1 (no resend; restart-safe via same persisted logic)")
alerts, st2 = _drive(st2, False, "Unavailable for Pickup")
assert len(alerts) == 1 and len(_web_before) == 2 and len(_emails()) == 2
_em2 = _emails()[1]; print("DRY-RUN email (soldout) Subject:", _em2["Subject"]); print(_em2.get_content())
assert "已无货: Unavailable for Pickup（之前: Available Tomorrow）" in _em2.get_content()
print("OK true->false: exactly 1 more ntfy + 1 email")
_app.smtplib.SMTP = _orig_smtp; _app.send_alert = _orig_send_alert
print("ALL TRANSITION TESTS PASSED")
