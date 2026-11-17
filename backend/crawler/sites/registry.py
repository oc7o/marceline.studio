"""Domain -> site adapter. Register new adapters in ADAPTERS."""

from crawler.sites.base import SiteAdapter
from crawler.sites.site13 import Site13Adapter
from crawler.sites.site14 import Site14Adapter
from crawler.sites.site05 import Site05Adapter
from crawler.sites.site11 import Site11Adapter
from crawler.sites.site04 import Site04Adapter
from crawler.sites.site09 import Site09Adapter
from crawler.sites.site02 import Site02Adapter
from crawler.sites.kvs import KVS_SITES, KvsAdapter
from crawler.sites.site07 import Site07Adapter
from crawler.sites.site01 import Site01Adapter
from crawler.sites.site12 import Site12Adapter
from crawler.sites.site10 import Site10Adapter
from crawler.sites.site03 import Site03Adapter
from crawler.sites.site08 import Site08Adapter
from crawler.sites.site06 import Site06Adapter

_ADAPTERS: list[SiteAdapter] = [
    Site01Adapter(),
    Site02Adapter(),
    Site03Adapter(),
    Site04Adapter(),
    Site05Adapter(),
    Site06Adapter(),
    Site07Adapter(),
    Site08Adapter(),
    Site09Adapter(),
    Site10Adapter(),
    Site11Adapter(),
    Site12Adapter(),
    Site13Adapter(),
    Site14Adapter(),
    # KVS template instances, one per entry in KVS_SITES (crawler/sites/kvs.py).
    *(KvsAdapter(site) for site in KVS_SITES),
]
ADAPTERS: dict[str, SiteAdapter] = {adapter.domain: adapter for adapter in _ADAPTERS}


def get_adapter(domain: str) -> SiteAdapter | None:
    return ADAPTERS.get(domain.lower().removeprefix("www."))
