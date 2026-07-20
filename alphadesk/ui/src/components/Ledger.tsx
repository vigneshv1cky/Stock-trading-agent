import { useEffect, useState } from "react"
import { api, etDateTime, fmtAlpha, type SymbolTimeline, type TimelineEvent, type Stats } from "@/lib/api"
import { dirUp, dirWord, plainEdge } from "@/lib/plain"
import { ArrowDown, ArrowUp, RotateCcw } from "lucide-react"

function Stat({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: number | null }) {
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
      <Stat label="Scored" value={String(graded)} sub={winRate != null ? `${winRate}% beat S&P` : "grading forward"} />
      <Stat label="Avg vs S&P" value={avg != null ? fmtAlpha(avg) : "—"} tone={avg} />
    </div>
  )
}

// The desk's current stance on a stock — the headline of its timeline card.
function StanceBadge({ current }: { current: string }) {
  const map: Record<string, { label: string; cls: string }> = {
    LONG: { label: "Buy", cls: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400" },
    SHORT: { label: "Short", cls: "bg-red-500/15 text-red-600 dark:text-red-400" },
    EXITED: { label: "Exited", cls: "bg-amber-500/15 text-amber-600 dark:text-amber-400" },
    CLOSED: { label: "Closed", cls: "bg-muted text-muted-foreground" },
  }
  const s = map[current] ?? map.CLOSED
  return <span className={`rounded px-1.5 py-0.5 text-[11px] font-semibold ${s.cls}`}>{s.label}</span>
}

// What happened with one call: vs S&P (graded), exited, or live P&L (open).
function Outcome({ e }: { e: TimelineEvent }) {
  if (e.state === "graded" && e.alpha_net != null) {
    return (
      <span className={`font-mono text-sm font-semibold tabular-nums ${e.alpha_net > 0 ? "text-emerald-500" : "text-red-500"}`}>
        {fmtAlpha(e.alpha_net)} <span className="text-[10px] font-normal text-muted-foreground">vs S&P</span>
      </span>
    )
  }
  if (e.state === "exited") {
    return <span className="text-xs font-medium text-amber-600 dark:text-amber-400">Exited</span>
  }
  if (e.pnl_pct != null) {
    const pos = e.pnl_pct >= 0
    const aPos = (e.alpha_so_far ?? 0) >= 0
    return (
      <span className="text-right">
        <span className="font-mono text-sm tabular-nums">${e.current}</span>{" "}
        <span className={`font-mono text-xs tabular-nums ${pos ? "text-emerald-500" : "text-red-500"}`}>
          ({pos ? "+" : ""}
          {e.pnl_pct}%)
        </span>
        {e.alpha_so_far != null && (
          <span className={`ml-1 text-[10px] ${aPos ? "text-emerald-500" : "text-red-500"}`}>
            S&P {aPos ? "+" : ""}
            {e.alpha_so_far}%
          </span>
        )}
      </span>
    )
  }
  return <span className="text-xs text-muted-foreground">scoring…</span>
}

function EventRow({ e, onSelect }: { e: TimelineEvent; onSelect: (id: number) => void }) {
  const up = dirUp(e.direction)
  return (
    <button
      onClick={() => onSelect(e.id)}
      className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm transition-colors hover:bg-muted/50"
    >
      {up ? <ArrowUp className="h-3.5 w-3.5 shrink-0 text-emerald-500" /> : <ArrowDown className="h-3.5 w-3.5 shrink-0 text-red-500" />}
      <span className={`text-xs font-medium ${up ? "text-emerald-500" : "text-red-500"}`}>{dirWord(e.direction)}</span>
      <span className="font-mono text-[11px] tabular-nums text-muted-foreground">{etDateTime(e.ts)}</span>
      {e.edge && <span className="hidden rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground sm:inline">{plainEdge(e.edge)}</span>}
      <span className="ml-auto shrink-0">
        <Outcome e={e} />
      </span>
    </button>
  )
}

function SymbolCard({ s, onSelect }: { s: SymbolTimeline; onSelect: (id: number) => void }) {
  const events = [...s.events].reverse() // newest first
  const shown = events.slice(0, 8)
  const more = events.length - shown.length
  const latest = events[0]
  const exitReason = s.current === "EXITED" ? latest?.exit_reason : null
  return (
    <div className="rounded-lg border border-border bg-card p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-semibold">{s.symbol}</span>
        <StanceBadge current={s.current} />
        {s.changed && (
          <span className="inline-flex items-center gap-1 rounded bg-fuchsia-500/15 px-1.5 py-0.5 text-[10px] font-semibold text-fuchsia-600 dark:text-fuchsia-400">
            <RotateCcw className="h-2.5 w-2.5" /> changed
          </span>
        )}
        <span className="ml-auto text-[11px] text-muted-foreground">
          {events.length} call{events.length > 1 ? "s" : ""}
        </span>
      </div>
      {exitReason && <p className="mt-1 text-xs text-amber-600 dark:text-amber-400">Exited: {exitReason}</p>}
      <div className="mt-1.5 divide-y divide-border/60">
        {shown.map((e) => (
          <EventRow key={e.id} e={e} onSelect={onSelect} />
        ))}
      </div>
      {more > 0 && <div className="mt-1 px-2 text-[11px] text-muted-foreground">+{more} earlier</div>}
    </div>
  )
}

export function Ledger({ stats, onSelect }: { stats: Stats | null; onSelect: (id: number) => void }) {
  const [symbols, setSymbols] = useState<SymbolTimeline[]>([])
  const [loaded, setLoaded] = useState(false)

  useEffect(() => {
    let alive = true
    const load = () =>
      api
        .timelines()
        .then((d) => {
          if (alive) {
            setSymbols(d.symbols)
            setLoaded(true)
          }
        })
        .catch(console.error)
    load()
    const t = setInterval(load, 30_000) // outcomes update live for open calls
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [])

  return (
    <div className="space-y-3">
      <PerfStrip stats={stats} />
      {loaded && symbols.length === 0 ? (
        <div className="rounded-lg border border-dashed border-border bg-card p-8 text-center">
          <p className="text-sm font-medium">No ideas yet</p>
          <p className="mx-auto mt-1 max-w-xs text-xs text-muted-foreground">
            Hit <b className="text-foreground">Run</b> on the desk. Each stock builds a timeline here —
            every call, whether it worked, and when the desk changed its mind.
          </p>
        </div>
      ) : (
        <div className="space-y-2">
          {symbols.map((s) => (
            <SymbolCard key={s.symbol} s={s} onSelect={onSelect} />
          ))}
        </div>
      )}
    </div>
  )
}
