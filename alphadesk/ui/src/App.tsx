import { Fragment, useCallback, useEffect, useState } from "react"
import {
  api,
  fmtAlpha,
  type FunnelWindow,
  type GraphSummary,
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
import { ArrowDown, ArrowUp, Brain, Database, Landmark, Zap } from "lucide-react"

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
  const [graph, setGraph] = useState<GraphSummary | null>(null)
  const [selected, setSelected] = useState<number | null>(null)

  const refresh = useCallback(() => {
    api.picks().then((d) => setPicks(d.picks)).catch(console.error)
    api.stats().then(setStats).catch(console.error)
    api.funnel().then(setFunnel).catch(console.error)
    api.tokens().then((d) => setTokens(d.usage)).catch(console.error)
    api.graph().then(setGraph).catch(console.error)
  }, [])

  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 60_000)
    return () => clearInterval(t)
  }, [refresh])

  const burn = tokens.reduce((a, t) => a + t.output_tok, 0)

  return (
    <div className="mx-auto max-w-6xl space-y-6 p-6">
      <header className="flex items-baseline justify-between">
        <h1 className="text-2xl font-bold tracking-tight">
          AlphaDesk{" "}
          <span className="text-sm font-normal text-muted-foreground">the desk, live</span>
        </h1>
        {funnel?.paused && <Badge variant="destructive">PAUSED: {funnel.paused}</Badge>}
      </header>

      <FindTrades onDone={refresh} />

      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <StatCard
          icon={<Landmark className="h-4 w-4 text-muted-foreground" />}
          label="Decisions"
          value={String(stats?.total.picks ?? "…")}
          sub={`${stats?.total.graded ?? 0} graded`}
        />
        <StatCard
          icon={<Zap className="h-4 w-4 text-muted-foreground" />}
          label="Avg net alpha"
          value={stats?.total.avg_alpha_net != null ? fmtAlpha(stats.total.avg_alpha_net) : "—"}
          sub={stats?.total.wins != null ? `${stats.total.wins} wins` : "awaiting grades"}
        />
        <StatCard
          icon={<Database className="h-4 w-4 text-muted-foreground" />}
          label="World model"
          value={String(graph?.articles ?? "…")}
          sub={`${graph?.relations ?? 0} relations · ${graph?.companies ?? 0} companies`}
        />
        <StatCard
          icon={<Brain className="h-4 w-4 text-muted-foreground" />}
          label="Tokens today"
          value={burn > 0 ? `${Math.round(burn / 1000)}k` : "0"}
          sub={tokens
            .slice(0, 3)
            .map((t) => `${t.role} ${Math.round(t.output_tok / 1000)}k`)
            .join(" · ")}
        />
      </div>

      <section>
        <h2 className="mb-2 text-lg font-semibold">
          Decisions{" "}
          <span className="text-sm font-normal text-muted-foreground">
            click a row to read the agents' conversation
          </span>
        </h2>
        <Card className="py-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>id</TableHead>
                <TableHead>time (UTC)</TableHead>
                <TableHead>symbol</TableHead>
                <TableHead>prediction</TableHead>
                <TableHead>score</TableHead>
                <TableHead>verdict</TableHead>
                <TableHead>arm</TableHead>
                <TableHead>edge</TableHead>
                <TableHead>book</TableHead>
                <TableHead className="text-right">alpha</TableHead>
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
                      {p.direction}
                    </span>{" "}
                    <span className="text-muted-foreground">· {p.horizon_days}d</span>
                  </TableCell>
                  <TableCell>
                    {Math.round(p.score)}
                    {p.adjusted_score != null && ` → ${Math.round(p.adjusted_score)}`}
                  </TableCell>
                  <TableCell>
                    {p.verdict && (
                      <Badge
                        variant={p.verdict === "REJECT" ? "destructive" : "secondary"}
                        className="font-normal"
                      >
                        {p.verdict}
                      </Badge>
                    )}
                  </TableCell>
                  <TableCell className="text-muted-foreground">{p.arm}</TableCell>
                  <TableCell className="text-muted-foreground">{p.edge ?? ""}</TableCell>
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
                        {p.direction === "LONG" ? "Long" : "Short"} for {p.horizon_days} trading day
                        {p.horizon_days === 1 ? "" : "s"}.
                      </span>{" "}
                      {p.triage_reason && (
                        <>
                          <span className="text-foreground/70">Catalyst:</span> {p.triage_reason}{" "}
                        </>
                      )}
                      {why && <span>— {why}</span>}
                    </TableCell>
                  </TableRow>
                )}
                </Fragment>
                )
              })}
              {picks.length === 0 && (
                <TableRow>
                  <TableCell colSpan={10} className="py-8 text-center text-muted-foreground">
                    No decisions yet — the desk convenes when the news deserves it.
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </Card>
      </section>

      <section>
        <h2 className="mb-2 text-lg font-semibold">
          Attention windows{" "}
          <span className="text-sm font-normal text-muted-foreground">
            what triage saw, picked, and skipped — with reasons
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
                      {w.window_ts.slice(5, 16).replace("T", " ")} — <b>{w.picked} picked</b> of{" "}
                      {w.candidates} candidates, {w.skipped} skipped
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
