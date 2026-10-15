"""Build, test and package one Windows payload as portable ZIP and per-user installer."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import uuid

from windows_package import (BuildError, approved_tools, audit_bundle, discover_iscc,
    file_records, git, inventory_digest, sha256, snapshot_source, source_inventory,
    stage_approved_tools, validate_dependencies, verify_resources, write_installer_files,
    write_json, zip_tree, zip_logs, verify_snapshot, verify_payload, verify_source_zip,
    extract_verified_portable, source_identity)
from runtime_licenses import collect_licenses

ROOT = Path(__file__).resolve().parents[1]


def run(command, cwd, log, env=None, timeout=1800):
    log.parent.mkdir(parents=True, exist_ok=True)
    print(f"Running {Path(str(command[0])).name}: {log.name}", flush=True)
    with log.open("w", encoding="utf-8") as stream:
        stream.write(subprocess.list2cmdline([str(c) for c in command]) + "\n")
        stream.flush()
        try:
            result = subprocess.run([str(c) for c in command], cwd=cwd, env=env,
                                    stdout=stream, stderr=subprocess.STDOUT, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise BuildError(f"Command timed out; see {log}") from error
    if result.returncode:
        raise BuildError(f"Command failed with exit code {result.returncode}; see {log}")


def preflight(args):
    if sys.platform != "win32" or platform.machine().lower() not in {"amd64", "x86_64"} or sys.maxsize <= 2**32:
        raise BuildError("Build with Windows x64 Python 3.12.")
    if sys.version_info[:2] != (3, 12):
        raise BuildError("Python 3.12 is required. Run scripts/bootstrap_windows.ps1 -Python PATH.")
    versions = validate_dependencies(ROOT)
    iscc = discover_iscc(args.iscc)
    if not shutil.which("node") or ((ROOT / '.git').exists() and not shutil.which('git')):
        raise BuildError("Node.js is required for regression tests; a Git checkout also needs Git on PATH.")
    from atelier_runtime_check import launch_issue
    issue = launch_issue()
    if issue:
        raise BuildError(issue)
    if args.tools_dir and not args.native_policy:
        raise BuildError("--tools-dir requires --native-policy; unreviewed binaries are never bundled.")
    native = approved_tools(args.native_policy, args.tools_dir)
    print("Native tools: " + (f"{len(native)} explicitly approved files" if native else "external; missing-tool warnings enabled"))
    return versions, iscc, native


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path, help="Outside the checkout; contains work/, reports/ and releases/.")
    parser.add_argument("--version", help="N.N.N[.N], each component 0..65535; defaults to version file.")
    parser.add_argument("--iscc", type=Path)
    parser.add_argument("--allow-dirty", action="store_true", help="Build an explicitly labelled working-tree source snapshot.")
    parser.add_argument("--tools-dir", type=Path)
    parser.add_argument("--native-policy", type=Path, help="Per-file hashes, licence texts and redistribution permission.")
    parser.add_argument("--previous-install-dir", type=Path, help="Test an upgrade from an existing installed EXE/runtime, copied read-only into isolation.")
    parser.add_argument("--preflight", action="store_true", help="Validate toolchain and native policy without building.")
    parser.add_argument("--clean", action="store_true", help="After success, remove only this run's owned work directory.")
    args = parser.parse_args(argv)
    output = args.output_dir.resolve()
    if output == ROOT or output.is_relative_to(ROOT):
        parser.error("--output-dir must be outside the source checkout.")
    output.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    report = output / "reports" / run_id
    report.mkdir(parents=True, exist_ok=False)
    status = {"status": "failed", "run_id": run_id, "checks": [], "release": None}
    work = output / "work" / run_id
    try:
        version = args.version or (ROOT / "version").read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"(?:0|[1-9]\d{0,4})(?:\.(?:0|[1-9]\d{0,4})){2,3}", version) or any(int(p) > 65535 for p in version.split(".")):
            raise BuildError("Version must be N.N.N[.N], each component 0..65535 (no leading zeros).")
        versions, iscc, native = preflight(args)
        run([sys.executable, "-m", "pip", "check"], ROOT, report / "pip-check.log")
        compiler_pin = json.loads((ROOT / "scripts/windows_toolchain.json").read_text())["inno_setup"]
        compiler_hashes = {}
        for filename, expected in compiler_pin["files"].items():
            file = iscc.parent / filename
            if not file.is_file() or sha256(file) != expected:
                raise BuildError(f"Compiler {filename} does not match pinned Inno Setup 6.7.3; run scripts/bootstrap_windows.ps1.")
            compiler_hashes[filename] = expected
        write_json(report / "inno-toolchain.json", {"version": compiler_pin["version"], "files": compiler_hashes})
        if args.preflight:
            status.update(status="preflight-passed", dependencies=versions, iscc=str(iscc))
            return 0
        commit, dirty, origin = source_identity(ROOT)
        if dirty and not args.allow_dirty:
            raise BuildError("Source has uncommitted changes. Use --allow-dirty for an auditable working-tree build; no commit is necessary.")
        inventory, excluded = source_inventory(ROOT)
        source_digest = inventory_digest(inventory)
        info = {"base_commit": commit, "working_tree": dirty, "input_source_sha256": source_digest,
                "application_version": version, "built_utc": datetime.now(timezone.utc).isoformat(),
                "python": sys.version, "dependencies": versions, "platform": "windows-x64",
                "inno_setup": compiler_pin['version'], "native_tools_included": bool(native), "official_release": False,
                "excluded_source_paths": excluded, "source_origin": origin}
        work.mkdir(parents=True, exist_ok=False)
        write_json(work / ".atelier-build-workspace.json", {"run_id": run_id, "source": str(ROOT)})
        source = work / "source"
        snapshot_source(ROOT, source, inventory, version)
        write_json(source / "BUILD_INFO.json", info)
        snapshot_records = file_records(source)
        write_json(source / "SOURCE_MANIFEST.json", snapshot_records)
        env = dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE='1',
                   PYINSTALLER_CONFIG_DIR=str(work / 'pyinstaller-cache'))
        for key in ("MR_TOOLS", "ATELIER_DATA_DIR", "ATELIER_PORT", "ATELIER_BUILD_VERSION_FILE", "ATELIER_PE_VERSION_FILE", 'PYTHONPATH', 'PYTHONHOME'):
            env.pop(key, None)
        # Tests run on the immutable snapshot, never the installed app or live projects.
        run([sys.executable, "tests/run_checks.py", "--report-dir", report / "regression"], source,
            report / "regression.log", env=env)
        status["checks"].append("isolated regression suites")
        from PyInstaller.utils.win32.versioninfo import (VSVersionInfo, FixedFileInfo, StringFileInfo,
                                                         StringTable, StringStruct, VarFileInfo, VarStruct)
        numeric_version = tuple(int(p) for p in version.split("."))
        numeric_version += (0,) * (4 - len(numeric_version))
        pe = VSVersionInfo(ffi=FixedFileInfo(filevers=numeric_version, prodvers=numeric_version, fileType=1), kids=[
            StringFileInfo([StringTable("040904B0", [StringStruct(k, v) for k, v in {
                "CompanyName": "Atelier", "FileDescription": "Atelier", "FileVersion": version,
                "ProductName": "Atelier", "ProductVersion": version, "OriginalFilename": "Atelier.exe"}.items()])]),
            VarFileInfo([VarStruct("Translation", [1033, 1200])])])
        pe_path = work / "pe-version.txt"
        pe_path.write_text(str(pe), encoding="utf-8")
        env["ATELIER_PE_VERSION_FILE"] = str(pe_path)
        run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", work / "dist",
             "--workpath", work / "pyinstaller", "Atelier.spec"], source, report / "pyinstaller.log", env=env)
        bundle = work / "dist" / "Atelier"
        shutil.copy2(source / "LICENSE", bundle / "LICENSE")
        shutil.copy2(source / "BUILD_INFO.json", bundle / "BUILD_INFO.json")
        packages = collect_licenses(source, bundle / "licenses")
        stage_approved_tools(native, args.tools_dir, bundle)
        write_json(bundle / "NATIVE_TOOLS.json", {"mode": "approved-files" if native else "external", "files": native})
        (bundle / "README.txt").write_text(
            f"Atelier {version} - Windows x64 contributor build\n"
            f"Base commit: {commit}\nWorking-tree snapshot: {dirty}\nSource digest: {source_digest}\n\n"
            "Extract the entire ZIP into a new writable folder; keep Atelier.exe beside _internal.\n"
            "Python is included. Windows 10/11 x64, .NET Framework 4.8 and Microsoft Edge WebView2 Runtime are required.\n"
            "WebView2: https://developer.microsoft.com/microsoft-edge/webview2/\n"
            "Native modding tools are external unless listed in NATIVE_TOOLS.json. Obtain compatible licensed\n"
            "UAssetTool, UAssetGUI, AtelierMesh, custom shader retoc/Oodle and DXC before using their features.\n"
            "Use Setup/Settings for game paths. Missing tools are reported on first run; see PACKAGING.md\n"
            "in the matching source archive for paths, provenance and licence policy. AES keys, mappings,\n"
            "projects and game files are never included. Data stays beside the executable by default.\n"
            "For isolated testing set ATELIER_DATA_DIR to a new directory and ATELIER_PORT to a free port.\n"
            "Installer upgrades and uninstall preserve user work. Never extract over an old portable runtime;\n"
            "use a fresh directory and point ATELIER_DATA_DIR at the existing data directory instead.\n"
            "Keep the matching source archive, licence texts, manifest and checksums with this unsigned build.\n",
            encoding="utf-8")
        audit_bundle(bundle)
        checks = verify_resources(source, bundle)
        status["checks"].append(f"{len(checks)} GUI, Blender and frozen Python source comparisons")
        manifest = {"build": info, "runtime_packages": packages, "files": file_records(bundle)}
        write_json(bundle / "MANIFEST.json", manifest)
        write_json(report / "MANIFEST.json", manifest)
        run([sys.executable, "tests/frozen_smoke.py", "--exe", bundle / "Atelier.exe",
             "--report-dir", report / "frozen"], source, report / "frozen.log", env=env, timeout=240)
        status["checks"].append("frozen executable and WebView2")
        verify_payload(bundle, manifest)
        payload_include = work / "payload.iss"
        write_installer_files(bundle, payload_include)
        candidate = work / "candidate"
        candidate.mkdir()
        name = f"Atelier-{version}-Windows-x64-{source_digest[:12]}" + ("-working-tree" if dirty else "")
        run([iscc, f"/DAppVersion={version}", f"/DPayloadInclude={payload_include}", f"/O{candidate}",
             f"/F{name}-Setup", source / "Atelier.iss"], source, report / "inno-build.log", env=env)
        installer = candidate / f"{name}-Setup.exe"
        install_command = [sys.executable, "tests/installer_smoke.py", "--installer", installer,
                           "--bundle", bundle, "--report-dir", report / "installer", "--iscc", iscc]
        if args.previous_install_dir:
            install_command += ["--previous-install-dir", args.previous_install_dir.resolve(strict=True)]
        run(install_command, source, report / "installer.log", env=env, timeout=900)
        status["checks"].append("installer install/upgrade/uninstall with preserved user-data hashes")
        current, _ = source_inventory(ROOT)
        if current != inventory or source_identity(ROOT)[0] != commit:
            raise BuildError("Source changed during build. No release was emitted; run again.")
        verify_snapshot(source, snapshot_records)
        verify_payload(bundle, manifest)
        portable_zip = candidate / f"{name}-Portable.zip"
        source_zip = candidate / f"{name}-Source.zip"
        zip_tree(bundle, portable_zip, "Atelier/")
        extracted = extract_verified_portable(portable_zip, work / 'portable-extracted', manifest)
        run([sys.executable, "tests/frozen_smoke.py", "--exe", extracted / 'Atelier.exe',
             "--report-dir", report / 'portable-extracted'], source, report / 'portable-extracted.log', env=env, timeout=240)
        verify_payload(extracted, manifest)
        status['checks'].append('delivered portable ZIP exact inventory, hashes and extracted WebView2 launch')
        zip_tree(source, source_zip, "Atelier-source/")
        verify_source_zip(source_zip, snapshot_records)
        status['checks'].append('matching source ZIP inventory and hashes')
        # Check both input and snapshot again after all tests, before emitting a release.
        if source_inventory(ROOT)[0] != inventory or source_identity(ROOT)[0] != commit:
            raise BuildError('Source changed during final archive verification; no release was emitted.')
        verify_snapshot(source, snapshot_records)
        for relative in ('frozen/results.json', 'portable-extracted/results.json', 'installer/installed-frozen/results.json'):
            check_result = json.loads((report / relative).read_text(encoding='utf-8'))
            for warning in check_result.get('screenshot_warnings', []):
                status.setdefault('diagnostic_warnings', []).append(f'{relative}: {warning}')
        shutil.copy2(report / "MANIFEST.json", candidate / "MANIFEST.json")
        write_json(candidate / "BUILD_INFO.json", info)
        zip_logs(report, candidate / f"{name}-Logs.zip")
        sums = "".join(f"{sha256(p)}  {p.name}\n" for p in sorted(candidate.iterdir()) if p.is_file())
        (candidate / "SHA256SUMS.txt").write_text(sums, encoding="utf-8")
        release = output / "releases" / (name + "-" + run_id)
        release.parent.mkdir(parents=True, exist_ok=True)
        completed_status = dict(status, status="passed", release=str(release), source=info)
        write_json(candidate / "VALIDATION.json", completed_status)
        # Cover validation as well, without a recursive checksum dependency.
        with (candidate / "SHA256SUMS.txt").open("a", encoding="utf-8") as stream:
            stream.write(f"{sha256(candidate / 'VALIDATION.json')}  VALIDATION.json\n")
        # Candidate lives on the same volume. Consumers only see a completed, validated directory.
        candidate.rename(release)
        status.update(completed_status)
        if args.clean:
            marker = work / ".atelier-build-workspace.json"
            try:
                if work.parent.resolve() != (output / "work").resolve() or work.is_symlink() or work.is_junction() or json.loads(marker.read_text())["run_id"] != run_id:
                    status["cleanup_warning"] = "Refused to clean an unowned work directory."
                else:
                    shutil.rmtree(work)
            except (OSError, ValueError, KeyError) as error:
                status["cleanup_warning"] = f"Validated release retained; work cleanup failed: {error}"
        print(f"Validated release: {release}", flush=True)
        return 0
    except Exception as error:
        status["error"] = str(error)
        print(f"BUILD FAILED: {error}\nReports: {report}", file=sys.stderr, flush=True)
        return 1
    finally:
        try:
            write_json(report / "VALIDATION.json", status)
            diagnostics = output / "diagnostics"
            diagnostics.mkdir(exist_ok=True)
            zip_logs(report, diagnostics / (run_id + ".zip"))
        except OSError as error:
            # A diagnostic write must not turn a completed release into a reported failed build,
            # or mask the original validation failure. The release has its own validation record.
            print(f'Diagnostic report could not be written: {error}', file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
