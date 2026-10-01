# Durable ingress transfer

## Source prerequisite and acceptance boundary

The upstream base does not include the incident's durable inbound spool. This branch ports the existing generic overlay implementation from patches 0026, 0036, 0027, 0040 and 0041, with the behavior tests from 0028, 0035 and 0037. Startup belongs to the ingress owner. Patch 0025 is unrelated resume-policy wording and is deliberately excluded; the policy assertions from 0034 are likewise excluded. No private configuration, messages or archives are included.

The earlier four-test coordinator scaffold was failed acceptance of the incident transfer path: it did not contain the spool owner. Its batch identity RED remains useful but its preserved partial-fix GREEN is not full repair proof.

Prerequisite verification: canonical scripts/run_tests.sh on test_shutdown_flush.py, test_delivery_ledger.py, test_delivery_ledger_producer.py and test_queued_final_ledger.py: 69 passed. This proves the port's bounded compatibility, not the complete repetition repair.

## Required invariant

Each original input must map to one authoritative recoverable output, with all batch member links committed before retiring any inbound record. Recovery of an answered input must deliver the stored output, not regenerate it. Fresh same-text identities must still execute. An unfinished request must remain recoverable. One output row per member would duplicate a failed batch reply and is not acceptable.

Telegram delivery remains at-least-once when the process loses a send receipt. No exactly-once transport claim is made. Live rollout, historical spool reconciliation and upstream publication are separate integration/operator gates.

## Current implementation — independent acceptance still required

The ledger now records one output with transactional links from every durable
batch member. The original envelopes survive merge/serialization, queued-first
delivery, and recursive terminal propagation. Preprocessing exposes their typed
provenance and refuses to regenerate an already-transferred batch. Link tombstones
survive output-content retention; these identity-only rows currently have no
pruning policy. Failed durable recording refuses an unowned send.

The fix rebuilds the same event object from untransferred members before model
preprocessing and final ownership transfer. Original reply anchors and incoming
attachment paths/inlining flags follow the retained members. Pending duplicate
identities no longer append their text; fresh same-text identities still append.
The retained adapter admission guard persists ordinary inputs before dispatch
and declines already-transferred identities. Generic base/pending/reply/approval
boundary tests pass; full multiplexed admission acceptance remains unproven.

The existing output row now stores a versioned attachment manifest, routing/reply
metadata and per-component completion. Normal, queued and already-streamed finals
use the same attachment dispatch. Recovery skips confirmed text and completed
attachments; unavailable or policy-denied paths remain failed rather than being
silently dropped. Local paths are revalidated before upload, including recovery.
Failed queued turns do not record or upload their generated artifacts.

Successful platform receipts belong to the same authoritative output. Fresh
reply ingress resolves exact receipt plus session/platform/chat/thread scope;
unknown or ambiguous receipts remain unknown, regardless of quoted wording.
Original input identities and the prior output disposition enter only the new
turn, with `original_inputs` in existing transcript display metadata. Cached
prompts and old conversation messages are not rewritten.

## Persistence compatibility and transport limits

Schema initialization adds nullable `output_payload` to `delivery_obligations`
and creates `delivery_receipts`; it does not replace the ledger or spool. Legacy
rows without a payload retain text-only recovery. No historical receipt IDs are
invented. Receipt rows expire with their owning output; input-link tombstones
continue preventing tool re-execution after output-content pruning. Profile
routing uses the existing session key and adapter-profile resolver.

Rollback code can read the extended database, but older code cannot recover new
attachment payloads or respect component completion. Drain or explicitly reconcile
pending manifests before such a downgrade; preserve database/spool evidence.

A transport may accept a send before its receipt is persisted. A process crash
in that interval can resend content without rerunning the model. Partial success
inside a multi-chunk send or image-batch dispatch can also repeat accepted pieces
when the aggregate result is failed or lost. Persisted successful dispatch units
are not resent, but this is not exactly-once Telegram delivery. Attachment paths
are retained, not immutable byte snapshots; deleted/replaced files need explicit
handling and no transport test here establishes their remote availability.

## Verification and remaining acceptance

The separate regression commit `ec23626a16` exposes seven attachment-recovery
failures and two known-receipt ingress failures on source checkpoint
`730cc2ad53`. All 26 new cases now pass, including actual compaction plus disk
reload, unknown-receipt and fresh-same-text controls, and unchanged internal
silence. Retained compaction metadata already worked; no compression-engine
rewrite was needed. Fake summaries prove plumbing, not model response meaning.

Commands use `HERMES_PYTHON=$(command -v python3) scripts/run_tests.sh` with
`--file-retries 0 -j 2 -q`. The 17-file combined replay/compatibility run passes
248 cases with one skip and no failures. A separate six-file media/TTS run passes
35 cases. Private coordination receipts are `ingress-implementation-final.txt`
and `ingress-media-compat.txt`; they name every executed file. Baseline RED
receipts remain separately preserved rather than overwritten by GREEN.

Independent validation, combined notifier/current-main integration and semantic
report rows 5/6/9 remain required. No model evaluation or live transport was run.
Internal notification silence policy is unchanged. The coordinator clarified
that resolved/withdrawn approval acceptance means Kanban `needs_input`, owned by
the notifier lane; native exec approval redesign is excluded, not a blocker for
ingress. Live rollout and historical reconciliation remain operator-owned.

## Lessons learned

Verify that the regression base contains the incident's canonical spool owner.
Count one output with member links, never one response copy per member. Retain
the complete output before retiring inputs; track transport confirmation separately
from output ownership. Distinguish disk/compaction provenance proof from semantic
acceptance, and never infer receipt identity from repeated text.

Rollback remains local: revert the fix commits in reverse order, retaining the
separate regression/prerequisite commits and receipts. No runtime was changed.
