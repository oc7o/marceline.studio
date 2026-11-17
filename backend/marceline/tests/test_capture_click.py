"""The one allowed play click, as a pure policy (ADR 0016 amendment 2026-09-18, issue #78).

No browser, no database: `marceline.capture.click` decides over *descriptions* of elements, which
is exactly how the real visit works — `capture.mjs` describes, Python decides, the browser clicks
the index it is handed back. So every clause of the rule is provable here, including the ones that
matter most: that a consent, age, login, subscribe, download or ad control can never be chosen,
that an element outside the page's own player can never be chosen, and that a visit has exactly
one click to spend.

The descriptions below are synthetic but shaped after the players the research document names
(`docs/research/browser-capture-no-video.md`): a JW-Player-style poster container that builds its
player on a click, a video.js big-play button over a `<video>`, and the ad furniture every one of
those pages carries next to it.
"""

import pytest

from marceline.capture.click import (
    ALREADY_CLICKED,
    MAX_CLICKS_PER_VISIT,
    NO_CANDIDATES,
    NO_PLAY_CONTROL,
    NOT_OPTED_IN,
    ClickPolicy,
    ElementDescription,
    admissible,
    choose_click_target,
    parse_candidates,
    selector_shape,
    tokens,
)


def element(**fields: object) -> ElementDescription:
    """A description that passes every clause, so a test changes exactly the one it is about."""
    defaults: dict[str, object] = {
        "tag": "div",
        "element_id": "",
        "class_name": "play-button",
        "role": "button",
        "aria_label": "",
        "title": "",
        "text": "",
        "rendered": (96, 96),
        "visible": True,
        "navigates": False,
        "inside_player": True,
        "frame_allowed": True,
        "is_video": False,
        "ancestors": ("div.player-container", "div.wrapper"),
    }
    defaults.update(fields)
    return ElementDescription(**defaults)  # type: ignore[arg-type]


# --- tokenisation: what the denylist actually sees ---------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("kt_player wrapper", {"kt", "player", "wrapper"}),
        ("playButton", {"play", "button"}),  # camelCase is split, so closeAd cannot hide
        ("closeAd", {"close", "ad"}),
        ("btn-skip-ad", {"btn", "skip", "ad"}),
        ("Skip Ad", {"skip", "ad"}),
        ("playlist", {"playlist"}),  # not "play": a playlist is not a play control
    ],
)
def test_identifiers_are_split_into_tokens(value: str, expected: set[str]) -> None:
    assert tokens(value) == expected


# --- what may be clicked -----------------------------------------------------------------------


def test_the_big_play_button_over_a_video_is_admissible() -> None:
    button = element(tag="button", class_name="vjs-big-play-button", aria_label="Play Video")

    assert admissible(button) == "play_control"


def test_the_video_element_itself_is_admissible() -> None:
    video = element(tag="video", class_name="", role="", is_video=True, inside_player=True)

    assert admissible(video) == "video_element"


def test_a_poster_container_that_builds_its_player_on_a_click_is_admissible() -> None:
    """The click-to-play case: there is no `<video>` on the page at all until this is clicked,
    so nothing structural marks it — its own markup naming it the player is what does."""
    container = element(
        tag="div",
        element_id="media-player",
        class_name="",
        role="",
        inside_player=False,  # no <video> anywhere near: the page has none yet
        ancestors=("div.video-wrapper", "div.content"),
    )

    assert admissible(container) == "player_container"


# --- what may never be clicked -----------------------------------------------------------------

FORBIDDEN = (
    pytest.param(element(class_name="play-button agree-consent"), id="consent"),
    pytest.param(element(class_name="play", text="I am over 18"), id="age-self-declaration"),
    pytest.param(element(class_name="play", aria_label="Verify your age"), id="age-verification"),
    pytest.param(element(class_name="play-login"), id="login"),
    pytest.param(element(class_name="play", text="Subscribe to watch"), id="subscribe"),
    pytest.param(element(class_name="play-download-btn"), id="download"),
    pytest.param(element(class_name="playBtn", title="Close"), id="close-ad"),
    pytest.param(element(class_name="play", text="Skip Ad"), id="skip-ad"),
    pytest.param(
        element(class_name="play", ancestors=("div.consent-modal", "div.player-container")),
        id="inside-a-consent-modal",
    ),
    pytest.param(
        element(class_name="play", ancestors=("div.ad-overlay", "div.player-container")),
        id="inside-an-ad-overlay",
    ),
)


@pytest.mark.parametrize("candidate", FORBIDDEN)
def test_a_forbidden_token_anywhere_refuses_the_element(candidate: ElementDescription) -> None:
    """Even next to "play": one token that reads as another action is the end of it."""
    assert admissible(candidate) == ""
    assert choose_click_target((candidate,)) is None


REFUSED_BY_SHAPE = (
    pytest.param(element(tag="a"), id="anchor-tag"),
    pytest.param(element(tag="input"), id="input-tag"),
    pytest.param(element(tag="iframe"), id="iframe-tag"),
    pytest.param(element(navigates=True), id="navigates"),
    pytest.param(element(visible=False), id="not-drawn"),
    pytest.param(element(rendered=(1, 1)), id="one-pixel"),
    pytest.param(element(rendered=(96, 8)), id="too-flat"),
    pytest.param(element(frame_allowed=False), id="frame-not-allowlisted"),
    pytest.param(element(role="link"), id="role-link"),
    pytest.param(element(role="checkbox"), id="role-checkbox"),
    pytest.param(element(role="menuitem"), id="role-menuitem"),
    pytest.param(
        element(inside_player=False, ancestors=("div.sidebar", "div.content")),
        id="outside-the-player",
    ),
    pytest.param(
        element(class_name="thumb", role="", inside_player=False, ancestors=("div.player",)),
        id="in-the-player-but-not-a-play-control",
    ),
)


@pytest.mark.parametrize("candidate", REFUSED_BY_SHAPE)
def test_every_other_clause_refuses_too(candidate: ElementDescription) -> None:
    assert admissible(candidate) == ""
    assert choose_click_target((candidate,)) is None


def test_the_largest_admissible_element_wins_and_the_choice_is_stable() -> None:
    small = element(class_name="play-small", rendered=(32, 32))
    large = element(class_name="play-big", rendered=(200, 120))
    ad = element(class_name="play-now", text="Close ad", rendered=(600, 400))

    first = choose_click_target((small, large, ad))
    second = choose_click_target((small, large, ad))

    assert first is not None and first.index == 1
    assert first == second


# --- what is stored ----------------------------------------------------------------------------


def test_the_stored_shape_is_markup_and_never_page_text() -> None:
    shape = selector_shape(
        element(
            tag="button",
            element_id="media-player",
            class_name="vjs-big-play-button",
            role="button",
            aria_label="Play the full video of Some Title",
            text="Play Some Title now",
        )
    )

    assert shape == "button[role=button].media.player.vjs"
    assert "Title" not in shape and "full" not in shape


def test_an_identifier_that_stopped_looking_like_markup_is_dropped() -> None:
    """Some themes hang long generated strings on their elements; those are not a shape."""
    long_token = "a" * 40
    shape = selector_shape(element(tag="div", element_id="", class_name=f"play {long_token} 42x"))

    assert shape == "div[role=button].play"


# --- one click per visit -----------------------------------------------------------------------


def test_a_site_without_the_opt_in_never_clicks() -> None:
    policy = ClickPolicy(allowed=False)

    assert policy.decide((element(),)) is None
    assert policy.reason == NOT_OPTED_IN
    assert policy.clicks_used == 0
    assert policy.as_json()["chosen"] is False


def test_the_second_click_of_a_visit_is_refused() -> None:
    policy = ClickPolicy(allowed=True)

    first = policy.decide((element(),))
    second = policy.decide((element(),))

    assert first is not None
    assert second is None
    assert policy.reason == ALREADY_CLICKED
    assert policy.clicks_used == MAX_CLICKS_PER_VISIT == 1


def test_the_refusal_reason_distinguishes_no_elements_from_no_play_control() -> None:
    assert ClickPolicy(allowed=True).decide(()) is None
    empty = ClickPolicy(allowed=True)
    empty.decide(())
    assert empty.reason == NO_CANDIDATES

    furniture = ClickPolicy(allowed=True)
    assert furniture.decide((element(class_name="play", text="Download"),)) is None
    assert furniture.reason == NO_PLAY_CONTROL


def test_the_record_carries_counts_and_a_shape_only() -> None:
    policy = ClickPolicy(allowed=True)
    policy.decide((element(tag="button", class_name="vjs-big-play-button", text="Play"),))
    policy.performed = True
    policy.video_after_click = True

    record = policy.as_json()

    assert record == {
        "allowed": True,
        "chosen": True,
        "performed": True,
        "candidates": 1,
        "kind": "play_control",
        "shape": "button[role=button].vjs.big.play",
        "reason": "",
        "video_after_click": True,
    }


# --- the browser's own JSON --------------------------------------------------------------------


def test_browser_json_is_parsed_and_anything_malformed_is_dropped() -> None:
    parsed = parse_candidates(
        [
            {
                "tag": "BUTTON",
                "id": "big-play",
                "className": "vjs-big-play-button",
                "role": "button",
                "ariaLabel": "Play",
                "rendered": [120, 80],
                "visible": True,
                "insidePlayer": True,
                "frameAllowed": True,
                "ancestors": ["div.player"],
                "unknownKey": "ignored",
            },
            "not an element",
            {"tag": "div"},
        ]
    )

    assert len(parsed) == 2
    assert parsed[0].rendered == (120, 80)
    assert parsed[0].ancestors == ("div.player",)
    assert parsed[1].rendered == (0, 0) and parsed[1].visible is False
    assert parse_candidates(None) == ()
