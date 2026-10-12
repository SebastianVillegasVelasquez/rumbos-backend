"""Where the API lives.

Every route is served under `API_PREFIX`. The prefix is also part of the URLs the
API hands out and stores (an asset is `{ASSETS_PATH}/{id}`, and a map's
`imageUrl` may be exactly that), so it is defined once, here.
"""

API_PREFIX = "/api/v1"
ASSETS_PATH = f"{API_PREFIX}/assets"
