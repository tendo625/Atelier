"""Release boundaries, sensitive-data rejection, provenance and failure semantics."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import windows_package as W
import package_release as P


class SensitiveData(unittest.TestCase):
    def test_forbidden_paths_at_arbitrary_depth(self):
        for rel in ("Tools/AES_KEY.txt", "_internal/secrets.json", "_internal/gui/.env.local",
                    "Tools/AtelierMesh/local.usmap", "assets/projects/work.png", "_internal/private.pak",
                    "_webview/storage/state", "_internal/key.pfx", "Tools/Mappings/anything.bin", "Tools/AtelierMesh/development.env"):
            with self.subTest(rel=rel):
                self.assertTrue(W.forbidden(rel))
        self.assertFalse(W.forbidden("_internal/webview/lib/WebView2Loader.dll"))
        self.assertFalse(W.forbidden("_internal/scipy/_lib/array_api_compat/common/_helpers.pyc"))

    def test_link_and_traversal_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("../secret", "D:/secret", "/root", "folder\\..\\secret"):
                with self.subTest(name=name), self.assertRaises(W.BuildError):
                    W.safe_file(root, name)

    def test_source_snapshot_excludes_tracked_browser_state_and_mcp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = {name: b"source" for name in ("window.py", "Atelier.spec", "Atelier.iss", "version", "requirements-build.txt", "LICENSE", "scripts/new.py")}
            files.update({".mcp.json": b"private", "_webview/storage/state": b"private", "retrieval/config.py": b"private", "Tools/AES_KEY.txt": b"private"})
            for name, content in files.items():
                file = root / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(content)
            with patch.object(W, "git", return_value=("\0".join(files) + "\0").encode()):
                inventory, excluded = W.source_inventory(root)
            self.assertIn("scripts/new.py", inventory)  # uncommitted new implementation is included
            self.assertEqual(set(excluded), {".mcp.json", "_webview/storage/state", "retrieval/config.py", "Tools/AES_KEY.txt"})
            snapshot = root / "snapshot"
            W.snapshot_source(root, snapshot, inventory, "1.2.3")
            self.assertEqual((snapshot / "version").read_text().strip(), "1.2.3")
            self.assertFalse((snapshot / ".mcp.json").exists())
            self.assertEqual((snapshot / "scripts/new.py").read_bytes(), b"source")

    def test_source_change_during_copy_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "window.py").write_bytes(b"changed")
            with self.assertRaisesRegex(W.BuildError, "Source changed"):
                W.snapshot_source(root, root / "snapshot", {"window.py": "0" * 64}, "1.2.3")


    def test_modified_snapshot_cannot_be_archived_as_matching_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "window.py").write_bytes(b"original")
            expected = W.file_records(root)
            W.write_json(root / "SOURCE_MANIFEST.json", expected)
            W.verify_snapshot(root, expected)
            (root / "mr_config.json").write_text("{}")
            with self.assertRaisesRegex(W.BuildError, "modified the source snapshot"):
                W.verify_snapshot(root, expected)

    def test_diagnostic_archives_exclude_profiles_and_installed_payloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "report"
            for name in ("build.log", "results.json", "frozen/isolated-data/mr_config.json", "installer/old-payload/private.json", "installer/installed/user.log", "installer/fresh-installed/user.json"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("test")
            destination = Path(tmp) / "logs.zip"
            W.zip_logs(root, destination)
            with zipfile.ZipFile(destination) as archive:
                self.assertEqual(set(archive.namelist()), {"logs/build.log", "logs/results.json"})


class NativePolicy(unittest.TestCase):
    def approved_record(self, root, name):
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b'tool')
        (root / 'LICENSE.txt').write_bytes(b'licence')
        return dict(path=name, sha256=W.sha256(file), source_url='https://example.test/releases/1', license='Test licence',
                    redistribution_permitted=True, license_path='LICENSE.txt', license_sha256=W.sha256(root / 'LICENSE.txt'))

    def test_windows_case_duplicate_native_records_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = self.approved_record(root, 'AtelierMesh/Example.dll')
            other = dict(record, path='AtelierMesh/example.dll')
            policy = root / 'policy.json'
            W.write_json(policy, {'files': [record, other]})
            with self.assertRaisesRegex(W.BuildError, 'duplicate'):
                W.approved_tools(policy, root)

    def test_native_provenance_urls_cannot_include_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = self.approved_record(root, 'UAssetTool.exe')
            policy = root / 'policy.json'
            for url in ('https://user:secret@example.test/file', 'https://example.test/file?token=private', 'https://example.test/file?X-Amz-Signature=private'):
                W.write_json(policy, {'files': [dict(record, source_url=url)]})
                with self.subTest(url=url), self.assertRaisesRegex(W.BuildError, 'without credentials'):
                    W.approved_tools(policy, root)

    def test_external_tools_do_not_require_development_tools(self):
        self.assertEqual(W.approved_tools(None, None), [])

    def test_no_redistribution_without_permission_and_licence_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "UAssetTool.exe").write_bytes(b"tool")
            (root / "LICENSE.txt").write_bytes(b"licence")
            policy = root / "policy.json"
            record = {"path": "UAssetTool.exe", "sha256": W.sha256(root / "UAssetTool.exe")}
            W.write_json(policy, {"files": [record]})
            with self.assertRaisesRegex(W.BuildError, "Redistribution"):
                W.approved_tools(policy, root)
            record.update(license="Test licence", source_url="https://example.test/source", redistribution_permitted=True,
                          license_path="LICENSE.txt", license_sha256=W.sha256(root / "LICENSE.txt"))
            W.write_json(policy, {"files": [record]})
            self.assertEqual(W.approved_tools(policy, root), [record])
            (root / "UAssetTool.exe").write_bytes(b"changed")
            with self.assertRaisesRegex(W.BuildError, "hash mismatch"):
                W.approved_tools(policy, root)

    def test_deny_native_private_and_traversal_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = root / "policy.json"
            for name in ("AES_KEY.txt", "Mappings/file.usmap", "AtelierMesh/../../outside.exe", "shaders/unreviewed.dll"):
                W.write_json(policy, {"files": [{"path": name}]})
                with self.subTest(name=name), self.assertRaises(W.BuildError):
                    W.approved_tools(policy, root)


class FailureAndInstaller(unittest.TestCase):
    def test_failure_has_report_and_no_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            with patch.object(P, "preflight", side_effect=W.BuildError("missing pinned tool")):
                code = P.main(["--output-dir", str(output), "--version", "1.2.3"])
            self.assertEqual(code, 1)
            report = next((output / "reports").glob("*/VALIDATION.json"))
            self.assertEqual(json.loads(report.read_text())["status"], "failed")
            self.assertFalse((output / "releases").exists())

    def test_installer_include_is_exact_and_handles_literal_braces(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "bundle {test}"
            (bundle / "_internal").mkdir(parents=True)
            (bundle / "Atelier.exe").write_bytes(b"exe")
            (bundle / "_internal/library.dll").write_bytes(b"dll")
            include = root / "payload.iss"
            W.write_installer_files(bundle, include)
            text = include.read_text(encoding="utf-8-sig")
            self.assertEqual(len(text.splitlines()), 2)
            self.assertNotIn("*", text)
            self.assertIn("{{test}", text)
            self.assertIn('DestDir: "{app}\\_internal"', text)

    def test_inno_discovery_is_actionable(self):
        with patch.dict(os.environ, {"ProgramFiles": "Z:/missing", "ProgramFiles(x86)": "Z:/missing", "LOCALAPPDATA": "Z:/missing", "ISCC": ""}), patch.object(W.shutil, "which", return_value=None):
            with self.assertRaisesRegex(W.BuildError, "--iscc"):
                W.discover_iscc()

    def test_unpinned_dependencies_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "requirements.txt"
            path.write_text("numpy>=2\n")
            with self.assertRaisesRegex(W.BuildError, "Unpinned"):
                W.pinned_requirements(path)

    def test_missing_webview_is_reported_before_shell_import(self):
        from atelier import windows_runtime
        with patch.object(windows_runtime.os, "name", "nt"), patch.object(windows_runtime, "webview2_version", return_value=None):
            self.assertIn("WebView2", windows_runtime.launch_issue())

    def test_diagnostic_failure_preserves_original_build_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(P, 'preflight', side_effect=W.BuildError('missing compiler')), patch.object(P, 'zip_logs', side_effect=OSError('diagnostic disk failure')):
                self.assertEqual(P.main(['--output-dir', str(Path(tmp) / 'output'), '--version', '1.2.3']), 1)
            report = next((Path(tmp) / 'output/reports').glob('*/VALIDATION.json'))
            self.assertEqual(json.loads(report.read_text())['error'], 'missing compiler')


class ExportedSource(unittest.TestCase):
    def make_export(self, root):
        for name in ('window.py', 'Atelier.spec', 'Atelier.iss', 'version', 'requirements-build.txt', 'LICENSE', 'scripts/new.py'):
            file = root / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b'source')
        W.write_json(root / 'BUILD_INFO.json', {'base_commit': 'a' * 40, 'working_tree': True})
        W.write_json(root / 'SOURCE_MANIFEST.json', W.file_records(root))

    def test_archive_is_rebuildable_without_git_and_with_bootstrapped_venv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_export(root)
            (root / '.venv-build').mkdir()
            (root / '.venv-build/python.exe').write_bytes(b'environment')
            with patch.object(W, 'git', side_effect=AssertionError('Must not need Git')):
                inventory, excluded = W.source_inventory(root)
                self.assertEqual(W.source_identity(root), ('a' * 40, True, 'source-archive'))
            self.assertIn('scripts/new.py', inventory)
            self.assertNotIn('BUILD_INFO.json', inventory)
            self.assertEqual(excluded, [])

    def test_modified_or_extra_exported_source_is_rejected(self):
        for name in ('scripts/new.py', 'scripts/extra.py'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.make_export(root)
                (root / name).write_bytes(b'changed')
                with self.assertRaises(W.BuildError):
                    W.source_inventory(root)


class DeliveredPayload(unittest.TestCase):
    def test_seed_is_required_on_fresh_install_and_preserved_only_on_upgrade(self):
        from installer_smoke import assert_payload
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle, installed = root / 'bundle', root / 'installed'
            (bundle / 'Tools').mkdir(parents=True)
            installed.mkdir()
            (bundle / 'Tools/MarvelRivalsCharacterIDs.md').write_text('packaged seed')
            with self.assertRaises(W.BuildError):
                assert_payload(bundle, installed)
            assert_payload(bundle, installed, preserve_seed=True)
            (installed / 'Tools').mkdir()
            (installed / 'Tools/MarvelRivalsCharacterIDs.md').write_text('updated user seed')
            assert_payload(bundle, installed, preserve_seed=True)
            with self.assertRaises(W.BuildError):
                assert_payload(bundle, installed)

    def bundle(self, root):
        for name in ('Atelier.exe', '_internal/python312.dll', '_internal/version', 'LICENSE', 'README.txt', 'BUILD_INFO.json', 'NATIVE_TOOLS.json'):
            file = root / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b'payload')
        manifest = {'files': W.file_records(root)}
        W.write_json(root / 'MANIFEST.json', manifest)
        return manifest

    def test_changed_added_removed_payload_and_manifest_are_rejected(self):
        for mode in ('changed', 'added', 'removed', 'manifest'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                bundle = Path(tmp) / 'bundle'
                manifest = self.bundle(bundle)
                if mode == 'changed':
                    (bundle / 'Atelier.exe').write_bytes(b'changed')
                elif mode == 'added':
                    (bundle / '_internal/stale.dll').write_bytes(b'stale')
                elif mode == 'removed':
                    (bundle / 'README.txt').unlink()
                else:
                    W.write_json(bundle / 'MANIFEST.json', {'files': []})
                with self.assertRaises(W.BuildError):
                    W.verify_payload(bundle, manifest)

    def test_portable_archive_roundtrip_and_unlisted_private_member_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / 'bundle'
            manifest = self.bundle(bundle)
            archive = root / 'portable.zip'
            W.zip_tree(bundle, archive, 'Atelier/')
            extracted = W.extract_verified_portable(archive, root / 'extracted', manifest)
            self.assertEqual((extracted / 'Atelier.exe').read_bytes(), b'payload')
            with zipfile.ZipFile(archive, 'a') as output:
                output.writestr('Atelier/Tools/AES_KEY.txt', 'private')
            with self.assertRaisesRegex(W.BuildError, 'inventory'):
                W.extract_verified_portable(archive, root / 'must-not-exist', manifest)
            self.assertFalse((root / 'must-not-exist').exists())

    def test_corrupted_portable_member_is_rejected_before_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self.bundle(root / 'bundle')
            archive = root / 'portable.zip'
            with zipfile.ZipFile(archive, 'w') as output:
                for file in (root / 'bundle').rglob('*'):
                    if file.is_file():
                        output.writestr('Atelier/' + file.relative_to(root / 'bundle').as_posix(), b'corrupt' if file.name == 'Atelier.exe' else file.read_bytes())
            with self.assertRaisesRegex(W.BuildError, 'hash mismatch'):
                W.extract_verified_portable(archive, root / 'extracted', manifest)

    def test_pinned_test_transitive_dependencies_are_validated(self):
        with patch.object(W, 'pinned_requirements', return_value={'pyee': '14.0.0'}), patch.object(W.importlib.metadata, 'version', return_value='0.0.1'):
            with self.assertRaisesRegex(W.BuildError, 'pyee'):
                W.validate_dependencies(ROOT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
