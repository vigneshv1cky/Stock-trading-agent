// Plain-English labels for the desk's internal jargon, so a non-trader can read the UI.

export const plainEdge = (e?: string | null): string =>
  ({
    SPILLOVER: "Ripple effect",
    MOMENTUM: "Momentum",
    THEME: "Theme",
    EARNINGS: "Earnings",
    WORLD: "World event",
  })[e ?? ""] ?? e ?? ""

export const plainVerdict = (v?: string | null): string =>
  ({ STRONG: "Approved", SOFT: "Approved (cautious)", PASS: "Rejected" })[v ?? ""] ??
  v ??
  ""

// LONG = buy, expecting the price to RISE. SHORT = bet the price FALLS.
export const dirWord = (d?: string): string => (d === "LONG" ? "Buy" : "Short")
export const dirUp = (d?: string): boolean => d === "LONG"
export const dirHint = (d?: string): string =>
  d === "LONG" ? "expecting the price to rise" : "betting the price falls"
