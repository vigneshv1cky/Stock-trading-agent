// Plain-English labels for the desk's internal jargon, so a non-trader can read the UI.

export const plainEdge = (e?: string | null): string =>
  ({
    RIPPLE: "Ripple effect",
    DRIFT: "Momentum",
    NARRATIVE: "Theme",
    EARNINGS: "Earnings",
    WORLD_EVENT: "World event",
  })[e ?? ""] ?? e ?? ""

export const plainVerdict = (v?: string | null): string =>
  ({ CONFIRM: "Approved", WEAKEN: "Approved (cautious)", REJECT: "Rejected" })[v ?? ""] ??
  v ??
  ""

// LONG = buy, expecting the price to RISE. SHORT = bet the price FALLS.
export const dirWord = (d?: string): string => (d === "LONG" ? "Buy" : "Short")
export const dirUp = (d?: string): boolean => d === "LONG"
export const dirHint = (d?: string): string =>
  d === "LONG" ? "expecting the price to rise" : "betting the price falls"
