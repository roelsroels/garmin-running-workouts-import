import shutil
import subprocess
from pathlib import Path

import pytest
from test_web import _app


def test_theme_control_and_early_script_are_shared_by_all_pages(tmp_path):
    client = _app(tmp_path).test_client()
    for url in ("/", "/calendar", "/goal", "/settings", "/workouts/new", "/cleanup"):
        response = client.get(url)
        assert response.status_code == 200
        html = response.data.decode()
        assert '<meta name="color-scheme" content="light dark">' in html
        assert html.index('src="/static/theme.js"') < html.index('href="/static/web.css"')
        assert 'role="group" aria-label="Color theme"' in html
        for mode in ("system", "light", "dark"):
            assert f'data-theme-choice="{mode}"' in html
        assert "script-src 'self'" in response.headers["Content-Security-Policy"]


def test_theme_assets_have_system_fallback_and_themed_surfaces(tmp_path):
    client = _app(tmp_path).test_client()
    script = client.get("/static/theme.js")
    styles = client.get("/static/web.css")
    assert script.status_code == 200
    assert styles.status_code == 200
    assert b"localStorage" in script.data
    assert b'addEventListener("change", applyTheme)' in script.data
    assert b"@media (prefers-color-scheme: light)" in styles.data
    assert b":root:not([data-theme])" in styles.data
    assert b".theme-switch[hidden]" in styles.data
    assert b"background: var(--field-bg)" in styles.data
    assert b"background: var(--wait-backdrop)" in styles.data
    assert b".theme-switch button[aria-pressed=" in styles.data


def test_theme_preference_behavior_with_node():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is optional and only needed for JavaScript unit tests")
    script = Path(__file__).with_name("theme.test.cjs")
    result = subprocess.run([node, "--test", str(script)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
