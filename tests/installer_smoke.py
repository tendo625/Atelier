"""Exercise Inno installation, upgrade and removal under a unique test identity.

Never run the production AppId against a developer's registered installation.
Run the exact delivered installer with its restricted PACKAGINGTESTID option.
The synthetic older build contains a retired runtime DLL; user work is seeded after install.
--previous-install-dir additionally uses an existing app's EXE/_internal as the old payload,
without reading its configuration, keys, mappings or projects.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from windows_package import BuildError, audit_bundle, file_records, sha256, write_installer_files, write_json, verify_payload


def command(args, cwd, log, timeout=240):
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    with log.open("w", encoding="utf-8") as stream:
        result = subprocess.run([str(x) for x in args], cwd=cwd, stdout=stream,
                                stderr=subprocess.STDOUT, startupinfo=startup, timeout=timeout)
    if result.returncode:
        raise BuildError(f"Installer check failed ({result.returncode}): {log}")


def assert_payload(bundle, installed, preserve_seed=False):
    for record in file_records(bundle):
        if preserve_seed and record["path"] == "Tools/MarvelRivalsCharacterIDs.md":
            continue  # A runtime-updated seed must be preserved, rather than match the package.
        target = installed / record["path"]
        if not target.is_file() or sha256(target) != record["sha256"]:
            raise BuildError(f"Installed payload differs: {record['path']}")


def production_registration():
    import winreg
    result = []
    key_path = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\{2E25ABE9-E3D5-4ABA-9E50-725BA75185AF}_is1'
    for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ | view) as key:
                values = []
                index = 0
                while True:
                    try:
                        values.append(winreg.EnumValue(key, index))
                        index += 1
                    except OSError:
                        break
                result.append(sorted(values))
        except FileNotFoundError:
            result.append(None)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installer", required=True, type=Path)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--report-dir", required=True, type=Path)
    parser.add_argument("--iscc", required=True, type=Path)
    parser.add_argument("--previous-install-dir", type=Path)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("Installer validation requires Windows.")
    report = args.report_dir.resolve()
    report.mkdir(parents=True, exist_ok=False)
    bundle = args.bundle.resolve(strict=True)
    audit_bundle(bundle)
    payload_manifest = json.loads((bundle / 'MANIFEST.json').read_text(encoding='utf-8'))
    verify_payload(bundle, payload_manifest)
    installed = report / "installed"
    fresh = report / 'fresh-installed'
    test_id = uuid.uuid4().hex
    identity = "AtelierPackagingTest-" + test_id
    result = {"status": "failed", "checks": [], "installer_sha256": sha256(args.installer),
              "test_identity": identity, "scope": "Exact delivered installer executable; isolated registry identity and shortcuts via PACKAGINGTESTID"}
    group = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs" / identity
    common = [args.iscc, f"/O{report}"]
    version = json.loads((bundle / "BUILD_INFO.json").read_text())["application_version"]
    # An actual older installed runtime can be read without touching its live user data.
    old = report / "old-payload"
    old.mkdir()
    if args.previous_install_dir:
        previous = args.previous_install_dir.resolve(strict=True)
        # Do not follow a junction/symlink into an unrelated directory while preparing
        # the older-runtime fixture. No live user data is intentionally copied.
        for path in [previous, previous / '_internal', *list((previous / '_internal').rglob('*'))]:
            if path.is_symlink() or path.is_junction():
                raise BuildError(f'Previous runtime contains a link/junction: {path}')
        shutil.copy2(previous / "Atelier.exe", old / "Atelier.exe")
        shutil.copytree(previous / "_internal", old / "_internal")
        result["old_payload_source"] = str(previous)
    else:
        shutil.copy2(bundle / "Atelier.exe", old / "Atelier.exe")
        (old / "_internal").mkdir()
        result["old_payload_source"] = "synthetic earlier payload"
    (old / "_internal" / "retired-packaging-test.dll").write_bytes(b"retired-runtime")
    old_include = report / "old-payload.iss"
    write_installer_files(old, old_include)
    command(common + ["/DAppVersion=0.0.1", f"/DPayloadInclude={old_include}", "/Fprevious", ROOT / "Atelier.iss"], ROOT, report / "compile-previous.log")
    options = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", f"/DIR={installed}", f"/PACKAGINGTESTID={test_id}", "/TASKS=desktopicon"]
    uninstalled = False
    registration_before = production_registration()
    try:
        command([report / "previous.exe", *options, f"/LOG={report / 'install-previous.log'}"], report, report / "previous-output.log")
        result["checks"].append("older payload installs per-user under isolated AppId")
        data = {
            "assets/projects/Existing project/edit.png": b"user-edit",
            "assets/projects/Existing project/project.json": b'{"textures":["edit.png"]}',
            "assets/exported/My mod.pak": b"user-export",
            "mr_config.json": b'{"paks":"D:/Games/Marvel/Paks","tools":"D:/MyTools","blender_path":"D:/Blender/blender.exe","preferences":{"theme":"dark"}}',
            "Tools/Mappings/Local.usmap": b"user-mapping",
            "Tools/AES_KEY.txt": b"TEST-ONLY-NOT-A-REAL-KEY",
            "Tools/shaders/custom-user-tool.dll": b"user-tool",
            "Tools/MarvelRivalsCharacterIDs.md": b"user-updated-seed",
            "_webview/preferences.txt": b"user-ui-preferences",
        }
        for rel, value in data.items():
            path = installed / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        before = {rel: sha256(installed / rel) for rel in data}
        result['preserved_before'] = before
        command([args.installer.resolve(), *options, f"/LOG={report / 'upgrade.log'}"], report, report / "upgrade-output.log")
        assert_payload(bundle, installed, preserve_seed=True)
        if (installed / "_internal/retired-packaging-test.dll").exists():
            raise BuildError("A retired runtime binary survived the upgrade.")
        for rel, digest in before.items():
            if sha256(installed / rel) != digest:
                raise BuildError(f"Upgrade changed user data: {rel}")
        result["checks"].append("upgrade payload hashes match; retired runtime removed; all seeded user data preserved")
        result['preserved_after_upgrade'] = {rel: sha256(installed / rel) for rel in data}
        shortcut = group / "Atelier.lnk"
        if not shortcut.is_file():
            raise BuildError("Start Menu shortcut was not created.")
        import ctypes
        desktop_buffer = ctypes.create_unicode_buffer(32768)
        # CSIDL_DESKTOPDIRECTORY resolves redirected/OneDrive desktops too.
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, desktop_buffer) != 0:
            raise BuildError("Could not locate the test desktop shortcut.")
        desktop_shortcut = Path(desktop_buffer.value) / (identity + ".lnk")
        if not desktop_shortcut.is_file():
            raise BuildError("Requested optional desktop shortcut was not created.")
        result["checks"].append("Start Menu and requested optional desktop shortcuts created")
        command([sys.executable, ROOT / "tests/frozen_smoke.py", "--exe", installed / "Atelier.exe", "--report-dir", report / "installed-frozen"], ROOT,
                report / "installed-frozen.log", timeout=240)
        result["checks"].append("installed executable and WebView2 shell pass existing frozen smoke")
        uninstallers = list(installed.glob("unins*.exe"))
        if len(uninstallers) != 1:
            raise BuildError("Expected exactly one test uninstaller.")
        command([uninstallers[0], "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/LOG={report / 'uninstall.log'}"], report, report / "uninstall-output.log")
        uninstalled = True
        for rel, digest in before.items():
            if not (installed / rel).is_file() or sha256(installed / rel) != digest:
                raise BuildError(f"Uninstall removed/changed user data: {rel}")
        if (installed / "Atelier.exe").exists() or shortcut.exists() or desktop_shortcut.exists():
            raise BuildError("Uninstall left the application executable or Start Menu shortcut behind.")
        result["checks"].append("uninstall removes application and shortcut while preserving all user data")
        result['preserved_after_uninstall'] = {rel: sha256(installed / rel) for rel in data}
        # Exercise fresh installation of the delivered binary too, with desktop shortcuts off.
        fresh_options = [f'/DIR={fresh}' if option.startswith('/DIR=') else '/TASKS=' if option.startswith('/TASKS=') else option for option in options]
        uninstalled = False
        command([args.installer.resolve(), *fresh_options, f"/LOG={report / 'fresh-install.log'}"], report, report / 'fresh-install-output.log')
        assert_payload(bundle, fresh)
        if desktop_shortcut.exists() or not shortcut.is_file():
            raise BuildError('Fresh installation did not respect optional desktop shortcut selection.')
        command([sys.executable, ROOT / 'tests/frozen_smoke.py', '--exe', fresh / 'Atelier.exe',
                 '--report-dir', report / 'fresh-first-run', '--first-run'], ROOT, report / 'fresh-first-run.log', timeout=240)
        fresh_uninstaller = next(fresh.glob('unins*.exe'))
        command([fresh_uninstaller, '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART'], report, report / 'fresh-uninstall.log')
        uninstalled = True
        if (fresh / 'Atelier.exe').exists() or shortcut.exists():
            raise BuildError('Fresh-install uninstall left application or shortcut behind.')
        result['checks'].append('Exact delivered installer passes fresh install, optional desktop-off, clean first-run Setup and removal')
        if production_registration() != registration_before:
            raise BuildError('Production Atelier registration changed during isolated installer validation.')
        result['checks'].append('Production Atelier registration remains unchanged in both registry views')
        verify_payload(bundle, payload_manifest)
        if sha256(args.installer) != result['installer_sha256']:
            raise BuildError('Delivered installer changed while it was being tested.')
        result["status"] = "passed"
    except Exception as error:
        result["error"] = str(error)
        raise
    finally:
        if not uninstalled:
            for uninstaller in [*installed.glob("unins*.exe"), *fresh.glob('unins*.exe')]:
                try:
                    command([uninstaller, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], report, report / "cleanup-uninstall.log")
                except Exception as error:
                    result["cleanup_error"] = str(error)
        write_json(report / "results.json", result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
