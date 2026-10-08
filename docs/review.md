# Human review decisions (Step 37)

A person can record an explicit decision about a content preview:

| Decision | Meaning |
|---|---|
| `approved_for_preview` | This exact preview is accepted **as a preview**. It is not permission to publish, not rights clearance and not fact verification. `publishable` stays `false`. |
| `changes_requested` | The preview needs changes. Nothing is revised automatically. |
| `rejected` | The preview is not accepted. |

Decisions are recorded only from the command line. The Living HQ shows them and never
records anything.

## Commands

From the repository root (`.\.venv\Scripts\python.exe` on Windows, `.venv/bin/python` on
Linux/macOS):

```
python -m vicekrack quality-report PRODUCTION_ID              # a Step 36 bound report, if you have none
python -m vicekrack review-list PRODUCTION_ID                 # history + what a decision needs
python -m vicekrack review-record PRODUCTION_ID --report REPORT_ID --binding DIGEST \
    --decision approved_for_preview --reviewer "Rich" [--ack CODE ...] \
    [--notes "text" | --notes-file notes.txt] [--supersedes REVIEW_ID]
python -m vicekrack review-inspect PRODUCTION_ID REVIEW_ID
```

- **`review-list`** prints every saved decision, newest first, with whether it applies
  now. It also lists each saved quality report of the production with the information a
  new decision needs:
  - its binding status and **binding digest**;
  - the technical result and the conditions seen;
  - the acknowledgments that apply;
  - the decisions allowed and anything blocking approval.
- **`review-record`** appends one decision. `--binding` must be the report's binding
  digest (or at least its first 12 hex characters), so the decision names exactly the
  artifacts you reviewed. Once a decision exists, `--supersedes` must name the latest one.
  Exit code 0 only when the record was saved.
- **`review-inspect`** prints one saved record and whether it applies now.

## Rules

Checked when recording, and again immediately before saving, while the production lock is
held (no production resume or quality check can run meanwhile):

1. **Matching binding.** The quality report must be a Step 36 report (version 1.1) whose
   binding still matches the current files (`quality-binding` = `matching`). Legacy reports
   cannot be reviewed. This applies to every decision.
2. **No approval of a failed report.** If the technical result is `fail`, only
   `changes_requested` or `rejected` can be recorded.
3. **Evidence freshness must be known.** Approval is refused when the verification record's
   freshness cannot be established now. Examples: the record is missing or invalid, the
   policy changed, or the record is dated in the future.
4. **Explicit acknowledgments.** Approval needs `--ack` for each condition that applies:

   | Acknowledgment | Applies when |
   |---|---|
   | `needs_review_result` | the technical result is `needs_review` |
   | `unavailable_checks` | any check is `unavailable` |
   | `draft_restrictions` | the production is a draft (unverified claims) |
   | `stale_evidence` | the verification record is older than its policy allows, now |

   Acknowledging a condition that does not apply is refused, so a record never claims more
   than was seen.
5. **Technical findings are unchanged.** The quality report is never edited.
6. **Changes during review are refused.** The system validates once, builds the record,
   then validates again before saving. The decision is refused, and nothing is saved, if
   any of these changed in between:
   - a bound file, or the report file;
   - the binding digest;
   - the conditions, including the evidence status;
   - the review history.

## Records and history

Saved as `runtime/reviews/<production_id>/<sequence>-<review_id>.json` (ignored by Git),
schema `schemas/content-review.schema.json` (`content_review` 1.0). Each record holds:
- the production ID, sequence number and the review it supersedes;
- the decision;
- the reviewer label with `authenticated: false`;
- the UTC time;
- the quality report's ID, file SHA-256, result and check time;
- the exact binding digest (SHA-256 of the report's canonical `binding`);
- the conditions seen (technical result, unavailable checks, draft, evidence freshness at
  check and now);
- the acknowledgments;
- optional notes (plain text, up to 2,000 characters);
- a fixed scope block: preview only, `publishable: false`, technical findings unchanged.

- **Append-only.** Records are never edited or deleted. A later decision names the
  previous latest in `supersedes`; nothing is overwritten.
- **Concurrent decisions are never lost.** Two things protect against a concurrent writer:
  - only one process can hold the production lock;
  - each record must supersede the current latest, and files are published under an
    exclusive name.

  A second reviewer who decided on an older history is refused with `review_conflict`.
  They re-read the history and decide again.
- **Atomic writes.** A record is written to a temporary file, flushed, then published with
  an exclusive hard link. An interrupted write leaves at most a `*.tmp` file, which is
  ignored and never promoted.
- **Corruption is visible.** If any record is unreadable or invalid, does not match its
  file name, breaks the sequence/supersedes chain, or an unexpected file appears:
  - the history is `history_corrupted`;
  - no decision is shown as current;
  - new decisions are refused until the history is repaired by hand.
- **Secrets and errors.** Records pass the same credential check as other saved files, so a
  note containing a key-like value is refused. Errors are fixed codes and never repeat the
  note.

## Whether a decision applies now

This is computed every time it is read, never stored:

| Field | Values |
|---|---|
| `applicability` | `current` (latest, report unchanged, files still match), `superseded`, `invalidated` (the report or a bound file changed or is missing), `unavailable` (history corrupted) |
| `artifact_binding_now` | `matching`, `changed`, `unavailable` |
| `current_preview_approval` | true only for the latest `approved_for_preview` that is `current` **and** whose evidence is still fresh now, or was stale and acknowledged as stale |

Evidence freshness is reported separately from artifact matching. Evidence that became
stale after an approval keeps the decision `current` with matching artifacts, but
`current_preview_approval` becomes false with `evidence_became_stale_since_review`.
Changing a file back to its exact bound bytes makes the decision current again.

## Living HQ

The Content Results Desk shows a **Human review decisions** section:
- whether there is a current preview approval;
- evidence freshness now;
- each decision with its reviewer label (marked self-declared), applicability, artifact
  binding now, quality report and digest, acknowledgments, supersession and notes.

Notes are shown as text and never interpreted as markup. The desk has no controls that
record anything.

In replay, a decision is shown only if a recorded timeline proves it was saved by that
position (its time is not after the event at that position). Even then, its applicability
is `not_evaluated_at_position`. Reconstructed timelines, corrupted histories or
inconsistent times make historical review status `unavailable`.

![Human review decisions in the Content Results Desk](images/hq/hq-step37-reviews-desktop.png)

## Privacy and limits

- **Notes are user text** and may contain sensitive content. They are stored unencrypted in
  local ignored storage and shown in the Living HQ. Keep them out of Git and check them
  before sharing a folder.
- **Reviewer labels are self-declared.** Anyone with access to the folder can type any
  label. There is no sign-in, signature or identity check.
- **Local trust only.** Someone with write access to the folder can delete or forge review
  files consistently. Corruption checks catch inconsistent edits, not a careful forger.
- **Not built:** publishing approval, uploads, automatic revisions and any dashboard write
  control. Approved previews can be packaged locally with `export-preview --purpose
  approved_preview` (Step 38, see [Portable preview packages](export.md)).
