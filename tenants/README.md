# tenants/

One YAML file per business: `tenants/<slug>.yaml`, validated by
`receptionist_shared.tenant.TenantConfig` (unknown keys are rejected, and the slug must match the
file name). These are the ONLY business facts the agent may state.

| Key | Notes |
| --- | --- |
| `slug`, `business_name`, `business_name_ur`, `business_type`, `address` | Identity. |
| `languages`, `default_language` | `en` and/or `ur`. Urdu tenants need `greeting.ur`. |
| `timezone`, `currency` | e.g. `Asia/Karachi`, `PKR`. |
| `opening_hours` | Per weekday: `["09:00-13:00", "14:00-18:00"]` or `closed`. |
| `services` | `name`, `name_ur`, `duration_min`, `price` (`null` = price on request, never quoted), `price_note`, `staff`. |
| `policies` | `cancellation`, `late_arrival`, `payment`, `other`. |
| `greeting` | `en`, `ur`: the first thing callers hear. |
| `handoff` | Who calls back; the phone number is never read out. |
| `faq` | `question`, `answer`, `answer_ur`, `keywords` (whole-word matches). |
| `booking_buffer_min`, `min_notice_min`, `max_days_ahead` | Slot rules. |
| `calendar_id` | Google Calendar id (Phase 3). |

Examples: `demo-salon.yaml` (bilingual, English first) and `demo-workshop.yaml` (Urdu first).
Pick one for test calls with `DEFAULT_TENANT` in `.env`, or `make chat ARGS="--tenant demo-workshop"`.
