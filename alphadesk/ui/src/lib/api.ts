// AlphaDesk API client — same-origin; Basic Auth handled by the browser.

export interface Concern {
  claim: string
  evidence: string
}

export interface Rebuttal {
  rebuttal: string
  revised_score: number
  concede: boolean
}

export interface Debate {
  concerns?: Concern[]
  rebuttal?: Rebuttal
  fact_flags?: string[]
  arbiter_summary?: string
  critic_stance?: string
  counter_direction?: string
  counter?: string
  proposed_direction?: string
  final_direction?: string
  flipped?: boolean
}

export interface Brief {
  kind: string
  summary: string
  key_facts?: { fact: string }[]
}

// Actionable execution levels for a committed call (may be null if the desk
// couldn't set a coherent plan — the directional call still stands).
export interface Plan {
  entry: number
  target: number
  stop: number
  note: string
  hold: string // "single-day" | "multi-day"
}

export interface Pick {
  id: number
  ts: string
  symbol: string
  arm: "TEAM" | "LONER"
  edge: string | null
  trigger_src: string
  session: string
  direction: "LONG" | "SHORT"
  horizon_days: number
  score: number
  adjusted_score: number | null
  confidence: number
  verdict: string | null
  approved: number
  triage_reason: string | null
  thesis: string | null
  debate: Debate | null
  briefs: Brief[] | null
  model_tags: Record<string, string> | null
  low_liquidity: number
  entry_price: number | null
  spy_price: number | null
  plan_entry: number | null
  plan_target: number | null
  plan_stop: number | null
  plan_note: string | null
  ret_1d: number | null
  ret_horizon: number | null
  spy_ret_horizon: number | null
  alpha_net: number | null
  alpha_adj: number | null // beta-adjusted + borrow-aware alpha (the honest number, alongside alpha_net)
  beta: number | null // stock's beta vs SPY (trailing daily returns, clamped 0–3)
  graded_at: string | null
}

// One open pick tracked live against the current price.
export interface LivePick {
  id: number
  ts: string
  entry_ts: string // honest entry fill time (9:30 open if the call was made off-hours)
  symbol: string
  direction: "LONG" | "SHORT"
  horizon_days: number
  session: string
  edge: string | null
  verdict: string | null
  approved: number
  taken: number
  plan_entry: number
  plan_target: number
  plan_stop: number
  plan_note: string | null
  order_type: string | null // 'market' (fill at open) | 'limit' (fill only if price reaches entry)
  current: number | null
  pnl_pct: number | null
  alpha_so_far: number | null // interim vs-SPY, net friction — NOT the official grade
  progress: number | null // 0 = at stop, 1 = at target
  status: string // working | near target | near stop | target hit | stopped out | no quote
}

// One call in a symbol's timeline, with its outcome.
export interface TimelineEvent {
  id: number
  ts: string
  entry_ts: string // honest entry fill time (9:30 open if the call was made off-hours)
  direction: "LONG" | "SHORT"
  horizon_days: number
  edge: string | null
  verdict: string | null
  approved: number
  adjusted_score: number | null
  plan_entry: number | null
  plan_target: number | null
  plan_stop: number | null
  entry_price: number | null
  alpha_net: number | null
  alpha_adj: number | null // beta-adjusted + borrow-aware alpha (honest counterpart to alpha_net)
  beta: number | null // stock's beta vs SPY
  graded_at: string | null
  exit_ts: string | null
  exit_reason: string | null
  exit_price: number | null
  exit_return_pct: number | null // realized return entry→exit (direction-aware)
  exit_alpha: number | null // realized alpha vs SPY over the hold, net friction
  mfe_pct: number | null // max favorable excursion (peak profit) over the hold, % vs entry
  mae_pct: number | null // max adverse excursion (worst drawdown) over the hold, % vs entry
  state: "open" | "graded" | "exited" | "not_taken" // not_taken = thesis died before the open fill (never held)
  current: number | null
  pnl_pct: number | null
  alpha_so_far: number | null // interim vs-SPY while open; official alpha_net settles at horizon
  status: string | null
}

// The desk's evolving view on one stock — the "track record", grouped.
export interface SymbolTimeline {
  symbol: string
  current: string // LONG | SHORT | EXITED | CLOSED
  changed: boolean
  last_ts: string
  events: TimelineEvent[]
}

export interface Stats {
  total: {
    picks: number
    graded: number
    avg_alpha_net: number | null
    avg_alpha_adj: number | null // beta-adjusted + borrow-aware mean alpha
    effective_graded: number | null // graded, cluster-deduped (correlated picks count once)
    wins: number | null
  }
  by: Record<
    string,
    { bucket: string; n: number; graded: number; avg_alpha_net: number | null; wins: number }[]
  >
  debate_lift: { post_debate_acc: number | null; pre_debate_acc: number | null }
}

export interface TokenRow {
  role: string
  model: string
  calls: number
  input_tok: number
  output_tok: number
}

export interface SourceStat {
  source: string
  articles: number
  candidates: number
  ingest_tokens: number
  debate_tokens: number
  tokens: number
  picks: number
  taken: number
  graded: number
  avg_alpha: number | null
}

export interface EarningsRow {
  symbol: string
  report_date: string
  session: string | null
  eps_estimate: number | null
  eps_actual?: number | null
  surprise_pct?: number | null
  move_since_report_pct?: number | null // total reaction since the report (gap + drift)
  move_gap_pct?: number | null // the overnight/pre-market gap — repriced before you could act (uncapturable)
  move_drift_pct?: number | null // capturable drift from the first post-report open — what you could trade
  market_cap?: number | null // for ranking big names first within a run-day
  run_at?: string | null // when to run the desk to catch the drift (9:30 ET, next session)
  // coverage self-assessment (reported names only): did the desk act on this reporter?
  engagement?: "TOOK" | "DEBATED" | "SKIPPED" | "UNSEEN"
  engagement_pick_id?: number | null
  engagement_dir?: "LONG" | "SHORT" | null
  engagement_verdict?: string | null // STRONG | SOFT | PASS (for took/debated)
  engagement_why?: string | null // the desk's own reason: judge summary / thesis / skip reason
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(path)
  if (!res.ok) throw new Error(`${path}: ${res.status}`)
  return res.json() as Promise<T>
}

export const api = {
  pick: (id: number) => get<Pick>(`/api/picks/${id}`),
  live: () => get<{ live: LivePick[]; market: string }>("/api/live"),
  timelines: () => get<{ symbols: SymbolTimeline[]; market: string }>("/api/timelines"),
  stats: () => get<Stats>("/api/stats"),
  tokens: (days = 1) => get<{ usage: TokenRow[] }>(`/api/tokens?days=${days}`),
  sources: (days = 30) => get<{ sources: SourceStat[] }>(`/api/sources?days=${days}`),
  earnings: () =>
    get<{ upcoming: EarningsRow[]; reported: EarningsRow[] }>("/api/earnings"),
}

// The market runs on US Eastern; show all decision timestamps there.
const ET = "America/New_York"

// "YYYY-MM-DD" in ET — used as a stable grouping key.
export function etDateKey(ts: string): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: ET,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date(ts))
}

// "Tue 07-21" in ET — a compact day-group header.
export function etDayLabel(ts: string): string {
  const wd = new Intl.DateTimeFormat("en-US", { timeZone: ET, weekday: "short" }).format(
    new Date(ts),
  )
  return `${wd} ${etDateKey(ts).slice(5)}`
}

// Group items by their ET day (newest day first), preserving each item's incoming
// order within a day. `ts` picks the timestamp to group on.
export function groupByDayKey<T>(
  items: T[],
  ts: (x: T) => string,
): { key: string; label: string; items: T[] }[] {
  const map = new Map<string, T[]>()
  for (const it of items) {
    const k = etDateKey(ts(it))
    ;(map.get(k) ?? map.set(k, []).get(k)!).push(it)
  }
  return [...map.entries()]
    .sort((a, b) => (a[0] < b[0] ? 1 : -1))
    .map(([key, group]) => ({ key, label: etDayLabel(ts(group[0])), items: group }))
}

// "Jul 18, 14:23" in ET.
export function etDateTime(ts: string): string {
  return new Intl.DateTimeFormat("en-US", {
    timeZone: ET,
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
  }).format(new Date(ts))
}

export function fmtAlpha(a: number | null): string {
  if (a === null || a === undefined) return "…"
  return `${a > 0 ? "+" : ""}${a.toFixed(2)}%`
}

export function exitDate(ts: string, session: string, horizonDays: number): string {
  const d = new Date(ts)
  if (session !== "OPEN") d.setDate(d.getDate() + 1)
  while (d.getDay() === 0 || d.getDay() === 6) d.setDate(d.getDate() + 1)
  let remaining = horizonDays
  while (remaining > 0) {
    d.setDate(d.getDate() + 1)
    if (d.getDay() !== 0 && d.getDay() !== 6) remaining -= 1
  }
  return d.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" })
}
