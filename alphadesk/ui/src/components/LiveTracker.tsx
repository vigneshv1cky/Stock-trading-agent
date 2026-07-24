import { useEffect, useState } from "react"
import { api, etDateTime, groupByDayKey, type LivePick } from "@/lib/api"
import { dirUp } from "@/lib/plain"
import { InfoTip } from "@/components/InfoTip"
import { Card } from "@/components/ui/card"
import { ArrowDown, ArrowUp, RefreshCw } from "lucide-react"

// Where the price sits on the stop→target track, plus the entry tick.
function Track({ p }: { p: LivePick }) {
  const up = dirUp(p.direction)
  const span = up ? p.plan_target - p.plan_stop : p.plan_stop - p.plan_target
  const frac = (v: number) =>
    span === 0 ? 0 : Math.max(0, Math.min(1, up ? (v - p.plan_stop) / span : (p.plan_stop - v) / span))
  const curF = p.progress ?? (p.current != null ? frac(p.current) : 0)
  const entryF = frac(p.plan_entry)
  const pending = p.status === "pending"
  const pos = (p.pnl_pct ?? 0) >= 0
  return (
    <div>
      <div className="relative mt-2 h-1.5 rounded-full bg-muted">
        {p.current != null && (
          <div
            // pending = not filled yet, so no P&L → neutral bar, never green/red
            className={`absolute top-0 h-1.5 ${pending ? "bg-muted-foreground/30" : pos ? "bg-emerald-500" : "bg-red-500"}`}
            style={{
              left: `${Math.min(entryF, curF) * 100}%`,
              width: `${Math.abs(curF - entryF) * 100}%`,
            }}
          />
        )}
        {/* entry tick */}
        <div
          className="absolute top-[-2px] h-[9px] w-0.5 bg-foreground/50"
          style={{ left: `${entryF * 100}%` }}
          title={`entry ${p.plan_entry}`}
        />
        {/* current marker */}
        {p.current != null && (
          <div
            className="absolute top-[-3px] h-[11px] w-[3px] rounded-full bg-foreground"
            style={{ left: `calc(${curF * 100}% - 1.5px)` }}
            title={`now ${p.current}`}
          />
        )}
      </div>
      <div className="mt-1.5 flex justify-between text-[11px] text-muted-foreground">
        <span className="text-red-600 dark:text-red-400">stop ${p.plan_stop}</span>
        <span>entry ${p.plan_entry}</span>
        <span className="text-emerald-600 dark:text-emerald-400">target ${p.plan_target}</span>
      </div>
    </div>
  )
}

export function LiveTracker() {
  const [rows, setRows] = useState<LivePick[]>([])
  const [market, setMarket] = useState("")
  const [loaded, setLoaded] = useState(false)

  useEffect(() => {
    let alive = true
    const load = () =>
      api
        .live()
        .then((d) => {
          if (!alive) return
          setRows(d.live)
          setMarket(d.market)
          setLoaded(true)
        })
        .catch(console.error)
    load()
    const t = setInterval(load, 15_000) // refresh every 15s
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [])

  if (loaded && rows.length === 0) {
    return (
      <Card className="text-sm text-muted-foreground">
        No open picks to track. Run <span className="font-medium text-foreground">Find Trades</span> —
        picks with a plan show up here and update live.
      </Card>
    )
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <RefreshCw className="h-3 w-3" />
        <span>
          Live · updates every 15s · market{" "}
          <span className={market === "OPEN" ? "font-medium text-emerald-600 dark:text-emerald-400" : ""}>{market}</span>
        </span>
      </div>

      {groupByDayKey(rows, (p) => p.ts).map((g) => (
        <div key={g.key} className="space-y-3">
          <div className="text-xs font-semibold text-indigo-600 dark:text-indigo-400">
            Chosen {g.label}
          </div>
          {g.items.map((p) => {
            const pos = (p.pnl_pct ?? 0) >= 0
            return (
              <Card
                key={p.id}
                className={`space-y-3 ${p.approved ? "" : "border-border/60 opacity-75"}`}
              >
            {/* just the money: which stock, how much it's making */}
            <div className="flex items-center gap-2.5">
              {dirUp(p.direction) ? (
                <ArrowUp className="h-5 w-5 shrink-0 text-emerald-600 dark:text-emerald-400" />
              ) : (
                <ArrowDown className="h-5 w-5 shrink-0 text-red-600 dark:text-red-400" />
              )}
              <span className="text-lg font-bold">{p.symbol}</span>
              <span className="ml-auto font-mono text-xl font-semibold tabular-nums">
                {p.current != null ? `$${p.current}` : "—"}
                {p.pnl_pct != null && (
                  <span className={`ml-2 ${pos ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400"}`}>
                    {pos ? "+" : ""}
                    {p.pnl_pct}%
                  </span>
                )}
              </span>
            </div>

            <Track p={p} />

            <div className="flex items-center justify-between text-xs text-muted-foreground">
              <span>
                {p.order_type === "limit" ? "limit @ $" + p.plan_entry + " · " : ""}
                {p.status === "pending" ? "fills " : "entered "}
                {etDateTime(p.entry_ts)}
                {p.status === "pending" && (
                  <span className="ml-1 text-amber-600 dark:text-amber-400">· pending</span>
                )}
              </span>
              {p.alpha_so_far != null && (
                <InfoTip
                  tip="Return vs S&P so far, net of friction — a live mark, not the official grade"
                  className={`cursor-help font-semibold ${p.alpha_so_far >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400"}`}
                >
                  vs S&P {p.alpha_so_far >= 0 ? "+" : ""}
                  {p.alpha_so_far}%
                </InfoTip>
              )}
            </div>
              </Card>
            )
          })}
        </div>
      ))}
    </div>
  )
}
