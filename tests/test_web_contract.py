"""The front end is plain ES modules with no build step, so nothing catches a
typo in an element id or an API path until it fails silently in the browser.

These tests are that check. They are deliberately crude — string matching, not a
JS parser — because the failure they prevent is crude: `$("job-detial")` returns
null, the render function throws inside a subscriber, and the panel simply never
appears with no error the user can see.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "web"
JS = sorted((WEB / "js").glob("*.js"))


def _html():
    return (WEB / "index.html").read_text()


def test_every_element_id_the_scripts_reach_for_exists():
    ids = set(re.findall(r'id="([^"]+)"', _html()))
    missing = {}
    for script in JS:
        for used in re.findall(r'\$\("([^"]+)"\)', script.read_text()):
            if used not in ids:
                missing.setdefault(script.name, set()).add(used)
    assert not missing, f"scripts reference ids that are not in index.html: {missing}"


def test_reconstruction_panel_is_wired_end_to_end():
    """Guards the upload -> reconstruct -> poll path specifically, because it is
    the newest and the one with the most moving parts."""
    html = _html()
    main = (WEB / "js" / "main.js").read_text()
    api = (WEB / "js" / "api.js").read_text()

    for element in ("reconstruct-panel", "reconstruct-btn", "reconstruct-hint",
                    "job", "job-stage", "job-pct", "job-fill", "job-detail", "job-log"):
        assert f'id="{element}"' in html, f"{element} missing from index.html"

    assert "startReconstruction" in main and "pollJob" in main
    assert 'addEventListener("click", startReconstruction)' in main
    assert "renderReconstruct" in main
    for call in ("reconstruct:", "job:", "engines:"):
        assert call in api, f"api.js is missing {call}"


def test_scripts_only_call_api_paths_the_server_serves():
    served = set(re.findall(r'@app\.(?:get|post)\("(/api/[^"]+)"\)',
                            (WEB.parent / "app" / "main.py").read_text()))
    # Collapse FastAPI path params to a marker so they compare with JS templates.
    served_shapes = {re.sub(r"\{[^}]+\}", "*", p) for p in served}

    api = (WEB / "js" / "api.js").read_text()
    called = set(re.findall(r'["`](/api/[^"`?]+)', api))
    called_shapes = {re.sub(r"\$\{[^}]*\}", "*", p).rstrip("/") for p in called}

    unknown = {p for p in called_shapes if p not in served_shapes}
    assert not unknown, f"api.js calls paths the server does not serve: {unknown}"


@pytest.mark.parametrize("script", JS, ids=lambda p: p.name)
def test_scripts_parse(script):
    """Real syntax check, when a JS engine is around to do it.

    An earlier version of this counted brackets instead, and reported perfectly
    good files as broken because regex literals contain brackets too. A check
    that cries wolf is worse than no check, so this one skips rather than guesses.
    """
    node = shutil.which("node") or shutil.which("nodejs")
    if node is None:
        pytest.skip("no JS engine on PATH; install node to syntax-check the front end")
    result = subprocess.run([node, "--check", str(script)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr.strip()
