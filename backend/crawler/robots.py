"""robots.txt policy, re-exported from `core.robots`.

The parser and cache moved to `core` so the media worker (`matching`, which may not import
`crawler`) obeys the same rules for media hosts. Existing `crawler.robots` imports keep working.
"""

from core.robots import (
    ALLOW_ALL,
    ROBOTS_MAX_AGE_S,
    InvalidRobotsUrlError,
    RobotsCache,
    RobotsFacts,
    RobotsRule,
    RobotsRules,
    RobotsUnavailableError,
    describe_robots,
    effective_delay_s,
    parse_robots,
)

__all__ = [
    "ALLOW_ALL",
    "ROBOTS_MAX_AGE_S",
    "InvalidRobotsUrlError",
    "RobotsCache",
    "RobotsFacts",
    "RobotsRule",
    "RobotsRules",
    "RobotsUnavailableError",
    "describe_robots",
    "effective_delay_s",
    "parse_robots",
]
