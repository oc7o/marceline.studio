import pytest

from crawler.blocking import (
    AGE_SELF_DECLARATION,
    AGE_VERIFICATION_PROVIDER,
    MAX_GATE_PAGE_LINKS,
    classify_age_gate,
    detect_block,
)
from crawler.tests.helpers import load_fixture

LINKS = "".join(f'<a href="/video/{i}/">v{i}</a>' for i in range(MAX_GATE_PAGE_LINKS + 5))
SELF_DECLARATION_FORM = """<html><head><title>Adults only</title></head><body>
<div class="age-gate"><h1>Are you 18 or older?</h1>
<form method="post" action="/age/"><button name="confirm" value="yes">Yes, I am 18+</button>
<a href="https://www.google.com/">No, leave</a></form></div></body></html>"""
COOKIE_GATE = """<html><body><p>This website is age-restricted.</p>
<button onclick="document.cookie='kt_agecheck=1;path=/';location.reload()">Enter</button>
</body></html>"""
AGEVERIF_PAGE = (
    '<html><head><script src="https://www.agegate.example/checker.js?key=abc"></script></head>'
    f"<body>{LINKS}</body></html>"
)
YOTI_PAGE = (
    '<html><body><h1>Verify your age</h1><iframe src="https://age.yoti.com/age-estimation?x=1">'
    "</iframe></body></html>"
)


def test_detects_cloudflare_access_denied_page() -> None:
    body = load_fixture("site15.example", "blocked-403-home.html")

    reason = detect_block(403, {"server": "cloudflare"}, body, "https://site15.example/")

    assert reason is not None
    assert "Cloudflare" in reason


def test_detects_challenge_header() -> None:
    assert detect_block(200, {"cf-mitigated": "challenge"}, "", "https://s.test/") is not None


def test_detects_login_redirect() -> None:
    assert detect_block(200, {}, "<html></html>", "https://s.test/login/?next=/x") is not None


def test_normal_video_page_is_not_blocked() -> None:
    body = load_fixture("site01.example", "video-1.html")

    assert detect_block(200, {}, body, "https://site01.example/video/8977/x/") is None


@pytest.mark.parametrize("body", [SELF_DECLARATION_FORM, COOKIE_GATE])
def test_click_through_age_gate_is_self_declaration(body: str) -> None:
    assert detect_block(200, {}, body, "https://s.test/") == AGE_SELF_DECLARATION


@pytest.mark.parametrize(
    "domain,fixture",
    [
        ("site19.example", "video.html"),
        ("site19.example", "search.html"),
        ("site22.example", "video.html"),
        ("site22.example", "search.html"),
        ("site02.example", "video-1.html"),
    ],
)
def test_client_side_overlay_on_full_page_is_not_a_gate(domain: str, fixture: str) -> None:
    body = load_fixture(domain, fixture)

    assert detect_block(200, {}, body, f"https://{domain}/x/") is None


@pytest.mark.parametrize(
    "body,provider",
    [
        (AGEVERIF_PAGE, "AgeGate"),
        (YOTI_PAGE, "Yoti"),
        ('<script src="https://verifymy.io/sdk.js"></script>' + LINKS, "VerifyMy"),
        ('<a href="https://ok.cam/verify?site=x">Verify</a>', "ok.cam"),
    ],
)
def test_verification_provider_markers(body: str, provider: str) -> None:
    assert classify_age_gate(body) == f"{AGE_VERIFICATION_PROVIDER}: {provider}"


@pytest.mark.parametrize(
    "body,method",
    [
        (
            '<h1>Age verification</h1><form><input type="password" name="pw"></form>',
            "login-based check",
        ),
        ("<h1>Verify your age</h1><p>Upload your ID or take a selfie.</p>", "ID/selfie"),
    ],
)
def test_verification_methods_without_named_provider(body: str, method: str) -> None:
    reason = classify_age_gate(body)

    assert reason is not None
    assert reason.startswith(AGE_VERIFICATION_PROVIDER)
    assert method in reason


def test_lookalike_domains_are_not_providers() -> None:
    body = (
        f'<img src="https://cdn.test/book.cam.png"><a href="https://notagegate.example/">x</a>{LINKS}'
    )

    assert classify_age_gate(body) is None


def test_age_notice_in_error_or_captcha_pages_is_not_an_age_gate() -> None:
    assert detect_block(404, {}, SELF_DECLARATION_FORM, "https://s.test/") is None
    captcha = SELF_DECLARATION_FORM.replace("</form>", '<div class="g-recaptcha"></div></form>')
    assert classify_age_gate(captcha) is None


# --- issue #78: a caller handing over a truncated document ------------------------------------
#
# The headless capture path caps what it ships to Python (`marceline/capture/capture.mjs`,
# `HTML_LIMIT`), so `body` there is only the first 50,000 characters of the rendered page. The
# `link_count` keyword simulates that: it is what capture.mjs counts over the *whole* document,
# before truncating, and is meant to override whatever `body` alone would say.

TRUNCATED_WINDOW_WITH_GATE_LIKE_CLASS = (
    '<html><body><div class="age-gate-page">'
    + "".join(f'<a href="/x/{i}/">x</a>' for i in range(4))
    + "<script>/* the rest of the page was cut off here */"
)


def test_recounting_on_a_truncated_body_alone_reproduces_the_false_positive() -> None:
    """Without a whole-document count, a normal page's truncated window can carry an unrelated
    layout class that coincides with an age-gate marker and, within that window alone, few links
    — the bug this issue is about."""
    assert classify_age_gate(TRUNCATED_WINDOW_WITH_GATE_LIKE_CLASS) == AGE_SELF_DECLARATION


def test_whole_document_link_count_clears_a_truncated_false_positive() -> None:
    body, url = TRUNCATED_WINDOW_WITH_GATE_LIKE_CLASS, "https://s.test/"

    assert classify_age_gate(body, link_count=96) is None
    assert detect_block(200, {}, body, url, link_count=96) is None


def test_a_real_gate_is_still_caught_with_its_own_whole_document_link_count() -> None:
    """A genuine self-declaration gate withholds the rest of the page, so its own whole-document
    link count stays low too. The override must not make the detector more permissive for a real
    gate — only correct an undercount for a page that is not one."""
    assert classify_age_gate(SELF_DECLARATION_FORM, link_count=1) == AGE_SELF_DECLARATION
    assert (
        detect_block(200, {}, SELF_DECLARATION_FORM, "https://s.test/", link_count=1)
        == AGE_SELF_DECLARATION
    )
