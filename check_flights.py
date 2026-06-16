#!/usr/bin/env python3
"""
Business-class fare watcher: Shanghai (PVG) -> New York (JFK/EWR)
Outbound ~2026-12-19, return ~2027-01-01.

Multi-source: queries every provider for which credentials are present, merges
and de-duplicates the results, and reports the single lowest business-class
fare across all of them. Email alerts are OPTIONAL (only sent if Gmail creds
are configured); the public page always shows all options.

Providers
  - SerpApi Google Flights   (REQUIRED)  -> aggregates airline + OTA prices
  - Amadeus Flight Offers     (optional)  -> GDS fares, direct + connecting
  - Kiwi / Tequila            (optional)  -> virtual interlining / split tickets

Outputs index.html (the public page) and data.json every run.
"""

import os
import sys
import json
import html
import smtplib
import datetime as dt
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
ORIGIN          = os.getenv("ORIGIN", "PVG")            # Shanghai Pudong
DEST            = os.getenv("DEST", "JFK,EWR")          # New York area
HUBS            = os.getenv("HUBS", "ICN,NRT,HND")      # Seoul / Tokyo split hubs
OUTBOUND_DATE   = os.getenv("OUTBOUND_DATE", "2026-12-19")
RETURN_DATE     = os.getenv("RETURN_DATE", "2027-01-01")
THRESHOLD_USD   = float(os.getenv("THRESHOLD_USD", "6500"))
CURRENCY        = "USD"

ALERT_TO        = os.getenv("ALERT_TO", "jeannekang@hotmail.com")
GMAIL_USER      = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
EMAIL_ENABLED   = bool(GMAIL_USER and GMAIL_APP_PASSWORD)

SERPAPI_KEY     = os.getenv("SERPAPI_KEY", "")
AMADEUS_CLIENT_ID     = os.getenv("AMADEUS_CLIENT_ID", "")
AMADEUS_CLIENT_SECRET = os.getenv("AMADEUS_CLIENT_SECRET", "")
AMADEUS_HOST    = os.getenv("AMADEUS_HOST", "https://api.amadeus.com")  # test: https://test.api.amadeus.com
KIWI_API_KEY    = os.getenv("KIWI_API_KEY", "")

# Re-alert only if a new (cheaper) deal appears, or 24h have passed.
STATE_FILE      = "state.json"
REALERT_HOURS   = float(os.getenv("REALERT_HOURS", "24"))

SEOUL_TOKYO     = {"ICN", "GMP", "NRT", "HND"}
HTTP_TIMEOUT    = 40


def log(msg):
    print("[" + dt.datetime.utcnow().isoformat(timespec="seconds") + "Z] " + str(msg), flush=True)


# --------------------------------------------------------------------------- #
# Offer model
# --------------------------------------------------------------------------- #
def make_offer(source, price, airlines, route, vias, stops, kind, link, dates):
    return {
        "source": source,
        "price": round(float(price)),
        "airlines": airlines,
        "route": route,
        "vias": vias,                 # list of layover airport ids
        "stops": stops,
        "kind": kind,                 # "Nonstop", "Connecting", or "Split-ticket"
        "link": link,
        "dates": dates,
        "via_seoul_tokyo": any(v in SEOUL_TOKYO for v in vias),
    }


def classify(vias, kind_hint=None):
    if kind_hint:
        return kind_hint
    return "Nonstop" if not vias else "Connecting"


# --------------------------------------------------------------------------- #
# Provider: SerpApi Google Flights
# --------------------------------------------------------------------------- #
def serpapi_call(params):
    p = {"engine": "google_flights", "api_key": SERPAPI_KEY,
         "currency": CURRENCY, "hl": "en", "gl": "us"}
    p.update(params)
    r = requests.get("https://serpapi.com/search", params=p, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def serpapi_parse(data, kind_hint=None, dates=""):
    offers = []
    link = data.get("search_metadata", {}).get("google_flights_url") \
        or data.get("google_flights_url") \
        or "https://www.google.com/travel/flights"
    for bucket in ("best_flights", "other_flights"):
        for opt in data.get(bucket, []) or []:
            price = opt.get("price")
            if not price:
                continue
            segs = opt.get("flights", []) or []
            airlines = sorted({s.get("airline") for s in segs if s.get("airline")})
            vias = [lo.get("id") for lo in opt.get("layovers", []) or [] if lo.get("id")]
            route = ""
            if segs:
                route = segs[0]["departure_airport"]["id"] + "->" + segs[-1]["arrival_airport"]["id"]
            offers.append(make_offer(
                "Google Flights (SerpApi)", price, airlines, route, vias,
                len(vias), classify(vias, kind_hint), link, dates))
    return offers


def provider_serpapi():
    out = []
    # 1) Round-trip business: direct + all connecting itineraries (incl. via ICN/NRT/HND)
    try:
        rt = serpapi_call({"departure_id": ORIGIN, "arrival_id": DEST,
                           "outbound_date": OUTBOUND_DATE, "return_date": RETURN_DATE,
                           "type": "1", "travel_class": "3", "adults": "1"})
        out += serpapi_parse(rt, dates=OUTBOUND_DATE + " / " + RETURN_DATE)
        log("SerpApi round-trip: " + str(len(out)) + " options")
    except Exception as ex:
        log("SerpApi round-trip failed: " + str(ex))

    # 2) One-way each direction -> sum cheapest = split-ticket (two one-ways) price
    try:
        ob = serpapi_call({"departure_id": ORIGIN, "arrival_id": DEST,
                           "outbound_date": OUTBOUND_DATE, "type": "2",
                           "travel_class": "3", "adults": "1"})
        rb = serpapi_call({"departure_id": DEST, "arrival_id": ORIGIN,
                           "outbound_date": RETURN_DATE, "type": "2",
                           "travel_class": "3", "adults": "1"})
        ob_off = serpapi_parse(ob, dates=OUTBOUND_DATE)
        rb_off = serpapi_parse(rb, dates=RETURN_DATE)
        if ob_off and rb_off:
            c_ob = min(ob_off, key=lambda o: o["price"])
            c_rb = min(rb_off, key=lambda o: o["price"])
            vias = list(dict.fromkeys(c_ob["vias"] + c_rb["vias"]))
            airlines = sorted(set(c_ob["airlines"] + c_rb["airlines"]))
            out.append(make_offer(
                "Google Flights (SerpApi)", c_ob["price"] + c_rb["price"],
                airlines, ORIGIN + "<->" + DEST, vias,
                c_ob["stops"] + c_rb["stops"], "Split-ticket",
                c_ob["link"], OUTBOUND_DATE + " / " + RETURN_DATE))
            log("SerpApi split (2 one-ways): $" + str(c_ob["price"]) + " + $" + str(c_rb["price"]))
    except Exception as ex:
        log("SerpApi one-way legs failed: " + str(ex))
    return out


# --------------------------------------------------------------------------- #
# Provider: Amadeus Flight Offers Search (optional)
# --------------------------------------------------------------------------- #
def amadeus_token():
    r = requests.post(AMADEUS_HOST + "/v1/security/oauth2/token",
                      data={"grant_type": "client_credentials",
                            "client_id": AMADEUS_CLIENT_ID,
                            "client_secret": AMADEUS_CLIENT_SECRET},
                      timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()["access_token"]


def amadeus_search(token, dest):
    body = {
        "currencyCode": CURRENCY,
        "originDestinations": [
            {"id": "1", "originLocationCode": ORIGIN, "destinationLocationCode": dest,
             "departureDateTimeRange": {"date": OUTBOUND_DATE}},
            {"id": "2", "originLocationCode": dest, "destinationLocationCode": ORIGIN,
             "departureDateTimeRange": {"date": RETURN_DATE}},
        ],
        "travelers": [{"id": "1", "travelerType": "ADULT"}],
        "sources": ["GDS"],
        "searchCriteria": {
            "maxFlightOffers": 20,
            "flightFilters": {"cabinRestrictions": [
                {"cabin": "BUSINESS", "coverage": "ALL_SEGMENTS",
                 "originDestinationIds": ["1", "2"]}]},
        },
    }
    r = requests.post(AMADEUS_HOST + "/v2/shopping/flight-offers",
                      headers={"Authorization": "Bearer " + token,
                               "Content-Type": "application/json"},
                      json=body, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def provider_amadeus():
    if not (AMADEUS_CLIENT_ID and AMADEUS_CLIENT_SECRET):
        return []
    out = []
    try:
        token = amadeus_token()
    except Exception as ex:
        log("Amadeus auth failed: " + str(ex))
        return []
    gf_link = ("https://www.google.com/travel/flights?q=flights+" +
               ORIGIN + "+to+" + DEST + "+business+" + OUTBOUND_DATE + "+" + RETURN_DATE)
    for dest in [d.strip() for d in DEST.split(",") if d.strip()]:
        try:
            data = amadeus_search(token, dest)
        except Exception as ex:
            log("Amadeus " + ORIGIN + "->" + dest + " failed: " + str(ex))
            continue
        carriers = (data.get("dictionaries", {}) or {}).get("carriers", {})
        for off in data.get("data", []) or []:
            try:
                price = float(off["price"]["grandTotal"])
            except Exception:
                continue
            airlines, vias, stops = set(), [], 0
            for itin in off.get("itineraries", []):
                segs = itin.get("segments", [])
                stops += max(0, len(segs) - 1)
                for i, s in enumerate(segs):
                    code = s.get("carrierCode", "")
                    airlines.add(carriers.get(code, code))
                    if i > 0:
                        vias.append(s["departure"]["iataCode"])
            out.append(make_offer(
                "Amadeus (GDS)", price, sorted(airlines),
                ORIGIN + "<->" + dest, vias, stops,
                classify(vias), gf_link, OUTBOUND_DATE + " / " + RETURN_DATE))
        log("Amadeus " + ORIGIN + "->" + dest + ": collected offers")
    return out


# --------------------------------------------------------------------------- #
# Provider: Kiwi / Tequila (optional) - virtual interlining / split tickets
# --------------------------------------------------------------------------- #
def provider_kiwi():
    if not KIWI_API_KEY:
        return []
    out = []

    def d(s):  # YYYY-MM-DD -> DD/MM/YYYY (Tequila format)
        y, m, dd = s.split("-")
        return dd + "/" + m + "/" + y

    params = {
        "fly_from": ORIGIN, "fly_to": DEST,
        "date_from": d(OUTBOUND_DATE), "date_to": d(OUTBOUND_DATE),
        "return_from": d(RETURN_DATE), "return_to": d(RETURN_DATE),
        "selected_cabins": "C", "curr": CURRENCY, "adults": 1,
        "limit": 30, "sort": "price", "vehicle_type": "aircraft",
    }
    try:
        r = requests.get("https://api.tequila.kiwi.com/v2/search",
                         headers={"apikey": KIWI_API_KEY}, params=params,
                         timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as ex:
        log("Kiwi search failed: " + str(ex))
        return []
    for it in data.get("data", []) or []:
        price = it.get("price")
        if not price:
            continue
        legs = it.get("route", []) or []
        airlines = sorted({l.get("airline") for l in legs if l.get("airline")})
        vias = []
        for l in legs[1:]:
            a = l.get("cityCodeFrom") or l.get("flyFrom")
            if a:
                vias.append(a)
        kind = "Split-ticket" if it.get("virtual_interlining") else classify(vias)
        out.append(make_offer(
            "Kiwi (Tequila)", price, airlines,
            ORIGIN + "<->" + DEST, vias, max(0, len(legs) - 2),
            kind, it.get("deep_link", "https://www.kiwi.com"),
            OUTBOUND_DATE + " / " + RETURN_DATE))
    log("Kiwi: " + str(len(out)) + " options")
    return out


# --------------------------------------------------------------------------- #
# Merge / dedupe
# --------------------------------------------------------------------------- #
def dedupe(offers):
    seen, out = {}, []
    for o in sorted(offers, key=lambda x: x["price"]):
        key = (o["source"], o["price"], tuple(o["airlines"]), o["kind"])
        if key in seen:
            continue
        seen[key] = True
        out.append(o)
    return out


# --------------------------------------------------------------------------- #
# Email (optional)
# --------------------------------------------------------------------------- #
def send_email(subject, html_body):
    if not EMAIL_ENABLED:
        log("Email skipped: Gmail credentials not set (email is optional).")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = ALERT_TO
    msg.attach(MIMEText(html_body, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            s.sendmail(GMAIL_USER, [ALERT_TO], msg.as_string())
        log("Alert email sent to " + ALERT_TO)
        return True
    except Exception as ex:
        log("Email failed: " + str(ex))
        return False


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def should_alert(best, state):
    """Alert on first deal, a new lower price, or after REALERT_HOURS."""
    last_price = state.get("last_alert_price")
    last_time = state.get("last_alert_time")
    if last_price is None:
        return True
    if best["price"] < last_price:
        return True
    if last_time:
        age = (dt.datetime.utcnow() -
               dt.datetime.fromisoformat(last_time)).total_seconds() / 3600
        if age >= REALERT_HOURS:
            return True
    return False


# --------------------------------------------------------------------------- #
# HTML page
# --------------------------------------------------------------------------- #
def e(x):
    return html.escape(str(x))


def render_page(offers, best, deal, now):
    rows = ""
    for i, o in enumerate(offers[:30]):
        via = " -> ".join(o["vias"]) if o["vias"] else "Nonstop"
        badges = ""
        if o["via_seoul_tokyo"]:
            badges += "<span class='tag tag-via'>via Seoul/Tokyo</span>"
        if i == 0:
            badges = "<span class='tag tag-best'>Lowest</span>" + badges
        kind_cls = {"Nonstop": "k-non", "Connecting": "k-con",
                    "Split-ticket": "k-split"}.get(o["kind"], "k-con")
        rows += (
            "<tr>"
            "<td class='price'>$" + e(o["price"]) + "</td>"
            "<td class='air'>" + e(", ".join(o["airlines"]) or "-") + badges + "</td>"
            "<td><span class='chip " + kind_cls + "'>" + e(o["kind"]) + "</span></td>"
            "<td class='route'>" + e(via) + "</td>"
            "<td class='src'>" + e(o["source"]) + "</td>"
            "<td><a class='book' href='" + e(o["link"]) + "' target='_blank' rel='noopener'>Book -></a></td>"
            "</tr>"
        )

    if deal:
        email_note = (" &middot; alert emailed to " + e(ALERT_TO)) if EMAIL_ENABLED else ""
        status = (
            "<div class='hero deal'><div class='hero-tag'>DEAL FOUND</div>"
            "<div class='hero-price'>$" + e(best["price"]) + "</div>"
            "<div class='hero-sub'>business class &middot; under your $" + e(int(THRESHOLD_USD)) + " target" + email_note + "</div>"
            "<a class='hero-cta' href='" + e(best["link"]) + "' target='_blank' rel='noopener'>"
            "Book " + e(", ".join(best["airlines"]) or "this fare") + " -></a></div>"
        )
    elif best:
        gap = best["price"] - int(THRESHOLD_USD)
        status = (
            "<div class='hero watch'><div class='hero-tag'>WATCHING</div>"
            "<div class='hero-price'>$" + e(best["price"]) + "</div>"
            "<div class='hero-sub'>current lowest &middot; $" + e(gap) + " above your $" + e(int(THRESHOLD_USD)) + " target</div>"
            "<a class='hero-cta ghost' href='" + e(best["link"]) + "' target='_blank' rel='noopener'>"
            "View " + e(", ".join(best["airlines"]) or "fare") + " -></a></div>"
        )
    else:
        status = ("<div class='hero watch'><div class='hero-tag'>NO DATA</div>"
                  "<div class='hero-sub'>No fares returned this run - will retry next hour.</div></div>")

    sources = ", ".join(sorted({o["source"] for o in offers})) or "-"
    tbody = rows or "<tr><td colspan='6' style='color:var(--mut)'>No options this run - retrying next hour.</td></tr>"
    lowest_txt = ("$" + e(best["price"])) if best else "-"

    return (
"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="900">
<title>PVG -> NYC Business Class Fare Watch</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
 :root{--bg:#0a0e1a;--card:#151c33;--card2:#1b2440;--ink:#eef2fb;--mut:#94a1c4;--line:#28324f;--good:#22c55e;--good-d:#16a34a;--accent:#6c9bff;}
 *{box-sizing:border-box} html{-webkit-text-size-adjust:100%}
 body{margin:0;color:var(--ink);font-family:Inter,-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;
   background:radial-gradient(1200px 600px at 80% -10%,rgba(108,155,255,.18),transparent 60%),
              radial-gradient(900px 500px at -10% 10%,rgba(34,197,94,.10),transparent 55%),var(--bg);}
 .wrap{max-width:1040px;margin:0 auto;padding:30px 20px 70px}
 .top{display:flex;align-items:baseline;justify-content:space-between;gap:12px;flex-wrap:wrap}
 h1{font-size:27px;font-weight:800;letter-spacing:-.02em;margin:0}
 .pill{display:inline-flex;align-items:center;gap:7px;background:var(--card);border:1px solid var(--line);
   color:var(--mut);font-size:13px;font-weight:500;padding:6px 12px;border-radius:999px}
 .dot{width:8px;height:8px;border-radius:50%;background:var(--good);box-shadow:0 0 0 4px rgba(34,197,94,.18)}
 .route-line{color:var(--mut);font-size:15px;margin:6px 0 22px;font-weight:500}
 .route-line b{color:var(--ink);font-weight:600}
 .hero{border-radius:20px;padding:26px 28px;margin:6px 0 22px;border:1px solid var(--line)}
 .hero.deal{background:linear-gradient(135deg,rgba(34,197,94,.22),rgba(34,197,94,.06));border-color:rgba(34,197,94,.55)}
 .hero.watch{background:linear-gradient(135deg,var(--card2),var(--card))}
 .hero-tag{font-size:12px;font-weight:700;letter-spacing:.14em;color:var(--mut)}
 .hero.deal .hero-tag{color:var(--good)}
 .hero-price{font-size:52px;font-weight:800;letter-spacing:-.03em;margin:2px 0;line-height:1}
 .hero-sub{color:var(--mut);font-size:15px;margin-bottom:16px}
 .hero-cta{display:inline-block;background:var(--good-d);color:#fff;font-weight:600;font-size:15px;
   padding:11px 20px;border-radius:12px;text-decoration:none}
 .hero-cta.ghost{background:transparent;border:1px solid var(--accent);color:var(--accent)}
 .grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:0 0 26px}
 @media(max-width:640px){.grid{grid-template-columns:repeat(2,1fr)}.hero-price{font-size:42px}}
 .stat{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px 18px}
 .stat .k{color:var(--mut);font-size:12.5px;font-weight:500}
 .stat .v{font-size:23px;font-weight:700;margin-top:5px}
 .stat .v.sm{font-size:14px;font-weight:600;line-height:1.35}
 .sec{font-size:14px;font-weight:600;color:var(--mut);margin:0 0 10px}
 .table-card{background:var(--card);border:1px solid var(--line);border-radius:16px;overflow:hidden}
 table{width:100%;border-collapse:collapse}
 th,td{text-align:left;padding:13px 16px;font-size:14px;border-bottom:1px solid var(--line)}
 th{color:var(--mut);font-weight:600;font-size:12px;letter-spacing:.05em;text-transform:uppercase;background:rgba(255,255,255,.02)}
 tr:last-child td{border-bottom:none}
 tbody tr:hover{background:rgba(108,155,255,.06)}
 td.price{font-weight:800;font-size:16px;white-space:nowrap}
 td.air{font-weight:500} td.route{color:var(--mut)} td.src{color:var(--mut);font-size:13px}
 .chip{display:inline-block;font-size:11.5px;font-weight:600;padding:3px 9px;border-radius:999px}
 .k-non{background:rgba(34,197,94,.16);color:#7ee2a0}
 .k-con{background:rgba(108,155,255,.16);color:#a8c2ff}
 .k-split{background:rgba(245,158,11,.16);color:#f7c267}
 .tag{display:inline-block;font-size:10.5px;font-weight:700;padding:2px 7px;border-radius:6px;margin-left:7px;vertical-align:middle}
 .tag-best{background:var(--good);color:#06210f}
 .tag-via{background:rgba(245,158,11,.18);color:#f7c267}
 a.book{color:var(--accent);font-weight:600;text-decoration:none;white-space:nowrap}
 a.book:hover{text-decoration:underline}
 footer{color:var(--mut);font-size:12.5px;margin-top:24px;line-height:1.6}
 footer b{color:#b9c4e0}
</style></head><body><div class="wrap">
 <div class="top">
   <h1>Shanghai -> New York &middot; Business</h1>
   <span class="pill"><span class="dot"></span>Updated """ + e(now) + """</span>
 </div>
 <p class="route-line"><b>""" + e(ORIGIN) + """</b> -> <b>""" + e(DEST) + """</b> &middot; depart <b>""" + e(OUTBOUND_DATE) + """</b> &middot; return <b>""" + e(RETURN_DATE) + """</b> &middot; 1 adult &middot; alert under <b>$""" + e(int(THRESHOLD_USD)) + """</b></p>
 """ + status + """
 <div class="grid">
   <div class="stat"><div class="k">Lowest fare now</div><div class="v">""" + lowest_txt + """</div></div>
   <div class="stat"><div class="k">Alert threshold</div><div class="v">$""" + e(int(THRESHOLD_USD)) + """</div></div>
   <div class="stat"><div class="k">Options found</div><div class="v">""" + e(len(offers)) + """</div></div>
   <div class="stat"><div class="k">Sources searched</div><div class="v sm">""" + e(sources) + """</div></div>
 </div>
 <p class="sec">All options &middot; cheapest first</p>
 <div class="table-card">
 <table>
  <thead><tr><th>Price</th><th>Airline(s)</th><th>Type</th><th>Routing</th><th>Source</th><th></th></tr></thead>
  <tbody>""" + tbody + """</tbody>
 </table>
 </div>
 <footer>Auto-refreshes hourly via GitHub Actions; this page also reloads itself every 15 min.<br>
   Prices are <b>indicative</b> and can change between searches - always confirm the final fare on the airline or OTA site before booking.</footer>
</div></body></html>""")


def email_body(best, offers, now):
    top = offers[:5]
    rows = "".join(
        "<li><b>$" + e(o["price"]) + "</b> - " + e(", ".join(o["airlines"]) or "-") +
        " (" + e(o["kind"]) + (", via " + e(", ".join(o["vias"])) if o["vias"] else "") + ") " +
        "[" + e(o["source"]) + "] - <a href='" + e(o["link"]) + "'>book</a></li>"
        for o in top)
    return (
        "<html><body style=\"font-family:Arial,sans-serif\">"
        "<h2>Business-class deal: Shanghai -> New York under $" + e(int(THRESHOLD_USD)) + "</h2>"
        "<p><b>Lowest fare: $" + e(best["price"]) + "</b> on " + e(", ".join(best["airlines"]) or "see link") +
        " (" + e(best["kind"]) + (", via " + e(", ".join(best["vias"])) if best["vias"] else "") + ").</p>"
        "<p>Route " + e(best["route"]) + " &middot; depart <b>" + e(OUTBOUND_DATE) + "</b>, return <b>" + e(RETURN_DATE) + "</b> &middot; business class &middot; 1 adult.</p>"
        "<p><a href=\"" + e(best["link"]) + "\" style=\"background:#16a34a;color:#fff;padding:10px 16px;border-radius:8px;text-decoration:none\">Book this fare</a></p>"
        "<h3>Other low options</h3><ul>" + rows + "</ul>"
        "<p style=\"color:#666;font-size:12px\">Found " + e(now) + ". Prices are indicative - confirm on the booking site. Automated watcher.</p>"
        "</body></html>")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    if not SERPAPI_KEY:
        log("FATAL: SERPAPI_KEY is required.")
        sys.exit(1)

    now = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    offers = []
    offers += provider_serpapi()
    offers += provider_amadeus()
    offers += provider_kiwi()
    offers = dedupe(offers)

    best = offers[0] if offers else None
    deal = bool(best and best["price"] <= THRESHOLD_USD)

    state = load_state()
    if deal and EMAIL_ENABLED and should_alert(best, state):
        sent = send_email(
            "PVG->NYC business $" + str(best["price"]) + " (under $" + str(int(THRESHOLD_USD)) + ")",
            email_body(best, offers, now))
        if sent:
            state["last_alert_price"] = best["price"]
            state["last_alert_time"] = dt.datetime.utcnow().isoformat()
            save_state(state)
    elif not deal:
        if state.get("last_alert_price") is not None:
            state["last_alert_price"] = None
            save_state(state)

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(render_page(offers, best, deal, now))
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump({"updated": now, "threshold": THRESHOLD_USD,
                   "deal": deal, "lowest": best, "offers": offers}, f, indent=2)

    log("Done. " + str(len(offers)) + " offers; lowest=" +
        (("$" + str(best["price"])) if best else "none") + "; deal=" + str(deal))


if __name__ == "__main__":
    main()
