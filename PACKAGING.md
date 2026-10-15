# Windows builds, packaging and validation

Use `feature/windows-packaging`, based on upstream `main` at
`d592133c17d516056d07b5f5918e1838e49f9314`. It includes the maintainer's native-tool
allowlist (`a712532`) and frozen-dependency exclusions (`ca7472b`). This work builds
on those changes; it does not merge the separate Material 3 redesign branch.

## Prepare the toolchain once

Install Windows x64 Python **3.12** and Node.js LTS (Node is for existing
regression tests only). A Git checkout also needs Git. Install Microsoft's [.NET Framework 4.8](https://dotnet.microsoft.com/download/dotnet-framework)
and [Evergreen WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)
if absent. A browser installation alone is not a WebView2 Runtime installation.
Then, from this checkout:

```powershell
.\scripts\bootstrap_windows.ps1 -Python 'C:\path\to\python.exe'
# Optionally provision missing WebView2 with Microsoft's signed bootstrapper:
.\scripts\bootstrap_windows.ps1 -Python 'C:\path\to\python.exe' -InstallWebView2
```

The bootstrap creates `.venv-build`, installs the pinned runtime/build/test
requirements, runs `pip check`, and downloads **Inno Setup 6.7.3** from the
[official release](https://github.com/jrsoftware/issrc/releases/tag/is-6_7_3).
It checks the published SHA-256 and the Pyrsys B.V. Authenticode signature before
installing the compiler per-user, without shortcuts or file associations. Use
`-ToolchainDir D:\AtelierBuildTools` to relocate it. `-NodeDirectory` can supply a
directory containing `node.exe`. Compiler discovery checks `--iscc`, `ISCC`, PATH,
then standard installation locations. Keep the `ISCC` assignment printed by the
bootstrap in each new shell when using a custom toolchain directory.
The bootstrap verifies installed compiler component hashes too. It checks .NET
and WebView2 before declaring the environment ready. `-InstallWebView2` verifies
the Microsoft Corporation signature before executing the Evergreen bootstrapper;
the runtime is serviced by Microsoft rather than pinned as an application binary.

Runtime requirements are in `requirements.txt`; `requirements-build.txt` adds
PyInstaller and its toolchain; `requirements-dev.txt` adds Playwright. Browser
downloads are unnecessary for the native WebView2 check.
Preflight verifies all runtime, build and test pins, including transitive test
dependencies. Build subprocesses clear inherited Python path overrides and use
a private PyInstaller cache inside the run's work directory.

## One command

```powershell
.\BUILD.bat --output-dir 'D:\AtelierBuilds\Windows' --allow-dirty
```

The batch file is only a launcher for `scripts/package_release.py` and propagates
its exit code. It uses `.venv-build`, `.venv`, or `ATELIER_PYTHON`. Use `--preflight`
to check the environment without building; `--iscc 'C:\path\ISCC.exe'` overrides
discovery. Missing compilers, pinned packages and runtimes fail before freezing.

`--allow-dirty` explicitly permits **uncommitted** work; it never makes a commit.
The build includes new, non-ignored source files and tracked edits/deletions in a
private source snapshot. Without it the checkout must be clean. Builds are marked
as contributor builds; working-tree artefact names contain `working-tree`. Version
defaults to `version`; `--version 0.3.5` overrides the snapshot, PE metadata and
installer together without changing the checkout's version file. Accepted versions
are three or four numeric components, each 0–65535, without leading zeros.

The output directory must be outside the checkout. Each run gets its own
`work/<UTC-id>/source`, PyInstaller work/dist directories, and `reports/<UTC-id>`.
`--clean` removes only that run's marked work directory after success, never the
checkout, output root, reports, releases or unrelated build directories. Failed
work is retained for diagnosis. Logs contain commands, compiler output, dependency
checks and test results; `VALIDATION.json` records failures and successful stages.

## Outputs and failure handling

### Rebuild the matching source archive

Extract `*-Source.zip`, enter its `Atelier-source` directory, run the same bootstrap
and BUILD.bat commands above, and pass `--allow-dirty` if its recorded source was
uncommitted. Git history and a new commit are not required. Every source file is
verified against `SOURCE_MANIFEST.json`; a modified or added source file fails.
Bootstrap environments and generated Python caches are excluded. To edit an archive's
source, overlay it onto a checkout of its recorded `base_commit` and work in Git.

### Validated artefacts

Only a fully validated build appears under `releases/<version-source-hash-run-id>`:

- `*-Portable.zip`: the entire application, Python runtime, GUI and Blender scripts.
- `*-Setup.exe`: the same payload in a per-user Inno Setup installer.
- `*-Source.zip`: the matching filtered source snapshot, including uncommitted work.
- `*-Logs.zip`: compiler and validation evidence.
- `MANIFEST.json`: every application file's size/hash and build provenance.
- `BUILD_INFO.json`, `VALIDATION.json` and `SHA256SUMS.txt`.

The manifest excludes its own hash; external checksums cover the manifest, metadata,
installer and archives. Nothing is signed or published automatically. Python modules
in the executable's PYZ, the launcher, frontend, standalone Blender resources and
version are compared with the snapshot before packaging. PE file/product versions
are checked too. After smoke tests, exact file inventories and hashes are checked
again so application writes cannot silently invalidate the manifest. The pipeline
extracts the actual portable ZIP and launches it in WebView2, verifies every source
ZIP member and checks the input/snapshot again before publication. Input changes during the
build fail the run. A failed build leaves diagnostics and candidate files under
`work/`, without a successful release directory. Do not distribute candidate files.

Source export uses an explicit source-directory allowlist and rejects sensitive
paths even when Git tracks them. Upstream's tracked `_webview` state, `.mcp.json`,
retrieval scripts/state, development Plugins, private credentials, keys, mappings,
game containers and projects are excluded. Excluded names are recorded in build
metadata. This filtering does not inspect arbitrary text for hardcoded secrets;
contributors must also review source before distribution.

## Native tool policy

**Default packages keep native modding tools external.** The Python runtime is
self-contained; WebView2/.NET and the appropriate licensed modding tools are
prerequisites. The app opens without native tools and reports missing tools through
the existing first-run prerequisite warnings. Optional feature tools do not block
unrelated features. The Windows shell gives an actionable message before importing
pywebview if WebView2 or .NET Framework is absent. The installer checks WebView2
and .NET before modifying application files; it links to the vendor rather than silently
downloading software. Offline machines must provision these prerequisites first.

The configured `tools` directory in `mr_config.json` defaults to `Tools` beside the
executable (or inside `ATELIER_DATA_DIR`). Preserve existing configuration when
pointing it at an external directory. Compatible Windows paths are:

| Feature | Paths under the tools directory |
| --- | --- |
| Extraction / mod builds | `UAssetTool.exe` |
| World editing | `UAssetGUI.exe` |
| Mesh previews | `AtelierMesh/AtelierMesh.exe` and its compatible dependencies |
| World / text / shaders | `shaders/retoc-rivals-cli.exe` (Atelier's custom shader build) |
| Pak decompression | `shaders/oo2core_9_win64.dll` from a source you are licensed to use |
| Shader compilation/disassembly | `shaders/dxc.exe`, `dxcompiler.dll`, `dxil.dll` |
| Character-name seed (optional) | `MarvelRivalsCharacterIDs.md` |

See `Install-AtelierTools.ps1` and DEVELOPMENT.md for existing developer tool
provisioning. That convenience script is **not redistribution permission**. Generic
retoc releases may lack Atelier's shader commands. Oodle redistribution rights
have not been established by this refactor, so no local Oodle DLL is silently shipped.
AES keys and `.usmap` mappings are downloaded/generated user resources and never
release payload. Game files, projects, preferences and exports are user data.

To bundle a reviewed tool, supply `--tools-dir PATH --native-policy policy.json`.
The policy must enumerate each file (including every approved vendor dependency):

```json
{
  "files": [{
    "path": "UAssetTool.exe",
    "sha256": "<64-character binary digest>",
    "source_url": "https://vendor.example/exact-release",
    "license": "<actual applicable licence>",
    "redistribution_permitted": true,
    "license_path": "notices/UAssetTool-LICENSE.txt",
    "license_sha256": "<64-character licence-text digest>"
  }]
}
```

The build checks both hashes before freezing, allows only paths from the maintainer's
Windows manifest (plus explicitly named AtelierMesh members), and copies only those
files and their licence texts. There are no directory-wide native-tool copies.
Missing files, absent permission evidence and private/game data fail the build.
The policy is an accountable contributor attestation, not an automated legal review.

## Installation, upgrade and uninstall

The installer retains upstream's AppId, installer registry view and
`%LOCALAPPDATA%\Atelier` default, needs no administrator rights,
creates a Start Menu shortcut and offers an unchecked
desktop-shortcut task. It preserves existing projects, exports, configuration,
external tool paths, game paths, mappings, keys and WebView preferences. Only the
installer-owned `_internal` runtime is cleared before upgrades, so deleted Python
extensions/DLLs cannot shadow new versions. Neither the application root nor Tools
nor any user-data parent is recursively deleted. User-updated character seeds are
installed only if absent. Uninstall has no data deletion callback.

For portable upgrades, extract into a fresh directory and point `ATELIER_DATA_DIR`
at the existing writable data directory. Do not overlay a new `_internal` on an old
one: ZIP extraction cannot remove obsolete binaries.

## Verification

Every build runs all existing `test_*.py` regression suites in disposable copies,
the release policy tests, source/resource comparisons, the actual frozen executable
inside WebView2, and real Inno install/upgrade/uninstall checks. Install tests use a
unique AppId and shortcut group by passing `/PACKAGINGTESTID=<32 hex digits>` and
an explicit `/DIR` to the **exact delivered installer executable**. Tests no longer
recompile a replacement current installer. The switch affects only registration
and shortcut names; it does not skip prerequisite checks or change the payload.
Without this switch the upstream AppId and normal names apply. Tests also compare
the production registration in both registry views to ensure it remains unchanged.
They hash seeded projects, edited textures, exports, config, local tools, keys,
mappings and preferences before upgrade and after uninstall. They check removal
of a retired runtime DLL and launch the installed executable in real WebView2.
They then fresh-install the delivered binary with the desktop task off, verify
clean first-run Setup without a project/config fixture, and uninstall it. Both
desktop-task settings are exercised. Preservation hashes are recorded in results.
Frozen checks choose free ports by default; explicit ports remain available.
Theme assertions check computed colours as well as selected state. PNG captures
have a bounded timeout because hidden WebView2 compositor capture can stall;
capture warnings appear in the test and build reports without suppressing any
functional, native-shell or backend assertion. First-run Setup is not captured
because it can display auto-detected paths or fetched key values.

To test using the runtime from an existing installation:

```powershell
.\BUILD.bat --output-dir 'D:\AtelierBuilds\Windows' --allow-dirty `
  --previous-install-dir 'D:\Atelier'
```

Only the old EXE and `_internal` are copied read-only into the isolated test; live
projects/configuration are not copied or changed. Without this option an older
minimal installer fixture is generated. This validates installed-payload upgrades;
it does not run an old production installer's historical custom code. Full tests
of that code and production registry identity belong on a disposable Windows VM.
Native game/mod export and in-game compatibility remain the existing separate
regression/manual checks. Native cases may explicitly skip without game fixtures.

## GitHub Actions

`.github/workflows/windows-packaging.yml` supports manual runs and packaging-related
pull requests on Windows x64. It provisions the pinned compiler and Python tools,
provisions WebView2 when absent, builds both formats, validates them, uploads successful release artefacts, and retains
diagnostics on failure. It has only `contents: read` permission and no release,
tagging, commit, push or publication step. Official release approval and signing
remain separate actions. This workflow must be committed/pushed before GitHub can
execute it; adding it locally does not start a hosted run.
