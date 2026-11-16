"""The dashboard's design rules, checked where they can be checked without a browser.

docs/plans/dashboard-calm-redesign.md: one token set for both appearances, text
that stays readable in both (including under the translucent header at its
worst case), one definition of each shared helper, colour only from tokens,
and a status verdict decided by the health engine rather than by the page.
"""

from __future__ import annotations

import re

import pytest

from fleet_manager.server.health_engine import Recommendation, Severity, fleet_status
from fleet_manager.server.routes import dashboard as d

# --- contrast ---------------------------------------------------------------


def _luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    chans = [int(h[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in chans]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _blend(top: str, under: str, alpha: float) -> str:
    t = [int(top.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)]
    u = [int(under.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)]
    return "#" + "".join(
        f"{round(alpha * x + (1 - alpha) * y):02x}" for x, y in zip(t, u, strict=True)
    )


THEMES = sorted(d._TOKENS)


def test_both_appearances_define_the_same_tokens():
    assert d._TOKENS["dark"].keys() == d._TOKENS["light"].keys()


TEXT_TOKENS = ["text-1", "text-2", "text-3", "accent", "ok", "warn", "fail", "info"]


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("fg", TEXT_TOKENS)
@pytest.mark.parametrize("bg", ["surface-0", "surface-1"])
def test_text_tokens_are_readable_as_body_text(theme, fg, bg):
    """4.5:1 (WCAG AA body text) for every token used as text, on the surfaces
    text sits on.  text-3 is included: it carries small de-emphasised text."""
    t = d._TOKENS[theme]
    assert _contrast(t[fg], t[bg]) >= 4.5, f"{theme}: --{fg} on --{bg}"


@pytest.mark.parametrize("theme", THEMES)
def test_primary_buttons_are_readable(theme):
    t = d._TOKENS[theme]
    assert _contrast(t["accent-ink"], t["accent-fill"]) >= 4.5


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("under", ["#ffffff", "#000000"])
def test_header_nav_is_readable_at_its_worst_case(theme, under):
    """The header is translucent, so it has no single colour: blend its tint at
    the real opacity over the lightest and the darkest thing that can scroll
    under it, and the nav text must clear 4.5:1 against both."""
    t = d._TOKENS[theme]
    worst = _blend(t["surface-0"], under, d._HEADER_TINT_PCT / 100)
    for fg in ("text-1", "text-2"):
        assert _contrast(t[fg], worst) >= 4.5, f"{theme}: --{fg} over {under}"


def test_header_css_uses_the_tint_the_test_checks():
    assert f"var(--surface-0) {d._HEADER_TINT_PCT}%, transparent" in d._SHARED_CSS


def test_glass_has_a_solid_fallback_for_every_route_to_it():
    css = d._SHARED_CSS
    for trigger in (
        "@supports not ((backdrop-filter",
        "prefers-reduced-transparency: reduce",
        "prefers-contrast: more",
        ':root[data-transparency="off"] .header',
    ):
        assert trigger in css, trigger


def test_motion_and_visibility_basics():
    css = d._SHARED_CSS
    assert "prefers-reduced-motion: reduce" in css
    assert ":focus-visible" in css
    # Components set display; the hidden attribute must still win.
    assert "[hidden] { display: none !important; }" in css


# --- one definition of each helper -------------------------------------------

PAGES = [
    "/dashboard",
    "/dashboard/health",
    "/dashboard/models",
    "/dashboard/tags",
    "/dashboard/settings",
    "/dashboard/color-states",
]
HELPERS = [
    "fmtNum",
    "fmtCompact",
    "fmtMs",
    "formatGB",
    "timeAgo",
    "statusDot",
    "chartTheme",
    "scoreClass",
    "showToast",
    "setTabAlert",
    "renderBriefing",
    "initTimeRange",
]


@pytest.fixture(scope="module")
def rendered():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(d.router)
    client = TestClient(app)
    return {p: client.get(p).text for p in PAGES}


@pytest.mark.parametrize("page", PAGES)
def test_shared_helpers_are_defined_once_per_page(rendered, page):
    """Each helper used to have per-page copies (three fmtNum with two
    behaviours, four chart colour objects).  A page must not redefine one."""
    html = rendered[page]
    for name in HELPERS:
        assert len(re.findall(rf"function {name}\(", html)) == 1, f"{page}: {name}"
    # Only the shared token() helper reads CSS variables; pages ask chartTheme().
    assert html.count("getPropertyValue(") == 1, page
    assert "const C =" not in html and "const chartDefaults" not in html


@pytest.mark.parametrize("page", PAGES)
def test_pages_use_tokens_not_hard_coded_colours(rendered, page):
    """Every colour comes from a token; the only hex literals are the token
    definitions themselves (and the colour reference, which prints them)."""
    html = rendered[page]
    body = html.split("</style>", 1)[1]  # skip the generated token stylesheet
    if page == "/dashboard/color-states":
        return
    found = set(re.findall(r"#[0-9a-fA-F]{6}\b", body))
    assert not found, f"{page}: {sorted(found)}"


# --- the status verdict --------------------------------------------------------


def _rec(sev, **data):
    return Recommendation(
        check_id="x", severity=sev, title="t", description="d", fix="f", data=data
    )


@pytest.mark.parametrize(
    ("recs", "verdict"),
    [
        ([], "healthy"),
        ([_rec(Severity.INFO), _rec(Severity.INFO)], "healthy"),
        ([_rec(Severity.INFO), _rec(Severity.WARNING)], "degraded"),
        ([_rec(Severity.WARNING), _rec(Severity.CRITICAL)], "critical"),
        ([_rec(Severity.CRITICAL, resolved=True)], "healthy"),
        ([_rec(Severity.CRITICAL, resolved=True), _rec(Severity.WARNING)], "degraded"),
    ],
)
def test_fleet_status_is_the_worst_open_severity(recs, verdict):
    """INFO never turns the status line amber, and a resolved finding does not
    count: a standing list of no-action items is how a status line stops being
    read."""
    assert fleet_status(recs) == verdict


def test_health_report_carries_the_verdict():
    from fleet_manager.server.health_engine import FleetVitals, HealthReport

    assert HealthReport(vitals=FleetVitals()).model_dump()["status"] == "healthy"


def test_overview_never_folds_a_failure():
    """Calm must not hide a problem: a model with failures counts as active and
    keeps its row, and warnings and critical findings render outside the fold."""
    assert (
        "function isActive(q) { return (q.in_flight || 0) + (q.pending || 0)"
        " + (q.completed || 0) + (q.failed || 0) > 0; }"
    ) in d._OVERVIEW_BODY
    assert "r.severity !== 'info' && !(r.data && r.data.resolved)" in d._HEALTH_BODY
