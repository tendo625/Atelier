# Windows packaging review against the implementation brief

The checkout remains on `feature/windows-packaging`, based on maintainer `main`
at `d592133c17d516056d07b5f5918e1838e49f9314`. No redesign branch was merged.
The maintainer's tool manifest and Python exclusions remain in place; Linux
staging is unchanged. No pull request or official release is part of this review.

## Defects found and corrected

| Finding | Correction and verification |
| --- | --- |
| Installer smoke recorded the delivered EXE hash but ran a recompiled replacement | Execute the delivered EXE for upgrade and fresh install, using a restricted test registration/shortcut namespace. Preserve the normal upstream identity; compare its registry values before/after. |
| Portable ZIP extraction verification was an external one-off script | Verify exact ZIP membership/content, extract it, launch the extracted executable in WebView2 and recheck its manifest in every build. |
| Application writes after manifest generation could make hashes stale | Reject changed, added, removed payload files or a changed manifest after smoke tests and before archive assembly. |
| Matching source ZIP could not build without Git history | Verify its exported manifest and provenance, support archive-only builds, reject edited/extra source, and test the archive path with Git unavailable. |
| .NET was checked only at application launch | Check .NET and WebView2 during build preflight, bootstrap and installer preparation. CI can provision Microsoft's signed WebView2 bootstrapper when missing. |
| Test dependency pins were not fully validated | Validate runtime, build and all development/test pins; clear inherited Python path overrides and isolate PyInstaller's cache. |
| Uninstall test omitted a fresh-install path and desktop-off selection | Exercise both shortcut settings, first-run Setup without configured fixtures, and fresh removal using the delivered installer. Record user-data preservation hashes. |
| Diagnostic/cleanup errors after a validated release could report a failed build | Retain the valid release outcome and report diagnostic/cleanup problems without masking the original validation failure. |
| Some runtime inputs did not trigger CI | Add version, root Python entrypoints, icon and licence paths to the packaging workflow filter. |
| Hidden WebView2 PNG capture could time out after successful functional checks | Bound diagnostic captures, record warnings, and independently assert computed theme colours; keep all functional, native-shell, resource and backend assertions mandatory. |
| Native policy accepted case-duplicate paths and credential-bearing provenance URLs | Reject Windows case duplicates, noncanonical paths, environment files and credential/signed-token URLs; retain per-file binary/licence hash checks. |

## Brief coverage

| Area | Result |
| --- | --- |
| Unified Windows build | BUILD.bat delegates to one orchestrator; pinned dependencies/compiler, isolated snapshots/work/logs, version/PE metadata and guarded cleanup. |
| Portable and installer | Same verified application payload, Python runtime/resources/licences; per-user modern installer, Start Menu and optional desktop shortcut. |
| Dependency boundaries | Runtime/build/test requirement files; explicit approved native-file policy; user/game/private data excluded. Missing dependencies report feature-specific guidance. |
| User-data protection | Upgrade replaces only the owned runtime; no recursive app-root/Tools/project cleanup. Upgrade/uninstall preserve seeded project edits, exports, preferences, paths, mappings and custom tools. |
| Automated verification | Existing suites retained, targeted release regressions, frozen bytecode/resources/PE checks, actual ZIP and delivered-installer tests, source and payload integrity checks. |
| Release automation | Windows CI with successful artefacts and safe diagnostics, read-only repository permissions, manual dispatch and PR triggers; no publishing step. |
| Documentation | PACKAGING.md covers bootstrap, one-command build, source-archive rebuilding, native licensing, upgrade safety, verification and limitations. |

## Validation and remaining limits

Each successful build records exact outcomes in `VALIDATION.json`, detailed suite
logs and installer preservation hashes. Use the newly rebuilt review artefacts;
earlier builds do not contain these corrections.

- Native modding tools remain external unless a contributor supplies explicit
  redistribution evidence and binary/licence hashes. This review does not grant
  redistribution permission for Oodle or other vendor tools.
- .NET Framework 4.8 and WebView2 remain Windows prerequisites. Installer checks
  stop before replacing application files if absent; optional bootstrap provisioning
  applies to WebView2. Missing-runtime clean-VM provisioning still merits VM QA.
- Existing-installation testing copies the old EXE/runtime into an isolated prior
  install. It does not execute historical production installer custom code or
  modify the user's registered production installation.
- GitHub-hosted execution has not yet run. The workflow supports manual dispatch
  and pull requests; workflow syntax and local equivalent steps are checked.
- Signing, official publication and in-game compatibility remain separate checks;
  native/game cases explicitly skip when their fixtures are absent.
