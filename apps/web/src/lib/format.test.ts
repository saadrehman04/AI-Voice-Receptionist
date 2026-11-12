import { describe, expect, it } from "vitest";

import {
  callerLabel,
  formatDateTime,
  usageLevel,
  formatDay,
  formatDuration,
  formatMs,
  formatPercent,
  formatPhone,
  formatPrice,
  outcomeLabel,
} from "./format";

describe("format", () => {
  it("shows times on the business's clock", () => {
    // whatever zone the test machine is in
    expect(formatDateTime("2026-11-16T15:30:00Z", "America/New_York")).toBe("Mon 16 Nov, 10:30");
    expect(formatDateTime("2026-11-16T15:30:00Z")).toBe("Mon 16 Nov, 15:30"); // UTC unless told
  });

  it("keeps a calendar day on its own date", () => {
    expect(formatDay("2026-11-16")).toBe("Mon 16");
  });

  it("formats durations, prices, latencies and rates", () => {
    expect(formatDuration(null)).toBe("in progress");
    expect(formatDuration(42)).toBe("42s");
    expect(formatDuration(125)).toBe("2m 05s");
    expect(formatPrice("2500")).toBe("USD 2,500");
    expect(formatPrice("40", "GBP")).toBe("GBP 40");
    expect(formatPrice(null)).toBe("on request");
    expect(formatMs(380.4)).toBe("380 ms");
    expect(formatMs(1834)).toBe("1.83 s");
    expect(formatPercent(0.4567)).toBe("46%");
    expect(formatPercent(null)).toBe("–");
  });

  it("shows phone numbers in international format", () => {
    expect(formatPhone("+14155550101")).toBe("+14155550101");
    expect(formatPhone(null)).toBe("unknown number");
    expect(callerLabel(null, "webrtc")).toBe("Browser call");
    expect(callerLabel("+14155550101", "whatsapp")).toBe("+14155550101");
  });

  it("labels outcomes for owners", () => {
    expect(outcomeLabel("booked")).toBe("Booked");
    expect(outcomeLabel(null)).toBe("Summarising…");
  });
});

describe("usageLevel", () => {
  it("says where a business is against its monthly minutes", () => {
    expect(usageLevel(100, 1000)).toMatchObject({ share: 0.1, level: "ok" });
    expect(usageLevel(850, 1000).level).toBe("near");
    expect(usageLevel(1000, 1000)).toMatchObject({ share: 1, level: "over" });
    expect(usageLevel(1500, 1000).share).toBe(1); // the meter never overflows
  });
});
