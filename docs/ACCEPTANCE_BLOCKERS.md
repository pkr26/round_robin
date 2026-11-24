# Acceptance work after implementation

November 16, 2026. The user authorized construction through the remaining
phases while deferring external acceptance work. The [fresh implementation audit](evidence/exhaustive-audit/REPORT.md)
is the current engineering review and records the accepted full verification run.
All 18 service/package groups exceed 95% line and branch coverage. Earlier
implementation and audit records describe historical snapshots. This engineering
pass does not clear the open upstream dependency advisories or external release gates.

The following previously missing workflows are now implemented. They are no
longer open engineering integration blockers; their product-quality and
real-provider acceptance obligations remain separate.

| Contract | Current implementation | Acceptance limit |
| --- | --- | --- |
| RPT-03: bounded substantive correction/re-audit | A successfully returned negative/critical audit can fund one corrective synthesis and a new independent audit. Original run/owner/deployment money, logical-role calls, infrastructure claims, requests, clocks and fences remain in force; insufficient resources produce an honest limited result. | Synthetic correction and exhaustion tests do not establish that real models reliably identify or repair defects. |
| Claim/version persistence and EVD-14 parent reuse | Successful output commits immutable server-assigned claims atomically. Explicit selected parent identities, versions and hashes are revalidated at Start and copied to child-scoped records with provenance and freshness treatment; unselected/foreign support is not imported. | Human passage support and real-world freshness still need evaluation. Apply the claim/reuse migration before compatible workers and retain those records through backup/deletion workflows. |
| LIM-09: atomic active-parent replacement | A linked draft preserves its active parent until a new preflight and explicit matching parent-version Start authorize replacement. Parent fencing/finalization intent and one queued child commit together; failed admission rolls the transaction back and existing exposure remains counted. | Deployment-specific recovery and usability must still be exercised; saving a draft alone does not authorize Stop or new inference. |

The remaining acceptance work requires observations, explicit funding, vendor
dependency fixes or a deployed environment. Passing deterministic tests or exceeding the executable
coverage threshold does not satisfy these gates.

| Item | Evidence to collect | Prepared implementation |
| --- | --- | --- |
| Customer workflow and comprehension, phases 0/4 | Five consented target-user records, supported evidence-format assessment, documented proceed/narrow decision; at least four of five independently complete and understand the integrated comparison | [Research protocol](RESEARCH_PROTOCOL.md), fictional sample and connected `/live` workflow |
| Funded protocol choice, phases 0B/2 | Freeze tasks, models, prompts, prices, limits and reviewer rules; authorize funding separately; run 16 independent cases with five fixed repeat cases, 26 trials per arm and 78 total; retain human grades and original failures | [Evaluation tools](../experiments/README.md), opt-in study runner, provider adapters and durable accounting |
| Local release, phase 5 | Freeze 48 fresh held-out end-to-end cases, eight in each required stratum; independent human passage/constraint review, at least 39/48 useful primaries, actual provider cost/latency, full applicable combined-fault review and timed recovery | [Release evaluation validator](../experiments/release.py), actual-service fault tests, measurement endpoint and operator CLI |
| Accessibility, phase 4/5 | Manual keyboard and screen-reader assessment of intake, status/Stop, evidence, follow-up, errors and downloads; record observed issues and fixes | Automated browser keyboard, narrow-layout and workflow checks |
| Invited hosting, phase 6 | Configure an actual OIDC client and issuer/subject invitations, TLS/proxy, private storage, explicit retention/backup schedule and recovery objective; exercise two-owner access, expiry, reconnect, deletion, migration/rollback and quarantine in staging | [Operator runbook](OPERATIONS.md), opaque sessions/CSRF, boundary tests and synthetic restore |
| Visual demand and capability, phase 7 | At least three target users with unmet jobs in one supported raster domain; capable human reviewers; explicitly funded real-model tests proving actual pixel input and two distinct outside critics when the generator is known | Gated `/design`, canonical assets, immutable versions, image/vision adapters, separate queue and actual-process tests |
| Useful automatic improvement, phase 8 | Freeze 12 matched briefs and criteria, four fixed briefs with three executions; compare generation alone, one-critic revision and multi-critic revision (20 trials per arm, 60 total); two competent independent reviewers for at least half and every suspected hard regression; retain decline, neutral/worse results and unknown costs | [Prospective visual study validator](../experiments/visual_evaluation.py), bounded image/critique/edit pipeline and user selection |
| Native domain acceptance, phase 9 | Explicit native-output demand decision and domain-specific human/functional evaluation; staging access/recovery tests before public delivery | Validated editable diagram rendering and tested isolated browser rendering with source/provenance hashes |
| Native container dependencies | Vendor fixes and a fresh immutable-image scan for the remaining 27 distinct upstream OS CVEs (40 medium/low package matches), followed by actual rendering/security checks before public release | Minimal supported Ubuntu 26.04 Chromium-only image; all high/critical and vendor-fixable scan findings removed. No finding suppression; unresolved matches and vendor status remain in the fresh audit report. |

The [original phase plan](IMPLEMENTATION_PHASES.md) and
[visual workflow](VISUAL_DESIGN_WORKFLOW.md) retain the full acceptance criteria.
All 100 failure IDs remain tracked individually; a partial tested invariant does
not declare the entire scenario or phase passed. CI configuration is available,
but checked-in evidence records local execution rather than a hosted CI run.

No users were contacted, no paid provider/search/image requests were made and
no application was deployed during this implementation. Credentials alone do
not enable paid work. Default simulation, closed optional feature gates and
operator spending locks remain explicit controls until configured.
