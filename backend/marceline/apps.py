from typing import Any

from django.apps import AppConfig


class MarcelineConfig(AppConfig):
    name = "marceline"
    verbose_name = "Marceline"

    def ready(self) -> None:
        from django.db.models.signals import post_save, pre_save

        from index.models import Page

        pre_save.connect(_note_safety_flag, sender=Page, dispatch_uid="marceline.cache.pre_save")
        post_save.connect(_purge_flagged, sender=Page, dispatch_uid="marceline.cache.post_save")

        from index.signals import safety_rules_applied

        # Incidents and the customer release queue for pages the crawler stored (docs/safety.md).
        safety_rules_applied.connect(
            _safety_rules_applied, sender=Page, dispatch_uid="marceline.safety_rules"
        )

        _install_data_request_counter()


def _install_data_request_counter() -> None:
    """Let the daily operator report count the open customer data requests (ADR 0056, issue #133).

    The report lives in `crawler/safety_digest.py` and `crawler` may not import `marceline`
    (import-linter). It therefore holds a hook and a two-integer value type, and the project package
    — which may import anything — fills it in here, once, at startup. The alternative would have
    been a `crawler` module reaching into a `marceline` table, which is the contract this repository
    does not bend; the other alternative, passing the count in from the scheduler, would have left
    `manage.py safety_digest --dry-run` printing a section the real mail has.
    """
    from crawler import safety_digest
    from marceline.deletion import requests as data_requests

    def counter() -> safety_digest.DataRequests:
        waiting = data_requests.waiting()
        return safety_digest.DataRequests(waiting=waiting.waiting, oldest_days=waiting.oldest_days)

    safety_digest.data_request_source = counter


FLAG_ATTRIBUTE = "_marceline_cache_purge"


#: Page columns the customer-visibility rule reads (`marceline.app.safety`). A save that touches
#: none of them cannot change whether the page may be shown.
SAFETY_FIELDS = ("safety_status", "safety_score", "safety_matches")


def _note_safety_flag(sender: Any, instance: Any, raw: bool = False, **kwargs: Any) -> None:
    """Before a page save: is this the moment the page stops being one a customer may see?

    Not just "stops being safety-clear" (ADR 0028 purge, ADR 0018): a page that keeps
    `safety_status=clear` but whose score drops below `SAFETY_GATE_MIN_SCORE` is hidden from every
    customer view exactly as a flagged one is, so production's cached copy of it has to go too.

    One extra read, and only for a page saved in a state a customer may not see, while purging is
    configured — the same cost shape as before.
    """
    from marceline import cache_purge
    from marceline.app.safety import fields_visible

    update_fields = kwargs.get("update_fields")
    if raw or instance.pk is None or not cache_purge.enabled():
        return
    if update_fields is not None and not set(SAFETY_FIELDS) & set(update_fields):
        return
    if fields_visible(instance.safety_status, instance.safety_score, instance.safety_matches):
        return
    previous = type(instance).objects.filter(pk=instance.pk).values_list(*SAFETY_FIELDS).first()
    if previous is not None and fields_visible(*previous):
        setattr(instance, FLAG_ATTRIBUTE, True)


def _purge_flagged(sender: Any, instance: Any, raw: bool = False, **kwargs: Any) -> None:
    from marceline import cache_purge

    if getattr(instance, FLAG_ATTRIBUTE, False):
        delattr(instance, FLAG_ATTRIBUTE)
        cache_purge.purge_flagged_page(instance.pk)


def _safety_rules_applied(sender: Any, **kwargs: Any) -> None:
    from marceline import safety_rules

    safety_rules.after_store(sender, **kwargs)
