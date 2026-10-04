"""UI screenshots for docs/screenshots (EXTENSION_SPEC step B5).

Serves extension/dist over localhost, screenshots popup / sidepanel / every
dashboard page in both themes. The dashboard has no server here, so these show
the empty/loading/error states; preview.html shows the mock-data states.
"""
import functools
import http.server
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent.parent
DIST = ROOT / "extension" / "dist"
OUT = ROOT / "docs" / "screenshots"


def serve():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(DIST))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 8766), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


PAGES = [
    ("popup", "popup.html", 380, 620),
    ("sidepanel", "sidepanel.html", 360, 720),
    ("home", "app.html#/home", 1280, 900),
    ("calls", "app.html#/calls", 1280, 900),
    ("ask", "app.html#/ask", 1280, 900),
    ("brain", "app.html#/brain", 1280, 900),
    ("todos", "app.html#/todos", 1280, 900),
    ("contacts", "app.html#/contacts", 1280, 900),
    ("settings", "app.html#/settings", 1280, 900),
    ("preview-home", "preview.html?page=home", 900, 900),
    ("preview-call", "preview.html?page=call", 900, 700),
    ("preview-error", "preview.html?page=error", 900, 500),
]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    serve()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for name, url, w, h in PAGES:
            for theme in ("light", "dark"):
                page = browser.new_page(viewport={"width": w, "height": h},
                                        color_scheme=theme)
                page.add_init_script(
                    f"try {{ localStorage.setItem('phathom-theme', '{theme}'); }} catch {{}}")
                full = f"http://127.0.0.1:8766/{url}"
                if "preview.html" in url:
                    sep = "&" if "?" in url else "?"
                    full = f"{full}{sep}theme={theme}"
                page.goto(full)
                page.wait_for_timeout(1500)
                path = OUT / f"{name}-{theme}.png"
                page.screenshot(path=str(path))
                print("saved", path)
                page.close()
        browser.close()


if __name__ == "__main__":
    main()
