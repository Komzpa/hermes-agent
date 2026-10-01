---
title: "Kanban Multi-Gateway Deployment"
description: "Running one kanban board across several per-profile gateways: single dispatcher, profile-owned delivery"
---

# Multi-gateway deployment

Hermes supports multiple gateway processes running concurrently — one per profile
(default, writer, admin, coder, researcher). Each gateway opens its own connection
to platform APIs and delivers messages for its profile's subscribers.

Task subscriptions also cover review feedback. A `changes_requested` review
event is delivered as an actionable review-BLOCK notification. Subscriptions
using `notify+wake` additionally wake the exact originating chat/thread/session
so the controller inspects the existing card and current run; `notify` remains
passive-only and `wake` remains wake-only. Review feedback never creates,
unblocks, requeues, or otherwise mutates a task.

## Single-dispatcher posture

Only one gateway owns the kanban dispatcher. The owning gateway keeps
`kanban.dispatch_in_gateway: true` (the default); every other gateway sets it
to `false`.

**Why this matters:** dispatching is single-owner so multiple gateways do not
race to spawn the same work. Notification delivery is profile-owned instead:
each gateway polls only subscriptions for profiles whose platform adapters it
hosts. The atomic event claim prevents duplicate delivery across watcher
processes.

## Configuration

On the dispatch-owning gateway (typically the `default` profile), no change is
needed. On every other profile gateway, add to `~/.hermes/config.yaml`:

```yaml
kanban:
  dispatch_in_gateway: false
```

Or set the env var: `HERMES_KANBAN_DISPATCH_IN_GATEWAY=false`

## What each gateway does

| Gateway role | dispatch_in_gateway | Opens subscribed board DBs? | Dispatcher | Notifier |
|---|---|---|---|---|
| default (confirmed dispatch-lock owner) | true (default) | yes | yes | owned profiles + legacy unstamped subscriptions |
| writer, admin, coder, etc. | false | yes, when the profile has subscriptions | no | that gateway's owned profiles |

Non-dispatch gateways still deliver messages for their own platform adapters
(Telegram, Discord, etc.). They do not dispatch tasks, and they skip boards
that have no subscriptions owned by their profiles.

## Research and proactive brief cards

The canonical notifier can deliver completed research and proactive briefs to
Litterbox instead of duplicating their ordinary Telegram completion message.
Other completed work, urgent outputs, approval requests, blocks, review requests,
and failures retain their existing Telegram behavior.

Configure the owning profile's `config.yaml`:

```yaml
kanban:
  result_cards:
    ingest_url: https://your-litterbox-origin/v1/ingest
```

Inject `LITTERBOX_SOURCE_TOKEN` through that profile's secret environment / `.env`.
Use an agent source token bound by Litterbox to the intended tenant and source;
the payload cannot select a tenant or source. Never commit the token. An empty
URL or absent token leaves Telegram delivery unchanged. Multiplexed secondary
profiles keep Telegram; configure card delivery in their own gateway process
so another profile's token cannot receive their outputs.

The canonical worker prompt and `kanban_complete` tool schema ask workers to
classify the actual deliverable with the top-level `output_kind` parameter:
`research_result`, `proactive_brief`, or `other`. The completion handler writes
that classification into the persisted run metadata consumed by the notifier.
No title keyword guessing is performed. `urgent: true` or
`approval_required: true` in metadata retains Telegram delivery.
The full run summary (legacy task result when no run summary exists), not the
truncated event preview, becomes the offline-readable card body. Empty results
and title-only echoes are not cards. Litterbox's shared substantive-result
validation rejects acknowledgement/probe/process-status summaries.

The external ID is `kanban:<board>:<task>:result:<completion-event-id>`.
It is independent of subscriber, chat and delivery attempt. Retrying the same
event upserts the same `(tenant, source, external_id)` card, preserving dismissal;
a new completion event is a new result version. Use a source token scoped to
this producer/board namespace, not shared by independent Hermes installations.

Only the canonical ingest HTTP **204** receipt suppresses the matching ordinary
Telegram text result. Artifact-bearing results cut over with their bytes: the
producer collects explicit `artifacts` plus files discovered in completion prose
through one shared helper (the same source Telegram uploads use), reads up to 10
files / 8 MiB total, and posts them as ingest `files[{name, media_type, data
base64}]`. A 204 then suppresses both the Telegram text and the native uploads,
so no duplicate lands in chat. An unreadable/missing file, an over-limit batch,
or any non-204 answer keeps existing Telegram text and native uploads; nothing
is silently discarded.
Missing configuration, timeout, rejection, unexpected responses and transport
failure retain existing Telegram delivery and its send-failure rewind/drop
policy. Redirects are not followed. A lost receipt may leave a card plus a
fallback Telegram message; it never silently discards the result. Creator wakes
are unchanged. Wake-only subscriptions do not create passive result cards.

Source implementation is not live delivery proof: deployment still needs an
authorized endpoint and source-token injection, then a real receipt and inbox
readback. No installed gateway or account change is required for the focused
source gate: `scripts/run_tests.sh tests/gateway/test_kanban_result_cards.py
tests/gateway/test_kanban_notifier.py -q`.
