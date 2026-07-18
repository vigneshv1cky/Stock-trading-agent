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
}

export interface Brief {
  kind: string
  summary: string
  key_facts?: { fact: string }[]
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
  ret_1d: number | null
  ret_horizon: number | null
  spy_ret_horizon: number | null
  alpha_net: number | null
  graded_at: string | null
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
