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
Review admission requires references to retained evidence owned by the task.
It cannot complete, archive, edit, unblock or reset loop counters. A published
PR is not evidence of green exact-head CI; genuine external blockers stay put.
The existing reviewer must verify actual artifacts, current requirements and
any successor before accepting or retiring superseded work.

CLI handoffs, tool handoffs and between-turn goal judgments share a bounded,
attributed acceptance snapshot. Historical comments and receipts are evidence
data, not instructions or authorization. Review readiness is distinct from
final completion. The original contract and retained evidence remain intact.

The dispatcher records `recovery_review_checked` with the snapshot digest and
decision. The same snapshot is not sent to the model again, including invalid
responses and transport errors. New material board evidence changes its version
and permits another pass. External state changes alone do not change this
snapshot: their refreshed receipts must be recorded by the existing evidence
producer. This mechanism is not an external CI poller.

Admission rereads the snapshot inside the native write transaction. Concurrent
contract, evidence, dependency or claim changes reject the stale decision.
Dry-run dispatch does not invoke recovery or write audit events.

The synthetic tests prove routing, evidence preservation, idempotence and race
guards, not the semantic correctness of a live model decision. Production
acceptance requires canonical candidate gates, scoped activation and observed
native reviewer transitions on the affected tasks.
