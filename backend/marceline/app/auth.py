"""Authentication for the customer API.

Session auth only: *who* is asking. **What they may see is never decided here** — it is decided
per resource by `ownership`, which scopes every queryset to one organisation and returns 404 for
anything else. An authenticated user with no organisation is a valid caller who owns nothing; that
is a 200 with an empty list, not a 403.

Staff get no special treatment. `is_staff` opens the review API, not another tenant's account.

Sessions are *created* in `marceline/app/session.py` (`/api/v1/app/auth/…`), which is where the
membership requirement, the throttling and the lockout live (ADR 0023). This module only reads the
session that is already there — a staff session reaching a customer endpoint is a valid caller who
owns nothing, exactly as ADR 0021 tests it.
"""

from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import AnonymousUser
from ninja.security import SessionAuth

# The user a request carries: an authenticated account, or nobody.
RequestUser = AbstractBaseUser | AnonymousUser

customer_auth = SessionAuth()
