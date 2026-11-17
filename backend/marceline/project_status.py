"""Pure derivations behind `manage.py project_status` (`make status`), issue #79.

Everything here is a function of text that is already on disk: no database, no subprocess, no
network, no clock of its own. The command module does the I/O and hands the text in, which is why
these parts — above all `env_gaps`, the settings↔compose gap detector — are the ones with tests.

The gap detector exists because of two day-long bugs (see the CLAUDE.md rule "Prove it before
reporting it"): a setting `backend/marceline/settings.py` reads from the environment but that
`infra/compose.yaml` passes to no service is **silently empty in every container, forever**,
however carefully `.env` sets it. `DJANGO_CSRF_TRUSTED_ORIGINS` was that way (every customer-app
POST failed its CSRF check), and `BROWSER_CAPTURE_ENABLED` /
`MATCHING_METADATA_CANDIDATES_ENABLED` were that way right after the operator turned them on.

How the two sides are read:

* settings side — every `env_str/env_bool/env_int/env_float/env_list/env_is_set("NAME"…)` call.
  That is the whole vocabulary `core.env` offers, and settings.py touches `os.environ` no other
  way, so the set is exact rather than a guess.
* compose side — every SCREAMING_SNAKE_CASE mapping key in the file. Compose's own schema keys are
  lower-case without exception (`build`, `command`, `profiles`, `environment`, `mem_limit`, …), so
  an upper-case key is an environment variable being passed to a container — including the ones
  under the `x-*-env` anchors that services merge in, which an `environment:`-block walk would
  have to re-implement YAML merge keys to find.

Both rules are deliberately syntactic: a gap detector that imports settings would only ever see
the machine it runs on, and this one has to answer for containers it is not inside.
"""

import re
from dataclasses import dataclass

#: `core.env`'s readers. Nothing in settings.py reads `os.environ` directly.
_ENV_READ = re.compile(
    r"\benv_(?:str|bool|int|float|list|is_set)\(\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']"
)
#: An upper-case YAML mapping key: an environment variable handed to a container (see above).
_COMPOSE_ENV_KEY = re.compile(r"^\s+([A-Z][A-Z0-9_]*):(?:\s|$)")
#: A compose service: a mapping key two spaces in, under the top-level `services:`.
_COMPOSE_SERVICE = re.compile(r"^  ([a-z][a-z0-9_-]*):\s*(?:&[\w-]+\s*)?$")
#: A `.env` assignment, matching `core.env.load_dotenv`'s own parsing.
_DOTENV_ASSIGNMENT = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def settings_env_names(settings_text: str) -> set[str]:
    """Every environment variable `settings.py` reads, by the `core.env` reader it reads it with."""
    return set(_ENV_READ.findall(settings_text))


def compose_env_names(compose_text: str) -> set[str]:
    """Every environment variable a compose file hands to a container."""
    return {
        match.group(1)
        for line in compose_text.splitlines()
        if (match := _COMPOSE_ENV_KEY.match(line))
    }


def compose_service_names(compose_text: str) -> set[str]:
    """The services a compose file defines (top-level keys under `services:`, anchors included)."""
    names: set[str] = set()
    in_services = False
    for line in compose_text.splitlines():
        if not line.startswith((" ", "\t", "#")) and line.strip():
            in_services = line.startswith("services:")
            continue
        if in_services and (match := _COMPOSE_SERVICE.match(line)):
            names.add(match.group(1))
    return names


def dotenv_names(dotenv_text: str) -> set[str]:
    """The names `.env` assigns, whether or not the value is empty (`core.env.load_dotenv`)."""
    names: set[str] = set()
    for raw_line in dotenv_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if match := _DOTENV_ASSIGNMENT.match(line):
            names.add(match.group(1))
    return names


@dataclass(frozen=True)
class EnvGaps:
    """Settings read from the environment that `infra/compose.yaml` passes to no service.

    `silent` is the alarming half: the operator set the variable in `.env`, so they believe it is
    in effect, and no container has ever seen it. `prod_only` and `default_only` are the rest,
    split by whether the production compose file passes them — a production-only setting missing
    from the development file is expected, a setting missing from both simply keeps its
    `settings.py` default everywhere.
    """

    silent: tuple[str, ...]
    prod_only: tuple[str, ...]
    default_only: tuple[str, ...]

    @property
    def total(self) -> int:
        return len(self.silent) + len(self.prod_only) + len(self.default_only)


def env_gaps(
    settings_text: str,
    compose_text: str,
    dotenv_text: str = "",
    prod_compose_text: str = "",
) -> EnvGaps:
    """Classify every settings-read variable `infra/compose.yaml` never passes through."""
    missing = settings_env_names(settings_text) - compose_env_names(compose_text)
    configured = dotenv_names(dotenv_text)
    in_prod = compose_env_names(prod_compose_text)
    silent = sorted(name for name in missing if name in configured)
    rest = missing - set(silent)
    return EnvGaps(
        silent=tuple(silent),
        prod_only=tuple(sorted(name for name in rest if name in in_prod)),
        default_only=tuple(sorted(name for name in rest if name not in in_prod)),
    )


def format_age(seconds: float) -> str:
    """A compact age: `now`, `7m`, `3h`, `2d`. Negative (a clock skew) reads as `now`."""
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def wrap(text: str, width: int) -> list[str]:
    """Break `text` on spaces into lines of at most `width`; a longer word gets its own line."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}" if current else word
        if current and len(candidate) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def truncate(text: str, limit: int) -> str:
    """One line, at most `limit` characters, with a trailing `…` when something was cut."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def flag_value(raw: str | None) -> str:
    """How one watched environment variable reads in a container.

    `None` — the variable is not in the container's environment at all, which is the gap this
    command exists to surface: the process is running on `settings.py`'s default and nothing in
    `.env` can change that. An empty string is `EMPTY`, so a variable that is passed but blank
    never looks the same as one that is absent.
    """
    if raw is None:
        return "not passed"
    return raw.strip() or "EMPTY"
