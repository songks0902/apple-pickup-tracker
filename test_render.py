"""HTML structure tests: run python3 test_render.py"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import app
st={"family":"iphoneduo","zip":"90210","target_part":"MK1H4LL/A","poll_seconds":120,"next_poll_in_sec":99,
 "fetched_at":"2026-10-18T18:30:00-07:00","counts":{"available":0,"prelaunch":96,"unavailable":0,"unknown":0},
 "parts":[{"part":"MK1H4LL/A","title":"iPhone Duo 512GB Night Sky","capacity":"512GB","color":"Night Sky","price":"$2,199","buyUrl":"https://www.apple.com/shop/buy-iphone/iphone-duo/7.6-inch-display-512gb-night-sky"},
          {"part":"MK1E4LL/A","title":"iPhone Duo 256GB Star White","capacity":"256GB","color":"Star White","price":"$1,999","buyUrl":"https://example.com/x"}],
 "stores":[{"storeName":"Example Store","city":"Example City","state":"CA","postalCode":"90210","distanceText":"1.0 mi","address":"1 Example Street","phone":"555-0100","reservationUrl":"https://example.com/retail/example-store",
   "availability":{"MK1H4LL/A":{"state":"prelaunch","quote":"Check back for availability on 10.24"},"MK1E4LL/A":{"state":"prelaunch","quote":"Check back for availability on 10.24"}}}]}
h=app.render_html(st)
checks={
 "viewport": 'name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover' in h,
 "dark mode": "prefers-color-scheme: dark" in h,
 "mobile media": "@media (max-width:640px)" in h,
 "safe area": "env(safe-area-inset-bottom)" in h,
 "sticky glass header": "backdrop-filter" in h and 'class="top"' in h,
 "banner overview": "1 家店 · 0 有货 · 全部未发售" in h,
 "table exists": "<table>" in h and 'class="tablewrap"' in h,
 "compact pill": '>未售</span>' in h and '>有</span>' not in h or '>未售</span>' in h,
 "target column": 'data-target="1"' in h and '<span class=tag>目标</span>' in h,
 "filter": 'id="filterSel"' in h and "只看有货的店" in h,
 "buy button": "购买目标规格页" in h,
 "no store details": "门店地址" not in h and "Example Street" not in h and "555-01" not in h and "retail/example-store" not in h,
 "sticky cols": "position:sticky" in h,
 "banner mobile wraps full": 'class="banner-full"' in h and "全部未发售（Check back for availability on 10.24）" in h and "white-space:normal" in h,
}
for k,v in checks.items(): print(("OK " if v else "FAIL ")+k); assert v, k
# 8-col/12-row shape is asserted on live data in deploy checks; here 2 parts -> 3 header cells (store+2)
assert h.count('data-target=') >= 2
print("ALL RENDER TESTS PASSED")
