"""Tiny helpers for reading configuration from environment variables."""

import os
from pathlib import Path


class MissingEnvError(RuntimeError):
    """Raised when a required environment variable is not set."""


def load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines from a .env file without overriding existing variables."""
    if not path.is_file():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def env_str(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, "")
    if value:
        return value
    if default is None:
        raise MissingEnvError(f"Environment variable {name} is required")
    return default


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Environment variable {name} is not a boolean: {value!r}")


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


def env_float(name: str, default: float) -> float:
    value = os.environ.get(name, "").strip()
    return float(value) if value else default


def env_list(name: str, default: list[str]) -> list[str]:
    value = os.environ.get(name, "")
    if not value.strip():
        return default
    return [item.strip() for item in value.split(",") if item.strip()]


def env_is_set(name: str) -> bool:
    """True when the variable has a non-empty value, as opposed to unset or `NAME=`."""
    return bool(os.environ.get(name, "").strip())
