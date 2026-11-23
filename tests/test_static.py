"""Static scan of the frontend: CSP-safe HTML, theme consistency, no deleted-asset links, no innerHTML."""
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from server.db import THEME_IDS

STATIC = Path(__file__).resolve().parent.parent / "static"
HTML_FILES = sorted(STATIC.glob("*.html"))

THEME_VARS = [
    "bg", "bg-alt", "bg-hl", "fg", "muted", "border", "accent", "accent-fg", "ok", "warn", "err", "info", "sel",
    "cursor", "black", "red", "green", "yellow", "blue", "magenta", "cyan", "white",
    "label-person", "label-vehicle", "label-animal", "label-object",
]


class Scan(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_head = False
        self.head_scripts = []   # (src or None) in order, head only
        self.inline_scripts = 0
        self.style_elements = 0
        self.style_attrs = 0
        self.handlers = []
        self.refs = []           # /static/... paths
        self.stylesheets = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "head":
            self.in_head = True
        if tag == "script":
            if not a.get("src"):
                self.inline_scripts += 1
            if self.in_head:
                self.head_scripts.append(a.get("src"))
        if tag == "style":
            self.style_elements += 1
        if tag == "link" and a.get("rel") == "stylesheet":
            self.stylesheets.append(a.get("href"))
        for k, v in attrs:
            if k == "style":
                self.style_attrs += 1
            if re.fullmatch(r"on[a-z]+", k):
                self.handlers.append(k)
            if k in ("src", "href") and v and v.startswith("/static/"):
                self.refs.append(v.split("?")[0].split("#")[0])

    def handle_endtag(self, tag):
        if tag == "head":
            self.in_head = False


def scan(path: Path) -> Scan:
    s = Scan()
    s.feed(path.read_text(encoding="utf-8"))
    return s


def test_html_pages_found():
    assert {p.name for p in HTML_FILES} >= {"index.html", "camera.html", "login.html", "welcome.html", "pair.html", "setup.html"}


@pytest.mark.parametrize("path", HTML_FILES, ids=lambda p: p.name)
def test_html_is_csp_clean(path):
    s = scan(path)
    assert s.inline_scripts == 0, "inline <script>"
    assert s.style_elements == 0, "<style> element"
    assert s.style_attrs == 0, "style= attribute"
    assert s.handlers == [], "on*= attributes"
    assert s.head_scripts and s.head_scripts[0] == "/static/js/theme-boot.js", "theme-boot.js must be the first head script"
    css = s.stylesheets
    assert css.index("/static/css/themes.css") < css.index("/static/css/base.css")
    for ref in s.refs:
        assert (STATIC / ref[len("/static/"):]).exists(), f"missing {ref}"
        assert ref != "/static/style.css"


@pytest.mark.parametrize("name", ["index.html", "camera.html"])
def test_app_pages_use_app_css_and_modules(name):
    s = scan(STATIC / name)
    assert "/static/css/app.css" in s.stylesheets
    assert "/static/js/viewer.js" in s.head_scripts or "/static/js/camera.js" in s.head_scripts


def test_camera_page_loads_tf_before_module():
    src = (STATIC / "camera.html").read_text(encoding="utf-8")
    tf, coco, mod = (src.index(x) for x in ("vendor/tf.min.js", "vendor/coco-ssd.min.js", "js/camera.js"))
    assert tf < coco < mod


def test_deleted_assets_gone():
    for name in ("style.css", "viewer.js", "camera.js", "welcome.js"):
        assert not (STATIC / name).exists()


def test_no_unsafe_html_sinks_in_js():
    bad = re.compile(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write\(")
    for js in (STATIC / "js").glob("*.js"):
        assert not bad.search(js.read_text(encoding="utf-8")), js.name


def test_theme_ids_consistent():
    js = (STATIC / "js" / "theme.js").read_text(encoding="utf-8")
    js_ids = re.findall(r"id:\s*'([a-z-]+)'", js)
    css = (STATIC / "css" / "themes.css").read_text(encoding="utf-8")
    css_ids = []
    for m in re.finditer(r':root\[data-theme="([a-z-]+)"\]', css):
        css_ids.append(m.group(1))
    assert js_ids == list(THEME_IDS)
    assert css_ids == list(THEME_IDS)


def test_every_theme_defines_every_variable():
    css = (STATIC / "css" / "themes.css").read_text(encoding="utf-8")
    blocks = re.findall(r'(:root\[data-theme="([a-z-]+)"\][^{]*)\{([^}]*)\}', css)
    assert sorted(b[1] for b in blocks) == sorted(THEME_IDS)
    for _, theme, body in blocks:
        for var in THEME_VARS:
            assert re.search(rf"--{re.escape(var)}\s*:", body), f"{theme} lacks --{var}"


def test_app_js_uses_shared_modules_and_no_inline_theme_save_on_camera():
    cam = re.sub(r"//[^\n]*", "", (STATIC / "js" / "camera.js").read_text(encoding="utf-8"))
    assert "saveTheme" not in cam
    calls = re.findall(r"\bapi\([^)]*\)", cam)
    assert all("redirectOn401: false" in c for c in calls), calls
    assert "/api/me" in cam  # 4401 probe before showing the revoked card
    assert "syncThemeFromSettings" in cam
    assert "camera-logic.js" in cam
    view = (STATIC / "js" / "viewer.js").read_text(encoding="utf-8")
    assert "/api/me" in view and "stableTimer" in view  # 4401 probe, 30 s stability reset
    assert "proto=2" in view and "createImageBitmap" in view and "backoffDelay" in view
