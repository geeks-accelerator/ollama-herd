# ruff: noqa: E501  (embedded browser JavaScript reads better unwrapped)
"""Rendered checks of the dashboard against a running router: text contrast and
reduced motion, on every page, in both appearances.

    uv run --with playwright python scripts/dashboard-contrast-sweep.py
    uv run --with playwright python scripts/dashboard-contrast-sweep.py --url http://host:11435

Why this exists beside tests/test_server/test_dashboard_design.py: that file
checks the token *pairs* someone thought to list.  This one measures what the
browser actually paints -- each text node's computed colour against the
background composited from every translucent layer beneath it.  It found the
white-on-red danger button (2.8:1) and a warn-tinted table row that the pair
tests passed, the same way the ollamaherd.com sweep found failures Lighthouse
never reported over gradients.

Transitions are disabled before measuring.  In a hidden or background tab they
freeze mid-flight and the sweep reads a half-faded colour as a failure.

Exit status is the number of failures, so it can gate a commit.  Needs a live
router.  Uses Playwright's bundled Chromium, or the installed Chrome if that
has not been downloaded.
"""

from __future__ import annotations

import argparse
import sys

# Not "networkidle": the Overview holds an SSE stream open, so the network is
# never idle.  Wait for load, then give the fetches time to render.
SETTLE_MS = 2500

PAGES = [
    "/dashboard",
    "/dashboard/health",
    "/dashboard/models",
    "/dashboard/tags",
    "/dashboard/settings",
    "/dashboard/benchmarks",
]

# Open every disclosure and the panels that start hidden, so folded rows are
# measured too.  Then walk each element that owns text.
SWEEP_JS = r"""
() => {
  document.querySelectorAll('details').forEach(d => d.open = true);
  document.querySelectorAll('.delete-section[hidden], #bench-progress[hidden]').forEach(e => e.hidden = false);
  function parse(c) {
    const m = c.match(/[\d.]+/g); if (!m) return null; const v = m.map(Number);
    if (c.startsWith('color(srgb')) return [v[0] * 255, v[1] * 255, v[2] * 255, v[3] ?? 1];
    return [v[0], v[1], v[2], v[3] ?? 1];
  }
  function lum([r, g, b]) {
    return [r, g, b].map(x => { x /= 255; return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4; })
      .reduce((a, x, i) => a + x * [0.2126, 0.7152, 0.0722][i], 0);
  }
  function ratio(a, b) { const [h, l] = [lum(a), lum(b)].sort((x, y) => y - x); return (h + 0.05) / (l + 0.05); }
  function backdrop(el) {
    const layers = [];
    for (let e = el; e; e = e.parentElement) {
      const c = parse(getComputedStyle(e).backgroundColor);
      if (c && c[3] > 0) { layers.push(c); if (c[3] >= 1) break; }
    }
    let b = parse(getComputedStyle(document.body).backgroundColor);
    for (let i = layers.length - 1; i >= 0; i--) {
      const [r, g, bb, a] = layers[i];
      b = [r * a + b[0] * (1 - a), g * a + b[1] * (1 - a), bb * a + b[2] * (1 - a), 1];
    }
    return b;
  }
  const out = [], seen = new Set();
  document.querySelectorAll('body *').forEach(el => {
    if (el.closest('svg, canvas, .swatch-panel')) return;
    const text = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent.trim()).join(' ').trim();
    if (!text) return;
    const cs = getComputedStyle(el);
    if (el.offsetParent === null && cs.position !== 'fixed') return;
    const fg = parse(cs.color); if (!fg) return;
    const size = parseFloat(cs.fontSize), bold = +cs.fontWeight >= 700;
    const need = (size >= 24 || (size >= 18.66 && bold)) ? 3 : 4.5;
    const r = ratio(fg, backdrop(el));
    const key = el.className + '|' + cs.color;
    if (r < need && !seen.has(key)) {
      seen.add(key);
      out.push(`${r.toFixed(2)} < ${need}  ${el.tagName.toLowerCase()}.${el.className}  "${text.slice(0, 40)}"`);
    }
  });
  return out;
}
"""

# Under reduced motion nothing may move: no running animation, no element with
# a non-zero transition, and the shared JS flag must agree with the media query.
MOTION_JS = r"""
() => {
  const moving = [...document.querySelectorAll('body *')].filter(el => {
    const cs = getComputedStyle(el);
    return parseFloat(cs.transitionDuration) > 0 || (cs.animationName !== 'none' && parseFloat(cs.animationDuration) > 0);
  }).map(el => el.tagName.toLowerCase() + '.' + el.className);
  return { flag: REDUCED_MOTION, running: document.getAnimations().length, moving: [...new Set(moving)].slice(0, 10) };
}
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default="http://localhost:11435")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    failures = 0
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception:  # bundled browser not downloaded: use installed Chrome
            browser = pw.chromium.launch(channel="chrome")
        for scheme in ("dark", "light"):
            page = browser.new_page(color_scheme=scheme, viewport={"width": 1280, "height": 900})
            for path in PAGES:
                page.goto(args.url + path, wait_until="load")
                page.wait_for_timeout(SETTLE_MS)
                page.add_style_tag(
                    content="*, *::before, *::after { transition: none !important; animation: none !important; }"
                )
                page.wait_for_timeout(300)
                found = page.evaluate(SWEEP_JS)
                failures += len(found)
                print(f"contrast  {scheme:5}  {path:22}  {'ok' if not found else ''}")
                for line in found:
                    print(f"    {line}")
            page.close()

        # Reduced motion: honoured at load, and followed when it changes mid-session.
        page = browser.new_page(reduced_motion="reduce")
        for path in PAGES:
            page.goto(args.url + path, wait_until="load")
            page.wait_for_timeout(SETTLE_MS)
            page.wait_for_timeout(300)
            m = page.evaluate(MOTION_JS)
            bad = (not m["flag"]) or m["running"] or m["moving"]
            failures += bool(bad)
            print(f"motion    reduce {path:22}  {'ok' if not bad else m}")
        # The change event is async: give it a tick before reading the flag.
        page.emulate_media(reduced_motion="no-preference")
        page.wait_for_timeout(200)
        live_off = not page.evaluate("REDUCED_MOTION")
        page.emulate_media(reduced_motion="reduce")
        page.wait_for_timeout(200)
        live_on = page.evaluate("REDUCED_MOTION")
        failures += not (live_off and live_on)
        print(
            f"motion    live toggle {'':17}  {'ok' if live_off and live_on else 'flag did not follow the media query'}"
        )
        browser.close()

    print(f"\n{failures} failure(s)")
    return min(failures, 125)


if __name__ == "__main__":
    sys.exit(main())
