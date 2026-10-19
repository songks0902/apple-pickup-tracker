"""Integration test with low threshold. Run: python3 test_auth_guard.py (starts app on 18099, no Apple dependency assert)."""
import os, subprocess, sys, time, urllib.request, urllib.error
PORT="18099"; BASE=f"http://127.0.0.1:{PORT}"
env=dict(os.environ, AUTH_USER="testuser", AUTH_PASSWORD="test-password-123", PORT=PORT, POLL_SECONDS="120",
         AUTH_FAIL_LIMIT="3", AUTH_FAIL_WINDOW="600", AUTH_BLOCK_SECONDS="900", CACHE_DIR="/tmp/authtest-data", WEBHOOK_URL="")
proc=subprocess.Popen([sys.executable, "app.py"], cwd=str(__import__("pathlib").Path(__file__).parent), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
def req(path="/", user=None, pw=None, ip=None):
    r=urllib.request.Request(BASE+path); 
    if user is not None:
        import base64; r.add_header("Authorization", "Basic "+base64.b64encode(f"{user}:{pw}".encode()).decode())
    if ip: r.add_header("X-Forwarded-For", ip)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp: return resp.status, resp.headers.get("Retry-After")
    except urllib.error.HTTPError as e: return e.code, e.headers.get("Retry-After")
try:
    for _ in range(30):
        try:
            if req("/healthz")[0]==200: break
        except Exception: time.sleep(0.5)
    A="203.0.113.10"; B="203.0.113.11"
    seq=[req(user="testuser", pw="wrong", ip=A)[0] for _ in range(3)]; print("IP-A wrong x3:", seq); assert seq==[401,401,401]
    code,ra=req(user="testuser", pw="wrong", ip=A); print("IP-A 4th wrong:", code, "Retry-After", ra); assert code==429 and int(ra)>0
    code,_=req(user="testuser", pw="test-password-123", ip=A); print("IP-A correct while blocked:", code); assert code==429
    code,_=req("/healthz", ip=A); print("IP-A healthz while blocked:", code); assert code==200
    # IP-B: 2 fails, success clears, then 2 fails are still 401 (not blocked)
    seq=[req(user="testuser", pw="wrong", ip=B)[0] for _ in range(2)]; assert seq==[401,401]
    code,_=req(user="testuser", pw="test-password-123", ip=B); print("IP-B correct after 2 fails:", code); assert code==200
    seq=[req(user="testuser", pw="wrong", ip=B)[0] for _ in range(2)]; print("IP-B wrong x2 after clear:", seq); assert seq==[401,401]
    print("ALL AUTH GUARD TESTS PASSED")
finally:
    proc.terminate()
    try: print(proc.communicate(timeout=8)[0][-800:])
    except Exception: proc.kill()
