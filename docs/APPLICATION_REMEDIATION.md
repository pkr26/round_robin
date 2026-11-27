# Application audit remediation

The sequence below follows the [November 27 audit](APPLICATION_AUDIT_2026-11-27.md). A completed row requires an independent code review and an independently executed functional test on the accepted revision. The evidence column records test results and any limits.

| # | Finding | Status | Review and test evidence |
| --- | --- | --- | --- |
| 1 | A new report can declare expired offers ready | Accepted | The independent reviewer retested the original two-supplier failure and passed 33 focused tests. The independent tester passed three API/worker scenarios and three mounted UI checks on the revised implementation, including the inclusive local expiry, a requirement-only reassessment, frozen downloads and dispatch across midnight. The frontend suite passed all 301 tests. [Functional results](../.local/remediation/f1-functional/pytest-revised.log) · [UI results](../.local/remediation/f1-functional/vitest-revised.log) |
| 2 | A requirement attestation cites passages the owner never selected | Accepted | Reviewer approved after 15 focused checks. An independent mounted web workspace exercised the real loopback API and SQLite, selected only Northline’s delivery passage, and verified its exact ID and digest in the receipt, report and downloaded PDF/Markdown. Both suppliers remained in the comparison. Required cost sources plus corroboration still work. The offline backend suite passed 4,682 tests with 51 environment-dependent skips. [API results](../.local/remediation/f2-functional/pytest.log) · [Workspace results](../.local/remediation/f2-functional/component.log) |
| 3 | A true tie produces an arbitrary winner in the simulator | Accepted | Independent review passed 16 checks. Independent Chromium and API tests covered nine saved scenarios and 27 downloads, including ID permutations, ties, a status quo winner, unresolved offers, hard failures and different requirement applicability. JSON, Markdown, PDF and the web agreed on the decision and version. [Functional results](../.local/remediation/f3-functional/pytest-additional.log) |
| 4 | The decisive tradeoff and next action can be generic or unstable | In progress | Implementation and independent verification, revision 3. |
| 5 | The person reading the report has to reconstruct the evidence chain | Queued | |
| 6 | Saving the first draft requires too much, and a valid draft shape crashes editing | Queued | |
| 7 | Ordinary offers can be saved but cannot reach Start | Queued | |
| 8 | The confirmation screen omits part of what the owner is authorizing | Queued | |
| 9 | Reconnect can claim success while the API is still down | Queued | |
| 10 | The calculator understands quote terms that the editor cannot capture | Queued | |
| 11 | History, corrections and later outcomes are hard to follow | Queued | |
| 12 | Small text and browser coverage need attention | Queued | |
| 13 | The recorded “actual model” can be the configured alias | Queued | |
| 14 | Usage accounting can settle a Gemini cache charge at the ordinary input rate | Queued | |
| 15 | A healthy API can conceal a stalled review service | Queued | |
| 16 | The history list loads complete drafts and up to 20 detailed run views per meeting | Queued | |
| 17 | Waiting tabs repeatedly fetch complete snapshots and renew hosted activity | Queued | |
| 18 | Backup size can turn ordinary data growth into a service interruption | Queued | |
| 19 | Capacity checks grow with unrelated history | Queued | |
| 20 | A study arm can measure a different workflow from the product | Queued | |
| 21 | The release and paired scorecards accept weak evidence | Queued | |
| 22 | The clock for first useful content can stop before a user sees it | Queued | |
| 23 | Visual engineering is ahead of evidence for user value | Queued | |
| 24 | Historical test percentages obscure the current release state | Queued | |

External acceptance also requires consented owner sessions, independently graded provider trials, an invited staging environment, manual assistive-technology checks and a release decision on the deployment image. Those outcomes require their own evidence.
