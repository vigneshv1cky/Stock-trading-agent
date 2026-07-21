"""The grader — turns picks into a scorecard. Pure code, zero judgment.

Semantics:
  • Closed-market decisions (entry_price NULL) enter at the OPEN of the first
    trading day after the decision — never at a stale prior close.
  • ret_1d = close of entry day +1 trading day; ret_horizon = close of entry
    day + horizon_days trading days. Direction-aware (SHORT inverts).
  • Benchmark: SPY over the identical window (short picks benchmark against
    short-SPY, keeping alpha symmetric).
  • alpha_net = directional return − benchmark − friction. Friction is
    2 × FRICTION_BPS_PER_SIDE (doubled again for LOW_LIQUIDITY picks).
"""

import logging
from datetime import datetime, timezone

from alphadesk.config import ET, FRICTION_BPS_PER_SIDE
from alphadesk.ledger import store

log = logging.getLogger("alphadesk.grader")

_history_cache: dict[str, object] = {}


def _daily_history(symbol: str):
    """Daily OHLC frame for the last ~60 days (cached per grading pass)."""
    if symbol in _history_cache:
        return _history_cache[symbol]
    import yfinance as yf
    df = yf.Ticker(symbol).history(period="60d", interval="1d")
    if df is None or df.empty:
        _history_cache[symbol] = None
        return None
    df = df.tz_convert(ET) if df.index.tz is not None else df.tz_localize(ET)
    _history_cache[symbol] = df
    return df


def _entry(row: dict, df):
    """(entry_day, entry_price) for a pick, or None if not determinable yet.
    Shared by the horizon grade and the MFE/MAE path so both anchor identically."""
    import pandas as pd

    decided = datetime.fromisoformat(row["ts"])
    if decided.tzinfo is None:
        decided = decided.replace(tzinfo=timezone.utc)
    decided_et = decided.astimezone(ET)
    decided_day = pd.Timestamp(decided_et).normalize()
    days = df.index.normalize().unique()

    if row["entry_price"] is not None:
        cand = days[days <= decided_day]
        if len(cand) == 0:
            return None
        return cand[-1], float(row["entry_price"])
    # decided while closed → enter at next trading day's open
    future = days[days > decided_day] if decided_et.hour >= 16 or row["session"] == "CLOSED" \
        else days[days >= decided_day]
    if len(future) == 0:
        return None
    entry_day = future[0]
    return entry_day, float(df.loc[df.index.normalize() == entry_day, "Open"].iloc[0])


def _window_end(row: dict, days, entry_day):
    """Trading day the hold window closes on: the exit day if exited early, else
    the horizon day if reached, else the latest bar so far (running for open picks).
    Clamped to horizon so an exit stamp never runs the window past it."""
    import pandas as pd

    after = days[days > entry_day]
    horizon = int(row["horizon_days"])
    horizon_day = after[horizon - 1] if len(after) >= horizon else None
    if row.get("exit_ts"):
        ex = datetime.fromisoformat(row["exit_ts"])
        if ex.tzinfo is None:
            ex = ex.replace(tzinfo=timezone.utc)
        ex_day = pd.Timestamp(ex.astimezone(ET)).normalize()
        cand = days[days <= ex_day]
        end = cand[-1] if len(cand) else entry_day
        return min(end, horizon_day) if horizon_day is not None else end
    return horizon_day if horizon_day is not None else days[-1]


def _entry_and_outcomes(row: dict, df, spy) -> dict | None:
    """Compute gradable fields for one pick, or None if not yet gradable."""
    days = df.index.normalize().unique()
    ent = _entry(row, df)
    if ent is None:
        return None
    entry_day, entry_price = ent

    after = days[days > entry_day]

    def _close_after(n_days: int) -> float | None:
        if len(after) < n_days:
            return None
        day = after[n_days - 1]
        return float(df.loc[df.index.normalize() == day, "Close"].iloc[0])

    sign = 1.0 if row["direction"] == "LONG" else -1.0
    out: dict = {}

    close_1d = _close_after(1)
    if close_1d is not None and entry_price:
        out["ret_1d"] = round(sign * (close_1d - entry_price) / entry_price * 100, 3)

    horizon = int(row["horizon_days"])
    close_h = _close_after(horizon)
    if close_h is None or not entry_price:
        # horizon not reached yet — partial grade only if 1d is available
        return out or None

    ret_h = sign * (close_h - entry_price) / entry_price * 100
    out["ret_horizon"] = round(ret_h, 3)

    # SPY over the identical window
    if spy is not None:
        sdays = spy.index.normalize().unique()
        s_entry_c = sdays[sdays >= entry_day]
        if len(s_entry_c) > 0:
            s_entry_day = s_entry_c[0]
            s_after = sdays[sdays > s_entry_day]
            if len(s_after) >= horizon:
                s_entry = float(spy.loc[spy.index.normalize() == s_entry_day, "Open"].iloc[0]) \
                    if row["entry_price"] is None else \
                    float(spy.loc[spy.index.normalize() == s_entry_day, "Close"].iloc[0])
                s_exit = float(spy.loc[spy.index.normalize() == s_after[horizon - 1], "Close"].iloc[0])
                spy_ret = (s_exit - s_entry) / s_entry * 100
                out["spy_ret_horizon"] = round(spy_ret, 3)
                benchmark = spy_ret if row["direction"] == "LONG" else -spy_ret
                friction = 2 * FRICTION_BPS_PER_SIDE / 100.0  # bps → %
                if row.get("low_liquidity"):
                    friction *= 2
                out["alpha_net"] = round(ret_h - benchmark - friction, 3)

    out["graded_at"] = datetime.now(timezone.utc).isoformat()
    if row["entry_price"] is None:
        out["entry_price"] = round(entry_price, 4)
    return out


def grade_due() -> int:
    """Grade all picks whose horizons have elapsed. Returns rows updated.
    Also updates the MFE/MAE path (open + closed) and skip grades each pass."""
    _history_cache.clear()
    spy = _daily_history("SPY")
    graded = 0
    for row in store.due_for_grading():
        try:
            df = _daily_history(row["symbol"])
            if df is None:
                continue
            out = _entry_and_outcomes(row, df, spy)
            if not out:
                continue
            store.update_pick(row["id"], **out)
            if "graded_at" in out:
                graded += 1
                log.info(
                    "Graded #%d %s %s %dd: ret=%.2f%% alpha_net=%s",
                    row["id"], row["symbol"], row["direction"], row["horizon_days"],
                    out.get("ret_horizon", float("nan")), out.get("alpha_net"),
                )
        except Exception as exc:
            log.warning("Grading failed for #%d %s: %s", row["id"], row["symbol"], exc)
    grade_paths()   # refresh MFE/MAE (open + closed); a routine update, not a "grade"
    return graded + grade_skips()


def grade_paths() -> int:
    """MFE/MAE over each position's hold window from daily High/Low — how far it
    ran in profit (max favorable) and how far underwater (max adverse) BEFORE it
    closed. Direction-aware, % vs entry. Running for open picks (updates each pass),
    frozen once exited or past horizon. Reuses the warm history cache; pure code."""
    due = store.picks_for_path()
    if not due:
        return 0
    updated = 0
    for row in due:
        try:
            df = _daily_history(row["symbol"])
            if df is None:
                continue
            ent = _entry(row, df)
            if ent is None:
                continue
            entry_day, entry_price = ent
            if not entry_price:
                continue
            days = df.index.normalize().unique()
            end_day = _window_end(row, days, entry_day)
            norm = df.index.normalize()
            window = df[(norm >= entry_day) & (norm <= end_day)]
            if window.empty:
                continue
            hi = float(window["High"].astype(float).max())
            lo = float(window["Low"].astype(float).min())
            if row["direction"] == "LONG":     # favorable = up, adverse = down
                mfe, mae = (hi - entry_price), (lo - entry_price)
            else:                              # SHORT: favorable = down, adverse = up
                mfe, mae = (entry_price - lo), (entry_price - hi)
            store.update_pick(row["id"],
                              mfe_pct=round(mfe / entry_price * 100, 3),
                              mae_pct=round(mae / entry_price * 100, 3))
            updated += 1
        except Exception as exc:
            log.warning("Path grading failed for #%d %s: %s", row["id"], row["symbol"], exc)
    return updated


def grade_skips() -> int:
    """Grade scout skips whose window has elapsed: a directionless |move vs SPY|
    over SKIP_GRADE_DAYS. missed=1 if it crossed the threshold — a dislocation we
    never looked at. Reuses the warm _history_cache from grade_due()."""
    import pandas as pd

    from alphadesk.config import SKIP_GRADE_DAYS, SKIP_MISS_ABS_ALPHA
    due = store.due_skips()
    if not due:
        return 0
    spy = _daily_history("SPY")
    sdays = spy.index.normalize().unique() if spy is not None else None
    now_iso = datetime.now(timezone.utc).isoformat()
    graded = 0

    def _window_ret(df, sdates, entry_day) -> float | None:
        after = sdates[sdates > entry_day]
        if len(after) < SKIP_GRADE_DAYS:
            return None
        c0 = float(df.loc[df.index.normalize() == entry_day, "Close"].iloc[0])
        c1 = float(df.loc[df.index.normalize() == after[SKIP_GRADE_DAYS - 1], "Close"].iloc[0])
        return (c1 - c0) / c0 * 100 if c0 else None

    for row in due:
        try:
            df = _daily_history(row["symbol"])
            if df is None:  # unpriceable (delisted/odd suffix) — close it so we stop retrying
                store.update_skip(row["id"], abs_alpha=None, missed=0, graded_at=now_iso)
                graded += 1
                continue
            decided = datetime.fromisoformat(row["ts"])
            if decided.tzinfo is None:
                decided = decided.replace(tzinfo=timezone.utc)
            decided_day = pd.Timestamp(decided.astimezone(ET)).normalize()
            days = df.index.normalize().unique()
            entry_c = days[days >= decided_day]
            if len(entry_c) == 0:
                continue
            sym_ret = _window_ret(df, days, entry_c[0])
            if sym_ret is None:
                continue  # window not elapsed yet
            spy_ret = 0.0
            if sdays is not None:
                s_entry_c = sdays[sdays >= entry_c[0]]
                if len(s_entry_c) > 0:
                    spy_ret = _window_ret(spy, sdays, s_entry_c[0]) or 0.0
            abs_alpha = abs(sym_ret - spy_ret)
            store.update_skip(row["id"], abs_alpha=round(abs_alpha, 3),
                              missed=int(abs_alpha >= SKIP_MISS_ABS_ALPHA), graded_at=now_iso)
            graded += 1
        except Exception as exc:
            log.warning("Skip grading failed for #%d %s: %s", row["id"], row["symbol"], exc)
    return graded
