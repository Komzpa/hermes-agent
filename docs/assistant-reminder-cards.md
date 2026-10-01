# Assistant reminders as Litterbox cards (R26/R27)

Assistant-set reminders become timed Litterbox cards through the shared
source-auth ingest. There is one transport owner:
`gateway/result_ingest.py::ingest_result_card` (HTTP 204 is the receipt).

## Producer classification (never by regex)

- Model tool `cronjob_manage create` accepts `output_kind="assistant_reminder"`
  (plus optional `urgent` / `approval_required`). It persists as
  `job["output_kind"]` via `cron/jobs.py::create_job` (validated; absent key =
  ordinary scheduled job).
- `update` can set/clear the same fields. Clearing restores ordinary
  scheduled-job semantics.
- The scheduler never regex-matches content to decide. `is_assistant_reminder_job`
  checks the stored `output_kind` only.

## Fire path (native scheduler → shared delivery)

1. The atomic fire claim stamps the exact due occurrence as
   `job["_scheduled_instant"]` before the recurring cursor is advanced; the
   returned claim carries it and completion clears the claim. Delivery builds
   `kind="reminder"`, `timed=True`, `at=<_scheduled_instant>` and
   `external_id="cron:<job_id>:reminder:<_scheduled_instant>"`. A replay keeps the
   same card; the next fire gets a distinct card. Unclaimed jobs (no
   `_scheduled_instant`) cannot invent a timestamp. Future jobs are refused by the
   native scheduler's due scan.
2. POST via the shared ingest (`try_ingest_assistant_reminder`). Media
   `MEDIA:` attachments are base64-encoded into the card; uninstallable or
   oversize batches abort the ingest (Telegram retained, never lost).
3. Only after an actual HTTP 204, skip the matching ordinary Telegram
   text/files for that run. Non-Telegram targets still deliver.
4. Failed ingest (non-204, missing endpoint/token, transport error) preserves
   ordinary Telegram delivery. `urgent` / `approval_required` jobs never
   attempt ingest and always retain Telegram. Empty or title-duplicate content
   never ingests. Unrelated jobs (`output_kind` absent/other) are untouched;
   creator-wake / mirror / bot-chat semantics are unchanged.

## Operator config

- `kanban.result_cards.ingest_url` in `config.yaml` (e.g. the Litterbox
  `/v1/ingest` URL) plus `LITTERBOX_SOURCE_TOKEN` (source-scoped Bearer).
  Without both, reminders fall back to plain Telegram delivery.
- Verify with an isolated job store: invoke native `tick`, observe claim,
  execution, persisted completion and the actual HTTP 204, then read the exact
  full summary and scheduled instant through `GET /v1/cards`. Replay the
  claimed occurrence through `run_one_job`; advance to the next recurring
  fire and verify distinct IDs. A direct `_deliver_result` call does not prove
  scheduler timing. This source behavior is not a production-deployment claim.

## Negative controls

- Wrong source token (401) → Telegram retained, no card.
- `urgent=true` or `approval_required=true` → no ingest attempt, Telegram sent.
- Ordinary job (no `output_kind`) → no ingest attempt, Telegram sent.
