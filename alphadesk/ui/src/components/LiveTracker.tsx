import { useEffect, useState } from "react"
import { api, exitDate, type LivePick } from "@/lib/api"
import { dirUp, dirWord, plainEdge } from "@/lib/plain"
import { ArrowDown, ArrowUp, RefreshCw } from "lucide-react"

const STATUS: Record<string, { label: string; cls: string }> = {
  "target hit": { label: "Target hit", cls: "bg-emerald-600 text-white" },
  "near target": { label: "Near target", cls: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400" },
  working: { label: "Working", cls: "bg-muted text-muted-foreground" },
  "near stop": { label: "Near stop", cls: "bg-amber-500/15 text-amber-600 dark:text-amber-400" },
  "stopped out": { label: "Stopped out", cls: "bg-red-600 text-white" },
  "no quote": { label: "No quote", cls: "bg-muted text-muted-foreground" },
}

// Where the price sits on the stop→target track, plus the entry tick.
function Track({ p }: { p: LivePick }) {
  const up = dirUp(p.direction)
  const span = up ? p.plan_target - p.plan_stop : p.plan_stop - p.plan_target
  const frac = (v: number) =>
    span === 0 ? 0 : Math.max(0, Math.min(1, up ? (v - p.plan_stop) / span : (p.plan_stop - v) / span))
  const curF = p.progress ?? (p.current != null ? frac(p.current) : 0)
  const entryF = frac(p.plan_entry)
  const pos = (p.pnl_pct ?? 0) >= 0
  return (
    <div>
      <div className="relative mt-2 h-1.5 rounded-full bg-muted">
        {p.current != null && (
          <div
            className={`absolute top-0 h-1.5 ${pos ? "bg-emerald-500" : "bg-red-500"}`}
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
      <div className="mt-1 flex justify-between text-[10px] text-muted-foreground">
        <span className="text-red-500">stop ${p.plan_stop}</span>
        <span>entry ${p.plan_entry}</span>
        <span className="text-emerald-500">target ${p.plan_target}</span>
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
      <div className="rounded-lg border border-border bg-card p-4 text-sm text-muted-foreground">
        No open picks to track. Run <span className="font-medium text-foreground">Find Trades</span> —
        picks with a plan show up here and update live.
      </div>
    )
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <RefreshCw className="h-3 w-3" />
        <span>
          Live · updates every 15s · market{" "}
          <span className={market === "OPEN" ? "font-medium text-emerald-500" : ""}>{market}</span>
        </span>
      </div>

      {rows.map((p) => {
        const st = STATUS[p.status] ?? STATUS["no quote"]
        const pos = (p.pnl_pct ?? 0) >= 0
        return (
          <div
            key={p.id}
            className={`rounded-lg border bg-card p-3 ${
              p.approved ? "border-border" : "border-border/60 opacity-80"
            }`}
          >
            <div className="flex flex-wrap items-center gap-2 text-sm">
              {dirUp(p.direction) ? (
                <ArrowUp className="h-4 w-4 text-emerald-500" />
              ) : (
                <ArrowDown className="h-4 w-4 text-red-500" />
              )}
              <span className={`font-bold ${dirUp(p.direction) ? "text-emerald-500" : "text-red-500"}`}>
                {dirWord(p.direction)}
              </span>
              <span className="font-bold">{p.symbol}</span>
              <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                {plainEdge(p.edge)}
              </span>
              <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold ${st.cls}`}>
                {st.label}
              </span>
              <span className="ml-auto font-mono text-sm tabular-nums">
                {p.current != null ? `$${p.current}` : "—"}{" "}
                {p.pnl_pct != null && (
                  <span className={pos ? "text-emerald-500" : "text-red-500"}>
                    ({pos ? "+" : ""}
                    {p.pnl_pct}%)
                  </span>
                )}
              </span>
            </div>

            <Track p={p} />

            <div className="mt-1.5 text-[11px] text-muted-foreground">
              hold ~{p.horizon_days}d · through {exitDate(p.ts, p.session, p.horizon_days)}
              {!p.approved && " · thin lean"}
            </div>
            {p.plan_note && <p className="mt-1 text-xs text-muted-foreground">{p.plan_note}</p>}
          </div>
        )
      })}
    </div>
  )
}
