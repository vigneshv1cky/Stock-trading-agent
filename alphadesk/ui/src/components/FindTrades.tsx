import { useRef, useState } from "react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { ArrowDown, ArrowUp, Loader2, Search } from "lucide-react"

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
}

const BUBBLE: Record<string, string> = {
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
    case "triage_pick":
      return (
        <div className={cls}>
          <span className="text-xs font-semibold uppercase tracking-wider text-yellow-500">
            Triage picked {ev.symbol}
          </span>{" "}
          <Badge variant="secondary" className="ml-1">
            {ev.edge}
          </Badge>
          <p className="mt-1 text-muted-foreground">{ev.reason}</p>
        </div>
      )
    case "brief":
      return (
        <div className={cls}>
          <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            {ev.kind} brief · {ev.symbol}
          </span>
          <p className="mt-1">{ev.summary}</p>
        </div>
      )
    case "thesis":
      return (
        <div className={cls}>
          <span className="text-xs font-semibold uppercase tracking-wider text-blue-500">
            Analyst · {ev.symbol}
          </span>
          <p className="mt-1">
            <b className={ev.direction === "LONG" ? "text-green-500" : "text-red-500"}>
              {ev.direction}
            </b>{" "}
            · {ev.horizon_days}d · score {ev.score}
          </p>
        </div>
      )
    case "concern":
      return (
        <div className={cls}>
          <span className="text-xs font-semibold uppercase tracking-wider text-red-500">
            Skeptic · {ev.symbol}
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
            Analyst rebuttal · {ev.symbol}
          </span>
          <p className="mt-1">
            revised {ev.revised_score} · conceded {String(ev.concede)}
          </p>
        </div>
      )
    case "decision":
      return (
        <div className={cls}>
          <span className="text-xs font-semibold uppercase tracking-wider text-green-500">
            Verdict · {ev.symbol}
          </span>
          <p className="mt-1">
            {ev.approved ? "✅ ON THE BOOK" : "❌ rejected"} · {ev.verdict} · conviction{" "}
            {ev.conviction}
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
  const esRef = useRef<EventSource | null>(null)

  function run() {
    setRunning(true)
    setFeed([])
    setBoard(null)
    setStatus("Starting…")
    const es = new EventSource("/api/find-trades?hours=48&max_debates=6")
    esRef.current = es
    es.onmessage = (e) => {
      const ev: Ev = JSON.parse(e.data)
      if (ev.type === "status") setStatus(ev.msg ?? "")
      else if (ev.type === "done") {
        setBoard(ev.board ?? [])
        setRunning(false)
        es.close()
        onDone()
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
      </CardHeader>
      <CardContent>
        {status && <p className="mb-3 text-sm text-muted-foreground">{status}</p>}

        {board && (
          <div className="mb-4 space-y-2">
            <div className="text-sm font-semibold">
              Opportunities found ({board.filter((b) => b.approved).length} on the book)
            </div>
            {board.map((r) => (
              <div
                key={r.id}
                className="flex items-center gap-3 rounded-md border p-2.5 text-sm"
              >
                {r.direction === "LONG" ? (
                  <ArrowUp className="h-4 w-4 text-green-500" />
                ) : (
                  <ArrowDown className="h-4 w-4 text-red-500" />
                )}
                <span
                  className={`font-bold ${r.direction === "LONG" ? "text-green-500" : "text-red-500"}`}
                >
                  {r.direction}
                </span>
                <span className="font-bold">{r.symbol}</span>
                <Badge variant="secondary">{r.edge}</Badge>
                <span className="text-muted-foreground">{r.horizon_days}d</span>
                <span className="text-muted-foreground">conviction {r.conviction}</span>
                {r.approved ? (
                  <Badge className="ml-auto bg-green-600">ON THE BOOK</Badge>
                ) : (
                  <Badge variant="outline" className="ml-auto">
                    rejected
                  </Badge>
                )}
              </div>
            ))}
            {board.length === 0 && (
              <p className="text-sm text-muted-foreground">
                No opportunities cleared the committee this run.
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
