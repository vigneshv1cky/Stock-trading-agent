import { useState, type ReactNode } from "react"
import { ChevronRight } from "lucide-react"
import type { EarningsRow } from "@/lib/api"

function Panel({
  title,
  sub,
  children,
  collapsible = false,
  defaultOpen = true,
  count,
}: {
  title?: string
  sub?: string
  children: ReactNode
  collapsible?: boolean
  defaultOpen?: boolean
  count?: number
}) {
  const [open, setOpen] = useState(defaultOpen)
  const show = !collapsible || open
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      {title &&
        (collapsible ? (
          <button
            onClick={() => setOpen((o) => !o)}
            className="flex w-full items-center gap-2 text-left"
            aria-expanded={open}
          >
            <ChevronRight
              className={`h-3.5 w-3.5 shrink-0 text-muted-foreground transition-transform ${
                open ? "rotate-90" : ""
              }`}
            />
            <div className={`min-w-0 flex-1 ${show ? "mb-2" : ""}`}>
              <div className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                {title}
                {count != null && (
                  <span className="ml-1.5 font-normal normal-case text-muted-foreground">
                    ({count})
                  </span>
                )}
              </div>
              {sub && open && <div className="text-[11px] text-muted-foreground">{sub}</div>}
            </div>
          </button>
        ) : (
          <div className="mb-2">
            <div className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              {title}
            </div>
            {sub && <div className="text-[11px] text-muted-foreground">{sub}</div>}
          </div>
        ))}
      {show && children}
    </div>
  )
}

function fmtCap(v?: number | null): string {
  if (v == null) return ""
  if (v >= 1e12) return `$${(v / 1e12).toFixed(1)}T`
  if (v >= 1e9) return `$${(v / 1e9).toFixed(0)}B`
  if (v >= 1e6) return `$${(v / 1e6).toFixed(0)}M`
  return `$${Math.round(v)}`
}

function dayLabel(day: string): string {
  const d = new Date(`${day}T12:00:00`) // noon avoids TZ date-rollover
  const wd = d.toLocaleDateString("en-US", { weekday: "short" })
  return `${wd} ${day.slice(5)}`
}

type DayGroup = { day: string; rows: EarningsRow[] }

// Rows arrive pre-sorted, so a single pass yields contiguous day-groups (biggest
// names first inside each). `key` picks the grouping day: run-day for upcoming,
// report-day for just-reported.
function groupByDay(rows: EarningsRow[], key: (e: EarningsRow) => string): DayGroup[] {
  const groups: DayGroup[] = []
  for (const e of rows) {
    const day = key(e)
    let g = groups[groups.length - 1]
    if (!g || g.day !== day) {
      g = { day, rows: [] }
      groups.push(g)
    }
    g.rows.push(e)
  }
  return groups
}

export function Earnings({
  earnings,
}: {
  earnings?: { upcoming: EarningsRow[]; reported: EarningsRow[] }
}) {
  if (!earnings || (earnings.reported.length === 0 && earnings.upcoming.length === 0)) {
    return (
      <Panel>
        <p className="text-sm text-muted-foreground">
          No earnings on the calendar yet — it refreshes a few times a day.
        </p>
      </Panel>
    )
  }

  return (
    <div className="space-y-3">
      {earnings.reported.length > 0 && (
        <Panel
          title="Just reported"
          sub="grouped by report day — move since the report (the drift so far)"
          collapsible
          defaultOpen={false}
          count={earnings.reported.length}
        >
          <div className="mb-2 flex items-center gap-2 border-b border-border pb-1.5 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
            <span className="w-16">Symbol</span>
            <span className="w-14">Cap</span>
            <span className="w-10">Session</span>
            <span className="ml-auto">Move</span>
          </div>
          <div className="space-y-3">
            {groupByDay(earnings.reported, (e) => e.report_date.slice(0, 10)).map((g) => (
              <div key={g.day}>
                <div className="mb-1 flex items-baseline justify-between">
                  <span className="text-xs font-semibold text-indigo-500">
                    Reported {dayLabel(g.day)}
                  </span>
                  <span className="text-[11px] text-muted-foreground">{g.rows.length} names</span>
                </div>
                <ul className="divide-y divide-border">
                  {g.rows.map((e) => {
                    const move = e.move_since_report_pct
                    const has = move != null
                    const up = (move ?? 0) >= 0
                    return (
                      <li
                        key={e.symbol + e.report_date}
                        className="flex items-center gap-2 py-1.5 text-sm"
                      >
                        <span className="w-16 font-semibold">{e.symbol}</span>
                        <span className="w-14 text-xs text-muted-foreground">
                          {fmtCap(e.market_cap)}
                        </span>
                        <span className="w-10 text-xs text-muted-foreground">{e.session}</span>
                        <span
                          className={`ml-auto font-mono tabular-nums ${
                            has ? (up ? "text-emerald-500" : "text-red-500") : "text-muted-foreground"
                          }`}
                        >
                          {has ? `${up ? "+" : ""}${move}%` : "—"}
                        </span>
                      </li>
                    )
                  })}
                </ul>
              </div>
            ))}
          </div>
        </Panel>
      )}

      {earnings.upcoming.length > 0 && (
        <Panel title="Reporting soon" sub="grouped by when to run the desk — biggest names first">
          <div className="mb-2 flex items-center gap-2 border-b border-border pb-1.5 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
            <span className="w-14">Symbol</span>
            <span className="w-14">Cap</span>
            <span className="ml-auto">Report</span>
          </div>
          <div className="space-y-3">
            {groupByDay(earnings.upcoming, (e) => (e.run_at ?? "").slice(0, 10) || "—").map((g) => {
              const shown = g.rows.slice(0, 8)
              const more = g.rows.length - shown.length
              return (
                <div key={g.day}>
                  <div className="mb-1 text-xs font-semibold text-emerald-500">
                    {g.day === "—" ? "Run time n/a" : `Run ${dayLabel(g.day)} · 9:30 ET`}
                  </div>
                  <ul className="divide-y divide-border">
                    {shown.map((e) => (
                      <li
                        key={e.symbol + e.report_date}
                        className="flex items-center gap-2 py-1.5 text-sm"
                      >
                        <span className="w-14 font-semibold">{e.symbol}</span>
                        <span className="w-14 text-xs text-muted-foreground">
                          {fmtCap(e.market_cap)}
                        </span>
                        <span className="ml-auto text-xs text-muted-foreground">
                          {e.report_date.slice(5, 10)} {e.session}
                        </span>
                      </li>
                    ))}
                  </ul>
                  {more > 0 && (
                    <div className="mt-1 text-xs text-muted-foreground">+{more} more</div>
                  )}
                </div>
              )
            })}
          </div>
        </Panel>
      )}
    </div>
  )
}
