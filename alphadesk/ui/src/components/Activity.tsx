import { useEffect, useState } from "react"
import { api, fmtAlpha, type SourceStat, type TokenRow } from "@/lib/api"

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
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="mb-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        By source · 30d
      </div>
      {sources.length === 0 ? (
        <p className="text-sm text-muted-foreground">No source data yet — run Find Trades.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-[10px] uppercase tracking-wider text-muted-foreground">
                <th className="pb-1 text-left font-medium">source</th>
                <th className="pb-1 text-right font-medium">arts</th>
                <th className="pb-1 text-right font-medium">tokens</th>
                <th className="pb-1 text-right font-medium">picks</th>
                <th className="pb-1 text-right font-medium">vs S&amp;P</th>
              </tr>
            </thead>
            <tbody>
              {sources.map((s) => (
                <tr key={s.source} className="border-t border-border/50">
                  <td className="py-1.5 font-medium">{s.source}</td>
                  <td className="py-1.5 text-right tabular-nums text-muted-foreground">
                    {s.articles || "—"}
                  </td>
                  <td className="py-1.5 text-right tabular-nums text-muted-foreground">
                    {s.tokens ? fmtTok(s.tokens) : "—"}
                  </td>
                  <td className="py-1.5 text-right tabular-nums">
                    {s.picks}
                    {s.graded > 0 && <span className="text-muted-foreground"> ({s.graded}g)</span>}
                  </td>
                  <td
                    className={`py-1.5 text-right font-mono tabular-nums ${
                      s.avg_alpha == null
                        ? "text-muted-foreground"
                        : s.avg_alpha > 0
                          ? "text-emerald-500"
                          : "text-red-500"
                    }`}
                  >
                    {s.avg_alpha == null ? "—" : fmtAlpha(s.avg_alpha)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-2 text-[10px] text-muted-foreground">
            tokens = ingestion + debate for that channel · (Ng) = graded so far ·
            “—” = not tracked (arts count from new runs only)
          </p>
        </div>
      )}
    </div>
  )
}

export function Activity({ tokens }: { tokens: TokenRow[] }) {
  const rows = [...tokens].sort(
    (a, b) => b.input_tok + b.output_tok - (a.input_tok + a.output_tok),
  )
  const total = rows.reduce((s, t) => s + t.input_tok + t.output_tok, 0)
  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-border bg-card p-4">
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
      </div>
      <BySource />
    </div>
  )
}
