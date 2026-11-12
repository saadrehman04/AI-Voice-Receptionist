import { describe, expect, it } from "vitest";

import {
  callerLabel,
  formatDateTime,
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
    // 05:30 UTC is 10:30 in Lahore (UTC+5), whatever zone the test machine is in
    expect(formatDateTime("2026-11-16T05:30:00Z")).toBe("Mon 16 Nov, 10:30");
  });

  it("keeps a calendar day on its own date", () => {
    expect(formatDay("2026-11-16")).toBe("Mon 16");
  });

  it("formats durations, prices, latencies and rates", () => {
    expect(formatDuration(null)).toBe("in progress");
    expect(formatDuration(42)).toBe("42s");
    expect(formatDuration(125)).toBe("2m 05s");
    expect(formatPrice("2500")).toBe("PKR 2,500");
    expect(formatPrice(null)).toBe("on request");
    expect(formatMs(380.4)).toBe("380 ms");
    expect(formatMs(1834)).toBe("1.83 s");
    expect(formatPercent(0.4567)).toBe("46%");
    expect(formatPercent(null)).toBe("–");
  });

  it("reads Pakistani mobile numbers the local way", () => {
    expect(formatPhone("+923001234567")).toBe("0300 1234567");
    expect(formatPhone("+441234567890")).toBe("+441234567890");
    expect(formatPhone(null)).toBe("unknown number");
    expect(callerLabel(null, "webrtc")).toBe("Browser call");
    expect(callerLabel("+923001234567", "whatsapp")).toBe("0300 1234567");
  });

  it("labels outcomes for owners", () => {
    expect(outcomeLabel("booked")).toBe("Booked");
    expect(outcomeLabel(null)).toBe("Summarising…");
  });
});
