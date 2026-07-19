import { useCallback, useEffect, useState } from "react"
import {
  api,
  fmtAlpha,
  type EarningsRow,
  type FunnelWindow,
  type Pick,
  type Stats,
  type TokenRow,
} from "@/lib/api"
import { useTheme } from "@/lib/theme"
import { FindTrades } from "@/components/FindTrades"
import { PickSheet } from "@/components/PickSheet"
import { RightRail } from "@/components/RightRail"
import { Badge } from "@/components/ui/badge"
import { Moon, Sun } from "lucide-react"

function Kpi({ label, value, tone }: { label: string; value: string; tone?: number | null }) {
  const color = tone == null ? "" : tone > 0 ? "text-emerald-500" : tone < 0 ? "text-red-500" : ""
  return (
    <div className="hidden text-right sm:block">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className={`font-mono text-sm font-semibold tabular-nums ${color}`}>{value}</div>
    </div>
  )
}

export default function App() {
  const [picks, setPicks] = useState<Pick[]>([])
  const [stats, setStats] = useState<Stats | null>(null)
  const [funnel, setFunnel] = useState<{ paused: string | null; windows: FunnelWindow[] }>()
  const [tokens, setTokens] = useState<TokenRow[]>([])
  const [earnings, setEarnings] = useState<{ upcoming: EarningsRow[]; reported: EarningsRow[] }>()
  const [selected, setSelected] = useState<number | null>(null)
  const [live, setLive] = useState(false)
  const [theme, toggleTheme] = useTheme()

  const refresh = useCallback(() => {
    api.picks().then((d) => setPicks(d.picks)).catch(console.error)
    api.stats().then(setStats).catch(console.error)
    api.funnel().then(setFunnel).catch(console.error)
    api.tokens().then((d) => setTokens(d.usage)).catch(console.error)
    api.earnings().then(setEarnings).catch(console.error)
  }, [])

  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 60_000)
    return () => clearInterval(t)
  }, [refresh])

  const burn = tokens.reduce((a, t) => a + t.input_tok + t.output_tok, 0)
  const graded = stats?.total.graded ?? 0
  const winRate =
    graded > 0 && stats?.total.wins != null ? Math.round((stats.total.wins / graded) * 100) : null
  const avg = stats?.total.avg_alpha_net ?? null

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-background text-foreground">
      <header className="z-30 shrink-0 border-b border-border bg-background/85 backdrop-blur">
        <div className="mx-auto flex max-w-[1400px] items-center gap-4 px-5 py-3">
          <div className="flex items-center gap-2.5">
            <span className="h-3.5 w-3.5 rotate-45 rounded-[3px] bg-indigo-500" />
            <div className="leading-none">
              <div className="text-sm font-semibold tracking-tight">AlphaDesk</div>
              <div className="mt-0.5 text-[11px] text-muted-foreground">AI stock-research desk</div>
            </div>
          </div>

          <div
            className={`ml-2 flex items-center gap-1.5 text-xs font-medium ${
              live ? "text-emerald-500" : "text-muted-foreground"
            }`}
          >
            <span
              className={`h-2 w-2 rounded-full ${
                live ? "animate-pulse bg-emerald-500" : "bg-muted-foreground/40"
              }`}
            />
            {live ? "running" : "idle"}
          </div>

          <div className="ml-auto flex items-center gap-5">
            <Kpi label="Ideas" value={String(stats?.total.picks ?? "—")} />
            <Kpi label="Beat S&P" value={winRate != null ? `${winRate}%` : "—"} />
            <Kpi label="Avg vs S&P" value={avg != null ? fmtAlpha(avg) : "—"} tone={avg} />
            <Kpi label="AI today" value={burn > 0 ? `${Math.round(burn / 1000)}k` : "0"} />
            {funnel?.paused && <Badge variant="destructive">PAUSED</Badge>}
            <button
              onClick={toggleTheme}
              aria-label="Toggle light / dark"
              className="grid h-8 w-8 place-items-center rounded-md border border-border text-muted-foreground transition-colors hover:text-foreground"
            >
              {theme === "dark" ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
            </button>
          </div>
        </div>
      </header>

      {/* App shell: on desktop the two columns each scroll independently and the
          window itself doesn't scroll; on mobile they stack and scroll normally. */}
      <main className="min-h-0 flex-1 overflow-y-auto lg:overflow-hidden">
        <div className="mx-auto grid max-w-[1400px] grid-cols-1 gap-5 px-5 py-5 lg:h-full lg:grid-cols-[minmax(0,440px)_minmax(0,1fr)] lg:py-0">
          <div className="min-w-0 lg:min-h-0 lg:overflow-y-auto lg:py-5 lg:pr-2">
            <FindTrades onDone={refresh} onRunningChange={setLive} />
          </div>
          <div className="min-w-0 lg:min-h-0 lg:overflow-y-auto lg:py-5 lg:pr-2">
            <RightRail
              picks={picks}
              stats={stats}
              funnel={funnel}
              tokens={tokens}
              earnings={earnings}
              onSelect={setSelected}
            />
          </div>
        </div>
      </main>

      <PickSheet pickId={selected} onClose={() => setSelected(null)} />
    </div>
  )
}
