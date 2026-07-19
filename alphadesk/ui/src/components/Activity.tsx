import type { TokenRow } from "@/lib/api"

export function Activity({ tokens }: { tokens: TokenRow[] }) {
  const rows = [...tokens].sort(
    (a, b) => b.input_tok + b.output_tok - (a.input_tok + a.output_tok),
  )
  const total = rows.reduce((s, t) => s + t.input_tok + t.output_tok, 0)
  return (
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
  )
}
