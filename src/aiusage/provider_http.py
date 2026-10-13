"""Bounded provider JSON transport; credentials never follow redirects or proxies."""
import json
import math
import urllib.request
from urllib.parse import urlsplit

OFFICIAL_HOSTS = frozenset({'chatgpt.com', 'cursor.com', 'www.cursor.com',
    'api2.cursor.sh', 'api.z.ai', 'open.bigmodel.cn'})


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def request_json(url, *, headers=None, body=None, timeout=12, max_bytes=1048576):
    target = urlsplit(url)
    if (target.scheme != 'https' or target.hostname not in OFFICIAL_HOSTS
            or target.username or target.password or target.port not in (None, 443)
            or target.fragment):
        raise ValueError('Unsupported provider endpoint')
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise ValueError('Invalid request deadline')
    if type(max_bytes) is not int or not 0 < max_bytes <= 2 * 1024 * 1024:
        raise ValueError('Invalid response limit')
    data = None if body is None else json.dumps(body, allow_nan=False).encode()
    safe_headers = {'User-Agent': 'Headroom/0.1', 'Accept': 'application/json', **(headers or {})}
    if data is not None:
        safe_headers['Content-Type'] = 'application/json'
    request = urllib.request.Request(url, headers=safe_headers, data=data)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=timeout) as response:
        raw = response.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ValueError('Provider response exceeds limit')
        result = json.loads(raw)
        if not isinstance(result, (dict, list)):
            raise ValueError('Provider response must be an object or array')
        return result
