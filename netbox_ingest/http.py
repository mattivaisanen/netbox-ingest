"""
Calling a source's API: authentication, limits, retries and paging.

Uses "requests" (installed with NetBox) and the standard library's thread
pool. Every limit a source sets is capped by the administrator's settings:
requests per second, parallel requests, timeout, response size and pages.
Secrets never appear in logs or reports: URLs are shown with query values
of the API key parameter replaced by "***".
"""
import fnmatch
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from . import jsonpath, parsers
from .choices import AuthTypeChoices, HttpMethodChoices, PaginationChoices
from .secrets import get_secret
from .settings import get_setting

USER_AGENT = 'netbox-ingest'


class FetchError(Exception):
    """
    A request failed for good (after retries). status is the HTTP status
    when the server answered, and token is True when the failing request
    was the OAuth2 token request.
    """

    def __init__(self, message, status=None, token=False):
        super().__init__(message)
        self.status = status
        self.token = token


def _capped(value, limit, default=None):
    if value in (None, ''):
        return limit if default is None else min(default, limit)
    return min(value, limit)


class RateLimiter:
    """Spaces request starts at least 1/rate seconds apart, across threads."""

    def __init__(self, rate):
        self.interval = 1.0 / rate if rate else 0
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self):
        if not self.interval:
            return
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next_at)
            self.next_at = start + self.interval
        if start > now:
            time.sleep(start - now)


class Client:
    """One run's connection to one source."""

    def __init__(self, source, secret=None):
        """
        secret replaces the source's stored secret, for testing a secret
        typed into the form before it is saved.
        """
        import requests

        self.source = source
        self.session = requests.Session()
        self.session.headers['User-Agent'] = USER_AGENT
        self.session.headers['Accept'] = 'application/json'
        for name, value in (source.headers or {}).items():
            self.session.headers[str(name)] = str(value)
        self.session.verify = source.verify_tls
        self.timeout = _capped(source.timeout, get_setting('request_timeout'))
        self.rate = RateLimiter(_capped(source.requests_per_second, get_setting('max_requests_per_second')))
        self.concurrency = max(1, int(_capped(source.concurrency, get_setting('max_concurrency'))))
        self.max_bytes = int(get_setting('max_response_mb')) * 1024 * 1024
        self.max_retries = int(get_setting('max_retries'))
        self.max_pages = int(get_setting('max_pages'))
        self.allowed_hosts = list(get_setting('allowed_hosts') or [])
        if source.auth_type == AuthTypeChoices.NONE:
            self.secret = ''
        else:
            self.secret = secret if secret is not None else get_secret(source)
        self.token = None
        self.stats = {'requests': 0, 'bytes': 0, 'pages': 0, 'retries': 0, 'errors': 0}
        self.stats_lock = threading.Lock()
        self._apply_auth()

    # Authentication

    def _apply_auth(self):
        source, kind = self.source, self.source.auth_type
        if kind == AuthTypeChoices.BASIC:
            self.session.auth = (source.auth_username, self.secret)
        elif kind == AuthTypeChoices.BEARER:
            self.session.headers['Authorization'] = f'Bearer {self.secret}'
        elif kind == AuthTypeChoices.HEADER:
            self.session.headers[source.auth_name] = self.secret
        elif kind == AuthTypeChoices.QUERY:
            self.session.params = {source.auth_name: self.secret}

    def _oauth_token(self, refresh=False):
        """Fetches (or reuses) an OAuth2 client-credentials token."""
        if self.token and not refresh:
            return self.token
        data = {'grant_type': 'client_credentials', 'client_id': self.source.auth_username,
                'client_secret': self.secret}
        if self.source.oauth_scope:
            data['scope'] = self.source.oauth_scope
        self.check_host(self.source.token_url)
        response = self.session.post(self.source.token_url, data=data, timeout=self.timeout)
        self._count(len(response.content))
        if response.status_code >= 400:
            raise FetchError(f'Token request failed: HTTP {response.status_code}',
                             status=response.status_code, token=True)
        try:
            self.token = response.json()['access_token']
        except (ValueError, KeyError) as exc:
            raise FetchError('The token response has no access_token.', token=True) from exc
        return self.token

    # Helpers

    def redact(self, url):
        """The URL with the API key query value hidden."""
        if self.source.auth_type != AuthTypeChoices.QUERY:
            return url
        parts = urlsplit(url)
        query = [(k, '***' if k == self.source.auth_name else v) for k, v in parse_qsl(parts.query, True)]
        return urlunsplit(parts._replace(query=urlencode(query)))

    def check_host(self, url):
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https'):
            raise FetchError(f'Only http and https URLs are allowed: {self.redact(url)}')
        host = (parts.hostname or '').lower()
        if self.allowed_hosts and not any(fnmatch.fnmatch(host, p.lower()) for p in self.allowed_hosts):
            raise FetchError(f'The host {host} is not in the administrator\'s allowed_hosts.')

    def _count(self, size, retry=False, error=False):
        with self.stats_lock:
            self.stats['requests'] += 1
            self.stats['bytes'] += size
            self.stats['retries'] += int(retry)
            self.stats['errors'] += int(error)

    # Requests

    def request(self, url, params=None, body=None):
        """
        One request: returns (parsed data, response). Retries 429 and 5xx
        (honouring Retry-After up to a minute), refreshes an OAuth2 token
        once on 401. Raises FetchError.
        """
        import requests

        self.check_host(url)
        method = self.source.method
        attempt, refreshed = 0, False
        while True:
            self.rate.wait()
            headers = {}
            if self.source.auth_type == AuthTypeChoices.OAUTH2:
                headers['Authorization'] = f'Bearer {self._oauth_token()}'
            if body is not None:
                headers['Content-Type'] = 'application/json'
            try:
                response = self.session.request(
                    method, url, params=params, data=body.encode() if body is not None else None,
                    headers=headers, timeout=self.timeout, stream=True, allow_redirects=True,
                )
                content = self._read(response)
            except requests.RequestException as exc:
                self._count(0, error=True)
                if attempt < self.max_retries:
                    attempt += 1
                    time.sleep(min(2 ** attempt, 30))
                    continue
                raise FetchError(f'{method} {self.redact(url)}: {exc.__class__.__name__}: {exc}') from None
            status = response.status_code
            retry = status == 429 or status >= 500
            self._count(len(content), retry=retry and attempt < self.max_retries, error=status >= 400)
            if status == 401 and self.source.auth_type == AuthTypeChoices.OAUTH2 and not refreshed:
                self._oauth_token(refresh=True)
                refreshed = True
                continue
            if retry and attempt < self.max_retries:
                attempt += 1
                time.sleep(self._retry_after(response, attempt))
                continue
            if status >= 400:
                snippet = content[:200].decode(errors='replace').strip()
                raise FetchError(f'{method} {self.redact(response.url)}: HTTP {status} {snippet}'.strip(),
                                 status=status)
            try:
                data = parsers.parse(self.source.format, content, response.encoding)
            except parsers.ParseError as exc:
                raise FetchError(f'{self.redact(response.url)}: {exc}', status=status) from None
            response.size = len(content)
            return data, response

    @staticmethod
    def _retry_after(response, attempt):
        value = response.headers.get('Retry-After', '')
        if value.isdigit():
            return min(int(value), 60)
        return min(2 ** attempt, 30)

    def _read(self, response):
        """The body, refusing anything larger than max_response_mb."""
        chunks, size = [], 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > self.max_bytes:
                response.close()
                raise FetchError(f'The response is larger than {self.max_bytes // (1024 * 1024)} MB '
                                 f'(max_response_mb).')
            chunks.append(chunk)
        return b''.join(chunks)

    # Records and paging

    @staticmethod
    def records_of(data, path):
        """The list of records at path; a single object counts as one record."""
        found = jsonpath.get(data, path, default=None) if path else data
        if found is None:
            return []
        return found if isinstance(found, list) else [found]

    def fetch_all(self, url, records_path='', body=None, log=None):
        """
        Every record from url, following the source's pagination up to
        max_pages. log(level, message) is told when paging stops early.
        """
        source = self.source
        kind = source.pagination
        params, records = {}, []
        page = 1 if kind == PaginationChoices.PAGE else 0
        if kind in (PaginationChoices.PAGE, PaginationChoices.OFFSET):
            params[source.page_param] = page
            if source.size_param and source.page_size:
                params[source.size_param] = source.page_size
        for _number in range(self.max_pages):
            data, response = self.request(url, params=params or None, body=body)
            batch = self.records_of(data, records_path)
            records.extend(batch)
            with self.stats_lock:
                self.stats['pages'] += 1
            if kind == PaginationChoices.NEXT_URL:
                next_url = jsonpath.get(data, source.next_path, default=None)
                if not next_url or not isinstance(next_url, str):
                    return records
                url, params = urljoin(response.url, next_url), {}
            elif kind == PaginationChoices.LINK_HEADER:
                next_url = response.links.get('next', {}).get('url')
                if not next_url:
                    return records
                url, params = urljoin(response.url, next_url), {}
            elif kind in (PaginationChoices.PAGE, PaginationChoices.OFFSET):
                if not batch or (source.page_size and len(batch) < source.page_size):
                    return records
                page += 1 if kind == PaginationChoices.PAGE else len(batch)
                params[source.page_param] = page
            else:
                return records
        if log:
            log('warning', f'Stopped after {self.max_pages} pages (max_pages); later records were not read.')
        return records

    def fetch_many(self, requests_):
        """
        Runs [(key, url, body)] in parallel (up to the concurrency limit).
        Returns {key: (data, None)} or {key: (None, error message)}.
        """
        def one(item):
            key, url, body = item
            try:
                data, _response = self.request(url, body=body)
                return key, (data, None)
            except FetchError as exc:
                return key, (None, str(exc))

        if self.concurrency == 1 or len(requests_) < 2:
            return dict(one(item) for item in requests_)
        with ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix='netbox-ingest') as pool:
            return dict(pool.map(one, requests_))

    def close(self):
        self.session.close()


def render_template(text, values, for_json=False):
    """
    Fills {key}, {lookup} and {pk} in a URL (URL-encoded) or JSON body
    (JSON-escaped). Other braces are left as they are.
    """
    from urllib.parse import quote

    for name, value in values.items():
        value = '' if value is None else str(value)
        value = json.dumps(value)[1:-1] if for_json else quote(value, safe='')
        text = text.replace('{' + name + '}', value)
    return text


def uses_placeholder(text, name):
    return '{' + name + '}' in (text or '')


def body_for(source):
    """The request body template, or None for GET."""
    if source.method == HttpMethodChoices.POST:
        return source.body or ''
    return None
