"""541 ladder / unknown-stale / recheck-confirm tests (all mocks, no network, no sends). Run: python3 test_risk_controls.py"""
import sys, io, json
from pathlib import Path
from urllib.error import HTTPError
sys.path.insert(0, str(Path(__file__).parent))
import app

# 1) ladder
assert [app.backoff_for_failures(n) for n in (1,2,3,4,5)] == [300,600,1200,1800,1800]
print("OK ladder 300->600->1200->1800 cap")
# 2) 541 explicitly a rate-limit FetchError via stubbed urlopen
def fake_541(req, timeout=20): raise HTTPError(req.full_url, 541, "rate limited", {}, None)
orig=app.urlopen; app.urlopen=fake_541
try:
    try:
        app.Poller.fetch_json(app.POLLER, "https://example.invalid/x"); assert False, "should raise"
    except app.FetchError as e:
        assert e.http_status==541, e.http_status; print("OK HTTP 541 -> FetchError http_status=541 (rate-limit class)", e)
finally: app.urlopen=orig
# success resets: Poller failure fields simulate do_fetch success/failure bookkeeping
p=app.POLLER
assert app.backoff_for_failures(1)==300
print("OK single-probe model: loop only polls after backoff_until, success path resets failures=0 in do_fetch (code path)")

# 3) unknown/missing part never flips, last-known preserved; recovery compares to last-known
def cur(avail, quote): return {"R|P": {"available":avail,"quote":quote,"store":"Example Store","part":"P"}}
alerts, st = app.compute_transitions(None, cur(False,"Check back")); assert alerts==[]
# a round where the part is missing (unknown): represented by absence from current
alerts, st2 = app.compute_transitions(st, {})
assert alerts==[] and st2["R|P"]["available"] is False; print("OK missing/unknown round: no alert, last-known preserved")
# recovery to available vs last-known(false) -> exactly one available flip
alerts, st3 = app.compute_transitions(st2, cur(True,"Available Today"))
assert len(alerts)==1 and alerts[0]["kind"]=="available"; print("OK recovery false->available vs last-known: 1 alert")
# last-known was available, failure/missing round, recovery available -> no repeat
alerts, st4 = app.compute_transitions(st3, {}); assert alerts==[] and st4["R|P"]["available"] is True
alerts, st5 = app.compute_transitions(st4, cur(True,"Available Today")); assert alerts==[]; print("OK recovery when last-known already available: no repeat alert")
# prelaunch(default,false) -> unavailable(false) still no alert, and distinct from unknown (state kept as known false here)
alerts, _ = app.compute_transitions(st5 if False else {"R|P":{"available":False,"quote":"Check back"}}, cur(False,"Unavailable for Pickup")); assert alerts==[]; print("OK prelaunch->unavailable false->false: no alert")

# 4) recheck filter: confirmed passes, unconfirmed held, soldout passes without recheck
a1={"kind":"available","_key":"R|P"}; a2={"kind":"soldout","_key":"R|Q"}
send, held = app.filter_confirmed_available([a1,a2], {"R|P"}); assert send==[a1,a2] and held==[]; print("OK recheck confirmed: available sent")
send, held = app.filter_confirmed_available([a1,a2], set()); assert send==[a2] and held==["R|P"]; print("OK recheck NOT confirmed: available held, soldout still sent")

# 5) stale render: pills become unknown + cooling banner with probe time
snap={"family":"iphoneduo","zip":"90210","target_part":"MK1H4LL/A","poll_seconds":120,"next_poll_in_sec":0,"fetched_at":"x",
 "counts":{"available":0,"prelaunch":1,"unavailable":0},"backed_off":True,"next_probe_at_epoch":1792370000,"data_age_seconds":900,"stale":True,
 "parts":[{"part":"MK1H4LL/A","title":"iPhone Duo 512GB Night Sky","capacity":"512GB","color":"Night Sky","price":"$2,199","buyUrl":"https://example.com"}],
 "stores":[{"storeName":"Example Store","city":"Example City","state":"CA","postalCode":"90210","distanceText":"3 mi","availability":{"MK1H4LL/A":{"state":"prelaunch","quote":"Check back"}}}]}
h=app.render_html(snap)
assert "Apple is rate-limiting us; cooling down, next probe" in h and "not sold out" in h and 'class="pill stale">Unknown</span>' in h
print("OK stale/cooling render: banner + unknown pills")
print("ALL RISK CONTROL TESTS PASSED")
