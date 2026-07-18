import { etDateKey, etDateTime, fmtAlpha, type Pick, type Stats } from "@/lib/api"
import { dirWord, plainEdge, plainVerdict } from "@/lib/plain"
import { ArrowDown, ArrowUp } from "lucide-react"

function Stat({
  label,
  value,
  sub,
  tone,
}: {
  label: string
  value: string
  sub?: string
  tone?: number | null
}) {
  const color = tone == null ? "" : tone > 0 ? "text-emerald-500" : tone < 0 ? "text-red-500" : ""
  return (
    <div className="rounded-lg border border-border bg-card p-3">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className={`mt-0.5 font-mono text-lg font-semibold tabular-nums ${color}`}>{value}</div>
      {sub && <div className="text-[11px] text-muted-foreground">{sub}</div>}
    </div>
  )
}

function PerfStrip({ stats }: { stats: Stats | null }) {
  const graded = stats?.total.graded ?? 0
  const wins = stats?.total.wins ?? 0
  const winRate = graded > 0 ? Math.round((wins / graded) * 100) : null
  const avg = stats?.total.avg_alpha_net ?? null
  return (
    <div className="grid grid-cols-3 gap-2">
      <Stat label="Ideas logged" value={String(stats?.total.picks ?? 0)} />
      <Stat
        label="Scored"
        value={String(graded)}
        sub={winRate != null ? `${winRate}% beat S&P` : "grading forward"}
      />
      <Stat label="Avg vs S&P" value={avg != null ? fmtAlpha(avg) : "—"} tone={avg} />
    </div>
  )
}

function IdeaRow({ n, p, onSelect }: { n: number; p: Pick; onSelect: (id: number) => void }) {
  const long = p.direction === "LONG"
  const why = p.debate?.arbiter_summary ?? p.thesis ?? p.triage_reason ?? ""
  const graded = p.alpha_net != null
  return (
    <button
      onClick={() => onSelect(p.id)}
      className="flex w-full gap-3 rounded-lg border border-border bg-card p-3 text-left transition-colors hover:border-indigo-500/40 hover:bg-muted/40"
    >
      <span className="w-6 shrink-0 pt-0.5 text-right font-mono text-sm tabular-nums text-muted-foreground/50">
        {n}
      </span>
      <div className="min-w-0 flex-1">
      <div className="flex items-center gap-2.5">
        <span
          className={`grid h-6 w-6 place-items-center rounded ${
            long ? "bg-emerald-500/10 text-emerald-500" : "bg-red-500/10 text-red-500"
          }`}
        >
          {long ? <ArrowUp className="h-3.5 w-3.5" /> : <ArrowDown className="h-3.5 w-3.5" />}
        </span>
        <span className="font-semibold">{p.symbol}</span>
        <span className={`text-xs font-medium ${long ? "text-emerald-500" : "text-red-500"}`}>
          {dirWord(p.direction)}
        </span>
        <span className="text-xs text-muted-foreground">hold ~{p.horizon_days}d</span>
        {p.edge && (
          <span className="rounded-full bg-muted px-2 py-0.5 text-[11px] text-muted-foreground">
            {plainEdge(p.edge)}
          </span>
        )}
        <span className="ml-auto text-right">
          {graded ? (
            <span
              className={`font-mono text-sm font-semibold tabular-nums ${
                p.alpha_net! > 0 ? "text-emerald-500" : "text-red-500"
              }`}
            >
              {fmtAlpha(p.alpha_net)}
            </span>
          ) : (
            <span className="text-xs text-muted-foreground">scoring…</span>
          )}
        </span>
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground">
        {p.verdict && (
          <span className={p.verdict === "PASS" ? "text-red-500" : "text-foreground"}>
            {plainVerdict(p.verdict)}
          </span>
        )}
        <span>· conf {Math.round(p.adjusted_score ?? p.score)}</span>
        <span>· {p.approved ? "acted ✓" : "skipped"}</span>
        <span>· {p.arm === "LONER" ? "Solo" : "Team"}</span>
        <span className="font-mono tabular-nums text-muted-foreground/70">
          · {etDateTime(p.ts)} ET
        </span>
        <span className="text-muted-foreground/70">· #{p.id}</span>
      </div>
      {why && <p className="mt-1.5 line-clamp-2 text-xs leading-snug text-muted-foreground">{why}</p>}
      </div>
    </button>
  )
}

function dayLabel(dateKey: string): string {
  const today = etDateKey(new Date().toISOString())
  const yesterday = etDateKey(new Date(Date.now() - 86_400_000).toISOString())
  if (dateKey === today) return "Today"
  if (dateKey === yesterday) return "Yesterday"
  // dateKey is already the ET calendar date; format it without shifting tz again.
  return new Date(dateKey + "T12:00:00Z").toLocaleDateString("en-US", {
    timeZone: "UTC",
    weekday: "short",
    month: "short",
    day: "numeric",
    year: "numeric",
  })
}

function groupByDay(picks: Pick[]): [string, Pick[]][] {
  const map = new Map<string, Pick[]>()
  for (const p of picks) {
    const d = etDateKey(p.ts)
    const arr = map.get(d)
    if (arr) arr.push(p)
    else map.set(d, [p])
  }
  return [...map.entries()]
}

function EmptyState() {
  return (
    <div className="rounded-lg border border-dashed border-border bg-card p-8 text-center">
      <p className="text-sm font-medium">No ideas yet</p>
      <p className="mx-auto mt-1 max-w-xs text-xs text-muted-foreground">
        Hit <b className="text-foreground">Run</b> on the desk to scan the market. Every idea is
        scored against the S&P 500 at its own horizon.
      </p>
    </div>
  )
}

export function Ledger({
  picks,
  stats,
  onSelect,
}: {
  picks: Pick[]
  stats: Stats | null
  onSelect: (id: number) => void
}) {
  return (
    <div className="space-y-3">
      <PerfStrip stats={stats} />
      {picks.length === 0 ? (
        <EmptyState />
      ) : (
        <div className="space-y-4">
          {(() => {
            const pos = new Map(picks.map((p, i) => [p.id, i + 1]))
            return groupByDay(picks).map(([date, items]) => (
              <div key={date} className="space-y-2">
                <div className="flex items-center gap-2 px-0.5">
                  <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                    {dayLabel(date)}
                  </h3>
                  <span className="text-[11px] tabular-nums text-muted-foreground">
                    {items.length}
                  </span>
                  <div className="ml-1 h-px flex-1 bg-border" />
                </div>
                {items.map((p) => (
                  <IdeaRow key={p.id} n={pos.get(p.id) ?? 0} p={p} onSelect={onSelect} />
                ))}
              </div>
            ))
          })()}
        </div>
      )}
    </div>
  )
}
