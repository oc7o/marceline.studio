"""Lift a login lockout by hand (ADR 0023).

The lockout expires on its own after `LOGIN_LOCKOUT_MINUTES`, so this exists for the case the
window is built for: a customer who locked themselves out and is on the phone. It is also the only
way to clear a bucket, because `accounts.LoginAttempt` stores digests rather than names — the admin
can see that *something* is locked, but only this command can turn a username back into its key.

    manage.py unlock_login --user someone@example.com
    manage.py unlock_login --ip 203.0.113.7
    manage.py unlock_login --expired          # housekeeping only; nothing is unlocked by it

Unlocking is not a password reset: passwords are set with `manage.py changepassword` (ADR 0023
defers self-service reset).
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from accounts import login_throttle


class Command(BaseCommand):
    help = "Clear counted login failures for an account or an IP address."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--user", dest="users", action="append", help="Username to unlock; repeatable."
        )
        parser.add_argument(
            "--ip", dest="ips", action="append", help="IP address to unthrottle; repeatable."
        )
        parser.add_argument(
            "--expired", action="store_true", help="Also delete attempts that no longer count."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        users: list[str] = options["users"] or []
        ips: list[str] = options["ips"] or []
        if not users and not ips and not options["expired"]:
            raise CommandError("Nothing to do: pass --user, --ip or --expired.")
        for username in users:
            removed = login_throttle.unlock(username)
            self.stdout.write(f"account {username}: {removed} counted failure(s) cleared")
        for ip in ips:
            removed = login_throttle.unlock_ip(ip)
            self.stdout.write(f"ip {ip}: {removed} counted failure(s) cleared")
        if options["expired"]:
            self.stdout.write(f"expired: {login_throttle.purge_expired()} row(s) deleted")
