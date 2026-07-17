"""News ingestion — Polygon REST poll → enrichment → graph + candidates.

The enrichment call (haiku, batched) does three jobs in one pass per article:
sentiment, label, and typed relation extraction from the text itself
(relations carry the article URL as evidence).

Universe note: the ANALYSIS universe is open — every tagged ticker goes into
the graph, foreign/private names included. Only CANDIDATES (things that may
become decisions) are filtered to the tradable pick universe.
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from alphadesk.config import in_universe
from alphadesk.ledger import store
from alphadesk.llm import LLMError, call_role, wrap_data

log = logging.getLogger("alphadesk.news")

_POLYGON_KEY = os.environ.get("POLYGON_API_KEY", "")
_BATCH = 30               # articles per enrichment call (bigger batch = fewer calls = less overhead)
_MAX_SCAN = 400           # cap raw articles paged through (free-tier rate-limit guard)
_seen_ids: set[str] = set()

_ENRICH_SYSTEM = (
    "You are a financial news enrichment engine. For each numbered article you "
    "receive, produce a substance category, sentiment, and any explicit "
    "inter-company relations stated in the text.\n"
    "category — what KIND of information this is:\n"
    "  BUSINESS_EVENT: something happened at the company — earnings/guidance, "
    "M&A, contracts, products, leadership, legal/regulatory action against it\n"
    "  SUPPLY_DEMAND: supply-chain, production, capacity, shortages, pricing "
    "power, demand signals, orders, inventory\n"
    "  MACRO_POLICY: rates, regulation, tariffs, geopolitics affecting sectors\n"
    "  PRICE_COMMENTARY: the article mainly narrates stock-price action "
    "('X soared/plunged/hit a high', 'why X stock moved', weekly recaps)\n"
    "  OPINION: listicles, 'top N stocks to buy', 'should you buy X', "
    "evergreen takes with no new information\n"
    "sentiment: -1.0 (very negative for the mentioned companies) to 1.0 (very "
    "positive). label: negative|neutral|positive.\n"
    "relations: ONLY relations explicitly stated or strongly implied by the "
    "article text itself (e.g. 'X supplies chips to Y', 'X competes with Y', "
    "'X partners with Y'). Use stock tickers. Do NOT add relations from your "
    "own knowledge — text evidence only. Most articles have none.\n"
    'Return ONLY JSON: {"items": [{"i": <number>, '
    '"category": "BUSINESS_EVENT|SUPPLY_DEMAND|MACRO_POLICY|PRICE_COMMENTARY|OPINION", '
    '"sentiment": <-1..1>, "label": "negative|neutral|positive", '
    '"relations": [{"a": "<TICKER>", "rel": "SUPPLIES|COMPETES|PARTNERS", "b": "<TICKER>"}]}]}'
)

# Substance policy (user-set): only real-world information can spawn candidates.
# Price narration and opinion pieces still enter the graph — labeled — but the
# desk never convenes over them.
_SUBSTANTIVE = {"BUSINESS_EVENT", "SUPPLY_DEMAND", "MACRO_POLICY"}

_ENRICH_SCHEMA = {
    "items": {
        "type": list,
        "maxitems": _BATCH,
        "items": {
            "i": {"type": int, "min": 1, "max": _BATCH},
            "category": {"type": str, "enum": [
                "BUSINESS_EVENT", "SUPPLY_DEMAND", "MACRO_POLICY",
                "PRICE_COMMENTARY", "OPINION",
            ]},
            "sentiment": {"type": (int, float), "min": -1, "max": 1},
            "label": {"type": str, "enum": ["negative", "neutral", "positive"]},
            "relations": {
                "type": list, "optional": True, "maxitems": 5,
                "items": {
                    "a": {"type": str, "maxlen": 10},
                    "rel": {"type": str, "enum": ["SUPPLIES", "COMPETES", "PARTNERS"]},
                    "b": {"type": str, "maxlen": 10},
                },
            },
        },
    }
}


def fetch_articles(since: datetime, limit: int = 200) -> list[dict]:
    """Raw Polygon articles (ticker-tagged) since `since`, oldest first.

    Bounded: stops after `limit` usable articles OR `_MAX_SCAN` raw items paged
    through — whichever comes first. `list_ticker_news` paginates with no ticker
    filter, and the free tier rate-limits deep paging hard (429 backoffs stack
    into multi-minute stalls), so we cap how far we page rather than hang.
    """
    import polygon
    client = polygon.RESTClient(api_key=_POLYGON_KEY)
    out: list[dict] = []
    scanned = 0
    for art in client.list_ticker_news(
        published_utc_gte=since.strftime("%Y-%m-%dT%H:%M:%SZ"),
        limit=min(limit, 1000), sort="published_utc", order="asc",
    ):
        scanned += 1
        if scanned > _MAX_SCAN:
            log.warning("Polygon scan cap (%d) hit — %d usable articles collected", _MAX_SCAN, len(out))
            break
        art_id = str(getattr(art, "id", "") or getattr(art, "article_url", ""))
        if not art_id or art_id in _seen_ids:
            continue
        _seen_ids.add(art_id)
        tickers = [t for t in (getattr(art, "tickers", None) or []) if t]
        title = getattr(art, "title", "") or ""
        if not tickers or not title:
            continue
        publisher = getattr(art, "publisher", None)
        out.append({
            "id": art_id,
            "title": title,
            "summary": (getattr(art, "description", "") or "")[:400],
            "source": publisher.name if publisher and hasattr(publisher, "name") else "Polygon",
            "url": getattr(art, "article_url", "") or "",
            "published_at": str(getattr(art, "published_utc", "") or datetime.now(timezone.utc).isoformat()),
            "tickers": tickers[:8],
        })
        if len(out) >= limit:
            break
    return out


def enrich(articles: list[dict]) -> list[dict]:
    """Attach sentiment/label/relations to each article, reusing the persistent
    cache so overlapping news is never re-enriched across runs/restarts (the
    biggest recurring token cost). Only uncached articles hit haiku.

    On LLM failure a batch falls back to neutral/UNCLASSIFIED (stays candidate-
    eligible so an outage never silently drops real news) — and is NOT cached, so
    it gets a real enrichment on a later run.
    """
    cached = store.get_enrichment([a["id"] for a in articles])
    to_enrich = [a for a in articles if a["id"] not in cached]
    fresh: dict[str, dict] = {}        # article_id → enrichment (all uncached)
    cacheable: list[dict] = []         # only genuine results (not fallbacks)

    for start in range(0, len(to_enrich), _BATCH):
        batch = to_enrich[start:start + _BATCH]
        numbered = "\n".join(
            f"{i + 1}. [{', '.join(a['tickers'])}] {a['title']}"
            + (f" — {a['summary'][:200]}" if a["summary"] else "")
            for i, a in enumerate(batch)
        )
        results: dict[int, dict] = {}
        try:
            out = call_role(
                "enrichment", _ENRICH_SYSTEM,
                "Articles:\n" + wrap_data("articles", numbered),
                schema=_ENRICH_SCHEMA,
            )
            results = {item["i"]: item for item in out.get("items", [])}
        except LLMError as exc:
            log.warning("Enrichment batch failed (%s) — neutral fallback ×%d", exc, len(batch))

        for i, art in enumerate(batch):
            item = results.get(i + 1)
            rec = {
                "sentiment": float((item or {}).get("sentiment", 0.0)),
                "label": (item or {}).get("label", "neutral"),
                "category": (item or {}).get("category", "UNCLASSIFIED"),
                "relations": [{"a": r["a"], "rel": r["rel"], "b": r["b"]}
                              for r in ((item or {}).get("relations") or [])],
            }
            fresh[art["id"]] = rec
            if item is not None:  # genuine result → safe to cache forever
                cacheable.append({"article_id": art["id"], **rec})

    store.save_enrichment(cacheable)

    enriched: list[dict] = []
    for art in articles:
        e = cached.get(art["id"]) or fresh[art["id"]]
        rels = e["relations"]
        if isinstance(rels, str):        # from the DB it's a JSON string
            rels = json.loads(rels or "[]")
        enriched.append({
            **art,
            "category": e["category"],
            "mentions": [
                {"symbol": t, "sentiment": e["sentiment"], "label": e["label"],
                 "category": e["category"]}
                for t in art["tickers"]
            ],
            "relations": [{"a": r["a"], "rel": r["rel"], "b": r["b"],
                           "evidence_url": art["url"]} for r in rels],
        })
    return enriched


def poll(since: datetime) -> tuple[int, dict[str, list[dict]]]:
    """One ingestion pass: fetch → enrich → graph. Returns
    (articles_ingested, candidates) where candidates maps pick-universe
    symbols → their fresh enriched articles."""
    try:
        raw = fetch_articles(since)
    except Exception as exc:
        log.warning("Polygon fetch failed: %s", exc)
        return 0, {}
    if not raw:
        return 0, {}

    enriched = enrich(raw)

    candidates: dict[str, list[dict]] = {}
    dropped = 0
    for art in enriched:
        # substance policy: price narration and opinion never spawn candidates
        # (they're in the graph, labeled — the desk just doesn't convene on them)
        if art.get("category") not in _SUBSTANTIVE and art.get("category") != "UNCLASSIFIED":
            dropped += 1
            continue
        for t in art["tickers"]:
            if in_universe(t):
                candidates.setdefault(t.upper(), []).append(art)
    log.info(
        "Ingested %d articles → %d candidate symbols (%d non-substantive filed to graph only)",
        len(enriched), len(candidates), dropped,
    )
    return len(enriched), candidates


def catch_up(hours: float) -> int:
    """Startup healer: backfill the gap since last run (same path as live)."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    total = 0
    for _ in range(10):  # page politely; free tier is rate-limited
        n, _cands = poll(since)
        total += n
        if n < 190:
            break
        time.sleep(13)
    return total
