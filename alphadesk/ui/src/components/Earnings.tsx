import type { ReactNode } from "react"
import type { EarningsRow } from "@/lib/api"

function Panel({ title, sub, children }: { title?: string; sub?: string; children: ReactNode }) {
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      {title && (
        <div className="mb-2">
          <div className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            {title}
          </div>
          {sub && <div className="text-[11px] text-muted-foreground">{sub}</div>}
        </div>
      )}
      {children}
    </div>
  )
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
        <Panel title="Just reported" sub="move since the report — the drift so far">
          <div className="flex flex-wrap gap-2">
            {earnings.reported.map((e) => {
              const move = e.move_since_report_pct
              const has = move != null
              const up = (move ?? 0) >= 0
              return (
                <span
                  key={e.symbol}
                  className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background px-2 py-1 text-sm"
                >
                  <span className="font-semibold">{e.symbol}</span>
                  {has ? (
                    <span className={up ? "text-emerald-500" : "text-red-500"}>
                      {up ? "+" : ""}
                      {move}%
                    </span>
                  ) : (
                    <span className="text-muted-foreground">—</span>
                  )}
                  <span className="text-xs text-muted-foreground">
                    {e.report_date.slice(5, 10)}
                    {e.session ? ` ${e.session}` : ""}
                  </span>
                </span>
              )
            })}
          </div>
        </Panel>
      )}

      {earnings.upcoming.length > 0 && (
        <Panel title="Reporting soon" sub="with the time to run the desk to catch the drift">
          <ul className="divide-y divide-border">
            {earnings.upcoming.slice(0, 20).map((e) => (
              <li
                key={e.symbol + e.report_date}
                className="flex items-center gap-2 py-1.5 text-sm"
              >
                <span className="w-14 font-semibold">{e.symbol}</span>
                <span className="text-muted-foreground">
                  {e.report_date.slice(5, 10)} {e.session}
                </span>
                {e.run_at && (
                  <span className="ml-auto text-xs font-medium text-emerald-500">
                    run {e.run_at.slice(5, 10)} · 9:30 ET
                  </span>
                )}
              </li>
            ))}
          </ul>
        </Panel>
      )}
    </div>
  )
}
