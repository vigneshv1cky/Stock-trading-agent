import { useEffect, useState } from "react"
import { api, fmtAlpha, type SourceStat, type TokenRow } from "@/lib/api"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Card } from "@/components/ui/card"

function fmtTok(n: number) {
  return n >= 1000 ? `${Math.round(n / 1000)}k` : String(n)
}

// Cost + value per ingestion channel: which source's articles earn their tokens.
function BySource() {
  const [sources, setSources] = useState<SourceStat[] | null>(null)
  useEffect(() => {
    api.sources().then((d) => setSources(d.sources)).catch(console.error)
  }, [])
  if (!sources) return null
  return (
    <Card>
      <div className="mb-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        By source · 30d
      </div>
      {sources.length === 0 ? (
        <p className="text-sm text-muted-foreground">No source data yet — run Find Trades.</p>
      ) : (
        <div>
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                {["source", "arts", "tokens", "picks", "vs S&P"].map((h, i) => (
                  <TableHead
                    key={h}
                    className={`h-auto pb-1 text-[10px] uppercase tracking-wider ${i === 0 ? "" : "text-right"}`}
                  >
                    {h}
                  </TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {sources.map((s) => (
                <TableRow key={s.source} className="border-border/50">
                  <TableCell className="py-1.5 font-medium">{s.source}</TableCell>
                  <TableCell className="py-1.5 text-right tabular-nums text-muted-foreground">
                    {s.articles || "—"}
                  </TableCell>
                  <TableCell className="py-1.5 text-right tabular-nums text-muted-foreground">
                    {s.tokens ? fmtTok(s.tokens) : "—"}
                  </TableCell>
                  <TableCell className="py-1.5 text-right tabular-nums">
                    {s.picks}
                    {s.graded > 0 && <span className="text-muted-foreground"> ({s.graded}g)</span>}
                  </TableCell>
                  <TableCell
                    className={`py-1.5 text-right font-mono tabular-nums ${
                      s.avg_alpha == null
                        ? "text-muted-foreground"
                        : s.avg_alpha > 0
                          ? "text-emerald-600 dark:text-emerald-400"
                          : "text-red-600 dark:text-red-400"
                    }`}
                  >
                    {s.avg_alpha == null ? "—" : fmtAlpha(s.avg_alpha)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <p className="mt-2 text-[10px] text-muted-foreground">
            tokens = ingestion + debate for that channel · (Ng) = graded so far ·
            “—” = not tracked (arts count from new runs only)
          </p>
        </div>
      )}
    </Card>
  )
}

export function Activity({ tokens }: { tokens: TokenRow[] }) {
  const rows = [...tokens].sort(
    (a, b) => b.input_tok + b.output_tok - (a.input_tok + a.output_tok),
  )
  const total = rows.reduce((s, t) => s + t.input_tok + t.output_tok, 0)
  return (
    <div className="space-y-4">
      <Card>
        <div className="mb-3 flex items-baseline justify-between">
          <div className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            AI usage today
          </div>
          <div className="text-xs tabular-nums text-muted-foreground">
            {Math.round(total / 1000)}k tokens
          </div>
        </div>
        {rows.length === 0 ? (
          <p className="text-sm text-muted-foreground">No calls yet today.</p>
        ) : (
          <div className="space-y-2.5">
            {rows.slice(0, 12).map((t) => {
              const tot = t.input_tok + t.output_tok
              const pct = total > 0 ? (tot / total) * 100 : 0
              return (
                <div key={t.role + t.model} className="text-sm">
                  <div className="flex items-baseline justify-between">
                    <span className="font-medium">
                      {t.role}{" "}
                      <span className="text-xs font-normal text-muted-foreground">{t.model}</span>
                    </span>
                    <span className="text-xs tabular-nums text-muted-foreground">
                      {Math.round(tot / 1000)}k · {t.calls} calls
                    </span>
                  </div>
                  <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-muted">
                    <div className="h-full rounded-full bg-indigo-500" style={{ width: `${pct}%` }} />
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </Card>
      <BySource />
    </div>
  )
}
