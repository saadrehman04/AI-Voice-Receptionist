# Connecting Google Calendar (free)

The api books appointments into each business's Google Calendar through a **service account**:
a robot Google identity that the business shares its calendar with. Free; no billing account needed.

Until this is set up, `CALENDAR_PROVIDER=fake` keeps bookings in an in-memory calendar (they are
still stored in Postgres).

## 1. Create the service account (once, ~5 minutes)

1. Go to https://console.cloud.google.com/ and create a project, e.g. `voice-receptionist`.
2. **APIs & Services -> Library**: search "Google Calendar API" and click **Enable**.
3. **APIs & Services -> Credentials -> Create credentials -> Service account**.
   Name it `receptionist-bot`. Skip the optional role and user steps.
4. Open the new service account -> **Keys -> Add key -> Create new key -> JSON**.
   A `.json` file downloads.
5. Move it into the repo root as `service-account.json`. It is git-ignored
   (`.gitignore` has `service-account*.json`). Never commit it or paste it anywhere.
6. Note the service account's email, like
   `receptionist-bot@voice-receptionist.iam.gserviceaccount.com`.

## 2. Share each business calendar with it

For a test, use a new calendar in your own Google account:

1. In Google Calendar: **Other calendars -> + -> Create new calendar**, e.g. "Noor Salon (test)".
2. That calendar's **Settings and sharing -> Share with specific people -> Add people**:
   paste the service account email and choose **Make changes to events**.
3. Under **Integrate calendar**, copy the **Calendar ID**
   (looks like `abc123...@group.calendar.google.com`).

## 3. Point the tenant and the api at it

In `tenants/demo-salon.yaml`:

```yaml
calendar_id: abc123...@group.calendar.google.com
```

In `.env`:

```
CALENDAR_PROVIDER=google
GOOGLE_SERVICE_ACCOUNT_FILE=service-account.json
```

Restart `make api`. Availability now reads the calendar's busy times (including events the owner
adds by hand), and each booking creates an event named like "Haircut: Sara".

## How it behaves

- Event ids are derived from the booking's idempotency key, so a retried write can never create a
  second event (Google answers 409, which we treat as "already done").
- The event is written inside the database transaction. If Google fails, the booking is not saved
  and the caller is offered a message instead; if the database fails after the event was created,
  the event is deleted again.
- If the calendar can't be reached at all, availability returns an error and the agent says it will
  take a message: it never offers times it couldn't check.
