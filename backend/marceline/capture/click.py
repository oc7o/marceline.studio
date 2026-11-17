"""Which element of a page — if any — the capture browser may click, exactly once (issue #78).

Until 2026-09-18 the headless browser performed **no interaction at all**: the page was read as
served, and a site that only creates its player after a click on its own play control simply gave
`no_video: click_to_play` (37 of 43 pages of the re-run in
`docs/research/browser-capture-no-video.md`). The operator decided that **one click on a page's
own play control is normal use** — a visitor does exactly that — while everything else stays what
it was: no consent click, no age click, no login, no subscribe, no download, no ad dismissal, no
second click, no keyboard, no scrolling trick.

Policy is enforced in code, not in prompts, so the whole decision lives here as a pure function
over a *description* of the page's elements. No DOM, no browser, no ORM, no I/O: `capture.mjs`
collects structural facts and this module decides. The browser never clicks anything this module
did not name, and it may name at most one element per visit (`MAX_CLICKS_PER_VISIT`).

## What may be clicked

`choose_click_target` admits an element only when **every** clause holds. Each clause exists
because of a way a click could stop being "press play":

1. **Tag and ARIA role** — the tag is one of `CLICKABLE_TAGS` (never `a`, `form`, `input`,
   `iframe`, `label`: those navigate, submit or belong to another document) and an explicit
   `role`, if the page set one, is one of `ALLOWED_ROLES`. A `role="link"` or `role="checkbox"`
   is the page saying the element is something other than a button, and something else is never
   clicked.
2. **It does not navigate** (`navigates`): not an anchor with an href, not inside one, not a
   submit control. A click that leaves the page is a fetch instruction written by the page.
3. **Its frame is one we were allowed to load** (`frame_allowed`): the main document, or a
   subframe the embed-host allowlist already permitted (`marceline.capture.requests`). An ad
   iframe is refused as a document and can never contribute a candidate.
4. **It is inside the page's own player**: either the browser saw a `<video>` at or right
   around it (`inside_player`, structural — no token is involved), or its own identifiers or
   those of an ancestor within `ANCESTOR_DEPTH` name a player container (`PLAYER_TOKENS`). An
   element outside the player is never the play control, whatever it is called.
5. **It is drawn, and big enough to be a control** (`visible`, `MIN_CLICK_SIZE`): a 1x1 "play"
   element is an ad trick, not a button.
6. **Nothing about it, or about any ancestor, reads as another action.** `FORBIDDEN_TOKENS`
   covers consent, age, login/subscribe/payment, download, share/report and ad dismissal
   ("close", "skip", "continue", …) — checked against the element's identifiers *and* its visible
   text, ARIA label, title and role. One forbidden token anywhere refuses the element outright,
   even if it also says "play". **This is what makes ad-skipping structurally impossible**: a
   "Skip ad" button can never be chosen, so a pre-roll ends the visit exactly as it does today.
7. **It reads as a play control**: the `<video>` element itself, an identifier token in
   `PLAY_TOKENS`, or the player container itself (the click-to-play case, where the page draws a
   poster inside `#…player…` and builds the player on click).

Ties go to the largest drawn element, then to the earliest one: deterministic, so the same page
produces the same decision twice.

## What is recorded, and what is not

`selector_shape` is what a visit stores (`capture_run.diagnostics.click`): the tag, the ARIA role
and up to `SHAPE_TOKENS` identifier tokens that look like markup (`^[a-z][a-z0-9-]{0,31}$`).
Never the element's text, never its ARIA label or title, never a URL — those are page content,
and findings name identifiers, never content. Text and label are *read* here to refuse an
element; they are never returned and never stored.
"""

import re
from dataclasses import dataclass

# Exactly one click per visit, ever. `capture.mjs` counts too; this constant is the policy.
MAX_CLICKS_PER_VISIT = 1
# A control smaller than this is not a play button a visitor could hit (CSS pixels).
MIN_CLICK_SIZE = 24
# Identifier tokens kept in a stored selector shape.
SHAPE_TOKENS = 3
MAX_SHAPE_LENGTH = 120
# How far up the ancestor chain `capture.mjs` describes; the player container must be within it.
ANCESTOR_DEPTH = 6

CLICKABLE_TAGS = frozenset({"video", "button", "div", "span", "i", "b", "img", "svg", "canvas"})
# An explicit ARIA role is the page telling us what the element is. Only these are compatible
# with "the play control"; `link`, `checkbox`, `dialog`, `menuitem` and the rest are the page
# saying it is something else, and something else is never clicked.
ALLOWED_ROLES = frozenset({"", "button", "presentation", "none", "img", "application"})

# Refusal codes stored on the visit (`capture_run.diagnostics.click.reason`). Never page text.
NOT_OPTED_IN = "site not opted in"
NO_CANDIDATES = "no candidate element"
NO_PLAY_CONTROL = "no element passes the play-control rule"
ALREADY_CLICKED = "one click per visit is spent"

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SAFE_TOKEN = re.compile(r"^[a-z][a-z0-9-]{0,31}$")

# Anything that reads as another action than "press play". One of these anywhere on the element
# or its ancestors refuses it, even next to a play token. Ordered by the thing it protects.
FORBIDDEN_TOKENS = frozenset(
    {
        # consent, age and disclaimers — never clicked, with or without an approved age cookie
        "consent",
        "cookie",
        "cookies",
        "gdpr",
        "agree",
        "accept",
        "decline",
        "age",
        "adult",
        "18",
        "21",
        "verify",
        "verified",
        "verification",
        "confirm",
        "disclaimer",
        "warning",
        "terms",
        "privacy",
        "policy",
        "enter",
        "exit",
        "leave",
        # authentication, membership and money
        "login",
        "logon",
        "signin",
        "signup",
        "signout",
        "logout",
        "register",
        "account",
        "member",
        "members",
        "membership",
        "premium",
        "vip",
        "subscribe",
        "subscription",
        "join",
        "upgrade",
        "buy",
        "purchase",
        "order",
        "pay",
        "payment",
        "checkout",
        "price",
        "trial",
        # taking the file, or leaving the page
        "download",
        "dl",
        "save",
        "torrent",
        "magnet",
        "redirect",
        "goto",
        "visit",
        "external",
        "out",
        "offer",
        # advertising and dismissal — this is what makes ad-skipping impossible
        "ad",
        "ads",
        "adv",
        "advert",
        "advertisement",
        "sponsor",
        "sponsored",
        "promo",
        "popup",
        "popunder",
        "banner",
        "interstitial",
        "preroll",
        "midroll",
        "vast",
        "vpaid",
        "ima",
        "close",
        "dismiss",
        "hide",
        "skip",
        "next",
        "continue",
        "start",
        # other actions a page offers next to its player
        "share",
        "embed",
        "report",
        "flag",
        "comment",
        "like",
        "dislike",
        "favorite",
        "favourite",
        "bookmark",
        "playlist",
        "upload",
        "delete",
        "search",
        "menu",
        "settings",
        "fullscreen",
        "mute",
        "unmute",
        "volume",
        "quality",
        "speed",
        "cast",
        "airplay",
        "pip",
    }
)

# An identifier token that names a play control.
PLAY_TOKENS = frozenset({"play", "playback", "bigplay", "playbtn", "playbutton", "playicon"})
# An identifier token that names the page's own video player container. "player" covers
# `#media-player`, `.jw-player`, `.kt_player`, `.fp-player`; the rest are player libraries whose
# containers do not spell it out.
PLAYER_TOKENS = frozenset({"player", "vjs", "plyr", "clappr", "jw", "flowplayer", "kt"})


def token_list(value: str) -> list[str]:
    """Identifier tokens of a class, id, role or piece of text, lowercased, in order.

    `kt_player wrapper` -> [kt, player, wrapper]; `playButton` -> [play, button]; `Skip Ad` ->
    [skip, ad]. Splitting on camelCase as well as on separators is what stops `closeAd` from
    hiding from `FORBIDDEN_TOKENS`.
    """
    spaced = _CAMEL.sub(" ", value)
    return [part for part in _TOKEN_SPLIT.split(spaced.lower()) if part]


def tokens(value: str) -> frozenset[str]:
    """`token_list` as a set, which is how every policy clause below asks its question."""
    return frozenset(token_list(value))


@dataclass(frozen=True)
class ElementDescription:
    """One clickable-looking element, as `capture.mjs` described it. Text-only, never a pixel.

    `text`, `aria_label` and `title` are page content: they are read to *refuse* an element and
    are never returned, logged or stored (`selector_shape` uses identifiers only).
    """

    tag: str
    element_id: str = ""
    class_name: str = ""
    role: str = ""
    aria_label: str = ""
    title: str = ""
    text: str = ""
    # Structural facts the browser computed; every one of them is re-checked here.
    rendered: tuple[int, int] = (0, 0)
    visible: bool = False
    navigates: bool = False
    inside_player: bool = False
    frame_allowed: bool = False
    is_video: bool = False
    # Shapes of up to ANCESTOR_DEPTH ancestors, nearest first: "div#media-player.jw-flag" etc.
    ancestors: tuple[str, ...] = ()

    @property
    def area(self) -> int:
        return max(0, self.rendered[0]) * max(0, self.rendered[1])

    @property
    def identifier_tokens(self) -> frozenset[str]:
        return tokens(self.element_id) | tokens(self.class_name) | tokens(self.tag)

    @property
    def content_tokens(self) -> frozenset[str]:
        return tokens(self.aria_label) | tokens(self.title) | tokens(self.text) | tokens(self.role)

    @property
    def ancestor_tokens(self) -> frozenset[str]:
        return frozenset().union(*(tokens(shape) for shape in self.ancestors))


@dataclass(frozen=True)
class ClickTarget:
    """The one element the browser may click, and how it was recognised."""

    index: int
    kind: str  # video_element | play_control | player_container
    shape: str


@dataclass
class ClickPolicy:
    """The click budget of one visit: opted in or not, and whether the click is spent.

    A `ClickPolicy` belongs to one visit and is never reused, so "one click per visit" is a
    property of the object rather than of a caller remembering to count.
    """

    allowed: bool = False
    clicks_used: int = 0
    reason: str = ""
    target: ClickTarget | None = None
    candidates: int = 0
    # Set by the worker from the browser's own report of the second look at the page: whether the
    # click really landed, and whether a player existed afterwards. Both are what the operator
    # reads to decide whether the click earns its place on a site.
    performed: bool = False
    video_after_click: bool = False

    @property
    def spent(self) -> bool:
        return self.clicks_used >= MAX_CLICKS_PER_VISIT

    def decide(self, candidates: tuple[ElementDescription, ...]) -> ClickTarget | None:
        """Pick the one element to click now, or None — and record why, either way."""
        self.candidates = len(candidates)
        if not self.allowed:
            self.reason = NOT_OPTED_IN
            return None
        if self.spent:
            self.reason = ALREADY_CLICKED
            return None
        target = choose_click_target(candidates)
        if target is None:
            self.reason = NO_CANDIDATES if not candidates else NO_PLAY_CONTROL
            return None
        self.clicks_used += 1
        self.target = target
        self.reason = ""
        return target

    def as_json(self) -> dict[str, object]:
        """What the visit records: whether a click was allowed, taken, and on what shape.

        Never the element's text, ARIA label or title, and never a URL (issue #78).
        """
        return {
            "allowed": self.allowed,
            "chosen": bool(self.clicks_used),
            "performed": self.performed,
            "candidates": self.candidates,
            "kind": self.target.kind if self.target else "",
            "shape": self.target.shape if self.target else "",
            "reason": self.reason,
            "video_after_click": self.video_after_click,
        }


def selector_shape(element: ElementDescription) -> str:
    """A storable shape of the element: tag, ARIA role and a few identifier tokens.

    Identifiers are markup, not page content. They are kept in document order — the id first,
    then the classes as the page wrote them, so the distinctive part survives the truncation to
    `SHAPE_TOKENS` — and only when they still look like markup afterwards (`_SAFE_TOKEN`: short,
    lowercase, not starting with a digit), which drops the long generated strings some themes put
    on their elements.
    """
    ordered: list[str] = []
    for token in token_list(element.element_id) + token_list(element.class_name):
        if _SAFE_TOKEN.match(token) and token not in ordered:
            ordered.append(token)
    shape = element.tag.lower()[:32] or "?"
    if element.role and _SAFE_TOKEN.match(element.role.lower()):
        shape += f"[role={element.role.lower()}]"
    for token in ordered[:SHAPE_TOKENS]:
        shape += f".{token}"
    return shape[:MAX_SHAPE_LENGTH]


def refuses(element: ElementDescription) -> bool:
    """Whether anything about this element, or its ancestors, names another action than play."""
    if FORBIDDEN_TOKENS & element.identifier_tokens:
        return True
    if FORBIDDEN_TOKENS & element.content_tokens:
        return True
    return bool(FORBIDDEN_TOKENS & element.ancestor_tokens)


def in_player_container(element: ElementDescription) -> bool:
    """Whether the element sits in the page's own player.

    Two ways, and a candidate needs one of them. **Structural**: the browser saw a `<video>` at
    the element, inside it, or inside an ancestor within `ANCESTOR_DEPTH` (`inside_player`) — no
    token is consulted, so a player that names nothing still works. **Named**: the element's own
    identifiers, or an ancestor's, carry a `PLAYER_TOKENS` token — which is the click-to-play
    case, where there is no `<video>` on the page at all until something is clicked.

    Everything else is outside the player and is never clicked, however it is labelled.
    """
    if element.is_video or element.inside_player:
        return True
    return bool(PLAYER_TOKENS & (element.identifier_tokens | element.ancestor_tokens))


def is_play_control(element: ElementDescription) -> str:
    """How this element reads as a play control, or "" when it does not.

    Three ways, in the order they are preferred: the `<video>` element itself, an element whose
    identifiers name a play control, and the player container itself — the click-to-play case,
    where the page draws a poster inside its player box and builds the player on the click.
    """
    if element.is_video:
        return "video_element"
    if PLAY_TOKENS & element.identifier_tokens:
        return "play_control"
    if PLAYER_TOKENS & element.identifier_tokens:
        return "player_container"
    return ""


def admissible(element: ElementDescription) -> str:
    """How this element may be clicked, or "" when it may not. Every clause of the docstring."""
    if element.tag.lower() not in CLICKABLE_TAGS:
        return ""
    if element.role.strip().lower() not in ALLOWED_ROLES:
        return ""
    if element.navigates or not element.frame_allowed or not element.visible:
        return ""
    width, height = element.rendered
    if width < MIN_CLICK_SIZE or height < MIN_CLICK_SIZE:
        return ""
    if not in_player_container(element):
        return ""
    if refuses(element):
        return ""
    return is_play_control(element)


def choose_click_target(candidates: tuple[ElementDescription, ...]) -> ClickTarget | None:
    """The one element the browser may click, or None. Deterministic: largest, then earliest."""
    admitted = [
        (index, element, kind)
        for index, element in enumerate(candidates)
        if (kind := admissible(element))
    ]
    if not admitted:
        return None
    index, element, kind = max(admitted, key=lambda item: (item[1].area, -item[0]))
    return ClickTarget(index=index, kind=kind, shape=selector_shape(element))


def parse_candidates(payload: object) -> tuple[ElementDescription, ...]:
    """Browser JSON -> descriptions. Unknown keys are ignored; anything malformed is dropped."""
    if not isinstance(payload, list):
        return ()
    described = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        rendered = item.get("rendered")
        size = (
            (int(rendered[0] or 0), int(rendered[1] or 0))
            if isinstance(rendered, (list, tuple)) and len(rendered) == 2
            else (0, 0)
        )
        described.append(
            ElementDescription(
                tag=str(item.get("tag") or ""),
                element_id=str(item.get("id") or ""),
                class_name=str(item.get("className") or ""),
                role=str(item.get("role") or ""),
                aria_label=str(item.get("ariaLabel") or ""),
                title=str(item.get("title") or ""),
                text=str(item.get("text") or ""),
                rendered=size,
                visible=bool(item.get("visible")),
                navigates=bool(item.get("navigates")),
                inside_player=bool(item.get("insidePlayer")),
                frame_allowed=bool(item.get("frameAllowed")),
                is_video=bool(item.get("isVideo")),
                ancestors=tuple(str(shape) for shape in item.get("ancestors") or ()),
            )
        )
    return tuple(described)
