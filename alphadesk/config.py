"""AlphaDesk configuration — facts only: model map, caps, sessions, universe.

Design law: code owns facts and safety rails; agents own judgment. Nothing in
this module makes a judgment call — the universe is a factual screen (tradable
at the broker), sessions are clock math, caps are resource physics.
"""

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("alphadesk.config")

ET = ZoneInfo("America/New_York")

DATA_DIR = Path(os.environ.get("ALPHADESK_DATA", "~/.alphadesk")).expanduser()
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Model map — role → model alias. Every role overridable: MODEL_<ROLE>=...
# Ladder tiers (for rate-limit downgrades) ordered strongest → cheapest.
# ---------------------------------------------------------------------------

TIERS = ["opus", "sonnet", "haiku"]

MODEL_MAP: dict[str, str] = {
    "enrichment": "haiku",     # sentiment + relation extraction, high volume
    "news_check": "haiku",    # same-story vs new-catalyst check on a recently-debated name
    "gate": "haiku",           # pre-debate catalyst screen — drop phantom setups before the debate
    "brief": "haiku",          # specialist subagents (technical/news/graph)
    "scout": "sonnet",        # attention desk
    "researcher": "sonnet",       # thesis + rebuttal
    "critic": "opus",         # adversarial challenge
    "judge": "opus",         # final verdict
    "loner": "opus",            # single-agent control arm
    "review": "opus",          # position review — HOLD/EXIT on still-open TAKEs
    "head": "opus",            # comparative head-to-head selection across debated ideas
    "earnings_reader": "sonnet",      # web-grounded read of an actual earnings report
    "connections": "opus",     # one web-grounded call: supplier/customer/competitor map → spillover candidates
    "plan": "sonnet",          # execution desk — entry/target/stop for a committed call
}

# Connections desk fires only on the top-N most material shocks per run (cost gate)
EXPOSURE_MAX_SHOCKS = int(os.environ.get("EXPOSURE_MAX_SHOCKS", "2"))

# World-news breadth per Find Trades run. Default 0 = OFF (world news was never
# part of the button flow historically, and GDELT 429s + its enrichment dominated
# run time). Set >0 to enable: the cursor rotates, so N per run covers the full
# 11-category taxonomy over ~11/N runs (e.g. 4 = full sweep every ~3 runs; 11 =
# every run, slowest). Capped at the taxonomy size.
WORLD_MAX_CATEGORIES = int(os.environ.get("WORLD_MAX_CATEGORIES", "0"))

for _role in list(MODEL_MAP):
    _override = os.environ.get(f"MODEL_{_role.upper()}")
    if _override:
        MODEL_MAP[_role] = _override

# ---------------------------------------------------------------------------
# Hard caps — resource physics, not judgment
# ---------------------------------------------------------------------------

MAX_PICKS_PER_WINDOW = 5
MAX_DEBATES_PER_DAY = 40
# env-overridable for small hosts (e.g. 1GB GCP e2-micro → set 1: debates queue)
MAX_CONCURRENT_WORKFLOWS = int(os.environ.get("MAX_CONCURRENT_WORKFLOWS", "4"))
SYMBOL_REPICK_COOLDOWN_MIN = 15
# Solo control arm: every Nth pick also goes to one solo agent (measures whether
# the team beats one agent). 0 = OFF (default — pure overhead, no trade output);
# set e.g. SOLO_ARM_EVERY_N=6 to resume accumulating that comparison.
SOLO_ARM_EVERY_N = int(os.environ.get("SOLO_ARM_EVERY_N", "0"))
TRIAGE_WINDOW_S = 120
NEWS_POLL_INTERVAL_S = 300
LLM_TIMEOUT_S = 120
LLM_TOOL_TIMEOUT_S = 300  # longer cap for tool-using calls (web-search round-trips)
LLM_MAX_CONCURRENCY = int(os.environ.get("LLM_MAX_CONCURRENCY", "4"))  # caps concurrent CLI spawns (memory)
LLM_MAX_INPUT_CHARS = int(os.environ.get("LLM_MAX_INPUT_CHARS", "48000"))  # ~12k tok; DoS/cost cap
LLM_TOOL_BUDGET_USD = float(os.environ.get("LLM_TOOL_BUDGET_USD", "0.50"))  # hard cap per web-agent call
MAX_RUNS_PER_DAY = int(os.environ.get("MAX_RUNS_PER_DAY", "50"))  # Find Trades runaway guard
FRICTION_BPS_PER_SIDE = 15      # grading haircut; doubled for LOW_LIQUIDITY
LOW_LIQUIDITY_DOLLAR_VOL = 10_000_000  # avg daily dollar volume below this → tag
# Anti-survivorship: grade scout SKIPS too. A skip has no direction, so a "miss"
# is a large move in EITHER direction vs SPY within a short window we ignored.
SKIP_GRADE_DAYS = 3             # trading days to judge a skipped name's forward move
SKIP_MISS_ABS_ALPHA = 6.0       # |symbol return − SPY| above this % = a missed dislocation
EARNINGS_DRIFT_DAYS = 3         # a name reported within this many days → post-earnings-drift candidate
REPICK_COOLDOWN_HOURS = int(os.environ.get("REPICK_COOLDOWN_HOURS", "24"))  # don't re-debate a name within this window; matches the 24h news window so a catalyst is debated once (anti-double-dip across runs)

# Exit-monitoring escalation SCREENS (not decisions — the opus reviewer decides).
# Cheap, deliberately generous code triggers that flag an open position for a
# thesis re-review between Find Trades runs, so a spent move (like a beat that has
# exceeded its implied move) gets closed before the gain decays. Tunable/removable;
# the reviewer is the real filter, so err toward escalating.
EXIT_NEAR_TARGET_FRAC = float(os.environ.get("EXIT_NEAR_TARGET_FRAC", "0.85"))  # ≥ this much of the entry→target move captured → ask "take it now?"
EXIT_GIVEBACK_MIN_PEAK = float(os.environ.get("EXIT_GIVEBACK_MIN_PEAK", "4.0"))  # only watch give-back once the favorable move peaked above this % (below this is intraday noise, not a spent thesis)
EXIT_GIVEBACK_FRAC = float(os.environ.get("EXIT_GIVEBACK_FRAC", "0.40"))         # faded ≥ this fraction of that peak → the MFE-decay flag
EXIT_REVIEW_COOLDOWN_S = int(os.environ.get("EXIT_REVIEW_COOLDOWN_S", "1800"))   # don't re-review the same open position more often than this

# ---------------------------------------------------------------------------
# Market sessions (ET clock math)
# ---------------------------------------------------------------------------


def now_et() -> datetime:
    return datetime.now(ET)


def session(dt: datetime | None = None) -> str:
    """Return PRE | OPEN | AFTER | CLOSED for a given ET moment."""
    dt = (dt or now_et()).astimezone(ET)
    if dt.weekday() >= 5:
        return "CLOSED"
    minutes = dt.hour * 60 + dt.minute
    if 4 * 60 <= minutes < 9 * 60 + 30:
        return "PRE"
    if 9 * 60 + 30 <= minutes < 16 * 60:
        return "OPEN"
    if 16 * 60 <= minutes < 20 * 60:
        return "AFTER"
    return "CLOSED"


# ---------------------------------------------------------------------------
# Pick universe — ALL Alpaca-tradable active US equities. A factual screen,
# auto-refreshed weekly; zero curation, zero liquidity judgment (liquidity is
# evidence downstream, not a filter here).
# ---------------------------------------------------------------------------

_UNIVERSE_CACHE = DATA_DIR / "universe.json"
_UNIVERSE_MAX_AGE_S = 7 * 24 * 3600
_universe: set[str] | None = None


def _fetch_universe_from_alpaca() -> list[str]:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import AssetClass, AssetStatus
    from alpaca.trading.requests import GetAssetsRequest

    client = TradingClient(
        os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"], paper=True
    )
    assets = client.get_all_assets(
        GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)
    )
    return sorted({
        a.symbol for a in assets
        if not isinstance(a, str) and getattr(a, "tradable", False)
    })


def load_universe(refresh: bool = False) -> set[str]:
    """Cached weekly; falls back to a stale cache if the broker is unreachable."""
    global _universe
    if _universe is not None and not refresh:
        return _universe

    cache_ok = _UNIVERSE_CACHE.exists() and (
        time.time() - _UNIVERSE_CACHE.stat().st_mtime < _UNIVERSE_MAX_AGE_S
    )
    if cache_ok and not refresh:
        _universe = set(json.loads(_UNIVERSE_CACHE.read_text()))
        return _universe

    try:
        symbols = _fetch_universe_from_alpaca()
        _UNIVERSE_CACHE.write_text(json.dumps(symbols))
        _universe = set(symbols)
        log.info("Universe refreshed from Alpaca: %d tradable symbols", len(symbols))
    except Exception as exc:
        if _UNIVERSE_CACHE.exists():
            _universe = set(json.loads(_UNIVERSE_CACHE.read_text()))
            log.warning("Universe refresh failed (%s) — using stale cache (%d)", exc, len(_universe))
        else:
            raise RuntimeError(f"No universe available: {exc}") from exc
    return _universe


def in_universe(symbol: str) -> bool:
    return symbol.upper() in load_universe()
