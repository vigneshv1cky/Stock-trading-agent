import { useRef, useState } from "react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { ArrowDown, ArrowUp, Loader2, Search } from "lucide-react"
import { dirUp, dirWord, plainEdge, plainVerdict } from "@/lib/plain"
import type { Plan } from "@/lib/api"

// Streamed events (loosely typed — they arrive as JSON off the SSE feed).
interface Ev {
  type: string
  msg?: string
  symbol?: string
  edge?: string
  reason?: string
  kind?: string
  summary?: string
  direction?: string
  horizon_days?: number
  score?: number
  claim?: string
  evidence?: string
  text?: string
  revised_score?: number
  concede?: boolean
  id?: number
  conviction?: number
  confidence?: number
  verdict?: string
  approved?: boolean
  board?: BoardRow[]
  skips?: { symbol: string; reason: string }[]
  shock?: string
  strength?: string
  chain?: string
  entry?: number
  now?: number
  target?: number
  stop?: number
  hold?: string
  note?: string
  stance?: string
  counter_direction?: string
  counter?: string
  proposed_from?: string
  flipped?: boolean
}

interface BoardRow {
  id: number
  symbol: string
  direction: string
  horizon_days: number
  edge: string | null
  conviction: number
  confidence: number
  verdict: string
  approved: boolean
  summary: string
  take?: boolean
  chief_reason?: string
  flipped?: boolean
  plan?: Plan | null
}

const ACCENT: Record<string, string> = {
  exposure_shock: "border-l-cyan-500",
  exposure_candidate: "border-l-cyan-500",
  triage_pick: "border-l-yellow-500",
  gate: "border-l-zinc-400 opacity-70",
  brief: "border-l-zinc-400 dark:border-l-zinc-500",
  thesis: "border-l-blue-500",
  concern: "border-l-red-500",
  counter: "border-l-fuchsia-500",
  fact_flag: "border-l-orange-500",
  rebuttal: "border-l-blue-500",
  decision: "border-l-emerald-500",
  plan: "border-l-indigo-500",
}

// "Buy at 253.80 · target 380 · stop 359.50 · multi-day" — the actionable levels.
function PlanLine({ plan, direction }: { plan: Plan; direction?: string }) {
  const action = dirUp(direction) ? "Buy" : "Short"
  return (
    <div className="mt-1.5 rounded-md bg-muted/50 px-2 py-1.5 text-xs">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
        <span className="font-semibold">
          {action} ${plan.entry}
        </span>
        <span className="text-muted-foreground">·</span>
        <span>
          target <span className="font-medium text-emerald-600 dark:text-emerald-400">${plan.target}</span>
        </span>
        <span className="text-muted-foreground">·</span>
        <span>
          stop <span className="font-medium text-red-600 dark:text-red-400">${plan.stop}</span>
        </span>
        <span className="text-muted-foreground">· {plan.hold}</span>
      </div>
      <p className="mt-1 text-muted-foreground">{plan.note}</p>
    </div>
  )
}

function Tag({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return (
    <span className={`text-[10px] font-semibold uppercase tracking-wider ${className}`}>
      {children}
    </span>
  )
}

function Line({ ev }: { ev: Ev }) {
  const cls = `rounded-md border border-l-4 ${ACCENT[ev.type] ?? "border-l-border"} bg-card p-2.5 text-sm`
  switch (ev.type) {
    case "exposure_shock":
      return (
        <div className={cls}>
          <Tag className="text-cyan-600 dark:text-cyan-400">Looking for companies affected by {ev.symbol}</Tag>
        </div>
      )
    case "exposure_candidate":
      return (
        <div className={cls}>
          <Tag className="text-cyan-600 dark:text-cyan-400">
            Knock-on: {ev.shock} → {ev.symbol}
          </Tag>{" "}
          <span className={dirUp(ev.direction) ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400"}>
            {dirWord(ev.direction)}
          </span>{" "}
          <Badge variant="secondary">{ev.strength}</Badge>
          <p className="mt-1 text-muted-foreground">{ev.chain}</p>
        </div>
      )
    case "triage_pick":
      return (
        <div className={cls}>
          <Tag className="text-yellow-600 dark:text-yellow-400">Shortlisted {ev.symbol}</Tag>{" "}
          <Badge variant="secondary" className="ml-1">
            {plainEdge(ev.edge)}
          </Badge>
          <p className="mt-1 text-muted-foreground">{ev.reason}</p>
        </div>
      )
    case "gate":
      return (
        <div className={cls}>
          <Tag className="text-muted-foreground">Gated out · {ev.symbol}</Tag>
          <p className="mt-1 text-muted-foreground">
            no verifiable catalyst — skipped before the debate. {ev.reason}
          </p>
        </div>
      )
    case "brief":
      return (
        <div className={cls}>
          <Tag className="text-muted-foreground">
            {ev.kind} note · {ev.symbol}
          </Tag>
          <p className="mt-1">{ev.summary}</p>
        </div>
      )
    case "thesis":
      return (
        <div className={cls}>
          <Tag className="text-blue-600 dark:text-blue-400">The case for {ev.symbol}</Tag>
          <p className="mt-1">
            <b className={dirUp(ev.direction) ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400"}>
              {dirWord(ev.direction)}
            </b>{" "}
            · hold ~{ev.horizon_days} days · confidence {ev.score}/100
          </p>
        </div>
      )
    case "concern":
      return (
        <div className={cls}>
          <Tag className="text-red-600 dark:text-red-400">The pushback · {ev.symbol}</Tag>
          <p className="mt-1 font-medium">{ev.claim}</p>
          <p className="text-muted-foreground">{ev.evidence}</p>
        </div>
      )
    case "counter":
      return (
        <div className={cls}>
          <Tag className="text-fuchsia-600 dark:text-fuchsia-400">
            {ev.stance === "FLIP" ? `Critic reverses the call · ${ev.symbol}` : `Critic: stand aside · ${ev.symbol}`}
          </Tag>
          {ev.stance === "FLIP" && (
            <p className="mt-1">
              <span className="text-muted-foreground line-through">{dirWord(ev.proposed_from)}</span>{" "}
              →{" "}
              <b className={dirUp(ev.counter_direction) ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400"}>
                {dirWord(ev.counter_direction)}
              </b>
            </p>
          )}
          <p className="mt-1 text-muted-foreground">{ev.counter}</p>
        </div>
      )
    case "fact_flag":
      return (
        <div className={cls}>
          <Tag className="text-orange-600 dark:text-orange-400">Fact-check</Tag>
          <p className="mt-1">{ev.text}</p>
        </div>
      )
    case "rebuttal":
      return (
        <div className={cls}>
          <Tag className="text-blue-600 dark:text-blue-400">Researcher's reply · {ev.symbol}</Tag>
          <p className="mt-1">
            updated confidence {ev.revised_score}/100 · agreed with the pushback:{" "}
            {ev.concede ? "yes" : "no"}
          </p>
        </div>
      )
    case "decision":
      return (
        <div className={cls}>
          <Tag className="text-emerald-600 dark:text-emerald-400">Decision · {ev.symbol}</Tag>
          <p className="mt-1">
            {ev.approved ? "✅ Conviction call" : "◦ Thin lean (tracked)"} ·{" "}
            {plainVerdict(ev.verdict)} · confidence {ev.conviction}/100
            {ev.flipped && <span className="text-fuchsia-600 dark:text-fuchsia-400"> · reversed by critic</span>}
          </p>
          <p className="text-muted-foreground">{ev.summary}</p>
        </div>
      )
    case "plan":
      return (
        <div className={cls}>
          <Tag className="text-indigo-600 dark:text-indigo-400">Trade plan · {ev.symbol}</Tag>
          {ev.entry != null && ev.target != null && ev.stop != null && (
            <PlanLine
              plan={{
                entry: ev.entry,
                target: ev.target,
                stop: ev.stop,
                note: ev.note ?? "",
                hold: ev.hold ?? "",
              }}
              direction={ev.direction}
            />
          )}
        </div>
      )
    default:
      return null
  }
}

export function FindTrades({
  onDone,
  onRunningChange,
}: {
  onDone: () => void
  onRunningChange?: (running: boolean) => void
}) {
  const [running, setRunning] = useState(false)
  const [status, setStatus] = useState("")
  const [feed, setFeed] = useState<Ev[]>([])
  const [board, setBoard] = useState<BoardRow[] | null>(null)
  const [chief, setChief] = useState("")
  const [positions, setPositions] = useState<Ev[]>([])
  const [deep, setDeep] = useState(false)
  const esRef = useRef<EventSource | null>(null)

  function setRun(b: boolean) {
    setRunning(b)
    onRunningChange?.(b)
  }

  function run() {
    setRun(true)
    setFeed([])
    setBoard(null)
    setChief("")
    setPositions([])
    setStatus("Starting…")
    const es = new EventSource(`/api/find-trades?hours=24&max_debates=6&expose=${deep}`)
    esRef.current = es
    es.onmessage = (e) => {
      const ev: Ev = JSON.parse(e.data)
      if (ev.type === "status") setStatus(ev.msg ?? "")
      else if (ev.type === "chief") {
        setChief(ev.summary ?? "")
        setBoard(ev.board ?? [])
      } else if (ev.type === "done") {
        setBoard(ev.board ?? [])
        setRun(false)
        es.close()
        onDone()
      } else if (ev.type === "position_exit" || ev.type === "position_hold") {
        setPositions((p) => [...p, ev])
      } else {
        setFeed((f) => [...f, ev])
      }
    }
    es.onerror = () => {
      setStatus("stream closed")
      setRun(false)
      es.close()
    }
  }

  const takes = board?.filter((b) => b.take).length ?? 0

  return (
    <Card className="overflow-hidden">
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="flex items-center gap-2">
              <h2 className="text-base font-semibold tracking-tight">Find Trades</h2>
              {running && (
                <span className="flex items-center gap-1 text-[11px] font-medium text-emerald-600 dark:text-emerald-400">
                  <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-500" /> live
                </span>
              )}
            </div>
            <p className="mt-0.5 text-xs text-muted-foreground">
              Your research team scans the news and debates the best ideas, live.
            </p>
          </div>
          <Button
            onClick={run}
            disabled={running}
            size="lg"
            className="h-9 bg-indigo-600 px-4 text-sm text-white hover:bg-indigo-500"
          >
            {running ? (
              <>
                <Loader2 className="mr-1.5 h-4 w-4 animate-spin" /> Scanning…
              </>
            ) : (
              <>
                <Search className="mr-1.5 h-4 w-4" /> Run
              </>
            )}
          </Button>
        </div>

        <label className="mt-3 flex items-center gap-2 text-xs text-muted-foreground">
          <input
            type="checkbox"
            checked={deep}
            disabled={running}
            onChange={(e) => setDeep(e.target.checked)}
            className="accent-indigo-500"
          />
          Deep scan — also map supply-chain ripples (slower, uses more)
        </label>

        <details className="mt-3 text-xs text-muted-foreground">
          <summary className="cursor-pointer select-none">How this works</summary>
          <div className="mt-1.5 space-y-1 border-l-2 border-border pl-3">
            <p>
              A <b className="text-foreground">researcher</b> argues the case, a{" "}
              <b className="text-foreground">critic</b> pushes back, a{" "}
              <b className="text-foreground">judge</b> rules, and a{" "}
              <b className="text-foreground">head</b> ranks the survivors.
            </p>
            <p>
              These are <b className="text-foreground">research ideas, not trades</b> — nothing is
              bought. Every idea is later scored against the S&amp;P 500.
            </p>
          </div>
        </details>

        {status && (
          <div className="mt-3 flex items-center gap-2 rounded-md bg-muted/50 px-3 py-2 text-sm text-muted-foreground">
            {running && <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" />}
            <span>{status}</span>
          </div>
        )}

        {positions.length > 0 && (
          <div className="mt-4 space-y-2">
            <div className="text-sm font-semibold">
              Your open picks
              <span className="ml-1 font-normal text-muted-foreground">
                — re-checked ({positions.filter((p) => p.type === "position_exit").length} to sell)
              </span>
            </div>
            {positions.map((p, i) => {
              const exit = p.type === "position_exit"
              return (
                <div
                  key={i}
                  className={`rounded-md border border-l-4 bg-card p-2.5 text-sm ${
                    exit ? "border-l-red-500" : "border-l-emerald-600"
                  }`}
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge className={exit ? "bg-red-600 text-white" : "bg-emerald-600 text-white"}>
                      {exit ? "Sell now" : "Keep"}
                    </Badge>
                    <span className={dirUp(p.direction) ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400"}>
                      {dirWord(p.direction)}
                    </span>
                    <span className="font-bold">{p.symbol}</span>
                    <span className="text-muted-foreground">~{p.horizon_days}-day pick</span>
                    {exit && p.entry != null && p.now != null && (
                      <span className="text-muted-foreground">
                        · {p.entry} → {p.now}
                      </span>
                    )}
                  </div>
                  <p className="mt-1 text-muted-foreground">{p.reason}</p>
                </div>
              )
            })}
          </div>
        )}

        {board && (
          <div className="mt-4 space-y-2">
            {chief && (
              <div className="rounded-md border border-l-4 border-l-amber-500 bg-amber-500/5 p-3">
                <Tag className="text-amber-600 dark:text-amber-400">Final call — comparing all the ideas</Tag>
                <p className="mt-1 text-sm">{chief}</p>
              </div>
            )}
            <div className="text-sm font-semibold">
              Best ideas{" "}
              <span className="font-normal text-muted-foreground">
                ({takes} worth acting on)
              </span>
            </div>
            {board.map((r, i) => (
              <div
                key={r.id}
                className={`rounded-md border p-2.5 text-sm ${
                  r.take ? "border-emerald-600/60 bg-emerald-500/5" : "opacity-70"
                }`}
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-muted-foreground">#{i + 1}</span>
                  {r.direction === "LONG" ? (
                    <ArrowUp className="h-4 w-4 text-emerald-600 dark:text-emerald-400" />
                  ) : (
                    <ArrowDown className="h-4 w-4 text-red-600 dark:text-red-400" />
                  )}
                  <span
                    className={`font-bold ${
                      dirUp(r.direction) ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400"
                    }`}
                  >
                    {dirWord(r.direction)}
                  </span>
                  <span className="font-bold">{r.symbol}</span>
                  {r.flipped && (
                    <Badge className="bg-fuchsia-600 text-white">reversed</Badge>
                  )}
                  <Badge variant="secondary">{plainEdge(r.edge)}</Badge>
                  <span className="text-muted-foreground">hold ~{r.horizon_days}d</span>
                  <span className="text-muted-foreground">conf {r.conviction}</span>
                  {r.take ? (
                    <Badge className="ml-auto bg-emerald-600 text-white">Suggested</Badge>
                  ) : (
                    <Badge variant="outline" className="ml-auto">
                      Skip
                    </Badge>
                  )}
                </div>
                <p className="mt-1 text-xs text-muted-foreground">{r.summary}</p>
                {r.plan && <PlanLine plan={r.plan} direction={r.direction} />}
                {r.chief_reason && (
                  <p className="mt-1 text-xs text-amber-600 dark:text-amber-400">
                    <span className="font-medium">Final call:</span> {r.chief_reason}
                  </p>
                )}
              </div>
            ))}
            {board.length === 0 && (
              <p className="text-sm text-muted-foreground">Nothing worth acting on this time.</p>
            )}
          </div>
        )}

        {feed.length > 0 && (
          <div className="mt-4 space-y-2">
            {feed.map((ev, i) => (
              <Line key={i} ev={ev} />
            ))}
          </div>
        )}
    </Card>
  )
}
