"""Run: python3 test_state_classifier.py — synthetic fixtures only (no external data files, no network)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from app import classify_state, parse_meta, normalize_pickup

cases=[
 ("available","Available Today","available"),
 ("available","Available Tomorrow","available"),
 ("default","Check back for availability on 10.24","prelaunch"),
 ("unavailable","Unavailable for Pickup","unavailable"),
 ("weird","???","unknown"),
 (None,"","unknown"),
]
for d,q,exp in cases:
    got=classify_state(d,q); assert got==exp, (d,q,got,exp); print(f"OK classify({d!r},{q!r})={got}")

# Synthetic product-locator meta: 2 parts, including the public target part number.
meta_raw={"body":{"productLocatorOverlayData":{"productLocatorMeta":{
  "dimensionsVariations":{"dimensionColor":[
    {"key":"night_sky","value":"Night Sky"},
    {"key":"star_white","value":"Star White"}]},
  "products":[
    {"partNumber":"MK1H4LL/A","productTitle":"iPhone Duo 512GB Night Sky","dimensionCapacity":"512GB","dimensionColor":"night_sky","price":"2199_00_example","productLink":"https://www.apple.com/shop/buy-iphone/iphone-duo/7.6-inch-display-512gb-night-sky"},
    {"partNumber":"MK1E4LL/A","productTitle":"iPhone Duo 256GB Star White","dimensionCapacity":"256GB","dimensionColor":"star_white","price":"1999_00_example","productLink":"https://example.com/buy/example"}
  ]}}}}
meta=parse_meta(meta_raw,"iphoneduo")
assert len(meta)==2, meta
assert any(p["part"]=="MK1H4LL/A" and p["capacity"]=="512GB" and p["color"]=="Night Sky" for p in meta), meta
print("OK synthetic meta 2 parts, target:", [p for p in meta if p["part"]=="MK1H4LL/A"])

# Synthetic pickup response: one prelaunch store, one available/unavailable mix.
pickup_raw={"body":{"stores":[
  {"storeName":"Example Store","storeNumber":"R001","city":"Example City","state":"CA",
   "address":{"address":"1 Example Street","address2":"","address3":"","postalCode":"90210"},
   "storedistance":"1.0 mi","storeDistanceWithUnit":"1.0 mi","phoneNumber":"555-0100",
   "reservationUrl":"https://example.com/retail/example-store","directionsUrl":"",
   "storeHours":{"hours":[{"storeDays":"Mon","storeTimings":"10 AM-8 PM"}]},
   "partsAvailability":{
     "MK1H4LL/A":{"pickupDisplay":"default","pickupSearchQuote":"Check back for availability on 10.24","messageTypes":{"regular":{"storePickupProductTitle":"iPhone Duo 512GB Night Sky"}}},
     "MK1E4LL/A":{"pickupDisplay":"default","pickupSearchQuote":"Check back for availability on 10.24","messageTypes":{"regular":{"storePickupProductTitle":"iPhone Duo 256GB Star White"}}}}},
  {"storeName":"Second Example Store","storeNumber":"R002","city":"Example City","state":"CA",
   "address":{"address":"2 Example Street","address2":"","address3":"","postalCode":"90210"},
   "storedistance":"2.0 mi","storeDistanceWithUnit":"2.0 mi","phoneNumber":"555-0101",
   "reservationUrl":"https://example.com/retail/example-store-2","directionsUrl":"",
   "storeHours":{"hours":[]},
   "partsAvailability":{
     "MK1H4LL/A":{"pickupDisplay":"available","pickupSearchQuote":"Available Today","messageTypes":{"regular":{"storePickupProductTitle":"iPhone Duo 512GB Night Sky"}}},
     "MK1E4LL/A":{"pickupDisplay":"unavailable","pickupSearchQuote":"Unavailable for Pickup","messageTypes":{"regular":{"storePickupProductTitle":"iPhone Duo 256GB Star White"}}}}}
]}}
stores=normalize_pickup(pickup_raw, meta)
states={a["state"] for s in stores for a in s["availability"].values()}
assert len(stores)==2 and states=={"prelaunch","available","unavailable"}, states
print(f"OK synthetic pickup: {len(stores)} stores, states={states}")
tgt=[p for p in meta if p["part"]=="MK1H4LL/A"][0]
assert tgt["buyUrl"]=="https://www.apple.com/shop/buy-iphone/iphone-duo/7.6-inch-display-512gb-night-sky", tgt
h=stores[0]
assert h["storeName"]=="Example Store" and h["phone"] and h["postalCode"]=="90210" and "Example Street" in h["address"] and h["reservationUrl"], h
print("OK enriched synthetic fields: buyUrl, address/phone/zip/reservationUrl")
print("ALL TESTS PASSED")
