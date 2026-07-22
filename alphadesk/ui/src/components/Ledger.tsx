import { useEffect, useState } from "react"
import { api, etDateTime, fmtAlpha, groupByDayKey, type SymbolTimeline, type TimelineEvent, type Stats } from "@/lib/api"
import { dirUp, dirWord, plainEdge } from "@/lib/plain"
import { ArrowDown, ArrowUp, RotateCcw } from "lucide-react"
import { InfoTip } from "@/components/InfoTip"
import { Card } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"

function Stat({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: number | null }) {
  const color = tone == null ? "" : tone > 0 ? "text-emerald-600 dark:text-emerald-400" : tone < 0 ? "text-red-600 dark:text-red-400" : ""
  return (
    <Card size="sm">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className={`mt-0.5 font-mono text-lg font-semibold tabular-nums ${color}`}>{value}</div>
      {sub && <div className="text-[11px] text-muted-foreground">{sub}</div>}
    </Card>
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

// Classify how a position was closed, from the recorded exit reason + realized
// alpha. The watcher writes deterministic "target hit …" / "stopped out …"
// reasons; the review agent writes a free-text thesis close. A target hit is a
// win (green), a stop a loss (red); a discretionary close is toned by what it
// actually banked (a small give-back reads red, a locked-in gain green).
function exitKind(
  reason: string | null | undefined,
  realized: number | null | undefined,
): { label: string; tone: number } {
  const r = (reason ?? "").toLowerCase()
  if (r.startsWith("target hit")) return { label: "target hit", tone: 1 }
  if (r.startsWith("stopped out")) return { label: "stopped out", tone: -1 }
  return { label: "closed early", tone: realized ?? 0 }
}

// Green for a gain, red for a loss, amber for a flat/unknown close.
function toneText(t: number): string {
  return t > 0
    ? "text-emerald-600 dark:text-emerald-400"
    : t < 0
      ? "text-red-600 dark:text-red-400"
      : "text-amber-600 dark:text-amber-400"
}
function toneChip(t: number): string {
  return t > 0
    ? "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400"
    : t < 0
      ? "bg-red-500/15 text-red-600 dark:text-red-400"
      : "bg-amber-500/15 text-amber-600 dark:text-amber-400"
}

// The desk's current stance on a stock — the headline of its timeline card. For
// an exited name the badge says WHY it closed and is colored by the outcome.
function StanceBadge({ current, exit }: { current: string; exit?: { label: string; tone: number } | null }) {
  if (current === "EXITED" && exit) {
    return <Badge className={`text-[11px] font-semibold ${toneChip(exit.tone)}`}>Exited · {exit.label}</Badge>
  }
  const map: Record<string, { label: string; cls: string }> = {
    LONG: { label: "Buy", cls: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400" },
    SHORT: { label: "Short", cls: "bg-red-500/15 text-red-600 dark:text-red-400" },
    EXITED: { label: "Exited", cls: "bg-amber-500/15 text-amber-600 dark:text-amber-400" },
    CLOSED: { label: "Closed", cls: "bg-muted text-muted-foreground" },
  }
  const s = map[current] ?? map.CLOSED
  return <Badge className={`text-[11px] font-semibold ${s.cls}`}>{s.label}</Badge>
}

// What happened with one call: vs S&P (graded), exited, or live P&L (open).
function Outcome({ e }: { e: TimelineEvent }) {
  if (e.state === "graded" && e.alpha_net != null) {
    return (
      <span className={`font-mono text-sm font-semibold tabular-nums ${e.alpha_net > 0 ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400"}`}>
        {fmtAlpha(e.alpha_net)} <span className="text-[10px] font-normal text-muted-foreground">vs S&P</span>
      </span>
    )
  }
  if (e.state === "exited") {
    // realized performance frozen at the exit price (vs S&P, net friction) —
    // distinct from the horizon grade; fall back to raw return, then bare label.
    // The tag says WHY it closed and is colored like the outcome: target=green,
    // stop=red, discretionary=by the alpha it banked.
    const ex = e.exit_alpha ?? e.exit_return_pct
    const k = exitKind(e.exit_reason, ex)
    return (
      <span className="text-right">
        <span className={`text-xs font-semibold ${toneText(k.tone)}`}>Exited · {k.label}</span>
        {ex != null && (
          <span className={`ml-1.5 font-mono text-sm font-semibold tabular-nums ${ex > 0 ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400"}`}>
            {fmtAlpha(ex)}{" "}
            <span className="text-[10px] font-normal text-muted-foreground">
              {e.exit_alpha != null ? "vs S&P" : "ret"}
            </span>
          </span>
        )}
      </span>
    )
  }
  if (e.pnl_pct != null) {
    const pos = e.pnl_pct >= 0
    const aPos = (e.alpha_so_far ?? 0) >= 0
    return (
      <span className="text-right">
        <span className="font-mono text-sm tabular-nums">${e.current}</span>{" "}
        <span className={`font-mono text-xs tabular-nums ${pos ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400"}`}>
          ({pos ? "+" : ""}
          {e.pnl_pct}%)
        </span>
        {e.alpha_so_far != null && (
          <InfoTip
            tip="How much it's beating the S&P 500 so far, net of friction — a live mark, not the official grade"
            className={`ml-1 cursor-help text-[10px] ${aPos ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400"}`}
          >
            vs S&P {aPos ? "+" : ""}
            {e.alpha_so_far}% so far
          </InfoTip>
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
      {up ? <ArrowUp className="h-3.5 w-3.5 shrink-0 text-emerald-600 dark:text-emerald-400" /> : <ArrowDown className="h-3.5 w-3.5 shrink-0 text-red-600 dark:text-red-400" />}
      <span className={`text-xs font-medium ${up ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400"}`}>{dirWord(e.direction)}</span>
      <span className="font-mono text-[11px] tabular-nums text-muted-foreground">{etDateTime(e.ts)}</span>
      {e.edge && <Badge className="hidden bg-muted font-normal text-muted-foreground sm:inline-flex">{plainEdge(e.edge)}</Badge>}
      <span className="ml-auto flex shrink-0 items-center gap-2.5">
        {e.mfe_pct != null && (
          <InfoTip
            tip="How far it ran / how far underwater while held (max favorable / adverse excursion vs entry)"
            className="hidden cursor-help font-mono text-[10px] tabular-nums md:inline"
          >
            <span className="text-emerald-600/80 dark:text-emerald-400/80">▲{e.mfe_pct >= 0 ? "+" : ""}{e.mfe_pct.toFixed(1)}%</span>{" "}
            {e.mae_pct != null && <span className="text-red-600/80 dark:text-red-400/80">▼{e.mae_pct.toFixed(1)}%</span>}
          </InfoTip>
        )}
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
  const exit =
    s.current === "EXITED" && latest
      ? exitKind(latest.exit_reason, latest.exit_alpha ?? latest.exit_return_pct)
      : null
  return (
    <Card size="sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-semibold">{s.symbol}</span>
        <StanceBadge current={s.current} exit={exit} />
        {s.changed && s.current !== "EXITED" && (
          <Badge className="gap-1 bg-fuchsia-500/15 font-semibold text-fuchsia-600 dark:text-fuchsia-400">
            <RotateCcw className="h-2.5 w-2.5" /> changed
          </Badge>
        )}
        <span className="ml-auto text-[11px] text-muted-foreground">
          {events.length} call{events.length > 1 ? "s" : ""}
        </span>
      </div>
      {exitReason && <p className={`mt-1 text-xs ${toneText(exit?.tone ?? 0)}`}>Exited: {exitReason}</p>}
      <div className="mt-1.5 divide-y divide-border/60">
        {shown.map((e) => (
          <EventRow key={e.id} e={e} onSelect={onSelect} />
        ))}
      </div>
      {more > 0 && <div className="mt-1 px-2 text-[11px] text-muted-foreground">+{more} earlier</div>}
    </Card>
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
        <Card className="border-dashed p-8 text-center">
          <p className="text-sm font-medium">No ideas yet</p>
          <p className="mx-auto mt-1 max-w-xs text-xs text-muted-foreground">
            Hit <b className="text-foreground">Run</b> on the desk. Each stock builds a timeline here —
            every call, whether it worked, and when the desk changed its mind.
          </p>
        </Card>
      ) : (
        <div className="space-y-3">
          {groupByDayKey(symbols, (s) => s.last_ts).map((g) => (
            <div key={g.key} className="space-y-2">
              <div className="text-xs font-semibold text-indigo-600 dark:text-indigo-400">
                {g.label}
              </div>
              {g.items.map((s) => (
                <SymbolCard key={s.symbol} s={s} onSelect={onSelect} />
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
