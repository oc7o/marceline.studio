"""site01.example adapter on the KVS template (`crawler/sites/kvs.py`).

- Seeds: `/search/{name}/` per creator name and alias (tag variants mostly return 404); free-text
  search seeds (`search_tasks`, e.g. video titles) use the same `/search/{slug}/` page.
- Listing/search pages: video links (each with its listing thumbnail frame) and pagination.
- Video pages: metadata from JSON-LD `VideoObject` and og/video meta tags, tags and categories from
  the `div.info` block, poster image. No model pages, no performer links.
"""

from crawler.sites.kvs import KvsAdapter, KvsSite

SITE01 = KvsSite(
    domain="site01.example",
    tag_selector='div.info a[href*="/tags/"]',
    category_selector='div.info a[href*="/categories/"]',
)


class Site01Adapter(KvsAdapter):
    def __init__(self) -> None:
        super().__init__(SITE01)
