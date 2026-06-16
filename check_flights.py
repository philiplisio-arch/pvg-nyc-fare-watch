#!/usr/bin/env python3
"""
Business-class fare watcher: Shanghai (PVG) -> New York (EWR/JFK)
Outbound ~2026-12-19, return ~2027-01-01.

Strategy
  Compares two ways to complete the full Shanghai <-> New York round trip:

  1. DIRECT     - a normal round-trip business ticket PVG <-> EWR/JFK.
  2. HUB-SPLIT  - the expensive long-haul leg bought as a Seoul/Tokyo <-> New York
                  business round trip, PLUS a cheap separate economy connector
                  PVG <-> the same hub. Often cheaper overall.

For the hub-split it prices, per hub (ICN / NRT / HND):
     long-haul business round trip  (hub <-> EWR,JFK)
   + cheapest connector round trip  (PVG <-> hub, economy)
and picks the cheapest hub. The "lowest fare" is the cheapest COMPLETE journey
across both strategies. Email alerts are optional (off unless Gmail creds set).

Outputs index.html (public page) and data.json every run.
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
DEST            = os.getenv("DEST", "EWR,JFK")          # Newark + JFK
HUBS            = os.getenv("HUBS", "ICN,NRT,HND")      # Seoul / Tokyo split hubs
OUTBOUND_DATE   = os.getenv("OUTBOUND_DATE", "2026-12-19")
RETURN_DATE     = os.getenv("RETURN_DATE", "2027-01-01")
THRESHOLD_USD   = float(os.getenv("THRESHOLD_USD", "6500"))
LONGHAUL_CLASS  = os.getenv("LONGHAUL_CLASS", "3")      # 3 = business
CONNECTOR_CLASS = os.getenv("CONNECTOR_CLASS", "1")     # 1 = economy (cheapest)
CURRENCY        = "USD"

ALERT_TO            = os.getenv("ALERT_TO", "jeannekang@hotmail.com")
AGENTMAIL_API_KEY   = os.getenv("AGENTMAIL_API_KEY", "")
AGENTMAIL_INBOX_ID  = os.getenv("AGENTMAIL_INBOX_ID", "")  # optional; auto-discovered if blank
AGENTMAIL_BASE      = "https://api.agentmail.to/v0"
EMAIL_ENABLED       = bool(AGENTMAIL_API_KEY)

SERPAPI_KEY     = os.getenv("SERPAPI_KEY", "")

STATE_FILE      = "state.json"
REALERT_HOURS   = float(os.getenv("REALERT_HOURS", "24"))
HTTP_TIMEOUT    = 40

HUB_NAMES = {"ICN": "Seoul", "GMP": "Seoul", "NRT": "Tokyo", "HND": "Tokyo"}

# Only show itineraries operated by United, or Chinese / Japanese / Korean carriers.
ALLOWED_AIRLINES = [
    "united",
    # Chinese (mainland)
    "air china", "china eastern", "china southern", "hainan", "xiamen",
    "shanghai airlines", "juneyao", "sichuan", "shenzhen", "spring",
    "beijing capital", "tianjin", "china united",
    # Japanese
    "all nippon", "japan airlines", "jal", "zipair", "peach", "starflyer",
    "skymark", "solaseed", "ibex", "fuji dream",
    # Korean
    "korean air", "asiana", "jin air", "air busan", "t'way", "tway",
    "jeju air", "air seoul", "eastar",
]


def airline_ok(name):
    n = (name or "").lower().strip()
    if n == "ana":                      # All Nippon shows as "ANA"
        return True
    return any(tok in n for tok in ALLOWED_AIRLINES)


def airlines_allowed(airlines):
    return bool(airlines) and all(airline_ok(a) for a in airlines)


def log(msg):
    print("[" + dt.datetime.utcnow().isoformat(timespec="seconds") + "Z] " + str(msg), flush=True)


# --------------------------------------------------------------------------- #
# SerpApi Google Flights
# --------------------------------------------------------------------------- #
def serpapi_call(params):
    p = {"engine": "google_flights", "api_key": SERPAPI_KEY,
         "currency": CURRENCY, "hl": "en", "gl": "us"}
    p.update(params)
    r = requests.get("https://serpapi.com/search", params=p, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def parse_options(data):
    """Return list of {price, airlines, first, last, vias, link} for a search."""
    out = []
    link = data.get("search_metadata", {}).get("google_flights_url") \
        or data.get("google_flights_url") \
        or "https://www.google.com/travel/flights"
    for bucket in ("best_flights", "other_flights"):
        for opt in data.get(bucket, []) or []:
            price = opt.get("price")
            if not price:
                continue
            segs = opt.get("flights", []) or []
            if not segs:
                continue
            out.append({
                "price": round(float(price)),
                "airlines": sorted({s.get("airline") for s in segs if s.get("airline")}),
                "first": segs[0]["departure_airport"]["id"],
                "last": segs[-1]["arrival_airport"]["id"],
                "vias": [lo.get("id") for lo in opt.get("layovers", []) or [] if lo.get("id")],
                "link": link,
            })
    return out


def rt_search(dep, arr, travel_class):
    return parse_options(serpapi_call({
        "departure_id": dep, "arrival_id": arr,
        "outbound_date": OUTBOUND_DATE, "return_date": RETURN_DATE,
        "type": "1", "travel_class": travel_class, "adults": "1"}))


def cheapest_by_hub(options, hubs, hub_is="first"):
    """Bucket options by the hub airport, keep cheapest per hub."""
    best = {}
    for o in options:
        hub = o["first"] if hub_is == "first" else o["last"]
        if hub not in hubs:
            continue
        if hub not in best or o["price"] < best[hub]["price"]:
            best[hub] = o
    return best


# --------------------------------------------------------------------------- #
# Build journeys + components
# --------------------------------------------------------------------------- #
def offer(price, airlines, kind, route, link, category, extra=None):
    o = {"price": round(float(price)), "airlines": airlines, "kind": kind,
         "route": route, "link": link, "category": category}
    if extra:
        o.update(extra)
    return o


def build():
    hubs = [h.strip() for h in HUBS.split(",") if h.strip()]
    journeys, components, best_hub = [], [], None

    # 1) DIRECT round trip PVG <-> EWR,JFK (business) -- comparison baseline
    try:
        direct = [o for o in rt_search(ORIGIN, DEST, LONGHAUL_CLASS)
                  if airlines_allowed(o["airlines"])]
        for o in sorted(direct, key=lambda x: x["price"])[:8]:
            kind = "Nonstop" if not o["vias"] else "Connecting"
            route = ORIGIN + " -> " + o["last"] + (" via " + ",".join(o["vias"]) if o["vias"] else "")
            journeys.append(offer(o["price"], o["airlines"], kind,
                                  route + "  (round trip)", o["link"], "direct"))
        log("Direct PVG<->" + DEST + ": " + str(len(direct)) + " options")
    except Exception as ex:
        log("Direct search failed: " + str(ex))

    # 2) HUB-SPLIT components
    try:
        longhaul = [o for o in rt_search(HUBS, DEST, LONGHAUL_CLASS)
                    if airlines_allowed(o["airlines"])]          # hub -> NYC (business)
        connector = [o for o in rt_search(ORIGIN, HUBS, CONNECTOR_CLASS)
                     if airlines_allowed(o["airlines"])]         # PVG <-> hub (economy)
        lh_by_hub = cheapest_by_hub(longhaul, hubs, hub_is="first")
        cn_by_hub = cheapest_by_hub(connector, hubs, hub_is="last")
        log("Long-haul hubs->" + DEST + ": " + str(len(longhaul)) +
            "; connector PVG<->hubs: " + str(len(connector)))

        combos = []
        for hub in hubs:
            if hub in lh_by_hub and hub in cn_by_hub:
                lh, cn = lh_by_hub[hub], cn_by_hub[hub]
                combos.append((hub, lh, cn, lh["price"] + cn["price"]))
        combos.sort(key=lambda c: c[3])

        for hub, lh, cn, total in combos:
            city = HUB_NAMES.get(hub, hub)
            journeys.append(offer(
                total, lh["airlines"], "Split-ticket",
                ORIGIN + " <-> " + hub + " <-> " + lh["last"] + "  (2 tickets)",
                lh["link"], "split",
                {"hub": hub, "hub_city": city,
                 "connector_price": cn["price"], "longhaul_price": lh["price"]}))

        if combos:
            best_hub, lh, cn, total = combos[0]
            city = HUB_NAMES.get(best_hub, best_hub)
            components.append(offer(
                cn["price"], cn["airlines"], "Economy",
                ORIGIN + " <-> " + best_hub + "  (connector, round trip)",
                cn["link"], "component", {"leg": "Connector " + city}))
            components.append(offer(
                lh["price"], lh["airlines"], "Business",
                best_hub + " <-> " + lh["last"] + "  (long-haul, round trip)",
                lh["link"], "component", {"leg": "Long-haul " + city}))
    except Exception as ex:
        log("Hub-split search failed: " + str(ex))

    journeys.sort(key=lambda x: x["price"])
    return journeys, components, best_hub


# --------------------------------------------------------------------------- #
# Email (optional)
# --------------------------------------------------------------------------- #
def _agentmail_inbox():
    """Return an inbox_id to send from: explicit env, else first existing, else create one."""
    if AGENTMAIL_INBOX_ID:
        return AGENTMAIL_INBOX_ID
    auth = {"Authorization": "Bearer " + AGENTMAIL_API_KEY}
    r = requests.get(AGENTMAIL_BASE + "/inboxes", headers=auth, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    inboxes = (r.json() or {}).get("inboxes", []) or []
    if inboxes:
        return inboxes[0].get("inbox_id")
    r = requests.post(AGENTMAIL_BASE + "/inboxes",
                      headers={"Authorization": "Bearer " + AGENTMAIL_API_KEY,
                               "Content-Type": "application/json"},
                      json={}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return (r.json() or {}).get("inbox_id")


def send_email(subject, html_body):
    if not EMAIL_ENABLED:
        log("Email skipped: AGENTMAIL_API_KEY not set (email is optional).")
        return False
    try:
        inbox_id = _agentmail_inbox()
        if not inbox_id:
            log("Email failed: no AgentMail inbox available.")
            return False
        r = requests.post(
            AGENTMAIL_BASE + "/inboxes/" + inbox_id + "/messages/send",
            headers={"Authorization": "Bearer " + AGENTMAIL_API_KEY,
                     "Content-Type": "application/json"},
            json={"to": ALERT_TO, "subject": subject, "html": html_body,
                  "text": "Business-class fare alert - open the HTML version for details."},
            timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        log("Alert email sent to " + ALERT_TO + " via AgentMail inbox " + str(inbox_id))
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
    last_price = state.get("last_alert_price")
    last_time = state.get("last_alert_time")
    if last_price is None:
        return True
    if best["price"] < last_price:
        return True
    if last_time:
        age = (dt.datetime.utcnow() - dt.datetime.fromisoformat(last_time)).total_seconds() / 3600
        if age >= REALERT_HOURS:
            return True
    return False


# --------------------------------------------------------------------------- #
# HTML page
# --------------------------------------------------------------------------- #
def e(x):
    return html.escape(str(x))


def journey_rows(journeys):
    rows = ""
    for i, o in enumerate(journeys[:20]):
        badges = "<span class='tag tag-best'>Lowest</span>" if i == 0 else ""
        if o["category"] == "split":
            badges += "<span class='tag tag-via'>via " + e(o.get("hub_city", "")) + "</span>"
            cls = "k-split"
            sub = ("<div class='sub'>$" + e(o["connector_price"]) + " connector + $" +
                   e(o["longhaul_price"]) + " long-haul</div>")
        else:
            cls = "k-non" if o["kind"] == "Nonstop" else "k-con"
            sub = ""
        rows += (
            "<tr>"
            "<td class='price'>$" + e(o["price"]) + "</td>"
            "<td class='air'>" + e(", ".join(o["airlines"]) or "-") + badges + "</td>"
            "<td><span class='chip " + cls + "'>" + e(o["kind"]) + "</span></td>"
            "<td class='route'>" + e(o["route"]) + sub + "</td>"
            "<td><a class='book' href='" + e(o["link"]) + "' target='_blank' rel='noopener'>Book -></a></td>"
            "</tr>"
        )
    return rows or "<tr><td colspan='5' style='color:var(--mut)'>No options this run.</td></tr>"


def component_rows(components):
    rows = ""
    for o in components:
        rows += (
            "<tr>"
            "<td class='price'>$" + e(o["price"]) + "</td>"
            "<td class='air'>" + e(o.get("leg", "")) + "</td>"
            "<td><span class='chip k-con'>" + e(o["kind"]) + "</span></td>"
            "<td class='route'>" + e(o["route"]) + "</td>"
            "<td><a class='book' href='" + e(o["link"]) + "' target='_blank' rel='noopener'>Book -></a></td>"
            "</tr>"
        )
    return rows


def render_page(journeys, components, best, deal, now):
    if deal:
        tag, cls = "DEAL FOUND", "deal"
        sub = "complete Shanghai <-> New York round trip, business long-haul, under your $" + e(int(THRESHOLD_USD)) + " target"
        if EMAIL_ENABLED:
            sub += " &middot; alert emailed to " + e(ALERT_TO)
        cta = "Book " + e(", ".join(best["airlines"]) or "this fare") + " ->"
    elif best:
        gap = best["price"] - int(THRESHOLD_USD)
        tag, cls = "WATCHING", "watch"
        sub = "current lowest complete journey &middot; $" + e(gap) + " above your $" + e(int(THRESHOLD_USD)) + " target"
        cta = "View ->"
    else:
        tag, cls, sub, cta = "NO DATA", "watch", "No fares returned this run - will retry next hour.", None

    how = ""
    if best and best.get("category") == "split":
        how = (" &middot; cheapest as a <b>" + e(best.get("hub_city", "")) +
               " split</b> (2 tickets)")
    elif best:
        how = " &middot; cheapest as a <b>direct round trip</b>"

    hero = "<div class='hero " + cls + "'><div class='hero-tag'>" + tag + "</div>"
    if best:
        hero += "<div class='hero-price'>$" + e(best["price"]) + "</div>"
    hero += "<div class='hero-sub'>" + sub + how + "</div>"
    if cta and best:
        hero += ("<a class='hero-cta' href='" + e(best["link"]) +
                 "' target='_blank' rel='noopener'>" + cta + "</a>")
    hero += "</div>"

    lowest_txt = ("$" + e(best["price"])) if best else "-"
    split_block = ""
    if components:
        split_block = (
            "<p class='sec'>Cheapest hub-split breakdown &middot; book these two tickets separately</p>"
            "<div class='table-card'><table>"
            "<thead><tr><th>Price</th><th>Leg</th><th>Cabin</th><th>Routing</th><th></th></tr></thead>"
            "<tbody>" + component_rows(components) + "</tbody></table></div>")

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
 h1{font-size:26px;font-weight:800;letter-spacing:-.02em;margin:0}
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
 .hero-sub b{color:var(--ink)}
 .hero-cta{display:inline-block;background:var(--good-d);color:#fff;font-weight:600;font-size:15px;
   padding:11px 20px;border-radius:12px;text-decoration:none}
 .grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:0 0 26px}
 @media(max-width:640px){.grid{grid-template-columns:repeat(2,1fr)}.hero-price{font-size:42px}}
 .stat{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px 18px}
 .stat .k{color:var(--mut);font-size:12.5px;font-weight:500}
 .stat .v{font-size:22px;font-weight:700;margin-top:5px}
 .stat .v.sm{font-size:14px;font-weight:600;line-height:1.35}
 .sec{font-size:14px;font-weight:600;color:var(--mut);margin:26px 0 10px}
 .table-card{background:var(--card);border:1px solid var(--line);border-radius:16px;overflow:hidden;margin-bottom:6px}
 table{width:100%;border-collapse:collapse}
 th,td{text-align:left;padding:13px 16px;font-size:14px;border-bottom:1px solid var(--line);vertical-align:top}
 th{color:var(--mut);font-weight:600;font-size:12px;letter-spacing:.05em;text-transform:uppercase;background:rgba(255,255,255,.02)}
 tr:last-child td{border-bottom:none}
 tbody tr:hover{background:rgba(108,155,255,.06)}
 td.price{font-weight:800;font-size:16px;white-space:nowrap}
 td.air{font-weight:500} td.route{color:var(--mut)}
 td.route .sub{color:var(--mut);font-size:12px;margin-top:3px;opacity:.85}
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
 <p class="route-line"><b>""" + e(ORIGIN) + """</b> -> <b>""" + e(DEST) + """</b> &middot; depart <b>""" + e(OUTBOUND_DATE) + """</b> &middot; return <b>""" + e(RETURN_DATE) + """</b> &middot; 1 adult &middot; business long-haul &middot; alert under <b>$""" + e(int(THRESHOLD_USD)) + """</b><br>Carriers: <b>United, Chinese, Japanese &amp; Korean airlines</b> only</p>
 """ + hero + """
 <div class="grid">
   <div class="stat"><div class="k">Lowest complete journey</div><div class="v">""" + lowest_txt + """</div></div>
   <div class="stat"><div class="k">Alert threshold</div><div class="v">$""" + e(int(THRESHOLD_USD)) + """</div></div>
   <div class="stat"><div class="k">Journeys compared</div><div class="v">""" + e(len(journeys)) + """</div></div>
   <div class="stat"><div class="k">Source</div><div class="v sm">Google Flights (SerpApi)</div></div>
 </div>
 <p class="sec">Complete journeys &middot; cheapest first (direct vs. Seoul/Tokyo split)</p>
 <div class="table-card"><table>
  <thead><tr><th>Total</th><th>Long-haul airline(s)</th><th>Type</th><th>Routing</th><th></th></tr></thead>
  <tbody>""" + journey_rows(journeys) + """</tbody>
 </table></div>
 """ + split_block + """
 <footer>Auto-refreshes hourly via GitHub Actions; this page also reloads itself every 15 min.<br>
   Hub-split = a Seoul/Tokyo &lt;-&gt; New York business round trip plus a separate economy PVG &lt;-&gt; hub connector, booked as two tickets.
   Prices are <b>indicative</b> - confirm each ticket on the airline/OTA site before booking.</footer>
</div></body></html>""")


def email_body(best, now):
    return (
        "<html><body style=\"font-family:Arial,sans-serif\">"
        "<h2>Business-class deal: Shanghai -> New York under $" + e(int(THRESHOLD_USD)) + "</h2>"
        "<p><b>Lowest complete journey: $" + e(best["price"]) + "</b> (" + e(best["kind"]) + ") - "
        + e(best["route"]) + ".</p>"
        "<p>Depart <b>" + e(OUTBOUND_DATE) + "</b>, return <b>" + e(RETURN_DATE) + "</b>.</p>"
        "<p><a href=\"" + e(best["link"]) + "\">View / book</a></p>"
        "<p style=\"color:#666;font-size:12px\">Found " + e(now) + ". Prices indicative. Automated watcher.</p>"
        "</body></html>")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    if not SERPAPI_KEY:
        log("FATAL: SERPAPI_KEY is required.")
        sys.exit(1)

    now = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    journeys, components, best_hub = build()
    best = journeys[0] if journeys else None
    deal = bool(best and best["price"] <= THRESHOLD_USD)

    state = load_state()
    if deal and EMAIL_ENABLED and should_alert(best, state):
        if send_email("PVG->NYC business $" + str(best["price"]) +
                      " (under $" + str(int(THRESHOLD_USD)) + ")", email_body(best, now)):
            state["last_alert_price"] = best["price"]
            state["last_alert_time"] = dt.datetime.utcnow().isoformat()
            save_state(state)
    elif not deal and state.get("last_alert_price") is not None:
        state["last_alert_price"] = None
        save_state(state)

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(render_page(journeys, components, best, deal, now))
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump({"updated": now, "threshold": THRESHOLD_USD, "deal": deal,
                   "best_hub": best_hub, "lowest": best,
                   "journeys": journeys, "components": components}, f, indent=2)

    log("Done. " + str(len(journeys)) + " journeys; lowest=" +
        (("$" + str(best["price"])) if best else "none") + "; deal=" + str(deal))


if __name__ == "__main__":
    main()
