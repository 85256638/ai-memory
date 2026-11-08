# Windows companion validation

Date: **2026-11-08**. Base: upstream **v3.4.3**. This record describes the
portable companion in this fork; it is separate from the validation history
of any private installation.

## Completed locally on Windows

| Check | Result |
| --- | --- |
| Python source parsing | 11 files passed |
| Windows PowerShell syntax parsing | 12 files passed |
| `install.ps1 -Plan` in a temporary installation | No data/backup directories written |
| Installation into separate temporary data and backup directories | Source copied and C# tray compiled successfully |
| Generated settings read back through Python | Custom data, binary and backup paths matched |
| Startup registration during installation test | Disabled; no service, tray or scheduled task started |
| Synthetic regression suite | 16 tests passed |

The regression suite includes:

- Image original-byte preservation, event scope/auth/usage preservation,
  unsupported events left unchanged, and concurrent repair refusal.
- Valid partial `recover` reports retained on exit 1, invalid reports and other
  CLI errors rejected.
- Portable archive paths, invalid settings rejected, private credential
  snapshot bytes retained without printing them, and SQLite verification
  without extracting traversal entries.
- Gateway authentication, fixed paths, body limits, changed-destination
  refusal and credential-bearing redirects, with authorized forwarding
  controls. All HTTP fixtures listen on temporary loopback ports.

The workflow [Windows companion](../../.github/workflows/windows-companion.yml)
repeats PowerShell parsing, C# compilation and regressions on a Windows runner.
Its actual run status is recorded by GitHub Actions, not assumed here.

## Publication checks

Publication excludes installed data, credentials, private configuration,
project mappings, operational reports, archives and compiled binaries.
The staging audit checks for known local credentials and personal identifiers
without printing their values. Gitleaks checks the source tree and the new
commit range with redacted output. The original upstream author/license
information remains intact.

The pre-publication tree scan found no known local credentials, personal
identifiers or prohibited runtime artifacts in the 35 staged files. Gitleaks
reported 17 findings in unchanged upstream sanitizer/e2e fixtures; their Git
blob identities matched the official v3.4.3 tag. None was introduced by this
fork, and no new scanner allowlist was added.

These checks reduce accidental disclosure; they do not replace reviewing the
exact staged files for future contributions. Local operational audit receipts
are deliberately kept outside this public checkout.

## Not claimed

- No clean-machine deployment or real Windows sign-out/reboot test of this
  generalized package was performed.
- The tray was compiled, but its position in the Windows notification area
  was not visually inspected during this publication.
- No real OAuth account or relay was invoked by the regression suite.
- No upstream Rust code, Cargo manifest, dependency lockfile or Hook bundle
  was changed. The full Rust build/test gates were not rerun for this
  companion publication.
- No compatibility claim is made for future upstream versions, multiple
  installations, custom service ports or non-Codex Hook trust diagnostics.
