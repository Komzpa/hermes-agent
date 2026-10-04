# Evidence-versioned Kanban recovery

Recovery is disabled by default. The operator can opt in using `config.yaml`:

```yaml
kanban:
  recovery_review:
    enabled: true
    root_tasks: [task-to-verify]
    max_per_tick: 1
```

The scope includes the listed tasks and their incoming prerequisites, never
their outgoing dependents. Native edges point from prerequisite to dependent.
Only inactive blocked, todo or triage tasks with satisfied prerequisites and
no live claim, run or worker can be considered. Existing dispatcher concurrency,
profile, respawn, review and completion gates continue to apply.

The classifier can return only `wait` or admission to independent `review`.
Review admission requires references to retained evidence owned by the task,
a spawnable reviewer, and original implementation-run provenance. A fresh
review run may use the same profile; it never substitutes the current assignee
for a different historical implementer. Requested changes return to that actor,
who must also pass the dispatcher's profile allowlist before review admission.
Only ended completion, review-handoff or blocked implementation runs with retained
summary or artifact evidence qualify; failed, timed-out and empty attempts do not
replace the original implementer.
It cannot complete, archive, edit, unblock or reset loop counters. A published
PR is not evidence of green exact-head CI; genuine external blockers stay put.
The existing reviewer must verify actual artifacts, current requirements and
any successor before accepting or retiring superseded work.

CLI handoffs, tool handoffs and between-turn goal judgments share a bounded,
attributed acceptance snapshot. Historical comments and receipts are evidence
data, not instructions or authorization. Review readiness is distinct from
final completion. The original contract and retained evidence remain intact.
Attachment filenames are bounded and redacted; absolute stored paths are not
included in auxiliary input. Both classifier and goal judge use a shared
structured run/event metadata sanitizer: local absolute paths become basename-only
references, and malformed metadata is omitted. Newest attachments survive budget
trimming; raw local versions still include the original metadata.
Both consumers use the same
aggregate context budget, preserving the full contract and labelling omitted
historical rows. Neither reads arbitrary local artifacts.
Operational claim locks and worker PIDs remain in the local snapshot for
staleness checks, but are omitted from both auxiliary prompt payloads.
Raw persisted contract and selected evidence rows are hashed locally before
redaction or truncation. That local fingerprint is omitted from prompt payloads;
secret-shaped edits still invalidate cached decisions and in-flight admission.
Legacy long titles and bodies are not rejected by per-field caps: only the shared
aggregate budget applies. A contract that cannot fit intact blocks its owning
goal-mode run rather than silently truncating criteria or stranding a running card.
The bounded prerequisite list is backed by a digest of every native edge and status,
so changes beyond its first 32 rows invalidate cached and in-flight decisions too.

The dispatcher records `recovery_review_checked` with the snapshot digest and
decision and policy version. Unchanged successful decisions and invalid response
schemas are not rejudged. Transport failures and unavailable review routing
retry after a five-minute cooldown rather than permanently caching failure.
Operational admission failures after a schema-valid response use that same cooldown;
invalid schemas remain fail-closed until their input or policy version changes.
New material board evidence changes its version
and permits another pass. External state changes alone do not change this
snapshot: their refreshed receipts must be recorded by the existing evidence
producer. This mechanism is not an external CI poller.

Admission rereads the snapshot inside the native write transaction. Concurrent
contract, evidence, dependency or claim changes reject the stale decision.
Disabling recovery or removing its configured scope during a model call also
refuses admission. Dry-run dispatch does not invoke recovery or write audit events.

After the normal claim tick, the dispatcher launches an isolated profile-local
recovery process with the exact existing board path.
Before promotion, inactive opt-in candidates are reserved until their current
evidence version has been classified. A valid unchanged `wait` without a routing
retry releases ordinary promotion; errors or new evidence keep the reservation.
Disabled recovery and tasks outside the configured scope retain normal dispatch.
The canonical served-profile child environment supplies only the target profile's
owned credentials when routing between profiles, never the launch profile's residue.
Its existing permission scope is unchanged. A separate singleton lock
serializes recovery passes; provider waits do not hold the dispatch lock or
delay ordinary reclaim/claim/spawn work. Native review admission uses its own
short transaction and is available to the next normal dispatcher pass.

The synthetic tests prove routing, evidence preservation, idempotence and race
guards, not the semantic correctness of a live model decision. Production
acceptance requires canonical candidate gates, scoped activation and observed
native reviewer transitions on the affected tasks.
