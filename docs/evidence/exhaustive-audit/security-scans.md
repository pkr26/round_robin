# Security scan evidence

Read-only scans against the frozen first-party source completed with **zero known Python/Node advisories**, **zero targeted Ruff findings**, and **zero credential-pattern hits**. Bandit reported **25 findings: 22 low, 3 medium, 0 high**; all are triaged below, with no actionable defect established. These scan commands did not edit source/test/configuration files; no Bandit suppressions were added. The final Bandit/Ruff/credential-pattern runs include the completed audit-runner (including the Darwin zombie-group repair) and renderer-input changes.

| Check | Verified result |
|---|---|
| Bandit 1.9.4 | 25 findings, 0 errors; exit 1 indicates findings, not a clean scanner pass |
| Ruff 0.17.5 (`E9,F63,F7,F82`) | 0 findings; this is a selected correctness rule set |
| pip-audit 2.10.1 | 57 pinned dependencies, 0 known advisories |
| pnpm 9.15.9 audit | 275 dependencies, 0 advisories at every reported severity |
| Local explicit credential patterns | 355 first-party text files, 0 potential secret locations; values never emitted |

The original Python final artifact has 56 dependencies/0 advisories; the fresh pinned-requirements scan adds only packaging 26.3. Pillow 12.3.0 had exactly 42 reported advisories in the initial artifact. Pillow 13.0.0 has zero in the new scan. Exact invocations, observed tool versions, byte counts, SHA-256 evidence hashes, scope and per-finding locations are recorded in [security-scans.json](security-scans.json). Original commands/versions were not captured and are not invented.

| Bandit group | Count | Source review disposition |
|---|---:|---|
| B101 | 1 | Fictional fixture precondition: The assertion checks a fixed authored sample horizon. It does not enforce authorization, spending or an untrusted live request boundary; live consumers validate their own inputs. Python optimization does not remove a live security gate here. |
| B105 | 2 | Not a credential: Development returns a null CSRF value; revocation clears the existing CSRF token to an empty string. Neither literal is an authenticating secret. Hosted mutation routes require a valid nonempty server token. |
| B108 | 1 | Private container tmpfs: The literal /tmp is inside the renderer container: bounded private tmpfs, mode 700, uid/gid 10001, noexec/nosuid/nodev. It is not a shared predictable host temp file; the container has no host mounts and uses a unique server job name. |
| B310 | 2 | Fixed loopback readiness probe: Both urlopen calls use a literal http://127.0.0.1:8000/ready with bounded timeout after local listener/process checks. No request URL or protocol is supplied by an untrusted caller, and no credentials or private payload accompany the probe. |
| B311 | 2 | Reproducible experiment ordering: Seeded random.Random shuffles study trials and review packets reproducibly. These calls do not generate sessions, CSRF tokens, provider credentials or cryptographic keys. Packet metadata retains separate private identity mapping and inspection requirements. |
| B404 | 4 | Reviewed subprocess import: Imports support fixed local verification/development commands and the isolated renderer. Call sites are argument vectors with shell=False default; an import alone is not an injection path. |
| B405 | 1 | Typed XML construction, no XML parsing: Diagram input is a bounded typed node/edge object. Element/SubElement/tostring construct SVG; labels are assigned as text and escaped by serialization. Arbitrary XML/SVG source is not accepted or parsed by this renderer. |
| B406 | 1 | Output escaping, no XML parsing: reports/exports.py imports only xml.sax.saxutils.escape and escapes saved text before ReportLab Paragraph rendering. No XML document parser or external entity resolution is invoked by this import/use. |
| B603 | 8 | Fixed argument vectors and bounded renderer: Reviewed all flagged call sites: audit/dev/verify use fixed command arrays and repository cwd. The native renderer passes HTML through stdin, binds the locally inspected sha256 image ID, bounds time/output and uses server-issued job IDs. No user text is interpolated into shell syntax. |
| B607 | 3 | Operator PATH boundary: docker and uv are intentionally resolved from the local operator PATH for development. Commands are fixed arrays, not browser-controlled executables. A compromised operator PATH remains outside this application authorization boundary; launchers are not privileged installers. |

The earlier 334-file secret-scan artifact was also checked: 0 hits, but 12 recorded file hashes changed afterward. A new explicit first-party scan supersedes it. It excludes `.local`, actual `.env` credentials, dependency trees, caches, build outputs and history. Fixed patterns cannot recognize every credential. No secret values are included in the checked-in evidence.

These checks cover known advisory feeds, selected static checks and the recorded source corpus. They do not prove absence of undisclosed issues. The separate [container security review](container-security.md) covers OS/runtime image packages and retains 40 vendor-unfixed package/advisory matches as open limitations. Final all-service test and coverage acceptance is recorded separately.

The final static refresh binds runner source `c4f5e848826f99f9eb2517613026695bd36badc502f078771496f0595001922f` and scans 11,948 Python source lines. It retains the same 25 reviewed Bandit findings, zero targeted Ruff findings and zero credential-pattern hits across 355 first-party files. Python/Node dependency scan results were retained without rerunning the advisory services: `uv.lock`, `pnpm-lock.yaml` and the exported requirements have unchanged recorded hashes. This is an input-scope check, not an assertion that advisory feeds cannot change.
