#!/usr/bin/env python3
"""
Drive the page in a real browser and assert what it does.

The archive asserts its data in check.py. This asserts the one thing a reader
actually touches, and it exists because the app bar was holding 515 of 844
pixels on a phone — the archive had less room than the controls for reading it.

Three faults were found writing it, none of which would have shown in a
screenshot: the collapsed bar ran its button off the edge of the screen because
a shorthand padding wiped out the page gutter; the summary repeated a count
left over from whichever view was open before; and the sentinel that notices
scrolling was one pixel tall in the wrong direction — an element with no area
never intersects anything, so it never fired at all.

    python3 tools/uicheck.py                      # against a local copy
    python3 tools/uicheck.py --url https://...    # against what is published

Needs playwright (pip install playwright && playwright install chromium). It
reads the page and never writes anything.
"""
import argparse
import functools
import http.server
import os
import socketserver
import sys
import threading

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8711
FAILED = []


def serve():
    """A quiet server over the working copy, so the check needs no setup."""
    class H(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a): pass
    socketserver.TCPServer.allow_reuse_address = True
    srv = socketserver.TCPServer(("127.0.0.1", PORT), functools.partial(H, directory=HERE))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{PORT}/index.html"


def ok(cond, what, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'}  {what}{('  — ' + detail) if detail and not cond else ''}")
    if not cond:
        FAILED.append(what)


def folded(pg):
    return pg.evaluate("document.querySelector('.appbar').classList.contains('min')")


def bar_height(pg):
    return pg.evaluate("Math.round(document.querySelector('.appbar').getBoundingClientRect().height)")


def check(pw, url, width, height, label):
    print(f"\n{label} ({width}x{height})")
    b = pw.chromium.launch()
    pg = b.new_page(viewport={"width": width, "height": height})
    pg.goto(url, wait_until="networkidle")
    pg.wait_for_timeout(2500)
    pg.fill("#q", "Deloitte")
    pg.wait_for_timeout(3500)
    pg.eval_on_selector("#q", "e=>e.blur()")
    pg.wait_for_timeout(300)

    full = bar_height(pg)
    ok(not folded(pg), "the bar is open before anything is scrolled")

    pg.mouse.wheel(0, 1400)
    try:
        pg.wait_for_function("()=>document.querySelector('.appbar').classList.contains('min')",
                             timeout=6000)
    except Exception:
        pass
    small = bar_height(pg)
    ok(folded(pg), "it folds once the reader is into the results")
    ok(small < full / 3, f"the fold is worth having: {full}px to {small}px",
       f"only {full - small}px returned")
    ok(small < height * 0.12, f"folded, it takes {round(small/height*100)}% of the screen")

    # The button must sit inside the page's own gutter, not run off the edge.
    box = pg.evaluate("""()=>{const e=document.querySelector('#miniChange');
        const r=e.getBoundingClientRect(); return {right:Math.round(r.right), vw:window.innerWidth};}""")
    ok(box["right"] <= box["vw"] - 8, "the Change button is inside the screen",
       f"right edge {box['right']} of {box['vw']}")

    said = pg.evaluate("""()=>({label:document.querySelector('#miniLabel').textContent,
        count:document.querySelector('#miniCount').textContent})""")
    ok(said["label"] == "Deloitte", "it says what was searched for", repr(said["label"]))
    ok(said["count"].endswith("contracts") and any(c.isdigit() for c in said["count"]),
       "it says how many were found", repr(said["count"]))

    # Nothing may flap while the reader sits still.
    flips = pg.evaluate("""async ()=>{const bar=document.querySelector('.appbar');
        let n=0,last=bar.classList.contains('min');
        const mo=new MutationObserver(()=>{const now=bar.classList.contains('min');
            if(now!==last){n++;last=now;}});
        mo.observe(bar,{attributes:true,attributeFilter:['class']});
        await new Promise(r=>setTimeout(r,2000)); mo.disconnect(); return n;}""")
    ok(flips == 0, "it does not flap while the reader sits still", f"{flips} changes")

    pg.click("#miniChange")
    try:
        pg.wait_for_function("()=>window.scrollY===0", timeout=5000)
    except Exception:
        pass
    pg.wait_for_timeout(400)
    ok(not folded(pg), "Change opens it again")
    ok(pg.evaluate("window.scrollY") == 0, "Change goes back to the top, where the search is")
    ok(pg.evaluate("document.activeElement.id") == "q", "Change puts the cursor in the search box")

    # Typing must never pull the field away mid-word.
    pg.type("#q", "Serco", delay=40)
    pg.wait_for_timeout(1200)
    ok(not folded(pg), "it stays open while the reader is typing")

    # The analysis view has no result set, so it must not claim a count.
    pg.eval_on_selector("#q", "e=>e.blur()")
    pg.click("#tab-a")
    pg.wait_for_timeout(2500)
    pg.mouse.wheel(0, 1400)
    pg.wait_for_timeout(900)
    if folded(pg):
        a = pg.evaluate("""()=>({label:document.querySelector('#miniLabel').textContent,
            count:document.querySelector('#miniCount').textContent})""")
        ok(a["count"] == "", "the analysis view claims no result count", repr(a["count"]))
        ok(a["label"] == "Analysis", "the analysis view is named as itself", repr(a["label"]))

    b.close()


def main():
    ap = argparse.ArgumentParser(description="Assert how the page behaves in a browser.")
    ap.add_argument("--url", help="a published copy; the working copy is served if omitted")
    args = ap.parse_args()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("uicheck needs playwright: pip install playwright && playwright install chromium")

    srv = None
    url = args.url
    if not url:
        srv, url = serve()
    print(f"Against {url}")
    with sync_playwright() as pw:
        check(pw, url, 390, 844, "On a phone")
        check(pw, url, 1440, 900, "On a desktop")
    if srv:
        srv.shutdown()
    print(f"\n{len(FAILED)} failure(s).")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
