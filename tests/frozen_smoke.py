"""Exercise a built Windows executable in its real WebView2 shell, with isolated data.

Requires Playwright in the test interpreter and an installed WebView2 runtime.
Game browsing uses explicit HTTP fixtures; no game tools, exports or installs run.
The application starts hidden, uses a fresh data directory and closes after checks.
"""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request


def wait_port(port, process, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f'Executable exited during startup: {process.returncode}')
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=.3):
                return
        except OSError:
            time.sleep(.1)
    raise TimeoutError(f'Port {port} did not open')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path, required=True)
    parser.add_argument('--report-dir', type=Path, required=True)
    parser.add_argument('--debug-port', type=int, default=0, help='0 selects an available port')
    parser.add_argument('--app-port', type=int, default=0, help='0 selects an available port')
    parser.add_argument('--first-run', action='store_true', help='Check actual initial setup without creating config or project fixtures')
    args = parser.parse_args()
    if os.name != 'nt':
        raise SystemExit('The frozen desktop smoke requires Windows.')
    exe = args.exe.resolve(strict=True)
    report = args.report_dir.resolve()
    report.mkdir(parents=True, exist_ok=True)
    data = report / 'isolated-data'
    data.mkdir(exist_ok=False)
    reserved = []
    try:
        for attribute in ('app_port', 'debug_port'):
            sock = socket.socket()
            reserved.append(sock)
            sock.bind(('127.0.0.1', getattr(args, attribute)))
            setattr(args, attribute, sock.getsockname()[1])
    except OSError as error:
        raise SystemExit(f'Test port is unavailable: {error}; no running application was stopped.')
    finally:
        for sock in reserved:
            sock.close()
    if not args.first_run:
        project = data / 'assets' / 'projects' / 'Frozen smoke'
        project.mkdir(parents=True)
        (data / 'mr_config.json').write_text(json.dumps({
            'paks': str(data / 'no-game-paks'), 'tools': str(data / 'Tools'),
            'active_project': 'Frozen smoke', 'mods_folder': str(data / 'test-mods'),
        }), encoding='utf-8')
    env = os.environ.copy()
    env['ATELIER_DATA_DIR'] = str(data)
    env['ATELIER_PORT'] = str(args.app_port)
    env['WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS'] = f'--remote-debugging-port={args.debug_port}'
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    process = subprocess.Popen([str(exe)], cwd=exe.parent, env=env, startupinfo=startup)
    stop_hiding = threading.Event()
    # pywebview can show its own window after process startup. Hide only this PID.
    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def hide_window(hwnd, _):
        pid = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == process.pid:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
        return True

    def hide_loop():
        while not stop_hiding.wait(.05):
            ctypes.windll.user32.EnumWindows(hide_window, 0)

    hider = threading.Thread(target=hide_loop, daemon=True)
    hider.start()
    result = {'exe_sha256': hashlib.sha256(exe.read_bytes()).hexdigest(),
              'checks': [], 'page_errors': [], 'ports': {'app': args.app_port, 'debug': args.debug_port},
              'fixture_scope': 'Game browsing and remote checks only'}
    try:
        wait_port(args.app_port, process)
        wait_port(args.debug_port, process)
        from playwright.sync_api import sync_playwright, TimeoutError as CaptureTimeout
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f'http://127.0.0.1:{args.debug_port}')
            context = browser.contexts[0]
            page = context.pages[0]
            page.on('pageerror', lambda error: result['page_errors'].append(str(error)))
            if args.first_run:
                context.route('https://**', lambda route: route.fulfill(json={}))
                context.route('**/api/update_check', lambda route: route.fulfill(json={'available': False}))
                page.reload()
                page.wait_for_function("document.querySelector('#setup-overlay')?.classList.contains('active')")
                with urllib.request.urlopen(f'http://127.0.0.1:{args.app_port}/api/setup_status') as response:
                    assert json.load(response)['configured'] is False
                # Setup may display auto-detected game paths or a fetched AES value.
                # Record assertions only; do not put a first-run screenshot in release logs.
                assert not result['page_errors'], result['page_errors']
                result['checks'].append('Clean first launch opens real Setup without development configuration, game files or native tools')
                print('PASS ' + result['checks'][-1])
                browser.close()
                return
            fixture_items = [dict(type='folder', name=n, label=n, rel_path=n, count=3)
                             for n in ('Characters', 'Textures', 'Materials', 'Weapons', 'Audio', 'Maps')]
            fixtures = {
                '/api/update_check': {'available': False},
                '/api/setup_status': {'configured': True},
                '/api/prereqs': {'issues': []}, '/api/browse': fixture_items,
                '/api/index_warnings': {'failed': []},
                '/api/stale_mips': {'ok': True, 'count': 0},
                '/api/usmap_update_check': {'ok': True},
                '/api/catalogue/status': {'updating': False, 'portraits': 0},
            }
            from urllib.parse import urlparse
            def fixture(route):
                value = fixtures.get(urlparse(route.request.url).path)
                route.fulfill(json=value) if value is not None else route.continue_()
            context.route('**/api/**', fixture)
            context.route('https://**', lambda route: route.fulfill(json={}))
            page.reload()
            page.wait_for_function("window.AtelierViewport && window.atelierProject?.() === 'Frozen smoke'")
            page.wait_for_function("document.querySelectorAll('#grid-area .card').length === 6")
            page.wait_for_function("!document.querySelector('.overlay.active')")
            # The grid and sidebar load independently. Require sidebar readiness rather than
            # sampling it immediately after the six browse cards appear.
            page.wait_for_function("document.querySelector('[data-atelier-motion=\"empty-assets\"]')?.getBoundingClientRect().width === 112")
            page.evaluate("() => document.fonts.ready")
            result['renderer'] = page.evaluate('navigator.userAgent')
            result['checks'].append('Actual frozen executable serves the interface in native WebView2')
            info = page.evaluate("""() => ({
                library: typeof AtelierLibraryTools === 'object',
                pins: typeof AtelierLibraryPins === 'object',
                exportReview: typeof AtelierExportReview === 'object',
                originalComparison: typeof materialDirtyCount === 'function',
                motions: Object.keys(AtelierMotion).sort(),
                folderMotion: !!document.querySelector('link[href$="folder-flap.css"]'),
                font: document.fonts.check('14px Roboto'),
                logo: document.querySelector('.studio-brand > img').naturalWidth,
                emptyTray: document.querySelector('[data-atelier-motion="empty-assets"]')?.getBoundingClientRect().width,
                singleMark: [...document.querySelectorAll('.studio-brand > img, .studio-brand > [data-atelier-motion="logo-entrance"]')]
                  .filter(el => getComputedStyle(el).display !== 'none').length
            })""")
            assert all(info[k] for k in ('library', 'pins', 'exportReview', 'originalComparison', 'folderMotion', 'font')), info
            assert info['motions'] == ['emptyAssets', 'exportFeedback', 'logoEntrance', 'materialSave', 'viewportLoader'], info
            assert info['logo'] > 0 and info['emptyTray'] == 112 and info['singleMark'] == 1, info
            result['interface'] = info
            result['checks'].append('All workflow modules, six motion sets, local font and logo load from the frozen bundle')
            theme_colors = {}
            for theme in ('light', 'dark'):
                page.locator('#topbar [data-theme-picker]').select_option(theme)
                assert page.evaluate('document.documentElement.dataset.theme') == theme
                if theme == 'dark':
                    page.wait_for_function('(previous) => getComputedStyle(document.body).backgroundColor !== previous',
                                           arg=theme_colors['light'], polling=100)
                theme_colors[theme] = page.evaluate('getComputedStyle(document.body).backgroundColor')
                try:
                    page.screenshot(path=str(report / f'frozen-{theme}.png'), animations='disabled', timeout=5000)
                except CaptureTimeout:
                    # A hidden WebView2 compositor can stall PNG capture while its DOM and
                    # backend remain responsive. Images are diagnostics; assertions still gate.
                    warning = f'{theme} PNG capture timed out in the hidden native shell'
                    result.setdefault('screenshot_warnings', []).append(warning)
                    print('DIAGNOSTIC ' + warning)
            assert theme_colors['light'] != theme_colors['dark'], theme_colors
            result['theme_colors'] = theme_colors
            result['checks'].append('Light and dark themes render in the native shell')
            # Confirm the real frozen backend registered the new endpoint, without native tools.
            try:
                urllib.request.urlopen(f'http://127.0.0.1:{args.app_port}/api/material_original?game_rel=../outside')
            except urllib.error.HTTPError as response:
                assert response.code == 400, response.code
                rejected = json.load(response)
            else:
                raise AssertionError('Invalid material path was not rejected with HTTP 400')
            assert rejected.get('ok') is False and rejected.get('error'), rejected
            result['checks'].append('Frozen Python backend includes original-material comparison and rejects traversal')
            # This request goes directly to the frozen backend, outside the UI's HTTP fixtures.
            with urllib.request.urlopen(f'http://127.0.0.1:{args.app_port}/api/prereqs') as response:
                prerequisites = json.load(response)
            messages = '\n'.join(issue['message'] for issue in prerequisites['issues'])
            for tool in ('UAssetTool.exe', 'retoc-rivals-cli.exe', 'oo2core_9_win64.dll'):
                assert tool in messages, (tool, prerequisites)
            result['checks'].append('Missing external UAssetTool, custom retoc and Oodle produce actionable prerequisite messages')
            assert not result['page_errors'], result['page_errors']
            browser.close()
    except Exception as exc:
        result['error'] = str(exc)
        raise
    finally:
        stop_hiding.set()
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        (report / 'results.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    for check in result['checks']:
        print('PASS ' + check)
    print('Report:', report / 'results.json')


if __name__ == '__main__':
    main()
