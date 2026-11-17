from typing import Any

from django.apps import AppConfig
from django.db.models.signals import post_delete, post_save


def _clear_reference_caches(**kwargs: Any) -> None:
    from crawler import services

    services.clear_reference_caches()


class CrawlerConfig(AppConfig):
    name = "crawler"
    verbose_name = "Crawler"

    def ready(self) -> None:
        from catalog.models import Creator, CreatorSuppression, SafetyTerm

        # A term, creator or suppression edited in this process applies to the next page at once;
        # other processes pick it up within crawler.services.REFERENCE_CACHE_TTL_S.
        for model in (SafetyTerm, Creator, CreatorSuppression):
            post_save.connect(
                _clear_reference_caches, sender=model, dispatch_uid=f"crawler-{model}"
            )
            post_delete.connect(
                _clear_reference_caches, sender=model, dispatch_uid=f"crawler-del-{model}"
            )
