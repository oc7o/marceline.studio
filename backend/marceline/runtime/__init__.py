"""The crawl runtime (ADR 0030, issue #75): one process per lane serving every site.

`crawl_runtime --lane pages` crawls, `--lane media` fetches and hashes media, each with a few
threads over a fair rotation of sites, database host slots (`core.host_slots`) for politeness and,
for media, PDQ hashing in child processes. Lives in the project package because it drives
`crawler.worker` and `matching.media_worker` together, a combination no app may import.

Keep this package's `__init__` free of imports: the hash children import `hash_pool` without Django.
"""
