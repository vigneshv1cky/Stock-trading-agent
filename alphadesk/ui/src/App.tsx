import { Fragment, useCallback, useEffect, useState } from "react"
import {
  api,
  fmtAlpha,
  type EarningsRow,
  type FunnelWindow,
  type Pick,
  type Stats,
  type TokenRow,
} from "@/lib/api"
import { FindTrades } from "@/components/FindTrades"
import { PickSheet } from "@/components/PickSheet"
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { ArrowDown, ArrowUp, Brain, Landmark, Zap } from "lucide-react"
import { dirWord, plainEdge, plainVerdict } from "@/lib/plain"

function StatCard({
  icon,
  label,
  value,
  sub,
}: {
  icon: React.ReactNode
  label: string
  value: string
  sub?: string
}) {
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between pb-1">
        <CardTitle className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
          {label}
        </CardTitle>
        {icon}
      </CardHeader>
      <CardContent>
        <div className="text-2xl font-bold">{value}</div>
        {sub && <p className="text-xs text-muted-foreground">{sub}</p>}
      </CardContent>
    </Card>
  )
}

export default function App() {
  const [picks, setPicks] = useState<Pick[]>([])
  const [stats, setStats] = useState<Stats | null>(null)
  const [funnel, setFunnel] = useState<{ paused: string | null; windows: FunnelWindow[] }>()
  const [tokens, setTokens] = useState<TokenRow[]>([])
  const [earnings, setEarnings] =
    useState<{ upcoming: EarningsRow[]; reported: EarningsRow[] }>()
  const [selected, setSelected] = useState<number | null>(null)

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
  const topRoles = [...tokens].sort(
    (a, b) => b.input_tok + b.output_tok - (a.input_tok + a.output_tok),
  )

  return (
    <div className="mx-auto max-w-6xl space-y-6 p-6">
      <header className="flex items-baseline justify-between">
        <h1 className="text-2xl font-bold tracking-tight">
          AlphaDesk{" "}
          <span className="text-sm font-normal text-muted-foreground">
            your AI stock-research team
          </span>
        </h1>
        {funnel?.paused && <Badge variant="destructive">PAUSED: {funnel.paused}</Badge>}
      </header>

      <Card className="px-4 py-1">
        <Accordion multiple={false}>
          <AccordionItem value="how">
            <AccordionTrigger className="text-sm text-muted-foreground">
              How this works
            </AccordionTrigger>
            <AccordionContent className="space-y-2 text-sm text-muted-foreground">
              <p>
                Click <b>Find Trades</b>. The AI reads today's news and earnings, shortlists a few
                stocks worth a closer look, then a small team debates each one:
              </p>
              <ul className="list-disc space-y-0.5 pl-5">
                <li>
                  <b className="text-foreground">The case</b> — a researcher argues why to buy it
                  (expecting it to rise) or short it (betting it falls)
                </li>
                <li>
                  <b className="text-foreground">The pushback</b> — a critic argues why that's wrong
                </li>
                <li>
                  <b className="text-foreground">The decision</b> — a judge weighs both and rules
                </li>
                <li>
                  <b className="text-foreground">Final call</b> — a head strategist compares the
                  survivors and marks the best ones
                </li>
              </ul>
              <p>
                These are <b>research ideas, not trades</b> — the app never buys anything. Every pick
                is later scored against the S&P 500 so you can see if it was right.
              </p>
            </AccordionContent>
          </AccordionItem>
        </Accordion>
      </Card>

      <FindTrades onDone={refresh} />

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <StatCard
          icon={<Landmark className="h-4 w-4 text-muted-foreground" />}
          label="Ideas checked"
          value={String(stats?.total.picks ?? "…")}
          sub={`${stats?.total.graded ?? 0} scored so far`}
        />
        <StatCard
          icon={<Zap className="h-4 w-4 text-muted-foreground" />}
          label="Avg vs S&P 500"
          value={stats?.total.avg_alpha_net != null ? fmtAlpha(stats.total.avg_alpha_net) : "—"}
          sub={stats?.total.wins != null ? `${stats.total.wins} winners` : "waiting for results"}
        />
        <StatCard
          icon={<Brain className="h-4 w-4 text-muted-foreground" />}
          label="AI usage today"
          value={burn > 0 ? `${Math.round(burn / 1000)}k` : "0"}
          sub={topRoles
            .slice(0, 3)
            .map((t) => `${t.role} ${Math.round((t.input_tok + t.output_tok) / 1000)}k`)
            .join(" · ")}
        />
      </div>

      {earnings && (earnings.reported.length > 0 || earnings.upcoming.length > 0) && (
        <Card className="py-3">
          <CardContent className="space-y-2 py-2 text-sm">
            {earnings.reported.length > 0 && (
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                  Just reported
                </span>
                {earnings.reported.map((e) => (
                  <Badge
                    key={e.symbol}
                    variant="secondary"
                    className={(e.surprise_pct ?? 0) >= 0 ? "text-green-500" : "text-red-500"}
                  >
                    {e.symbol} {(e.surprise_pct ?? 0) >= 0 ? "+" : ""}
                    {e.surprise_pct}%
                  </Badge>
                ))}
              </div>
            )}
            {earnings.upcoming.length > 0 && (
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                  Reporting soon
                </span>
                {earnings.upcoming.slice(0, 12).map((e) => (
                  <span key={e.symbol + e.report_date} className="text-muted-foreground">
                    <span className="font-medium text-foreground">{e.symbol}</span>{" "}
                    {e.report_date.slice(5, 10)} {e.session}
                    {e.run_at && (
                      <span className="text-emerald-500"> → run {e.run_at.slice(5, 10)} 9:30 ET</span>
                    )}
                  </span>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      )}

      <section>
        <h2 className="mb-2 text-lg font-semibold">
          Ideas{" "}
          <span className="text-sm font-normal text-muted-foreground">
            click any row to see the reasoning behind it
          </span>
        </h2>
        <Card className="py-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>#</TableHead>
                <TableHead>when</TableHead>
                <TableHead>stock</TableHead>
                <TableHead>call</TableHead>
                <TableHead>confidence</TableHead>
                <TableHead>decision</TableHead>
                <TableHead>by</TableHead>
                <TableHead>why</TableHead>
                <TableHead>acted?</TableHead>
                <TableHead className="text-right">vs S&P</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {picks.map((p) => {
                const why = p.debate?.arbiter_summary ?? p.thesis
                return (
                <Fragment key={p.id}>
                <TableRow className="cursor-pointer border-0" onClick={() => setSelected(p.id)}>
                  <TableCell className="text-muted-foreground">#{p.id}</TableCell>
                  <TableCell className="text-muted-foreground">
                    {p.ts.slice(5, 16).replace("T", " ")}
                  </TableCell>
                  <TableCell className="font-bold">{p.symbol}</TableCell>
                  <TableCell>
                    <span
                      className={`inline-flex items-center gap-1 font-medium ${
                        p.direction === "LONG" ? "text-green-500" : "text-red-500"
                      }`}
                    >
                      {p.direction === "LONG" ? (
                        <ArrowUp className="h-3.5 w-3.5" />
                      ) : (
                        <ArrowDown className="h-3.5 w-3.5" />
                      )}
                      {dirWord(p.direction)}
                    </span>{" "}
                    <span className="text-muted-foreground">· hold ~{p.horizon_days}d</span>
                  </TableCell>
                  <TableCell>
                    {Math.round(p.adjusted_score ?? p.score)}/100
                  </TableCell>
                  <TableCell>
                    {p.verdict && (
                      <Badge
                        variant={p.verdict === "PASS" ? "destructive" : "secondary"}
                        className="font-normal"
                      >
                        {plainVerdict(p.verdict)}
                      </Badge>
                    )}
                  </TableCell>
                  <TableCell className="text-muted-foreground">
                    {p.arm === "LONER" ? "Solo" : "Team"}
                  </TableCell>
                  <TableCell className="text-muted-foreground">{plainEdge(p.edge)}</TableCell>
                  <TableCell>{p.approved ? "✅" : "❌"}</TableCell>
                  <TableCell
                    className={`text-right font-medium ${
                      p.alpha_net == null
                        ? "text-muted-foreground"
                        : p.alpha_net > 0
                          ? "text-green-500"
                          : "text-red-500"
                    }`}
                  >
                    {fmtAlpha(p.alpha_net)}
                  </TableCell>
                </TableRow>
                {(p.triage_reason || why) && (
                  <TableRow
                    className="cursor-pointer hover:bg-muted/30"
                    onClick={() => setSelected(p.id)}
                  >
                    <TableCell />
                    <TableCell colSpan={9} className="pt-0 align-top text-xs leading-snug text-muted-foreground">
                      <span className="font-medium text-foreground">
                        {dirWord(p.direction)} · hold ~{p.horizon_days} days
                      </span>
                      {p.triage_reason && (
                        <>
                          {" · "}
                          <span className="text-foreground/70">Why:</span> {p.triage_reason}
                        </>
                      )}
                      {why && (
                        <>
                          {" · "}
                          <span className="text-foreground/70">Takeaway:</span> {why}
                        </>
                      )}
                    </TableCell>
                  </TableRow>
                )}
                </Fragment>
                )
              })}
              {picks.length === 0 && (
                <TableRow>
                  <TableCell colSpan={10} className="py-8 text-center text-muted-foreground">
                    No ideas yet — click "Find Trades" to scan the market.
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </Card>
      </section>

      <section>
        <h2 className="mb-2 text-lg font-semibold">
          What it looked at{" "}
          <span className="text-sm font-normal text-muted-foreground">
            the stocks it considered and skipped each scan — with reasons
          </span>
        </h2>
        <Card className="px-4 py-1">
          <Accordion multiple={false}>
            {(funnel?.windows ?? []).map((w) => {
              let skips: { symbol: string; reason: string }[] = []
              try {
                skips = JSON.parse(w.skip_reasons ?? "[]")
              } catch {
                /* ignore */
              }
              return (
                <AccordionItem key={w.id} value={String(w.id)}>
                  <AccordionTrigger className="text-sm">
                    <span>
                      {w.window_ts.slice(5, 16).replace("T", " ")} — <b>{w.picked} looked into</b> of{" "}
                      {w.candidates} stocks, {w.skipped} skipped
                    </span>
                  </AccordionTrigger>
                  <AccordionContent>
                    <ul className="list-disc space-y-1 pl-5 text-sm text-muted-foreground">
                      {skips.map((s, i) => (
                        <li key={i}>
                          <b className="text-foreground">{s.symbol}</b>: {s.reason}
                        </li>
                      ))}
                      {skips.length === 0 && <li>no skips recorded</li>}
                    </ul>
                  </AccordionContent>
                </AccordionItem>
              )
            })}
          </Accordion>
        </Card>
      </section>

      <PickSheet pickId={selected} onClose={() => setSelected(null)} />
    </div>
  )
}
