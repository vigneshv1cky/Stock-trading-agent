import { useEffect, useState } from "react"
import { api, etDateTime, exitDate, fmtAlpha, type Pick } from "@/lib/api"
import { dirWord, plainEdge, plainVerdict } from "@/lib/plain"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent } from "@/components/ui/card"
import { Separator } from "@/components/ui/separator"
import { Skeleton } from "@/components/ui/skeleton"
import { ArrowDown, ArrowUp, X } from "lucide-react"

const ROLE_STYLES: Record<string, string> = {
  scout: "border-l-yellow-500",
  brief: "border-l-zinc-500",
  researcher: "border-l-blue-500",
  critic: "border-l-red-500",
  judge: "border-l-green-500",
  flag: "border-l-orange-500",
  loner: "border-l-purple-500",
}

function Bubble({
  role,
  who,
  children,
}: {
  role: keyof typeof ROLE_STYLES
  who: string
  children: React.ReactNode
}) {
  return (
    <div className={`rounded-md border border-l-4 ${ROLE_STYLES[role]} bg-card p-3 text-sm`}>
      <div className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        {who}
      </div>
      {children}
    </div>
  )
}

function TheCall({ pick }: { pick: Pick }) {
  const long = pick.direction === "LONG"
  return (
    <Card className="border-2">
      <CardContent className="space-y-2 pt-4">
        <div className="flex items-center gap-2 text-lg font-bold">
          {long ? (
            <ArrowUp className="h-5 w-5 text-green-500" />
          ) : (
            <ArrowDown className="h-5 w-5 text-red-500" />
          )}
          <span className={long ? "text-green-500" : "text-red-500"}>{dirWord(pick.direction)}</span>
          <span>{pick.symbol}</span>
          <span className="text-sm font-normal text-muted-foreground">
            hold ~{pick.horizon_days} trading days (≈ until{" "}
            {exitDate(pick.ts, pick.session, pick.horizon_days)})
          </span>
        </div>
        <div className="text-sm text-muted-foreground">
          buy at {pick.entry_price ? `$${pick.entry_price}` : "next market open"} · confidence{" "}
          {Math.round(pick.adjusted_score ?? pick.score)}/100
        </div>
        {pick.plan_entry != null && pick.plan_target != null && pick.plan_stop != null && (
          <div className="rounded-md bg-muted/50 px-2.5 py-2 text-sm">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
              <span className="font-semibold">
                {long ? "Buy" : "Short"} ${pick.plan_entry}
              </span>
              <span className="text-muted-foreground">·</span>
              <span>
                target <span className="font-medium text-green-500">${pick.plan_target}</span>
              </span>
              <span className="text-muted-foreground">·</span>
              <span>
                stop <span className="font-medium text-red-500">${pick.plan_stop}</span>
              </span>
            </div>
            {pick.plan_note && <p className="mt-1 text-muted-foreground">{pick.plan_note}</p>}
          </div>
        )}
        <div className="text-sm">
          {pick.approved ? (
            <Badge className="bg-green-600">Conviction call</Badge>
          ) : (
            <Badge variant="secondary">Thin lean (tracked)</Badge>
          )}{" "}
          {pick.alpha_net !== null && (
            <Badge variant="outline" className={pick.alpha_net > 0 ? "text-green-500" : "text-red-500"}>
              vs S&P 500 {fmtAlpha(pick.alpha_net)}
            </Badge>
          )}
        </div>
      </CardContent>
    </Card>
  )
}

export function PickSheet({
  pickId,
  onClose,
}: {
  pickId: number | null
  onClose: () => void
}) {
  const [pick, setPick] = useState<Pick | null>(null)

  useEffect(() => {
    setPick(null)
    if (pickId !== null) {
      api.pick(pickId).then(setPick).catch(console.error)
    }
  }, [pickId])

  useEffect(() => {
    if (pickId === null) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose()
    }
    document.addEventListener("keydown", onKey)
    document.body.style.overflow = "hidden"
    return () => {
      document.removeEventListener("keydown", onKey)
      document.body.style.overflow = ""
    }
  }, [pickId, onClose])

  if (pickId === null) return null

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
    >
      <div
        className="absolute inset-0 bg-black/50 backdrop-blur-sm"
        onClick={onClose}
        aria-hidden="true"
      />
      <div className="relative z-10 flex max-h-[88vh] w-full max-w-4xl flex-col overflow-hidden rounded-xl border border-border bg-background shadow-2xl">
        <div className="flex items-start justify-between gap-3 border-b border-border p-4">
          <div className="min-w-0">
            <div className="text-base font-semibold tracking-tight">
              #{pick?.id ?? pickId}
              {pick && <> · {pick.symbol}</>}
            </div>
            {pick && (
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                <Badge variant="secondary">{pick.arm === "LONER" ? "Solo" : "Team"}</Badge>
                {pick.edge && <Badge variant="secondary">{plainEdge(pick.edge)}</Badge>}
                <Badge variant="secondary">{etDateTime(pick.ts)} ET</Badge>
              </div>
            )}
          </div>
          <button
            onClick={onClose}
            aria-label="Close"
            className="grid h-8 w-8 shrink-0 place-items-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="no-scrollbar min-h-0 flex-1 overflow-y-auto p-4">
          {!pick ? (
            <div className="space-y-3">
              <Skeleton className="h-24 w-full" />
              <Skeleton className="h-16 w-full" />
              <Skeleton className="h-16 w-full" />
            </div>
          ) : (
            <div className="space-y-3">
              <TheCall pick={pick} />
              <div className="text-center text-xs text-muted-foreground">
                confidence {Math.round(pick.score)} → {pick.adjusted_score ?? "—"} after the debate ·{" "}
                {plainVerdict(pick.verdict) || "—"}
              </div>
              <Separator />

              {pick.triage_reason && (
                <Bubble role="scout" who="Shortlist — why we looked at it">
                  {pick.triage_reason}
                </Bubble>
              )}

              {(pick.briefs ?? []).map((b, i) => (
                <Bubble key={i} role="brief" who={`${b.kind} note`}>
                  <p>{b.summary}</p>
                  {b.key_facts && b.key_facts.length > 0 && (
                    <ul className="mt-1.5 list-disc pl-4 text-muted-foreground">
                      {b.key_facts.map((f, j) => (
                        <li key={j}>{typeof f === "string" ? f : f.fact}</li>
                      ))}
                    </ul>
                  )}
                </Bubble>
              ))}

              {pick.thesis && (
                <Bubble role="researcher" who="The case — researcher">
                  <p>{pick.thesis}</p>
                  <p className="mt-1.5 text-muted-foreground">
                    confidence {Math.round(pick.score)}/100 · hold ~{pick.horizon_days} days
                  </p>
                </Bubble>
              )}

              {(pick.debate?.concerns ?? []).map((c, i) => (
                <Bubble key={i} role="critic" who={`The pushback #${i + 1} — critic`}>
                  <p className="font-medium">{c.claim}</p>
                  <p className="mt-1 text-muted-foreground">{c.evidence}</p>
                </Bubble>
              ))}

              {pick.debate?.critic_stance && pick.debate.critic_stance !== "SUPPORT" && (
                <Bubble role="critic" who="Critic's counter-call">
                  {pick.debate.critic_stance === "FLIP" ? (
                    <p className="font-medium">
                      Reverse: {dirWord(pick.debate.proposed_direction)} →{" "}
                      <span className={pick.debate.counter_direction === "LONG" ? "text-green-500" : "text-red-500"}>
                        {dirWord(pick.debate.counter_direction)}
                      </span>
                    </p>
                  ) : (
                    <p className="font-medium">Stand aside — no edge either way</p>
                  )}
                  {pick.debate.counter && (
                    <p className="mt-1 text-muted-foreground">{pick.debate.counter}</p>
                  )}
                </Bubble>
              )}

              {(pick.debate?.fact_flags ?? []).map((f, i) => (
                <Bubble key={i} role="flag" who="Fact-check">
                  {f}
                </Bubble>
              ))}

              {pick.debate?.rebuttal && (
                <Bubble role="researcher" who="The researcher's reply">
                  <p>{pick.debate.rebuttal.rebuttal}</p>
                  <p className="mt-1.5 text-muted-foreground">
                    updated confidence {pick.debate.rebuttal.revised_score}/100 · agreed with the
                    pushback: {pick.debate.rebuttal.concede ? "yes" : "no"}
                  </p>
                </Bubble>
              )}

              {pick.debate?.arbiter_summary && (
                <Bubble role="judge" who="The decision — judge">
                  <p>{pick.debate.arbiter_summary}</p>
                  <p className="mt-1.5 text-muted-foreground">
                    final confidence {pick.adjusted_score}/100 · {plainVerdict(pick.verdict)} ·{" "}
                    {pick.approved ? "Suggested" : "Skipped"}
                  </p>
                </Bubble>
              )}

              {pick.arm === "LONER" && (
                <Bubble role="loner" who="Second opinion — works alone">
                  Reviewed the same evidence on its own, without the team's debate.
                </Bubble>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
