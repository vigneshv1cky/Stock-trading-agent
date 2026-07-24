"""AlphaDesk configuration — facts only: model map, caps, sessions, universe.

Design law: code owns facts and safety rails; agents own judgment. Nothing in
this module makes a judgment call — the universe is a factual screen (tradable
at the broker), sessions are clock math, caps are resource physics.
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta
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

# CHEAP-models mode (default ON) — for cheap, frequent (hourly) automation: downgrade the
# opus judgment roles to sonnet, so a full run has NO opus calls and costs a fraction. It's a
# quality/direction BET on an unproven system: sonnet judgment vs opus, and researcher+critic
# land on the same tier (some error-decorrelation lost, since the opus critic used to differ
# from the sonnet researcher on purpose). Every pick is model-tagged, so compare the cheap vs
# opus cohorts in the ledger. Keep any single role sharp with a per-role override, e.g.
# MODEL_JUDGE=opus. Set CHEAP_MODELS=0 to restore the opus defaults.
if os.environ.get("CHEAP_MODELS", "1") not in ("0", "", "false", "False", "no"):
    for _r in ("critic", "judge", "loner", "review", "head", "connections"):
        MODEL_MAP[_r] = "sonnet"

for _role in list(MODEL_MAP):   # per-role override wins over the cheap default
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
# Honest-alpha prototype (computed ALONGSIDE alpha_net, never replacing it): a SHORT
# pays borrow, which SPY-relative alpha_net ignored. Annualized % borrow rate, charged
# over the holding period — tiered by liquidity (low_liquidity is the hard-to-borrow
# proxy until a real borrow-rate feed exists). LONGs pay nothing here.
SHORT_BORROW_APR = float(os.environ.get("SHORT_BORROW_APR", "2.0"))            # easy-to-borrow baseline
SHORT_BORROW_APR_ILLIQUID = float(os.environ.get("SHORT_BORROW_APR_ILLIQUID", "30.0"))  # hard-to-borrow proxy
# Concentration cap: at most this many TAKEN picks per correlation cluster (sector+direction)
# per day. Stops the desk booking 5 same-sector same-direction names on one driver — which is
# 5x the intended risk AND makes the ledger count one bet as many independent wins.
CONCENTRATION_MAX_PER_CLUSTER = int(os.environ.get("CONCENTRATION_MAX_PER_CLUSTER", "2"))
# Pre-committed horizon: the grading horizon is FIXED per edge, decided in advance — NOT
# chosen by the judge after seeing the setup. Removes the garden-of-forking-paths (the same
# catalyst bookable as a 1d or 10d call grades differently and only the chosen spec is logged),
# so alpha_net is an honest out-of-sample number. Env-overridable per edge.
# SHORT-HORIZON daily-run mode (2026-07-24): the desk runs every day, so the forward CALL is
# STRICTLY today→tomorrow (horizon 1) for EVERY edge — fill at the open, grade at the next
# session's close. The multi-day nature of SPILLOVER/THEME/WORLD is handled on the INPUT side
# (look back as many days as needed to DETECT the buildup — price 5d/20d/90d is already baked
# into every candidate; THEME mention-velocity keys on the news window), NOT on the horizon.
# So: read the slow signal, bet the next 1-2 days. Env-overridable per edge if you change your mind.
EDGE_HORIZON_DAYS = {
    "MOMENTUM": int(os.environ.get("EDGE_HORIZON_MOMENTUM", "1")),    # today → tomorrow
    "SPILLOVER": int(os.environ.get("EDGE_HORIZON_SPILLOVER", "1")),  # detect over days, bet tomorrow
    "THEME": int(os.environ.get("EDGE_HORIZON_THEME", "1")),          # detect over days, bet tomorrow
    "WORLD": int(os.environ.get("EDGE_HORIZON_WORLD", "1")),          # detect over days, bet tomorrow
}
DEFAULT_EDGE_HORIZON_DAYS = int(os.environ.get("DEFAULT_EDGE_HORIZON_DAYS", "1"))
# Always enter at the CURRENT price (market fill), never a far-off AI-chosen level. And if a
# CLOSED-market decision's open has GAPPED away from the price the AI planned around by more
# than this %, the setup rested on a stale price → NOT TAKEN (re-evaluate live next run). This
# is the WAB failure mode (planned on a pre-gap price, market opened elsewhere). 0 disables.
ENTRY_GAP_SKIP_PCT = float(os.environ.get("ENTRY_GAP_SKIP_PCT", "2.0"))
# Scout coverage: how many candidates reach the scout (and get a price-context fetch). The
# window is now ranked by MATERIALITY (earnings reaction size, else news intensity), NOT
# market cap — so the biggest movers are seen first instead of being truncated behind
# mega-caps (the THRM +22.7% miss). Raise to see more per run (more scout tokens + fetches).
SCOUT_MAX_CANDIDATES = int(os.environ.get("SCOUT_MAX_CANDIDATES", "60"))
# Auto-run: in `dashboard` mode, fire Find Trades every AUTORUN_INTERVAL_HOURS within the
# [AUTORUN_START_ET, AUTORUN_END_ET] ET window on trading days — no button click. Default:
# hourly, 09:35–16:00 (market hours; start a few min after 9:30 so BMO reporters are public
# and pricing is live for the gap-guard). Hourly is cheap + information-driven because the 24h
# repick cooldown means each run only debates NEW catalysts, not the same names on price.
# Widen END (e.g. 23:59) for around-the-clock; INTERVAL_HOURS<=0 or empty START disables it.
AUTORUN_INTERVAL_HOURS = float(os.environ.get("AUTORUN_INTERVAL_HOURS", "1"))
AUTORUN_START_ET = os.environ.get("AUTORUN_START_ET", "09:35").strip()
AUTORUN_END_ET = os.environ.get("AUTORUN_END_ET", "16:00").strip()
# Paper portfolio manager — route booked picks to an Alpaca PAPER account (real fills/slippage
# as an honest scoreboard). OPT-IN: nothing trades until PAPER_TRADING=1. Conviction-weighted
# sizing: $PM_BASE_USD for a conviction-50 pick, scaled by adjusted_score, capped at
# PM_MAX_POSITION_USD; at most PM_MAX_POSITIONS open (best conviction first). Reconciliation loop
# (desk.portfolio.reconcile) makes Alpaca match the ledger's open-taken positions — idempotent.
PAPER_TRADING = os.environ.get("PAPER_TRADING", "0") not in ("0", "", "false", "False", "no")
PM_BASE_USD = float(os.environ.get("PM_BASE_USD", "1000"))            # $ for a conviction-50 position
PM_MAX_POSITION_USD = float(os.environ.get("PM_MAX_POSITION_USD", "2500"))
PM_MAX_POSITIONS = int(os.environ.get("PM_MAX_POSITIONS", "20"))


def pinned_horizon(edge: str | None) -> int:
    """The PRE-COMMITTED grading horizon for an edge (fixed in advance, not judge-chosen)."""
    return EDGE_HORIZON_DAYS.get((edge or "").upper(), DEFAULT_EDGE_HORIZON_DAYS)
# Anti-survivorship: grade scout SKIPS too. A skip has no direction, so a "miss"
# is a large move in EITHER direction vs SPY within a short window we ignored.
SKIP_GRADE_DAYS = 3             # trading days to judge a skipped name's forward move
SKIP_MISS_ABS_ALPHA = 6.0       # |symbol return − SPY| above this % = a missed dislocation
EARNINGS_DRIFT_DAYS = 3         # a name reported within this many days → post-earnings-drift candidate
# Post-earnings drift needs a VISIBLE reaction to continue — betting before the stock
# has moved on the print is a coin flip. A reporter must have reacted at least this %
# (live price vs pre-report close, extended-hours-aware) to be a directional candidate;
# below this = no drift setup yet, skip it (don't guess the print).
MATERIAL_REACTION_PCT = float(os.environ.get("MATERIAL_REACTION_PCT", "1.5"))
# Shadow A/B on the gate above: EVERY public reporter's reaction is logged (passed AND
# dropped) and graded forward vs SPY in the reaction direction over this fixed horizon,
# so we can see whether forward alpha actually turns on at MATERIAL_REACTION_PCT — i.e.
# whether the gate is filtering noise or throwing away quiet under-reactions. Fixed
# (not the per-pick horizon) so all reactions are compared on one clock. `abtest` CLI.
REACTION_AB_HORIZON_DAYS = int(os.environ.get("REACTION_AB_HORIZON_DAYS", "3"))
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

# Limit-order fills: a 'limit' pick fills only if the market reaches the plan entry
# — with this buffer of tolerance so a near-miss still counts as a fill (else it's
# "not taken"). 'market' picks always fill at the open. (Model A honesty on the level.)
LIMIT_FILL_BUFFER_PCT = float(os.environ.get("LIMIT_FILL_BUFFER_PCT", "0.25"))
# A limit that fills already most of the way from the planned entry to the STOP has
# had the reaction move against the thesis before entry (you'd fill one nudge from
# invalidation — the SLG case). Require the fill to keep at least this fraction of the
# planned entry→stop cushion, else it's NOT TAKEN.
LIMIT_FILL_MIN_CUSHION_FRAC = float(os.environ.get("LIMIT_FILL_MIN_CUSHION_FRAC", "0.4"))

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


def next_market_open(dt: datetime) -> datetime:
    """The 9:30 ET open of the next REGULAR session at/after dt (weekends skipped;
    holidays not modelled). Model A: fills happen only in regular hours."""
    dt = dt.astimezone(ET)

    def open_at(d) -> datetime:
        return datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET)

    if dt.weekday() < 5 and dt < open_at(dt.date()):
        return open_at(dt.date())            # still before today's open
    d = dt.date() + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return open_at(d)


def market_context() -> dict:
    """The market clock, as facts for the agents — so they decide direction for a
    fill at the RIGHT moment and weigh what has already traded in extended hours.
    Model A: a call made off-hours fills at the next regular 9:30 open, so the
    tradeable move is the drift FROM that open (the overnight/after-hours gap is
    already gone). is_open lets the agents distinguish 'act now' from 'act at the
    open'."""
    now = now_et()
    sess = session(now)
    nxt = next_market_open(now)
    return {
        "session": sess,
        "is_open": sess == "OPEN",
        "now_et": now.strftime("%a %Y-%m-%d %H:%M ET"),
        "fills_at": "now (market open)" if sess == "OPEN"
        else nxt.strftime("%a %Y-%m-%d 09:30 ET"),
        "hours_to_open": 0.0 if sess == "OPEN"
        else round((nxt - now).total_seconds() / 3600, 1),
    }


def market_context_line() -> str:
    """One-line market-clock note for agent prompts."""
    m = market_context()
    if m["is_open"]:
        return (f"Market clock: OPEN now ({m['now_et']}). A committed call can be "
                "acted on immediately at the current price.")
    return (f"Market clock: CLOSED/extended-hours now ({m['now_et']}). Under regular-hours "
            f"trading a committed call FILLS at the next open ({m['fills_at']}, ~{m['hours_to_open']}h away) "
            "— so judge the tradeable move as the drift FROM that open, weighing how much of "
            "the reaction has ALREADY happened in extended hours since the catalyst (a move "
            "largely spent overnight leaves little to capture at the open).")


def entry_fill_time(ts_iso: str, sess: str | None) -> datetime | None:
    """When a pick actually FILLS under Model A (regular hours): the decision time
    if the market was open, else the next 9:30 regular open. This is the honest
    entry — a closed-market call can't fill at 3am, it fills at the open (the same
    price the grader enters at). None on a bad timestamp."""
    try:
        dt = datetime.fromisoformat(ts_iso).astimezone(ET)
    except (ValueError, TypeError):
        return None
    return dt if sess == "OPEN" else next_market_open(dt)


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
