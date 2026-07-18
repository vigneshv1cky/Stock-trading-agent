import { useRef, useState } from "react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { ArrowDown, ArrowUp, Loader2, Search } from "lucide-react"
import { dirUp, dirWord, plainEdge, plainVerdict } from "@/lib/plain"

// Streamed events (loosely typed — they come as JSON off the SSE feed)
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
}

const BUBBLE: Record<string, string> = {
  exposure_shock: "border-l-cyan-500 bg-muted/40",
  exposure_candidate: "border-l-cyan-500",
  triage_pick: "border-l-yellow-500",
  brief: "border-l-zinc-500 bg-muted/40",
  thesis: "border-l-blue-500",
  concern: "border-l-red-500",
  fact_flag: "border-l-orange-500 bg-muted/40",
  rebuttal: "border-l-blue-500",
  decision: "border-l-green-500",
}

function Line({ ev }: { ev: Ev }) {
  const cls = `rounded-md border border-l-4 ${BUBBLE[ev.type] ?? "border-l-border"} p-2.5 text-sm`
  switch (ev.type) {
    case "exposure_shock":
      return (
        <div className={cls}>
          <span className="text-xs font-semibold uppercase tracking-wider text-cyan-500">
            Looking for companies affected by {ev.symbol}
          </span>
        </div>
      )
    case "exposure_candidate":
      return (
        <div className={cls}>
          <span className="text-xs font-semibold uppercase tracking-wider text-cyan-500">
            Knock-on effect: {ev.shock} → {ev.symbol}
          </span>{" "}
          <span className={dirUp(ev.direction) ? "text-green-500" : "text-red-500"}>
            {dirWord(ev.direction)}
          </span>{" "}
          <Badge variant="secondary">{ev.strength}</Badge>
          <p className="mt-1 text-muted-foreground">{ev.chain}</p>
        </div>
      )
    case "triage_pick":
      return (
        <div className={cls}>
          <span className="text-xs font-semibold uppercase tracking-wider text-yellow-500">
            Shortlisted {ev.symbol}
          </span>{" "}
          <Badge variant="secondary" className="ml-1">
            {plainEdge(ev.edge)}
          </Badge>
          <p className="mt-1 text-muted-foreground">{ev.reason}</p>
        </div>
      )
    case "brief":
      return (
        <div className={cls}>
          <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            {ev.kind} note · {ev.symbol}
          </span>
          <p className="mt-1">{ev.summary}</p>
        </div>
      )
    case "thesis":
      return (
        <div className={cls}>
          <span className="text-xs font-semibold uppercase tracking-wider text-blue-500">
            The case for {ev.symbol}
          </span>
          <p className="mt-1">
            <b className={dirUp(ev.direction) ? "text-green-500" : "text-red-500"}>
              {dirWord(ev.direction)}
            </b>{" "}
            · hold ~{ev.horizon_days} days · confidence {ev.score}/100
          </p>
        </div>
      )
    case "concern":
      return (
        <div className={cls}>
          <span className="text-xs font-semibold uppercase tracking-wider text-red-500">
            The pushback · {ev.symbol}
          </span>
          <p className="mt-1 font-medium">{ev.claim}</p>
          <p className="text-muted-foreground">{ev.evidence}</p>
        </div>
      )
    case "fact_flag":
      return (
        <div className={cls}>
          <span className="text-xs font-semibold uppercase tracking-wider text-orange-500">
            Fact-check
          </span>
          <p className="mt-1">{ev.text}</p>
        </div>
      )
    case "rebuttal":
      return (
        <div className={cls}>
          <span className="text-xs font-semibold uppercase tracking-wider text-blue-500">
            Researcher's reply · {ev.symbol}
          </span>
          <p className="mt-1">
            updated confidence {ev.revised_score}/100 · agreed with the pushback:{" "}
            {ev.concede ? "yes" : "no"}
          </p>
        </div>
      )
    case "decision":
      return (
        <div className={cls}>
          <span className="text-xs font-semibold uppercase tracking-wider text-green-500">
            Decision · {ev.symbol}
          </span>
          <p className="mt-1">
            {ev.approved ? "✅ Worth acting on" : "❌ Skipped"} · {plainVerdict(ev.verdict)} ·
            confidence {ev.conviction}/100
          </p>
          <p className="text-muted-foreground">{ev.summary}</p>
        </div>
      )
    default:
      return null
  }
}

export function FindTrades({ onDone }: { onDone: () => void }) {
  const [running, setRunning] = useState(false)
  const [status, setStatus] = useState("")
  const [feed, setFeed] = useState<Ev[]>([])
  const [board, setBoard] = useState<BoardRow[] | null>(null)
  const [chief, setChief] = useState<string>("")
  const [positions, setPositions] = useState<Ev[]>([])
  const [deep, setDeep] = useState(false)
  const esRef = useRef<EventSource | null>(null)

  function run() {
    setRunning(true)
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
        setRunning(false)
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
      setRunning(false)
      es.close()
    }
  }

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <CardTitle className="text-lg">Find Trades</CardTitle>
        <div className="flex items-center gap-3">
          <label className="flex items-center gap-1.5 text-sm text-muted-foreground">
            <input
              type="checkbox"
              checked={deep}
              disabled={running}
              onChange={(e) => setDeep(e.target.checked)}
            />
            Deep scan (also check related companies)
          </label>
          <Button onClick={run} disabled={running}>
          {running ? (
            <>
              <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Scanning…
            </>
          ) : (
            <>
              <Search className="mr-2 h-4 w-4" /> Find Trades
            </>
          )}
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        {status && <p className="mb-3 text-sm text-muted-foreground">{status}</p>}

        {positions.length > 0 && (
          <div className="mb-4 space-y-2">
            <div className="text-sm font-semibold">
              Your open picks
              <span className="ml-1 font-normal text-muted-foreground">
                — re-checked against today's news ({positions.filter((p) => p.type === "position_exit").length} to sell)
              </span>
            </div>
            {positions.map((p, i) => {
              const exit = p.type === "position_exit"
              return (
                <div
                  key={i}
                  className={`rounded-md border border-l-4 p-2.5 text-sm ${
                    exit ? "border-l-red-500 bg-red-950/20" : "border-l-emerald-600"
                  }`}
                >
                  <div className="flex items-center gap-2">
                    <Badge className={exit ? "bg-red-600" : "bg-emerald-700"}>
                      {exit ? "Sell now" : "Keep"}
                    </Badge>
                    <span className={dirUp(p.direction) ? "text-green-500" : "text-red-500"}>
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
          <div className="mb-4 space-y-2">
            {chief && (
              <div className="rounded-md border-l-4 border-l-amber-500 bg-muted/40 p-3">
                <div className="text-xs font-semibold uppercase tracking-wider text-amber-500">
                  Final call — comparing all the ideas
                </div>
                <p className="mt-1 text-sm">{chief}</p>
              </div>
            )}
            <div className="text-sm font-semibold">
              Best ideas ({board.filter((b) => b.take).length} worth acting on)
            </div>
            {board.map((r, i) => (
              <div
                key={r.id}
                className={`rounded-md border p-2.5 text-sm ${r.take ? "border-green-700 bg-green-950/20" : "opacity-70"}`}
              >
                <div className="flex items-center gap-3">
                  <span className="text-muted-foreground">#{i + 1}</span>
                  {r.direction === "LONG" ? (
                    <ArrowUp className="h-4 w-4 text-green-500" />
                  ) : (
                    <ArrowDown className="h-4 w-4 text-red-500" />
                  )}
                  <span
                    className={`font-bold ${dirUp(r.direction) ? "text-green-500" : "text-red-500"}`}
                  >
                    {dirWord(r.direction)}
                  </span>
                  <span className="font-bold">{r.symbol}</span>
                  <Badge variant="secondary">{plainEdge(r.edge)}</Badge>
                  <span className="text-muted-foreground">hold ~{r.horizon_days}d</span>
                  <span className="text-muted-foreground">confidence {r.conviction}</span>
                  {r.take ? (
                    <Badge className="ml-auto bg-green-600">Suggested</Badge>
                  ) : (
                    <Badge variant="outline" className="ml-auto">
                      Skip
                    </Badge>
                  )}
                </div>
                <p className="mt-1 pl-8 text-xs text-muted-foreground">
                  <span className="text-foreground/70">
                    {dirWord(r.direction)} · hold ~{r.horizon_days} days.
                  </span>{" "}
                  {r.summary}
                </p>
                {r.chief_reason && (
                  <p className="mt-1 pl-8 text-amber-500/90">
                    <span className="font-medium">Final call:</span> {r.chief_reason}
                  </p>
                )}
              </div>
            ))}
            {board.length === 0 && (
              <p className="text-sm text-muted-foreground">
                Nothing worth acting on this time.
              </p>
            )}
          </div>
        )}

        {feed.length > 0 && (
          <div className="max-h-[28rem] space-y-2 overflow-y-auto">
            {feed.map((ev, i) => (
              <Line key={i} ev={ev} />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  )
}
