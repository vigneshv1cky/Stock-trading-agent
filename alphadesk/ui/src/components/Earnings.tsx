import { useState, type ReactNode } from "react"
import { ChevronDown } from "lucide-react"
import type { EarningsRow } from "@/lib/api"
import { InfoTip } from "@/components/InfoTip"
import { Badge } from "@/components/ui/badge"
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible"

function Panel({
  title,
  sub,
  children,
  collapsible = false,
  defaultOpen = true,
  count,
}: {
  title?: string
  sub?: string
  children: ReactNode
  collapsible?: boolean
  defaultOpen?: boolean
  count?: number
}) {
  const [open, setOpen] = useState(defaultOpen)
  if (collapsible && title) {
    // controlled Collapsible: keeps the exact chevron/count/sub look while adding
    // the animated height reveal + aria-expanded/controls for free.
    return (
      <Collapsible
        open={open}
        onOpenChange={setOpen}
        className="rounded-lg border border-border bg-card p-4"
      >
        <CollapsibleTrigger
          render={
            <button className="group -mx-1 flex w-full items-center gap-2 rounded-md px-1 py-0.5 text-left transition-colors hover:bg-muted/40" />
          }
        >
          <span className="text-xs font-semibold uppercase tracking-wider text-foreground/75">
            {title}
          </span>
          {count != null && (
            <Badge variant="secondary" className="tabular-nums">
              {count}
            </Badge>
          )}
          <ChevronDown
            className={`ml-auto h-4 w-4 shrink-0 text-muted-foreground/70 transition-transform group-hover:text-foreground ${
              open ? "" : "-rotate-90"
            }`}
          />
        </CollapsibleTrigger>
        <CollapsibleContent>
          {sub && <div className="mb-2 mt-1 px-1 text-[11px] text-muted-foreground">{sub}</div>}
          {children}
        </CollapsibleContent>
      </Collapsible>
    )
  }
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      {title && (
        <div className="mb-2">
          <div className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            {title}
          </div>
          {sub && <div className="text-[11px] text-muted-foreground">{sub}</div>}
        </div>
      )}
      {children}
    </div>
  )
}

function fmtCap(v?: number | null): string {
  if (v == null) return ""
  if (v >= 1e12) return `$${(v / 1e12).toFixed(1)}T`
  if (v >= 1e9) return `$${(v / 1e9).toFixed(0)}B`
  if (v >= 1e6) return `$${(v / 1e6).toFixed(0)}M`
  return `$${Math.round(v)}`
}

function dayLabel(day: string): string {
  const d = new Date(`${day}T12:00:00`) // noon avoids TZ date-rollover
  const wd = d.toLocaleDateString("en-US", { weekday: "short" })
  return `${wd} ${day.slice(5)}`
}

type DayGroup = { day: string; rows: EarningsRow[] }

// Rows arrive pre-sorted, so a single pass yields contiguous day-groups (biggest
// names first inside each). `key` picks the grouping day: run-day for upcoming,
// report-day for just-reported.
function groupByDay(rows: EarningsRow[], key: (e: EarningsRow) => string): DayGroup[] {
  const groups: DayGroup[] = []
  for (const e of rows) {
    const day = key(e)
    let g = groups[groups.length - 1]
    if (!g || g.day !== day) {
      g = { day, rows: [] }
      groups.push(g)
    }
    g.rows.push(e)
  }
  return groups
}

// Did the desk act on a reporter? (coverage self-assessment)
const ENG: Record<string, { label: string; cls: string }> = {
  TOOK: { label: "Took", cls: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400" },
  DEBATED: { label: "Debated", cls: "bg-indigo-500/15 text-indigo-600 dark:text-indigo-400" },
  SKIPPED: { label: "Skipped", cls: "bg-amber-500/15 text-amber-600 dark:text-amber-400" },
}

function EngBadge({ state }: { state?: string }) {
  const b = state ? ENG[state] : undefined
  if (!b) return <span className="text-[11px] text-muted-foreground/40">·</span>
  return <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${b.cls}`}>{b.label}</span>
}

const BIG_MOVE = 6 // % drift that counts as a real move (matches the skip-miss line)
const THIN_CAP = 100_000_000 // below ~$100M cap: effectively untradeable at size

// Classify a reporter's outcome vs what the desk did. A big drift the desk didn't
// act on is only a TRUE miss if it was tradeable; in a thin/illiquid name it's a
// FALSE miss (a pump you couldn't have captured — the HIHO case). For names the
// desk DID act on, whether the interim drift is going its way (not the official
// grade, which settles at the horizon).
function assess(e: EarningsRow): { label: string; cls: string; tip: string } | null {
  const move = e.move_since_report_pct
  if (move == null) return { label: "pending", cls: "text-muted-foreground/50", tip: "no post-report session yet" }
  const eng = e.engagement
  if (eng === "TOOK" || eng === "DEBATED") {
    if (!e.engagement_dir || Math.abs(move) < 1)
      return { label: "flat", cls: "text-muted-foreground/60", tip: "little drift so far" }
    const favorable = e.engagement_dir === "LONG" ? move > 0 : move < 0
    return favorable
      ? { label: "on track", cls: "text-emerald-600 dark:text-emerald-400", tip: "interim drift is going our way (not the official grade)" }
      : { label: "adverse", cls: "text-red-600 dark:text-red-400", tip: "interim drift is against our call (not the official grade)" }
  }
  // SKIPPED / UNSEEN
  if (Math.abs(move) < BIG_MOVE)
    return { label: "fair pass", cls: "text-muted-foreground/60", tip: "small move — nothing forgone" }
  const thin = (e.market_cap ?? Infinity) < THIN_CAP
  return thin
    ? { label: "false miss", cls: "text-amber-600 dark:text-amber-400", tip: "big move but too illiquid to trade at size — uncatchable" }
    : { label: "true miss", cls: "font-semibold text-red-600 dark:text-red-400", tip: "big, tradeable move the desk didn't act on" }
}

function AssessTag({ e }: { e: EarningsRow }) {
  const a = assess(e)
  if (!a) return null
  return (
    <InfoTip tip={a.tip} className={`cursor-help text-[10px] ${a.cls}`}>
      {a.label}
    </InfoTip>
  )
}

// One-glance "did we do well?" — how many reporters the desk took / debated /
// skipped / never saw, plus the biggest drift it didn't act on.
function CoverageSummary({ reported }: { reported: EarningsRow[] }) {
  const c = (s: string) => reported.filter((e) => e.engagement === s).length
  const took = c("TOOK")
  const debated = c("DEBATED")
  const skipped = c("SKIPPED")
  const unseen = reported.length - took - debated - skipped
  const trueMiss = reported.filter((e) => assess(e)?.label === "true miss").length
  const falseMiss = reported.filter((e) => assess(e)?.label === "false miss").length
  const worst = reported
    .filter((e) => assess(e)?.label === "true miss")
    .sort((a, b) => Math.abs(b.move_since_report_pct!) - Math.abs(a.move_since_report_pct!))[0]
  return (
    <div className="mb-2 rounded-md bg-muted/40 px-2.5 py-2 text-[11px]">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="font-medium text-muted-foreground">Desk coverage</span>
        <span className="text-emerald-600 dark:text-emerald-400">{took} took</span>
        <span className="text-indigo-600 dark:text-indigo-400">{debated} debated</span>
        <span className="text-amber-600 dark:text-amber-400">{skipped} skipped</span>
        <span className="text-muted-foreground">{unseen} not seen</span>
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className={trueMiss > 0 ? "font-semibold text-red-600 dark:text-red-400" : "text-muted-foreground"}>
          {trueMiss} true miss{trueMiss === 1 ? "" : "es"}
        </span>
        <span className="text-amber-600 dark:text-amber-400">{falseMiss} false (untradeable)</span>
        {worst && (
          <span className="text-muted-foreground">
            worst: <span className="font-semibold text-foreground">{worst.symbol}</span>{" "}
            <span className={worst.move_since_report_pct! >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400"}>
              {worst.move_since_report_pct! >= 0 ? "+" : ""}
              {worst.move_since_report_pct}%
            </span>
          </span>
        )}
      </div>
    </div>
  )
}

function whyText(e: EarningsRow): string {
  if (e.engagement === "UNSEEN")
    return "Not surfaced — the desk didn't run after this reported, or it wasn't in that run's news/earnings window."
  return e.engagement_why || "(no reason recorded)"
}

// A reporter row that expands to show WHY the desk acted as it did (its own
// stored reasoning: judge summary / thesis for takes & debates, the scout's
// reason for skips, or the coverage-gap note for unseen).
function ReportedRow({ e }: { e: EarningsRow }) {
  const [open, setOpen] = useState(false)
  const move = e.move_since_report_pct
  const has = move != null
  const up = (move ?? 0) >= 0
  const took = e.engagement === "TOOK" || e.engagement === "DEBATED"
  return (
    <li className="text-sm">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 py-1.5 text-left transition-colors hover:bg-muted/30"
      >
        <span className="w-16 font-semibold">{e.symbol}</span>
        <span className="w-14 text-xs text-muted-foreground">{fmtCap(e.market_cap)}</span>
        <span className="w-10 text-xs text-muted-foreground">{e.session}</span>
        <span className="w-20">
          <EngBadge state={e.engagement} />
        </span>
        <span className="w-20">
          <AssessTag e={e} />
        </span>
        <span
          className={`ml-auto font-mono tabular-nums ${
            has ? (up ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400") : "text-muted-foreground"
          }`}
        >
          {has ? `${up ? "+" : ""}${move}%` : "—"}
        </span>
        <ChevronDown
          className={`h-3.5 w-3.5 shrink-0 text-muted-foreground/40 transition-transform ${
            open ? "" : "-rotate-90"
          }`}
        />
      </button>
      {open && (
        <div className="mb-1.5 ml-16 mr-6 rounded-md bg-muted/40 px-2.5 py-2 text-xs leading-relaxed text-muted-foreground">
          {took && e.engagement_dir && (
            <span className="mr-1 font-medium text-foreground">
              {e.engagement_dir === "LONG" ? "Long" : "Short"}
              {e.engagement_verdict ? ` · ${e.engagement_verdict}` : ""}:
            </span>
          )}
          {whyText(e)}
        </div>
      )}
    </li>
  )
}

// One run-day group in "Reporting soon" — shows the biggest 8, with the rest
// expandable/collapsible via the "+N more / show less" toggle.
function RunGroup({ g }: { g: DayGroup }) {
  const [expanded, setExpanded] = useState(false)
  const shown = expanded ? g.rows : g.rows.slice(0, 8)
  const more = g.rows.length - 8
  return (
    <div>
      <div className="mb-1 text-xs font-semibold text-emerald-600 dark:text-emerald-400">
        {g.day === "—" ? "Run time n/a" : `Run ${dayLabel(g.day)} · 9:30 ET`}
      </div>
      <ul className="divide-y divide-border">
        {shown.map((e) => (
          <li key={e.symbol + e.report_date} className="flex items-center gap-2 py-1.5 text-sm">
            <span className="w-14 font-semibold">{e.symbol}</span>
            <span className="w-14 text-xs text-muted-foreground">{fmtCap(e.market_cap)}</span>
            <span className="ml-auto text-xs text-muted-foreground">
              {e.report_date.slice(5, 10)} {e.session}
            </span>
          </li>
        ))}
      </ul>
      {more > 0 && (
        <button
          onClick={() => setExpanded((v) => !v)}
          className="mt-1 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground"
        >
          {expanded ? "− show less" : `+${more} more`}
        </button>
      )}
    </div>
  )
}

export function Earnings({
  earnings,
}: {
  earnings?: { upcoming: EarningsRow[]; reported: EarningsRow[] }
}) {
  if (!earnings || (earnings.reported.length === 0 && earnings.upcoming.length === 0)) {
    return (
      <Panel>
        <p className="text-sm text-muted-foreground">
          No earnings on the calendar yet — it refreshes a few times a day.
        </p>
      </Panel>
    )
  }

  return (
    <div className="space-y-3">
      {earnings.reported.length > 0 && (
        <Panel
          title="Just reported"
          sub="the drift so far — price move since each report went public"
          collapsible
          defaultOpen={false}
          count={earnings.reported.length}
        >
          <CoverageSummary reported={earnings.reported} />
          <div className="mb-2 flex items-center gap-2 border-b border-border pb-1.5 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
            <span className="w-16">Symbol</span>
            <span className="w-14">Cap</span>
            <span className="w-10">Session</span>
            <span className="w-20">Desk</span>
            <span className="w-20">Verdict</span>
            <span className="ml-auto">Move</span>
          </div>
          <div className="space-y-3">
            {groupByDay(earnings.reported, (e) => e.report_date.slice(0, 10)).map((g) => (
              <div key={g.day}>
                <div className="mb-1 flex items-baseline justify-between">
                  <span className="text-xs font-semibold text-indigo-600 dark:text-indigo-400">
                    Reported {dayLabel(g.day)}
                  </span>
                  <span className="text-[11px] text-muted-foreground">{g.rows.length} names</span>
                </div>
                <ul className="divide-y divide-border">
                  {g.rows.map((e) => (
                    <ReportedRow key={e.symbol + e.report_date} e={e} />
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </Panel>
      )}

      {earnings.upcoming.length > 0 && (
        <Panel title="Reporting soon" sub="grouped by when to run the desk — biggest names first">
          <div className="mb-2 flex items-center gap-2 border-b border-border pb-1.5 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
            <span className="w-14">Symbol</span>
            <span className="w-14">Cap</span>
            <span className="ml-auto">Report</span>
          </div>
          <div className="space-y-3">
            {groupByDay(earnings.upcoming, (e) => (e.run_at ?? "").slice(0, 10) || "—").map((g) => (
              <RunGroup key={g.day} g={g} />
            ))}
          </div>
        </Panel>
      )}
    </div>
  )
}
