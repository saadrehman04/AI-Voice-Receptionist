import { describe, expect, it } from "vitest";

import {
  applyDraft,
  hoursFromForm,
  hoursToForm,
  slugify,
  starterConfig,
  toPayload,
  validateConfig,
} from "./tenant-form";
import type { TenantConfig, WebsiteDraft } from "./types";

function ready(): TenantConfig {
  return {
    ...starterConfig("Noor Beauty Salon", "salon"),
    services: [{ name: "Haircut", duration_min: 45, price: "2500" }],
    handoff: { name: "Ayesha", phone: "0300 1234567" },
  };
}

describe("slugify", () => {
  it("makes a URL-safe name the API accepts", () => {
    expect(slugify("Noor Beauty Salon!")).toBe("noor-beauty-salon");
    expect(slugify("  Café   Lahore ")).toBe("cafe-lahore");
    expect(slugify("نور")).toBe("business-new"); // nothing ASCII left
    expect(slugify("a".repeat(80))).toHaveLength(40);
  });
});

describe("hours", () => {
  it("round-trips between the API's shape and the form", () => {
    const form = hoursToForm({
      monday: [
        { start: "15:00:00", end: "20:00:00" },
        { start: "11:00:00", end: "13:00:00" },
      ],
    });
    expect(form.monday[0]).toEqual({ start: "15:00", end: "20:00" });
    expect(form.tuesday).toEqual([]);
    expect(hoursFromForm(form).monday).toEqual(["11:00-13:00", "15:00-20:00"]);
    expect(hoursFromForm(form).sunday).toEqual([]); // closed
  });
});

describe("validateConfig", () => {
  it("accepts a complete setup", () => {
    expect(validateConfig(ready())).toEqual([]);
  });

  it("explains what's missing in plain words", () => {
    const empty = starterConfig("Noor", "salon");
    const errors = validateConfig(empty);
    expect(errors).toContain("Add at least one service.");
    expect(errors.some((e) => e.startsWith("Enter who callers are handed to"))).toBe(true);
  });

  it("flags a service with no duration (we never guess one)", () => {
    const c = { ...ready(), services: [{ name: "Facial", duration_min: 0, price: null }] };
    expect(validateConfig(c)).toEqual(['Set how many minutes "Facial" takes.']);
  });

  it("flags bad hours, duplicate services and bad prices", () => {
    const c: TenantConfig = {
      ...ready(),
      services: [
        { name: "Haircut", duration_min: 30, price: "-5" },
        { name: "haircut", duration_min: 30, price: "abc" },
      ],
    };
    const hours = hoursToForm(c.opening_hours);
    hours.monday = [
      { start: "10:00", end: "14:00" },
      { start: "13:00", end: "18:00" },
    ];
    hours.tuesday = [{ start: "18:00", end: "09:00" }];
    const errors = validateConfig(c, hours);
    expect(errors).toEqual(
      expect.arrayContaining([
        '"haircut" is listed twice.',
        'The price of "Haircut" can\'t be negative.',
        'The price of "haircut" must be a number.',
        "The opening hours on monday overlap.",
        "On tuesday, opening time must be before closing time.",
      ]),
    );
  });

  it("checks the WhatsApp number id is Meta's digits-only id", () => {
    expect(validateConfig({ ...ready(), whatsapp_phone_number_id: "106540352242922" })).toEqual([]);
    expect(validateConfig({ ...ready(), whatsapp_phone_number_id: "+92 300" })).toHaveLength(1);
  });

  it("starts new businesses in English, on the owner's clock, in US dollars", () => {
    const c = starterConfig("Bright Smile Dental", "dental clinic");
    expect(c.languages).toEqual(["en"]);
    expect(c.greeting).toEqual({ en: "Thank you for calling Bright Smile Dental. How can I help you today?" });
    expect(c.timezone).toBe(Intl.DateTimeFormat().resolvedOptions().timeZone);
    expect(c.currency).toBe("USD");
  });
});

describe("applyDraft", () => {
  const draft: WebsiteDraft = {
    business_name: "Noor Beauty Salon",
    address: "12-B Main Boulevard",
    opening_hours_text: null,
    services: [
      { name: "Haircut", price: "9999", price_note: null, duration_min: 30, evidence: "" },
      { name: "Facial", price: null, price_note: null, duration_min: null, evidence: "" },
    ],
    faq: [{ question: "Parking?", answer: "Free parking at the back.", evidence: "" }],
    dropped: [],
  };

  it("adds new services and FAQ, never overwrites what the owner typed", () => {
    const out = applyDraft(ready(), draft);
    expect(out.services.map((s) => [s.name, s.price, s.duration_min])).toEqual([
      ["Haircut", "2500", 45], // the owner's, untouched
      ["Facial", null, 0], // duration left for the owner
    ]);
    expect(out.faq).toEqual([{ question: "Parking?", answer: "Free parking at the back." }]);
    expect(out.address).toBe("12-B Main Boulevard");
  });
});

describe("toPayload", () => {
  it("sends hours as strings and empty prices as null", () => {
    const c: TenantConfig = {
      ...ready(),
      services: [
        { name: " Haircut ", duration_min: 45, price: "2500" },
        { name: "Consultation", duration_min: 15, price: "" },
      ],
    };
    const payload = toPayload(c, hoursToForm(c.opening_hours)) as {
      services: { name: string; price: number | null }[];
      opening_hours: Record<string, string[]>;
    };
    expect(payload.services).toMatchObject([
      { name: "Haircut", price: 2500 },
      { name: "Consultation", price: null },
    ]);
    expect(payload.opening_hours.monday).toEqual(["10:00-19:00"]);
  });
});
