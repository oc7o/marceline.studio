"""site08.example adapter on the KVS template (`crawler/sites/kvs.py`).

- Seeds: `/search/{slug}/` per creator name and alias (the search form's `data-url` is
  `https://site08.example/search/%QUERY%/`); free-text search seeds use the same page. Model pages
  exist but their slug carries an unpredictable suffix, so there are no model seeds.
- Listing and search pages: cards `[id^="list_videos_"] div.item a[href]` link
  `/videos/{id}/{slug}/` with one timeline frame (`…/{id}/320x180/{n}.jpg`); lazy cards keep it in
  `data-original` behind a base64 placeholder `src`, and only `/contents/videos_screenshots/`
  images count. Plain and AJAX (`?from_videos=N`) pagination.
- Video pages: JSON-LD `VideoObject` and og/video meta (the JSON-LD `duration` is malformed,
  `video:duration` wins); categories, tags and models from `div.info` (models also kept in
  `extra["models"]`); the poster plus the numbered timeline frames of the screenshots tab
  (`div.block-screenshots img`), never their tokenized `/get_file/` links.
"""

from crawler.sites.kvs import SITE08, KvsAdapter


class Site08Adapter(KvsAdapter):
    def __init__(self) -> None:
        super().__init__(SITE08)
