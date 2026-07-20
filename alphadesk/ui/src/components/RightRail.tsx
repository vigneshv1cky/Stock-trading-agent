import { useState } from "react"
import type { EarningsRow, Pick, Stats, TokenRow } from "@/lib/api"
import { Ledger } from "@/components/Ledger"
import { Earnings } from "@/components/Earnings"
import { Activity } from "@/components/Activity"
import { LiveTracker } from "@/components/LiveTracker"

type View = "live" | "record" | "calendar" | "usage"

export function RightRail({
  picks,
  stats,
  tokens,
  earnings,
  onSelect,
}: {
  picks: Pick[]
  stats: Stats | null
  tokens: TokenRow[]
  earnings?: { upcoming: EarningsRow[]; reported: EarningsRow[] }
  onSelect: (id: number) => void
}) {
  const [view, setView] = useState<View>("live")
  const tabs: { id: View; label: string }[] = [
    { id: "live", label: "Live" },
    { id: "record", label: "Track record" },
    { id: "calendar", label: "Calendar" },
    { id: "usage", label: "Usage" },
  ]

  return (
    <div className="space-y-4">
      <div className="inline-flex rounded-lg border border-border bg-card p-0.5">
        {tabs.map((t) => (
          <button
            key={t.id}
            onClick={() => setView(t.id)}
            className={`rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
              view === t.id
                ? "bg-indigo-600 text-white"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {view === "live" && <LiveTracker />}
      {view === "record" && <Ledger picks={picks} stats={stats} onSelect={onSelect} />}
      {view === "calendar" && <Earnings earnings={earnings} />}
      {view === "usage" && <Activity tokens={tokens} />}
    </div>
  )
}
