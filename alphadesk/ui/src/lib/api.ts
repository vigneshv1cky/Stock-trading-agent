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
  graded_at: string | null
}

// One open pick tracked live against the current price.
export interface LivePick {
  id: number
  ts: string
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
  current: number | null
  pnl_pct: number | null
  progress: number | null // 0 = at stop, 1 = at target
  status: string // working | near target | near stop | target hit | stopped out | no quote
}

export interface Stats {
  total: {
    picks: number
    graded: number
    avg_alpha_net: number | null
    wins: number | null
  }
  by: Record<
    string,
    { bucket: string; n: number; graded: number; avg_alpha_net: number | null; wins: number }[]
  >
  debate_lift: { post_debate_acc: number | null; pre_debate_acc: number | null }
}

export interface FunnelWindow {
  id: number
  window_ts: string
  ingested: number
  candidates: number
  picked: number
  skipped: number
  skip_reasons: string // JSON string of [{symbol, reason}]
}

export interface TokenRow {
  role: string
  model: string
  calls: number
  input_tok: number
  output_tok: number
}

export interface EarningsRow {
  symbol: string
  report_date: string
  session: string | null
  eps_estimate: number | null
  eps_actual?: number | null
  surprise_pct?: number | null
  move_since_report_pct?: number | null // % price move since the report went public (the drift so far)
  market_cap?: number | null // for ranking big names first within a run-day
  run_at?: string | null // when to run the desk to catch the drift (9:30 ET, next session)
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(path)
  if (!res.ok) throw new Error(`${path}: ${res.status}`)
  return res.json() as Promise<T>
}

export const api = {
  picks: (limit = 40) => get<{ picks: Pick[] }>(`/api/picks?limit=${limit}`),
  pick: (id: number) => get<Pick>(`/api/picks/${id}`),
  live: () => get<{ live: LivePick[]; market: string }>("/api/live"),
  stats: () => get<Stats>("/api/stats"),
  funnel: (limit = 20) =>
    get<{ paused: string | null; windows: FunnelWindow[] }>(`/api/funnel?limit=${limit}`),
  tokens: (days = 1) => get<{ usage: TokenRow[] }>(`/api/tokens?days=${days}`),
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

// "Jul 18, 14:23" in ET.
export function etDateTime(ts: string): string {
  return new Intl.DateTimeFormat("en-US", {
    timeZone: ET,
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
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
