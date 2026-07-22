"""GDELT world-news layer — the engine's global eyes.

GDELT DOC 2.0 API: free, no key, ~15-min freshness, 100+ languages machine-
translated — local/foreign press hours before English financial media.

Flow (one poll tick):
  1. fetch: 2-3 query categories (rotating through the 11-category taxonomy —
     full coverage roughly every hour), dedupe by URL, domain junk filter
  2. ONE batched haiku call per ~15 headlines does relevance + enrichment:
     "could this affect any industry's supply/demand?" with the
     ACTION-OVER-TALK gradient (enacted > proposed > punditry), event type,
     magnitude, affected themes, and EXPOSURE HYPOTHESES (≤3 tradable names)
  3. graph: (:Event)-[:AFFECTS]->(:Theme) + quarantined hypothesis edges
     (:Company)-[:EXPOSED_TO {hypothesis: true}]->(:Theme)
  4. candidates: exposure hypotheses on tradable symbols → the SAME desk.
     The candidate text says loudly that the exposure is a HYPOTHESIS —
     the Critic's first job is to attack the chain.

Judgment doctrine: the query taxonomy is a user-set attention policy (like
the universe rule); every per-article call is the agent's. Code only
dedupes, filters junk domains, and validates tickers against the universe.
"""

import hashlib
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
import json as _json
from datetime import datetime, timezone

from alphadesk.config import in_universe
from alphadesk.llm import LLMError, call_role, wrap_data

log = logging.getLogger("alphadesk.world")

_DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"
_BATCH = 15
_seen_urls: set[str] = set()

# Domains that are overwhelmingly noise/aggregator spam (factual junk filter)
_DOMAIN_BLOCKLIST = {
    "prnewswire.com", "globenewswire.com", "openpr.com", "einnews.com",
    "menafn.com", "streetinsider.com",
}

# ---------------------------------------------------------------------------
# The 11-category query taxonomy (user-set attention policy).
# Every category carries its positive/boom-side terms — news negativity bias
# means positive shocks are under-covered and likely slower-priced.
# ---------------------------------------------------------------------------

QUERY_TAXONOMY: dict[str, str] = {
    "CONFLICT": '("military strike" OR ceasefire OR "peace deal" OR "war escalation" OR mobilization OR "border closure" OR blockade)',
    "ENERGY": '(OPEC OR "oil production" OR "refinery outage" OR "pipeline" OR "LNG deal" OR "power outage" OR "gas prices" OR "oil discovery")',
    "COMMODITIES": '("mine strike" OR "export ban" OR lithium OR "rare earth" OR copper OR "crop failure" OR "record harvest" OR fertilizer OR nickel)',
    "TRADE_POLICY": '(tariff OR "export controls" OR sanctions OR "trade deal" OR "trade agreement" OR subsidies OR "price cap" OR antitrust)',
    "SUPPLY_CHAIN": '("supply chain" OR "port strike" OR "port congestion" OR "factory fire" OR "plant shutdown" OR "plant opening" OR "new factory" OR shortage OR reshoring)',
    "LABOR": '("workers strike" OR "labor strike" OR "union deal" OR "mass layoffs" OR "labor shortage" OR "strike ends")',
    "DISASTER": '(earthquake OR typhoon OR hurricane OR flood OR wildfire OR drought OR outbreak OR heatwave)',
    "TECH_SCIENCE": '("breakthrough" OR "drug approval" OR "regulatory approval" OR cyberattack OR "data breach" OR "chip production" OR semiconductor)',
    "DEMAND": '("record sales" OR "record demand" OR "booking surge" OR "tourism recovery" OR "incentive program" OR "infrastructure program" OR restocking)',
    "WORLD_MACRO": '("central bank" OR "interest rate decision" OR "currency crisis" OR devaluation OR "capital controls" OR "sovereign default")',
    "GOVERNMENT": '("bill passed" OR "law signed" OR "executive order" OR "court ruling" OR "regulation approved" OR deregulation OR "contract awarded" OR "election result" OR "policy change")',
}

_rotation = list(QUERY_TAXONOMY.items())
_rotation_idx = 0

_WORLD_SYSTEM = (
    "You are the world-events desk of a predictive stock research firm. The "
    "firm predicts which US-listed stocks will outperform over the next 1-10 "
    "trading days by understanding GLOBAL supply, demand, trade, and policy "
    "BEFORE financial media digests the implications.\n\n"
    "For each numbered headline (from world press, many machine-translated):\n"
    "relevant: could this event plausibly affect any industry's supply, "
    "demand, costs, or pricing power? Apply the ACTION-OVER-TALK gradient: "
    "things that HAPPENED or were ENACTED/signed/ruled/awarded rank highest; "
    "formal proposals lower; campaign rhetoric, punditry, and speculation are "
    "NOT relevant. Also not relevant: sports, celebrity, local crime, stock-"
    "price commentary.\n"
    "For relevant items add: event_type, magnitude (MINOR local → MAJOR "
    "global), themes (affected industries/commodities/regions, short phrases), "
    "and exposures: up to 3 US-listed stocks plausibly affected, each with "
    "direction and a one-line causal chain. Exposures are HYPOTHESES for the "
    "research team to verify — propose only chains you can articulate, "
    "and prefer LESS-obvious second-order names over megacaps everyone "
    "watches. If you cannot name a defensible exposure, give none.\n\n"
    'Return ONLY JSON: {"items": [{"i": <n>, "relevant": <bool>, '
    '"reason": "<one line>", "event_type": "CONFLICT|ENERGY|COMMODITIES|'
    'TRADE_POLICY|SUPPLY_CHAIN|LABOR|DISASTER|TECH_SCIENCE|DEMAND|WORLD_MACRO|'
    'GOVERNMENT", "magnitude": "MINOR|NOTABLE|MAJOR", "themes": ["..."], '
    '"exposures": [{"symbol": "<US ticker>", "direction": "LONG|SHORT", '
    '"chain": "<event → mechanism → company, one line>"}]}]}'
)

_WORLD_SCHEMA = {
    "items": {
        "type": list, "maxitems": _BATCH,
        "items": {
            "i": {"type": int, "min": 1, "max": _BATCH},
            "relevant": {"type": bool},
            "reason": {"type": str, "maxlen": 200},
            # not enum-validated: an off-taxonomy value (e.g. "COMPANY") falls back
            # to the query category in assess() — no costly re-ask over a label.
            "event_type": {"type": str, "optional": True, "maxlen": 40},
            "magnitude": {"type": str, "optional": True, "enum": ["MINOR", "NOTABLE", "MAJOR"]},
            "themes": {"type": list, "optional": True, "maxitems": 4},  # list of strings
            "exposures": {
                "type": list, "optional": True, "maxitems": 3,
                "items": {
                    "symbol": {"type": str, "maxlen": 10},
                    "direction": {"type": str, "enum": ["LONG", "SHORT"]},
                    "chain": {"type": str, "maxlen": 250},
                },
            },
        },
    }
}


# ---------------------------------------------------------------------------
# GDELT fetch
# ---------------------------------------------------------------------------

def fetch_category(category: str, query: str, timespan: str = "1h",
                   max_records: int = 50) -> list[dict]:
    """One polite DOC-API request for one taxonomy category."""
    params = urllib.parse.urlencode({
        "query": query, "mode": "artlist", "maxrecords": max_records,
        "timespan": timespan, "format": "json", "sort": "datedesc",
    })
    req = urllib.request.Request(
        f"{_DOC_API}?{params}", headers={"User-Agent": "alphadesk-research/0.1"}
    )
    # GDELT's DOC API throttles aggressively (HTTP 429). Without a retry a transient
    # throttle silently zeroes out geopolitical ingestion (returns []), so back off
    # and retry a couple of times on 429 before giving up. Any other error fails fast.
    payload = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = _json.loads(resp.read().decode("utf-8", errors="replace"))
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 3:
                wait = 5.0 * attempt   # 5s, 10s
                log.warning("GDELT 429 (%s) — backoff %.0fs (attempt %d/3)", category, wait, attempt)
                time.sleep(wait)
                continue
            log.warning("GDELT fetch failed (%s): %s", category, exc)
            return []
        except Exception as exc:
            log.warning("GDELT fetch failed (%s): %s", category, exc)
            return []
    if payload is None:
        return []

    out = []
    for art in payload.get("articles", []):
        url = art.get("url", "")
        domain = art.get("domain", "")
        if not url or url in _seen_urls or domain in _DOMAIN_BLOCKLIST:
            continue
        _seen_urls.add(url)
        seen = art.get("seendate", "")
        try:
            published = datetime.strptime(seen, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc).isoformat()
        except ValueError:
            published = datetime.now(timezone.utc).isoformat()
        out.append({
            "category": category,
            "title": (art.get("title") or "").strip()[:250],
            "url": url,
            "domain": domain,
            "sourcecountry": art.get("sourcecountry", ""),
            "language": art.get("language", ""),
            "published_at": published,
        })
    return out


# ---------------------------------------------------------------------------
# Relevance + enrichment (one batched agent call)
# ---------------------------------------------------------------------------

def assess(headlines: list[dict]) -> list[dict]:
    """Attach the world-desk agent's judgment to each headline."""
    assessed: list[dict] = []
    for start in range(0, len(headlines), _BATCH):
        batch = headlines[start:start + _BATCH]
        numbered = "\n".join(
            f"{i + 1}. [{h['category']}|{h['sourcecountry'] or '?'}|{h['domain']}] {h['title']}"
            for i, h in enumerate(batch)
        )
        try:
            out = call_role(
                "enrichment", _WORLD_SYSTEM,
                "World headlines:\n" + wrap_data("headlines", numbered),
                schema=_WORLD_SCHEMA, source="WORLD",
            )
            results = {item["i"]: item for item in out.get("items", [])}
        except LLMError as exc:
            log.warning("World assessment batch failed (%s) — %d headlines skipped",
                        exc, len(batch))
            results = {}
        for i, h in enumerate(batch):
            item = results.get(i + 1)
            if item and item.get("relevant"):
                themes = [
                    (t.get("name", "") if isinstance(t, dict) else str(t))[:60]
                    for t in (item.get("themes") or [])
                ]
                themes = [t for t in themes if t]
                assessed.append({
                    **h,
                    "reason": item.get("reason", ""),
                    "event_type": item.get("event_type", h["category"]),
                    "magnitude": item.get("magnitude", "MINOR"),
                    "themes": themes,
                    "exposures": item.get("exposures") or [],
                })
    return assessed


# ---------------------------------------------------------------------------
# One poll tick
# ---------------------------------------------------------------------------

def poll(categories_per_tick: int = 3) -> tuple[int, dict[str, list[dict]]]:
    """Rotate through the taxonomy; returns (events_ingested, candidates).

    Candidates carry the exposure HYPOTHESIS in the article text so the
    team knows exactly what chain it must verify.
    """
    global _rotation_idx
    headlines: list[dict] = []
    n_cats = min(categories_per_tick, len(_rotation))  # >taxonomy would just re-fetch
    for k in range(n_cats):
        category, query = _rotation[_rotation_idx % len(_rotation)]
        _rotation_idx += 1
        headlines.extend(fetch_category(category, query))
        if k < n_cats - 1:
            time.sleep(5.0)  # polite spacing between GDELT calls (429s under faster polling)

    if not headlines:
        return 0, {}

    events = assess(headlines)

    candidates: dict[str, list[dict]] = {}
    for ev in events:
        for exp in ev["exposures"]:
            sym = (exp.get("symbol") or "").upper()
            if not in_universe(sym):
                continue  # analysis universe is open; PICKS must be tradable
            sentiment = 0.5 if exp["direction"] == "LONG" else -0.5
            candidates.setdefault(sym, []).append({
                "id": f"world-{hashlib.sha1(ev['url'].encode()).hexdigest()[:16]}-{sym}",
                "title": f"[WORLD:{ev['event_type']}/{ev['magnitude']}] {ev['title']}",
                "summary": (
                    f"HYPOTHESIS (verify the chain): {exp['chain']} | "
                    f"world-desk reason: {ev['reason']} | themes: {', '.join(ev['themes'])} | "
                    f"source: {ev['domain']} ({ev['sourcecountry']}, {ev['language']})"
                ),
                "source": ev["domain"],
                "url": ev["url"],
                "published_at": ev["published_at"],
                "category": "WORLD",
                "tickers": [sym],
                "mentions": [{"symbol": sym, "sentiment": sentiment,
                              "label": "positive" if sentiment > 0 else "negative",
                              "category": "WORLD"}],
                "relations": [],
            })

    log.info(
        "World tick: %d headlines → %d relevant events → %d exposure candidates",
        len(headlines), len(events), len(candidates),
    )
    return len(events), candidates
