import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def checker():
    path = Path(__file__).parents[2] / "deploy/check_frontend_assets.py"
    spec = importlib.util.spec_from_file_location("check_frontend_assets", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_entry_assets_include_preloads_and_deduplicate(checker):
    parser = checker.EntryAssets()
    parser.feed('''<script src="/assets/main.js"></script>
        <link rel="modulepreload" href="/assets/vendor.js">
        <link rel="stylesheet" href="/assets/main.css">
        <script src="/assets/main.js"></script><link rel="icon" href="/icon.svg">''')
    assert parser.assets == {("/assets/main.js", "js"), ("/assets/vendor.js", "js"), ("/assets/main.css", "css")}


@pytest.mark.parametrize("status,mime,body,kind", [
    (200, "text/html", b"<html>fallback</html>", "js"),
    (200, "application/javascript", b"<!DOCTYPE html><html>fallback</html>", "js"),
    (404, "application/javascript", b"missing", "js"),
    (200, "text/css", b"", "css"),
    (200, "text/plain", b"body{}", "css"),
])
def test_invalid_asset_is_rejected(checker, status, mime, body, kind):
    with pytest.raises(ValueError):
        checker.check_asset(status, mime, body, kind)


@pytest.mark.parametrize("mime,body,kind", [
    ("text/javascript", b"export default 1", "js"),
    ("application/javascript", b"export default 1", "js"),
    ("text/css", b"body{}", "css"),
])
def test_valid_asset(checker, mime, body, kind):
    checker.check_asset(200, mime, body, kind)


def test_verify_requires_missing_resource_404(checker, monkeypatch):
    def fetch(url):
        if url.endswith("/"):
            return 200, "text/html", b'<script src="/assets/main.js"></script>'
        if "missing-" in url:
            return 200, "text/html", b"<html>fallback</html>"
        return 200, "text/javascript", b"export default 1"
    monkeypatch.setattr(checker, "fetch", fetch)
    with pytest.raises(ValueError, match="404"):
        checker.verify("http://localhost/")


def test_verify_success(checker, monkeypatch):
    def fetch(url):
        if url.endswith("/"):
            return 200, "text/html", b'<script src="/assets/main.js"></script>'
        if "missing-" in url:
            return 404, "text/html", b"<html>404</html>"
        return 200, "text/javascript", b"export default 1"
    monkeypatch.setattr(checker, "fetch", fetch)
    checker.verify("http://localhost/")
