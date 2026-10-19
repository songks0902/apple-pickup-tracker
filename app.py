#!/usr/bin/env python3
"""Apple pickup availability tracker — stdlib only. Browser never hits Apple directly."""
import base64, hashlib, hmac, html, ipaddress, json, os, random, re, smtplib, sys, threading, time
from email.message import EmailMessage
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.environ.get("TZ_NAME", "UTC"))
except Exception:
    TZ = timezone.utc

BASE = Path(__file__).resolve().parent
CACHE_DIR = Path(os.environ.get("CACHE_DIR", BASE / "data")); CACHE_DIR.mkdir(parents=True, exist_ok=True)
PORT = int(os.environ.get("PORT", "8080"))
POLL_SECONDS = max(120, int(os.environ.get("POLL_SECONDS", "120")))  # hard clamp >=120
AUTH_USER = os.environ.get("AUTH_USER", "").strip()
AUTH_PASS = os.environ.get("AUTH_PASSWORD", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip()
ALERT_EMAIL = os.environ.get("ALERT_EMAIL", "").strip()
SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587") or 587)
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "").strip()
SMTP_TLS = os.environ.get("SMTP_TLS", "starttls").strip().lower()
AUTH_FAIL_LIMIT = int(os.environ.get("AUTH_FAIL_LIMIT", "10") or 10)
AUTH_FAIL_WINDOW = int(os.environ.get("AUTH_FAIL_WINDOW", "600") or 600)
AUTH_BLOCK_SECONDS = int(os.environ.get("AUTH_BLOCK_SECONDS", "900") or 900)
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
RATE_LIMIT_STATUSES = {403, 429, 541}  # 541 (Apple rate-limit) explicitly treated like 403/429
STALE_AFTER_SECONDS = int(os.environ.get("STALE_SECONDS", "300") or 300)  # ~2 poll cycles at 120s

class FetchError(Exception):
    def __init__(self, message, http_status=None):
        super().__init__(message); self.http_status = http_status

def backoff_for_failures(n):
    """Ladder: 1st failure 300s -> 600 -> 1200 -> capped 1800s."""
    return min(1800, 300 * (2 ** max(0, n - 1)))

def filter_confirmed_available(alerts, recheck_available_keys):
    """Available alerts need instant-recheck confirmation; soldout alerts pass through.
    Returns (to_send, unconfirmed_keys) — unconfirmed keep their old persisted state."""
    to_send, unconfirmed = [], []
    for a in alerts:
        key = a.get("_key", "")
        if a.get("kind") == "available" and key not in recheck_available_keys:
            unconfirmed.append(key)
        else:
            to_send.append(a)
    return to_send, unconfirmed

def env_family_zip():
    return (os.environ.get("TRACK_FAMILY") or os.environ.get("FAMILY") or "iphoneduo").strip(), \
           (os.environ.get("TRACK_ZIP") or os.environ.get("ZIP_CODE") or os.environ.get("ZIP") or "90210").strip()
DEF_FAMILY, DEF_ZIP = env_family_zip()
TARGET_PART = (os.environ.get("TARGET_PART") or "MK1H4LL/A").strip()
PARTS_ENV = [p.strip() for p in os.environ.get("PARTS","").split(",") if p.strip()]

def classify_state(pickup_display, quote_text=""):
    d=(pickup_display or "").strip().lower(); q=(quote_text or "").strip().lower()
    if d=="available" or "available today" in q or "available tomorrow" in q: return "available"
    if d=="default" or "check back" in q: return "prelaunch"
    if d=="unavailable" or "unavailable for pickup" in q: return "unavailable"
    return "unknown"
STATE_META={
 "available":("Available","#0a7a2e","#e6f7ea"),
 "prelaunch":("Pre-launch","#9a6200","#fff3d6"),
 "unavailable":("Unavailable","#8a1f1f","#fdeaea"),
 "unknown":("Unknown","#555","#f0f0f0"),
}

def parse_meta(raw, family):
    try:
        meta=raw["body"]["productLocatorOverlayData"]["productLocatorMeta"]
    except Exception:
        return []
    # colour display names
    colors={}
    try:
        for c in meta.get("dimensionsVariations",{}).get("dimensionColor",[]):
            v=re.sub(r"<[^>]+>","",c.get("value","")).strip()
            if v: colors[c.get("key","")]=v
    except Exception: pass
    seen={}
    for p in meta.get("products",[]):
        pn=p.get("partNumber"); 
        if not pn or pn in seen: continue
        title=re.sub(r"\s+(AT&T|T-Mobile|Verizon)$","",p.get("productTitle","")).strip()
        cap=(p.get("dimensionCapacity") or "").upper()
        color=colors.get(p.get("dimensionColor",""), p.get("dimensionColor",""))
        price=None
        m=re.match(r"(\d+)_(\d+)_", p.get("price","") or "")
        if m: price=f"${int(m.group(1)):,}"
        seen[pn]={"part":pn,"title":title or pn,"capacity":cap,"color":color,"price":price,"buyUrl":p.get("productLink","")}
    # sort by capacity order then color
    order={"256GB":0,"512GB":1,"1TB":2,"2TB":3}
    return sorted(seen.values(), key=lambda x:(order.get(x["capacity"],9), x["color"], x["part"]))

def normalize_pickup(raw, parts_meta):
    stores=[]
    pmap={p["part"]:p for p in parts_meta}
    for s in raw.get("body",{}).get("stores",[]) or []:
        addr=s.get("address") or {}
        availability={}
        for pn, v in (s.get("partsAvailability") or {}).items():
            q=v.get("pickupSearchQuote","") or ""
            availability[pn]={"state":classify_state(v.get("pickupDisplay"),q),"pickupDisplay":v.get("pickupDisplay"),"quote":q,
                              "title":((v.get("messageTypes") or {}).get("regular") or {}).get("storePickupProductTitle","")}
        # ensure all meta parts present
        for pn in pmap:
            availability.setdefault(pn, {"state":"unknown","pickupDisplay":None,"quote":"—","title":pmap[pn]["title"]})
        stores.append({
            "storeName":s.get("storeName",""),"storeNumber":s.get("storeNumber",""),"city":s.get("city",""),"state":s.get("state",""),
            "postalCode":addr.get("postalCode",""),"distance":s.get("storedistance") or s.get("storeDistanceWithUnit",""),
            "distanceText":s.get("storeDistanceWithUnit",""),"phone":s.get("phoneNumber",""),
            "address":" ".join(x for x in [addr.get("address",""),addr.get("address2",""),addr.get("address3","")] if x).strip(),
            "reservationUrl":s.get("reservationUrl") or s.get("makeReservationUrl") or "",
            "directionsUrl":s.get("directionsUrl") or "",
            "hours":"; ".join(f"{h.get('storeDays','')} {h.get('storeTimings','')}".strip() for h in ((s.get("storeHours") or {}).get("hours") or [])),
            "availability":availability})
    return stores

PUBLIC_URL = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")

def build_alert_text(msg):
    """(title, body) shared by ntfy and email — minimal format: spec / store name / quote / 2 links."""
    product_title = msg.get("product_title") or msg.get("product") or ""
    spec_line = product_title
    if msg.get("part"): spec_line += f" ({msg['part']})"
    if msg.get("price"): spec_line += f" {msg['price']}"
    short = product_title.replace("iPhone ", "").strip() or msg.get("part","")
    store = msg.get("store","")
    kind = msg.get("kind", "available")
    is_test = bool(msg.get("test")) or "TEST" in str(msg.get("product","")) or "TEST" in store
    # Push body is intentionally minimal (store name only, no address/phone/hours/city/distance):
    if kind == "soldout":
        title = (f"TEST ignore — ❌ {short} sold out — {store}" if is_test else f"❌ {short} sold out / out of stock — {store}")
        soldout_spec = product_title + (f" ({msg['part']})" if msg.get("part") else "")
        lines = [soldout_spec, f"Store: {store}"]
        if msg.get("quote"):
            lines.append(f"Sold out: {msg['quote']}" + (f" (previously: {msg['prev_quote']})" if msg.get("prev_quote") else ""))
    else:
        title = (f"TEST ignore — 🍎 {short} in stock — {store}" if is_test else f"🍎 {short} in stock — {store}")
        lines = [spec_line, f"Store: {store}"]
        if msg.get("quote"): lines.append(f"Pickup: {msg['quote']}")
    if msg.get("buy_url"): lines.append(f"Buy this configuration: {msg['buy_url']}")
    if PUBLIC_URL: lines.append(f"Dashboard: {PUBLIC_URL}/")
    body = "\n".join(x for x in lines if x and not x.endswith(": "))
    if msg.get("title_override"): title = msg["title_override"]
    return title, body

def send_alert(msg):
    """ntfy/webhook only. Email is separate (send_email_alert); failures here never affect email and vice versa."""
    if not WEBHOOK_URL: return False
    title, body = build_alert_text(msg)
    try:
        if "ntfy." in WEBHOOK_URL or "ntfy.sh" in WEBHOOK_URL:
            u = urlparse(WEBHOOK_URL); topic = u.path.strip("/")
            payload = {"topic": topic, "title": title, "message": body, "priority": 4, "tags": ["apple", "shopping_bags"]}
            if msg.get("buy_url"): payload["click"] = msg["buy_url"]
            endpoint = f"{u.scheme}://{u.netloc}/"
            req = Request(endpoint, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST",
                          headers={"Content-Type": "application/json; charset=utf-8"})
        else:
            req = Request(WEBHOOK_URL, data=json.dumps(msg, ensure_ascii=False).encode(), headers={"Content-Type": "application/json", "User-Agent": UA}, method="POST")
        urlopen(req, timeout=10).read()
        print(f"[webhook] alert sent to {WEBHOOK_URL.split('/')[2] if '://' in WEBHOOK_URL else 'webhook'} title={title}", flush=True)
        return True
    except Exception as e:
        print(f"[webhook] failed {e}", flush=True)
        return False

def send_email_alert(msg):
    """SMTP email copy of the same alert. Unconfigured -> graceful skip log, never raises."""
    try:
        if not ALERT_EMAIL or not SMTP_HOST or not SMTP_USER or not SMTP_PASSWORD:
            print("[email] skipped: SMTP not configured", flush=True); return False
        title, body = build_alert_text(msg)
        em = EmailMessage(); em["Subject"] = title; em["From"] = SMTP_FROM or SMTP_USER; em["To"] = ALERT_EMAIL
        em.set_content(body)
        if SMTP_TLS == "ssl":
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20) as s:
                auth_code, _ = s.login(SMTP_USER, SMTP_PASSWORD); refused = s.send_message(em)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
                if SMTP_TLS == "starttls": s.starttls()
                auth_code, _ = s.login(SMTP_USER, SMTP_PASSWORD); refused = s.send_message(em)
        print(f"[email] sent to {ALERT_EMAIL} auth_code={auth_code} refused={refused} title={title}", flush=True); return True
    except Exception as e:
        print(f"[email] failed: {type(e).__name__}: {e}", flush=True); return False

def notify(msg):
    """Fan-out: ntfy and email are independent; either failing never blocks the other."""
    try: send_alert(msg)
    except Exception as e: print(f"[webhook] unexpected failure: {e}", flush=True)
    try: send_email_alert(msg)
    except Exception as e: print(f"[email] unexpected failure: {e}", flush=True)

def compute_transitions(prev_states, current):
    """Binary flip logic per (store,part). is_available from payload['available'] (pickupDisplay=='available').
    prev_states=None means no baseline yet -> build baseline, alert nothing.
    Returns (alerts, new_states). alerts are payload copies with kind/prev_quote set."""
    # Start from last-known states: keys missing this round (unknown/failed/part absent)
    # keep their previous state untouched and never participate in flips.
    new_states = {k: (dict(v) if isinstance(v, dict) else {"available": bool(v), "quote": ""}) for k, v in (prev_states or {}).items()}
    for k, v in current.items():
        new_states[k] = {"available": bool(v.get("available")), "quote": v.get("quote", "")}
    if prev_states is None:
        return [], new_states
    alerts = []
    for k, v in current.items():
        if k not in prev_states:
            continue  # newly-seen store/part: baseline it, do not alert
        old = bool(prev_states[k].get("available") if isinstance(prev_states[k], dict) else prev_states[k])
        new = bool(v.get("available"))
        if old == new:
            continue  # includes prelaunch->unavailable (false->false) and repeated available
        a = dict(v); a["_key"] = k; a["kind"] = "available" if new else "soldout"
        a["prev_quote"] = (prev_states[k].get("quote", "") if isinstance(prev_states[k], dict) else "")
        alerts.append(a)
    return alerts, new_states

class Poller:
    def __init__(self):
        self.lock=threading.Lock(); self.fetch_lock=threading.Lock()
        self.family=DEF_FAMILY; self.zip=DEF_ZIP
        self.parts_meta=[]; self.status=None; self.last_attempt=0; self.last_success=0
        self.next_poll=time.time(); self.failures=0; self.backoff_until=0; self.error=None; self.fetching=False; self.last_http_status=None
        self.prev_states=None; self.meta_fetched_at=0
        self._load_disk(); self._load_alert_state()
    def alert_state_file(self): return CACHE_DIR / "alert_state.json"
    def _load_alert_state(self):
        try:
            f=self.alert_state_file()
            if f.exists():
                data=json.loads(f.read_text()); self.prev_states=data.get("states") or {}
                print(f"[alert] loaded persisted state: {len(self.prev_states)} (store,part) entries from {f}", flush=True)
            else:
                self.prev_states=None; print("[alert] no persisted state yet; first fetch will build baseline without alerting", flush=True)
        except Exception as e:
            self.prev_states=None; print(f"[alert] load state failed ({e}); will rebuild baseline", flush=True)
    def _save_alert_state(self):
        try:
            f=self.alert_state_file(); tmp=f.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version":1,"updated_at":datetime.now(TZ).isoformat(),"states":self.prev_states}, ensure_ascii=False))
            tmp.replace(f)
        except Exception as e: print(f"[alert] save state failed: {e}", flush=True)
    def cache_file(self): return CACHE_DIR / f"cache_{re.sub(r'[^A-Za-z0-9_-]','_',self.family)}_{self.zip}.json"
    def meta_file(self): return CACHE_DIR / f"meta_{re.sub(r'[^A-Za-z0-9_-]','_',self.family)}.json"
    def _load_disk(self):
        try:
            f=self.cache_file()
            if f.exists():
                self.status=json.loads(f.read_text()); self.last_success=self.status.get("fetched_at_epoch",0)
        except Exception as e: print(f"[cache] load failed: {e}", flush=True)
    def set_config(self, family=None, zip_code=None):
        changed=False
        with self.lock:
            if family and family!=self.family and re.fullmatch(r"[A-Za-z0-9_-]{1,40}",family): self.family=family; self.parts_meta=[]; self.meta_fetched_at=0; changed=True
            if zip_code and zip_code!=self.zip and re.fullmatch(r"\d{5}",zip_code): self.zip=zip_code; changed=True
            if changed: self.status=None; self._load_disk()
        if changed: self.request_poll(reason="config-change")
        return changed
    def fetch_json(self, url):
        req=Request(url, headers={"User-Agent":UA,"Accept":"application/json,text/plain,*/*","Accept-Language":"en-US,en;q=0.9"})
        try:
            with urlopen(req, timeout=20) as r:
                if r.status!=200: raise FetchError(f"HTTP {r.status}", http_status=r.status)
                return json.loads(r.read().decode("utf-8"))
        except HTTPError as e:
            kind = "rate-limit" if e.code in RATE_LIMIT_STATUSES else "http"
            raise FetchError(f"HTTP {e.code} ({kind})", http_status=e.code) from e
        except FetchError: raise
        except Exception as e:
            raise FetchError(f"{type(e).__name__}: {e}", http_status=None) from e
    def ensure_meta(self):
        if self.parts_meta and time.time()-self.meta_fetched_at<86400: return
        mf=self.meta_file()
        if mf.exists() and time.time()-mf.stat().st_mtime<86400:
            try: self.parts_meta=parse_meta(json.loads(mf.read_text()),self.family); 
            except Exception: self.parts_meta=[]
            if self.parts_meta: self.meta_fetched_at=mf.stat().st_mtime; return
        raw=self.fetch_json(f"https://www.apple.com/shop/product-locator-meta?family={quote(self.family)}")
        try: mf.write_text(json.dumps(raw))
        except Exception: pass
        self.parts_meta=parse_meta(raw,self.family); self.meta_fetched_at=time.time()
    def do_fetch(self):
        if not self.fetch_lock.acquire(blocking=False): return
        self.fetching=True; self.last_attempt=time.time()
        try:
            self.ensure_meta()
            if PARTS_ENV: parts=PARTS_ENV
            elif self.parts_meta: parts=[p["part"] for p in self.parts_meta]
            else: parts=[TARGET_PART]
            if not self.parts_meta:
                self.parts_meta=[{"part":p,"title":p,"capacity":"","color":"","price":None} for p in parts]
            qs="&".join(f"parts.{i}={quote(p,safe='')}" for i,p in enumerate(parts))
            pickup_url=f"https://www.apple.com/shop/retail/pickup-message?{qs}&location={quote(self.zip)}"
            raw=self.fetch_json(pickup_url); self.last_http_status=200
            stores=normalize_pickup(raw,self.parts_meta)
            if not stores and raw.get("body",{}).get("stores") is not None and len(raw["body"]["stores"])==0:
                pass
            now=time.time()
            status={"family":self.family,"zip":self.zip,"target_part":TARGET_PART,"parts":self.parts_meta,"stores":stores,
                    "fetched_at_epoch":now,"fetched_at":datetime.fromtimestamp(now,TZ).isoformat(),"error":None,"stale":False}
            # binary flip transitions, persisted to data/alert_state.json (restart-safe)
            current={}
            for s in stores:
                for pn,a in s["availability"].items():
                    if a.get("state")=="unknown" or a.get("pickupDisplay") is None: continue  # unknown never enters flips/state
                    k=f"{s['storeNumber'] or s['storeName']}|{pn}"
                    pm=(self.parts_meta and next((x for x in self.parts_meta if x["part"]==pn), {})) or {}
                    current[k]={"available": a.get("pickupDisplay")=="available",
                             "product":self.family,"product_title":pm.get("title") or a.get("title",""),"part":pn,"price":pm.get("price"),"buy_url":pm.get("buyUrl",""),
                             "store":s["storeName"],"city":s["city"],"store_state":s.get("state",""),"store_zip":s.get("postalCode",""),
                             "distance":s.get("distanceText") or s.get("distance",""),"address":s.get("address",""),"phone":s.get("phone",""),
                             "hours":s.get("hours",""),"reservationUrl":s.get("reservationUrl",""),
                             "quote":a["quote"],"zip":self.zip,"timestamp":status["fetched_at"],"tracker_url":(PUBLIC_URL+"/" if PUBLIC_URL else "")}
            alerts, new_states = compute_transitions(self.prev_states, current)
            if self.prev_states is None:
                print(f"[alert] baseline built: {len(new_states)} (store,part) entries, no alerts on first run", flush=True)
            # Instant recheck (one merged request) before any available alert; soldout not delayed.
            if any(m.get("kind")=="available" for m in alerts):
                raw2=self.fetch_json(pickup_url)  # a 541 here raises -> backoff, nothing sent, states uncommitted
                stores2=normalize_pickup(raw2,self.parts_meta)
                confirmed={f"{s['storeNumber'] or s['storeName']}|{pn}" for s in stores2 for pn,a in s["availability"].items() if a.get("pickupDisplay")=="available"}
                alerts, unconfirmed = filter_confirmed_available(alerts, confirmed)
                for k in unconfirmed:
                    print(f"[alert] recheck did NOT confirm {k}; holding alert/state until next poll", flush=True)
                    if self.prev_states and k in self.prev_states: new_states[k]=dict(self.prev_states[k])
            for msg in alerts:
                label = "AVAILABLE" if msg["kind"]=="available" else "SOLDOUT"
                print(f"\n🚨🚨🚨 ALERT {label}: {json.dumps(msg,ensure_ascii=False)} 🚨🚨🚨\n", flush=True)
                notify(msg)
            self.prev_states=new_states; self._save_alert_state()
            with self.lock:
                self.status=status; self.last_success=now; self.failures=0; self.backoff_until=0; self.error=None
                try: self.cache_file().write_text(json.dumps(status))
                except Exception: pass
            print(f"[poll] ok family={self.family} zip={self.zip} stores={len(stores)} parts={len(parts)} at {status['fetched_at']}", flush=True)
        except Exception as e:
            with self.lock:
                if getattr(e, "http_status", None) is not None: self.last_http_status=e.http_status
                self.failures+=1; backoff=backoff_for_failures(self.failures); self.backoff_until=time.time()+backoff; self.error=f"{type(e).__name__}: {e}"
                print(f"[poll] FAILED failures={self.failures} http_status={self.last_http_status} backoff={backoff}s err={self.error}", flush=True)
        finally:
            self.fetching=False; self.fetch_lock.release()
    def request_poll(self, reason=""):
        now=time.time()
        if self.fetching: return False
        if now < self.backoff_until: return False
        # config-change (ZIP/family switch) may trigger ONE immediate fetch; routine polls respect 120s floor
        if reason!="config-change" and self.status and now-self.last_attempt < 120: return False
        threading.Thread(target=self.do_fetch, daemon=True).start(); return True
    def loop(self):
        # initial disk meta attempt (offline-safe)
        while True:
            self.request_poll(reason="loop")
            # compute sleep: poll interval + jitter, or backoff remainder
            now=time.time()
            if now < self.backoff_until: delay=max(5, self.backoff_until-now)
            else: delay=POLL_SECONDS + random.uniform(0,30)  # jitter only adds: actual >= POLL_SECONDS (>=120s floor)
            self.next_poll=now+delay
            time.sleep(max(5,delay))
    def snapshot(self):
        with self.lock:
            st=json.loads(json.dumps(self.status)) if self.status else {"family":self.family,"zip":self.zip,"target_part":TARGET_PART,"parts":self.parts_meta,"stores":[],"fetched_at":None,"fetched_at_epoch":0}
            now=time.time()
            fetched_epoch=st.get("fetched_at_epoch") or (self.last_success or None)
            age=int(now-fetched_epoch) if fetched_epoch else None
            probe_at=self.backoff_until if now<self.backoff_until else self.next_poll
            st.update({"poll_seconds":POLL_SECONDS,"next_poll_in_sec":max(0,int(self.next_poll-now)),"backed_off":now<self.backoff_until,
                       "backoff_until": datetime.fromtimestamp(self.backoff_until,TZ).isoformat() if self.backoff_until>now else None,
                       "backoff_until_epoch": self.backoff_until if self.backoff_until else None,
                       "next_probe_at": datetime.fromtimestamp(probe_at,TZ).isoformat(),
                       "next_probe_at_epoch": probe_at,
                       "last_http_status": self.last_http_status,
                       "consecutive_failures": self.failures,
                       "last_success_at": datetime.fromtimestamp(self.last_success,TZ).isoformat() if self.last_success else None,
                       "data_age_seconds": age,
                       "stale_after_seconds": STALE_AFTER_SECONDS,
                       "error":self.error,"failures":self.failures,"fetching":self.fetching,
                       "stale": bool(now<self.backoff_until or self.error or (age is not None and age>STALE_AFTER_SECONDS) or age is None)})
            # counts
            counts={"available":0,"prelaunch":0,"unavailable":0,"unknown":0}
            for s in st.get("stores",[]):
                for a in s.get("availability",{}).values(): counts[a["state"]]=counts.get(a["state"],0)+1
            st["counts"]=counts; return st

POLLER=Poller()

def client_ip(handler):
    """Source IP. app is only reachable via Caddy on the docker network (8080 not published),
    so X-Forwarded-For is trusted only when the direct peer is private/loopback (the proxy);
    a hypothetical direct public peer cannot spoof it."""
    remote = handler.client_address[0] if handler.client_address else ""
    try: is_priv = ipaddress.ip_address(remote).is_private or ipaddress.ip_address(remote).is_loopback
    except Exception: is_priv = False
    if is_priv:
        xff = handler.headers.get("X-Forwarded-For", "")
        if xff: return xff.split(",")[0].strip() or remote
    return remote

class AuthGuard:
    """In-memory per-IP failed-login blocker (resets on restart, by design). Thread-safe."""
    def __init__(self): self.lock = threading.Lock(); self.fails = {}; self.blocked_until = {}
    def retry_after(self, ip):
        with self.lock: return max(0, int(self.blocked_until.get(ip, 0) - time.time()))
    def record_failure(self, ip):
        now = time.time()
        with self.lock:
            lst = [x for x in self.fails.get(ip, []) if now - x < AUTH_FAIL_WINDOW]; lst.append(now); self.fails[ip] = lst
            if len(lst) >= AUTH_FAIL_LIMIT and self.blocked_until.get(ip, 0) <= now:
                self.blocked_until[ip] = now + AUTH_BLOCK_SECONDS
                print(f"[auth] BLOCKED ip={ip} failures={len(lst)} block_seconds={AUTH_BLOCK_SECONDS}", flush=True)
            return len(lst)
    def clear(self, ip):
        with self.lock: self.fails.pop(ip, None)
AUTH_GUARD = AuthGuard()

def check_auth(handler):
    if not AUTH_USER or not AUTH_PASS: return True
    h=handler.headers.get("Authorization","")
    if not h.startswith("Basic "): return False
    try: u,p=base64.b64decode(h[6:]).decode().split(":",1)
    except Exception: return False
    return hmac.compare_digest(u,AUTH_USER) and hmac.compare_digest(p,AUTH_PASS)

def render_html(st):
    parts=st.get("parts",[]); target=st.get("target_part",TARGET_PART)
    target_meta=next((x for x in parts if x["part"]==target), {})
    target_label=target_meta.get("title","iPhone Duo 512GB Night Sky")
    stores=st.get("stores",[]); counts=st.get("counts",{})
    avail_stores=sum(1 for s in stores if any(a.get("state")=="available" for a in s.get("availability",{}).values()))
    target_avail=sum(1 for s in stores if s.get("availability",{}).get(target,{}).get("state")=="available")
    if avail_stores:
        banner_cls="ok"; banner=f"✅ {len(stores)} stores · {avail_stores} in stock" + (f" · target available at {target_avail}" if target_avail else " · target not yet available")
    elif counts.get("prelaunch"):
        banner_cls="amber"; banner=f"🟠 {len(stores)} stores · 0 in stock · all pre-launch (Check back for availability on 10.24)"
    elif stores: banner_cls="grey"; banner=f"{len(stores)} stores · 0 in stock"
    else: banner_cls="grey"; banner="No data yet — waiting for first fetch"
    _age=st.get("data_age_seconds"); _age_txt=(f"{_age//60} min" if isinstance(_age,int) and _age>=60 else (f"{_age} sec" if _age is not None else "unknown"))
    _probe=""
    if st.get("next_probe_at_epoch"):
        try: _probe=datetime.fromtimestamp(st["next_probe_at_epoch"],TZ).strftime("%H:%M")
        except Exception: _probe=""
    if st.get("backed_off"):
        banner_cls="warn"; banner=f"⚠️ Apple is rate-limiting us; cooling down, next probe {_probe} · data last updated {_age_txt} ago (showing last known status below, not sold out)"
    elif st.get("stale") and stores:
        banner_cls="warn"; banner=f"⚠️ Data is stale/unknown — no successful refresh for {_age_txt} (showing last known status below, not sold out)"
    # Mobile short banner: same meaning, shorter (no truncation/ellipsis anywhere)
    import re as _re
    _m=_re.search(r"on (\d+\.\d+)", banner)
    if avail_stores: banner_short=f"✅ {len(stores)} stores·{avail_stores} in stock" + (f"·target {target_avail}" if target_avail else "·target none")
    elif counts.get("prelaunch"): banner_short=f"🟠 {len(stores)} stores·0 in stock·pre-launch" + (f"·on sale {_m.group(1)}" if _m else "")
    elif stores: banner_short=f"{len(stores)} stores·0 in stock"
    else: banner_short="No data yet·waiting"
    if banner_cls=="warn": banner_short=banner
    short_color={"Star White":"Star White","Night Sky":"Night Sky"}
    def col_label(p):
        cap=p.get("capacity","").replace("GB","").replace("TB","T")
        return f"{cap} {short_color.get(p.get('color',''), p.get('color',''))}"
    pill_short={"available":"Available","prelaunch":"Pre-launch","unavailable":"Unavailable","unknown":"?"}
    cols="".join(f'<th class="{"target" if p["part"]==target else ""}" data-target="{1 if p["part"]==target else 0}" title="{html.escape(p["part"])}">{html.escape(col_label(p))}{"<br><span class=tag>target</span>" if p["part"]==target else ""}<br><small>{html.escape(p["part"][:5])}</small></th>' for p in parts)
    rows=""
    for s in stores:
        has_av=any(a.get("state")=="available" for a in s.get("availability",{}).values())
        cells=""
        for p in parts:
            a=s.get("availability",{}).get(p["part"],{"state":"unknown","quote":"—"}); state=a.get("state","unknown")
            label,fg,bg=STATE_META.get(state,STATE_META["unknown"]); is_t=p["part"]==target
            pill=('<span class="pill stale">Unknown</span>' if st.get("stale") else f'<span class="pill" style="color:{fg};background:{bg}">{pill_short.get(state,"?")}</span>')
            if not st.get("stale") and state=="available" and p.get("buyUrl"): pill=f'<a class="buycell" href="{html.escape(p["buyUrl"], quote=True)}" target="_blank" rel="noopener" title="Buy this configuration">{pill}</a>'
            cells+=f'<td class="{"target" if is_t else ""}" data-target="{1 if is_t else 0}" title="{html.escape(a.get("quote") or "")}">{pill}</td>'
        rows+=(f'<tr data-available="{1 if has_av else 0}"><th class="storecell" title="{html.escape(s["storeName"])}">'
               f'<b>{html.escape(s["storeName"])}</b><br><small>{html.escape(s["city"])} · {html.escape(str(s.get("distanceText") or s.get("distance","")))}</small></th>{cells}</tr>')
    if not stores: rows='<tr><td colspan="99">No data yet — waiting for first fetch.</td></tr>'
    target_buy=f'<a class="buybtn" href="{html.escape(target_meta.get("buyUrl",""), quote=True)}" target="_blank" rel="noopener">Buy target configuration ({html.escape(target_label)}) ↗</a>' if target_meta.get("buyUrl") else ""
    fetched=st.get("fetched_at") or "—"
    counts_str=f"In stock {counts.get('available',0)} · Pre-launch {counts.get('prelaunch',0)} · Unavailable {counts.get('unavailable',0)}"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"><meta name="robots" content="noindex,nofollow"><title>iPhone Duo Pickup Tracker</title>
<style>
:root{{--bg:#f5f5f7;--card:#fff;--text:#1d1d1f;--sub:#6e6e73;--line:#d2d2d7;--target:#fff3cf;--head:rgba(255,255,255,.85)}}
@media (prefers-color-scheme: dark){{:root{{--bg:#000;--card:#1c1c1e;--text:#f5f5f7;--sub:#aeaeb2;--line:#38383a;--target:#4a3a10;--head:rgba(28,28,30,.85)}}}}
*{{box-sizing:border-box}}body{{font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC",sans-serif;margin:0;background:var(--bg);color:var(--text);font-size:14px;line-height:1.35;padding-bottom:env(safe-area-inset-bottom)}}
header.top{{position:sticky;top:0;z-index:20;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);background:var(--head);border-bottom:1px solid var(--line);padding:8px 14px;display:flex;flex-wrap:wrap;gap:4px 14px;align-items:baseline}}
header.top h1{{font-size:17px;margin:0}} .tpill{{border:1px solid var(--line);border-radius:999px;padding:2px 9px;font-size:12px;background:var(--card)}} .tmeta{{color:var(--sub);font-size:12px}}
main{{max-width:1180px;margin:0 auto;padding:10px 12px}} .banner{{padding:8px 12px;border-radius:10px;margin:8px 0;font-weight:600;border:1px solid var(--line);background:var(--card);white-space:nowrap;overflow:hidden}} .banner-short{{display:none}} .banner.ok{{background:#e6f7ea;color:#0a5c24}} .banner.amber{{background:#fff3d6;color:#7a4d00}} .banner.warn{{background:#fdeaea;color:#8a1f1f}} @media (prefers-color-scheme: dark){{.banner.ok{{background:#12351c;color:#7ee2a0}}.banner.amber{{background:#3a2c08;color:#ffcf6e}}.banner.warn{{background:#3d1515;color:#ff9c9c}}}}
.tablewrap{{overflow:auto;border:1px solid var(--line);border-radius:12px;background:var(--card)}} table{{border-collapse:separate;border-spacing:0;width:100%;font-size:13px}} th,td{{border-bottom:1px solid var(--line);border-right:1px solid var(--line);padding:4px 4px;text-align:center;white-space:nowrap}} thead th{{position:sticky;top:0;z-index:5;background:var(--card);font-size:12px;line-height:1.25}} th.storecell{{position:sticky;left:0;z-index:4;background:var(--card);text-align:left;min-width:118px;max-width:170px;white-space:normal}} thead th.storecell{{z-index:6}} th.target,td.target{{background:var(--target)}} .pill{{display:inline-block;min-width:30px;padding:2px 6px;border-radius:999px;font-weight:700;font-size:11px}} .pill.stale{{color:#555;background:repeating-linear-gradient(45deg,#eee,#eee 4px,#ddd 4px,#ddd 8px);border:1px dashed #888;font-size:10px;padding:1px 4px}} @media (prefers-color-scheme: dark){{.pill.stale{{color:#ccc;background:#2c2c2e}}}} .tag{{background:#f0b429;color:#111;border-radius:5px;padding:0 5px;font-size:10px}} small{{color:var(--sub);font-size:10px}} a{{color:#0066cc}} .buycell{{text-decoration:none}} .buybtn{{display:inline-block;padding:8px 12px;border:1px solid var(--line);border-radius:10px;background:var(--card);font-weight:600;margin:6px 0;min-height:40px;line-height:24px}} select,input,button{{font-size:14px;min-height:40px;padding:6px;border-radius:9px;border:1px solid var(--line);background:var(--card);color:var(--text)}} .controls{{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:6px 0}} details{{margin-top:10px}} summary{{cursor:pointer;min-height:40px;line-height:40px;font-weight:600}}
@media (max-width:640px){{main{{padding:6px}} .legend{{display:none}} table{{font-size:11px}} th,td{{padding:2px 1px}} th.storecell{{min-width:68px;max-width:78px;font-size:10px}} th.storecell b, th.storecell small{{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}} .pill{{min-width:21px;padding:1px 3px;font-size:10px}} thead th{{font-size:10px}} thead th small{{display:none}} header.top h1{{font-size:15px}} .controls input[name="family"]{{width:110px}} .banner{{font-size:13.5px;padding:8px 10px;white-space:normal;line-height:1.5}} .banner-full{{display:inline}} .banner-short{{display:none}}}}
</style>
<script>
let nxt={st.get("next_poll_in_sec",0)};setInterval(()=>{{if(nxt>0){{nxt--;var e=document.getElementById('nxt');if(e)e.textContent=nxt;}}}},1000);setInterval(()=>location.reload(),60000);
function applyFilter(v){{document.querySelectorAll('tbody tr').forEach(function(r){{r.style.display=(v==='available'&&r.dataset.available!=='1')?'none':'';}});document.querySelectorAll('[data-target]').forEach(function(c){{c.style.display=(v==='target'&&c.dataset.target!=='1'&&!c.classList.contains('storecell'))?'none':'';}});}}
</script></head>
<body><header class="top"><h1>🍎 iPhone Duo Pickup Tracker</h1><span class="tpill">Target {html.escape(target_label)} · {html.escape(target)} · {html.escape(st.get("zip",""))}</span><span class="tmeta">Updated {html.escape(str(fetched))} (PT) · polling every {st.get("poll_seconds")}s · next in <span id="nxt">{st.get("next_poll_in_sec",0)}</span>s · {counts_str}</span></header>
<main><div class="banner {banner_cls}" title="{html.escape(banner, quote=True)}"><span class="banner-full">{html.escape(banner)}</span><span class="banner-short">{html.escape(banner_short)}</span></div>
<form method="get" class="controls"><label>ZIP <input name="zip" value="{html.escape(st.get("zip",""))}" pattern="[0-9]{{5}}" size="5"></label><label>family <input name="family" value="{html.escape(st.get("family",""))}" size="12"></label><button>Switch</button>
<label>Filter <select id="filterSel" onchange="applyFilter(this.value)"><option value="all">All</option><option value="available">Stores with stock only</option><option value="target">Target column only</option></select></label>{target_buy}</form>
<p class="legend" style="margin:4px 0">Legend: column header = capacity + color; <b>Available</b> = in stock, <b>Pre-launch</b> = not yet on sale (not sold out), <b>Unavailable</b> = sold out; hover or long-press a cell for the full status. An in-stock pill links directly to the buy page for that configuration.</p>
<div class="tablewrap"><table><thead><tr><th class="storecell">Store</th>{cols}</tr></thead><tbody>{rows}</tbody></table></div>
<p><small>This page reloads every 60 seconds, requests only the local service, and does not increase Apple polling; cached data is shown while rate-limited.</small></p></main></body></html>"""

class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): sys.stderr.write("%s - %s\n"%(self.address_string(), fmt%args))
    def send_common(self, code, body, ctype):
        b=body.encode() if isinstance(body,str) else body
        self.send_response(code); self.send_header("Content-Type",ctype); self.send_header("Content-Length",str(len(b)))
        self.send_header("Cache-Control","no-store"); self.send_header("X-Robots-Tag","noindex, nofollow"); self.send_header("X-Content-Type-Options","nosniff"); self.send_header("Referrer-Policy","no-referrer")
        self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        u=urlparse(self.path); q=parse_qs(u.query)
        if u.path=="/healthz": return self.send_common(200, json.dumps({"ok":True}), "application/json")
        ip = client_ip(self)
        ra = AUTH_GUARD.retry_after(ip) if (AUTH_USER and AUTH_PASS) else 0
        if ra > 0:
            self.send_response(429); self.send_header("Retry-After", str(ra)); self.send_header("Cache-Control","no-store"); self.send_header("Content-Type","text/plain; charset=utf-8"); self.end_headers(); self.wfile.write(b"Too many failed login attempts. Try again later."); return
        if not check_auth(self):
            if AUTH_USER and AUTH_PASS:
                n=AUTH_GUARD.record_failure(ip); print(f"[auth] failed login ip={ip} failures_in_window={n}", flush=True)
            self.send_response(401); self.send_header("WWW-Authenticate",'Basic realm="Apple Tracker"'); self.send_header("Cache-Control","no-store"); self.end_headers(); return
        AUTH_GUARD.clear(ip)
        # config via query
        if q.get("zip") or q.get("family"):
            POLLER.set_config(q.get("family",[None])[0], q.get("zip",[None])[0])
        else:
            POLLER.request_poll()
        if u.path=="/api/status": return self.send_common(200, json.dumps(POLLER.snapshot(),ensure_ascii=False), "application/json; charset=utf-8")
        if u.path in ("/","/index.html"): return self.send_common(200, render_html(POLLER.snapshot()), "text/html; charset=utf-8")
        return self.send_common(404, "not found", "text/plain")
if __name__=="__main__":
    if not AUTH_USER or not AUTH_PASS: print("⚠️ WARNING: AUTH_USER/AUTH_PASSWORD not set — local dev only, do NOT expose publicly!", flush=True)
    threading.Thread(target=POLLER.loop, daemon=True).start()
    print(f"Listening on 0.0.0.0:{PORT} family={DEF_FAMILY} zip={DEF_ZIP} poll={POLL_SECONDS}s target={TARGET_PART}", flush=True)
    ThreadingHTTPServer(("0.0.0.0",PORT), H).serve_forever()
