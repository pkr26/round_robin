"""Bounded public HTTPS retrieval with DNS pinning and no environment proxies."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
import hashlib
import http.client
import io
import ipaddress
import re
import socket
import ssl
import threading
import time
from typing import Callable, Protocol
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit, urlunsplit
from xml.sax import SAXException
from xml.sax.handler import ContentHandler

from defusedxml.common import DefusedXmlException
from defusedxml.expatreader import DefusedExpatParser


class FetchRejected(ValueError):
    pass


class FetchUnavailable(RuntimeError):
    pass


SECRET_KEYS = re.compile(r"(?:^|[_-])(?:key|token|auth|secret|password|credential|signature|sig|session)(?:$|[_-])", re.I)
SECRET_COMPACT_KEYS = {"apikey", "accesskey", "privatekey", "authorization", "authtoken", "accesstoken", "clientsecret", "clienttoken", "sessionid", "sessiontoken", "password", "signature", "token", "secret", "key", "session", "credential", "credentials"}
_DNS_CAPACITY = threading.BoundedSemaphore(2)


@dataclass(frozen=True)
class Target:
    url: str
    hostname: str
    address: str
    request_target: str


def public_addresses(hostname: str, *, timeout: float = 5) -> list[str]:
    # Native resolver calls cannot be interrupted. Bound their caller time and
    # outstanding daemon threads so slow DNS cannot spawn unbounded work.
    deadline = time.monotonic() + timeout
    if not 0 < timeout <= 30 or not _DNS_CAPACITY.acquire(timeout=timeout):
        raise FetchUnavailable('Public DNS capacity or deadline unavailable')
    finished, outcome = threading.Event(), []
    def resolve():
        try:
            outcome.append(list({item[4][0] for item in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)}))
        except BaseException as error:
            outcome.append(error)
        finally:
            _DNS_CAPACITY.release()
            finished.set()
    try:
        threading.Thread(target=resolve, daemon=True, name='round-robin-public-dns').start()
    except (RuntimeError, OSError):
        _DNS_CAPACITY.release()
        raise FetchUnavailable('Public DNS worker unavailable') from None
    if not finished.wait(max(0, deadline - time.monotonic())):
        raise FetchUnavailable('Public DNS total deadline exceeded')
    if isinstance(outcome[0], BaseException):
        raise FetchUnavailable('Public DNS resolution failed')
    return outcome[0]


def validate_target(url: str, resolver: Callable[[str], list[str]] = public_addresses, *, resolution_timeout: float = 5) -> Target:
    if not isinstance(url, str) or len(url) > 2048 or any(ord(char) < 33 or ord(char) == 127 for char in url) or "\\" in url:
        raise FetchRejected("Unsupported public URL")
    try:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or parts.username is not None or parts.password is not None or parts.port not in {None, 443}:
            raise FetchRejected("Public evidence requires credential-free HTTPS on port 443")
        hostname = parts.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except (ValueError, UnicodeError) as exc:
        raise FetchRejected("Invalid public URL") from exc
    if hostname in {"localhost", "metadata.google.internal"} or hostname.endswith((".localhost", ".local", ".internal")):
        raise FetchRejected("Private destinations are forbidden")
    decoded = url
    # Check the fully decoded spelling. A fixed number of decoding passes lets
    # nested percent escapes hide credential keys from the public URL filter.
    # Reject excessive nesting instead of sending a partially checked URL.
    for _ in range(16):
        expanded = unquote(decoded)
        if expanded == decoded:
            break
        decoded = expanded
        if any(ord(char) < 32 or ord(char) == 127 for char in decoded):
            raise FetchRejected("URL contains encoded control characters")
    else:
        raise FetchRejected("URL contains excessively nested percent encoding")
    # Decode query content without reparsing it as a URL. An encoded '#'
    # belongs to the query sent upstream; reparsing would hide later secret
    # keys inside a newly invented fragment.
    decoded_query = parts.query
    for _ in range(16):
        expanded_query = unquote(decoded_query)
        if expanded_query == decoded_query:
            break
        decoded_query = expanded_query
    for key, value in parse_qsl(decoded_query, keep_blank_values=True):
        compact = re.sub(r'[^a-z0-9]', '', key.lower())
        camel_split = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', key)
        if compact in SECRET_COMPACT_KEYS or SECRET_KEYS.search(camel_split) or key.lower().startswith(("x-amz-", "x-goog-")):
            raise FetchRejected("Secret-bearing URLs cannot become public evidence")
    if re.search(r"(?:sk-|AIza|Bearer%20|Bearer )[A-Za-z0-9_-]{12,}", decoded, re.I):
        raise FetchRejected("Credential-like URL content is forbidden")
    try:
        addresses = public_addresses(hostname, timeout=resolution_timeout) if resolver is public_addresses else resolver(hostname)
        if not addresses:
            raise FetchUnavailable("Public destination did not resolve")
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified or getattr(ip, "ipv4_mapped", None):
                raise FetchRejected("Every resolved address must be a public unicast destination")
    except FetchRejected:
        raise
    except (ValueError, socket.gaierror) as exc:
        raise FetchUnavailable("Public DNS resolution failed") from exc
    authority = f'[{hostname}]' if ':' in hostname else hostname
    canonical = urlunsplit(("https", authority, parts.path or "/", parts.query, ""))
    return Target(canonical, hostname, sorted(addresses)[0], urlunsplit(("", "", parts.path or "/", parts.query, "")))


@dataclass(frozen=True)
class FetchResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class PublicTransport(Protocol):
    def get(self, target: Target, *, timeout: float, byte_limit: int) -> FetchResponse: ...


class DeadlineReader(io.RawIOBase):
    def __init__(self, sock, deadline: float, on_close=None):
        self.sock, self.deadline, self.on_close = sock, deadline, on_close

    def readable(self):
        return True

    def readinto(self, buffer):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Public fetch exceeded its total deadline")
        self.sock.settimeout(remaining)
        return self.sock.recv_into(buffer)

    def close(self):
        if not self.closed:
            super().close()
            if self.on_close:
                self.on_close()


class DeadlineSocket:
    def __init__(self, sock, deadline: float):
        self.sock, self.deadline = sock, deadline
        self.file_references = 0
        self.closing = False

    def sendall(self, value):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Public fetch exceeded its total deadline")
        self.sock.settimeout(remaining)
        return self.sock.sendall(value)

    def makefile(self, mode, *args, **kwargs):
        if mode != "rb":
            raise FetchRejected("Unsupported public transport stream")
        self.file_references += 1
        return io.BufferedReader(DeadlineReader(self.sock, self.deadline, self._release_file))

    def _release_file(self):
        self.file_references -= 1
        if self.closing and self.file_references == 0:
            self.sock.close()

    def close(self):
        self.closing = True
        if self.file_references == 0:
            self.sock.close()


class SocketPublicTransport:
    """Connect to the validated IP while preserving original Host and TLS SNI."""

    def get(self, target: Target, *, timeout: float, byte_limit: int, private_headers: dict[str, str] | None = None) -> FetchResponse:
        deadline = time.monotonic() + timeout
        connection = http.client.HTTPConnection(target.hostname, 443, timeout=timeout)
        try:
            raw = socket.create_connection((target.address, 443), timeout=timeout)
            try:
                raw.settimeout(max(.001, deadline - time.monotonic()))
                connection.sock = DeadlineSocket(ssl.create_default_context().wrap_socket(raw, server_hostname=target.hostname), deadline)
            except BaseException:
                raw.close()
                raise
            safe_private_headers = {key: value for key, value in (private_headers or {}).items() if key.lower() != "host"}
            connection.request("GET", target.request_target, headers={"User-Agent": "RoundRobin/0.1 evidence-fetcher", "Accept": "text/plain,text/html,application/xhtml+xml", "Accept-Encoding": "identity", **safe_private_headers, "Host": f'[{target.hostname}]' if ':' in target.hostname else target.hostname})
            response = connection.getresponse()
            headers = {key.lower(): value for key, value in response.getheaders()}
            if headers.get("content-length") and int(headers["content-length"]) > byte_limit:
                raise FetchRejected("Public response exceeds the byte limit")
            body = response.read(byte_limit + 1)
            if len(body) > byte_limit:
                raise FetchRejected("Public response exceeds the byte limit")
            return FetchResponse(response.status, headers, body)
        except FetchRejected:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException, ValueError) as exc:
            raise FetchUnavailable("Public fetch failed; source remains unverified") from exc
        finally:
            connection.close()


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self.ignored += 1
        elif tag in {"p", "div", "br", "li", "h1", "h2", "h3", "tr"} and not self.ignored:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"}:
            self.ignored = max(0, self.ignored - 1)

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)


class XHTMLTextExtractor(ContentHandler):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.depth = 0
        self.nodes = 0
        self.ignored = 0

    def startElementNS(self, name, qname, attrs):
        self.depth += 1
        self.nodes += 1
        if self.depth > 256 or self.nodes > 50000:
            raise FetchRejected("Public XHTML exceeds its structural limits")
        namespace, tag = name
        is_xhtml = namespace in {None, "http://www.w3.org/1999/xhtml"}
        is_ignored = namespace == "http://www.w3.org/2000/svg" or (
            is_xhtml and tag in {"script", "style", "noscript", "svg", "template"}
        )
        if self.ignored or is_ignored:
            self.ignored += 1
        elif is_xhtml and tag in {"p", "div", "br", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")

    def endElementNS(self, name, qname):
        if self.ignored:
            self.ignored -= 1
        self.depth -= 1

    def characters(self, content):
        if not self.ignored:
            self.parts.append(content)


@dataclass(frozen=True)
class RetrievedSource:
    url: str
    text: str
    content_hash: str
    fetched_at: str
    origin_group: str
    status: str = "not_checked"


@dataclass
class FetchAllowance:
    pages: int = 0
    queries: int = 0
    max_pages: int = 8
    max_queries: int = 6
    byte_limit: int = 1_048_576
    registered_urls: dict[str, str] = field(default_factory=dict)


def collect_source(candidate_id: str, allowance: FetchAllowance, *, transport: PublicTransport | None = None, resolver=public_addresses, timeout: float = 10) -> RetrievedSource:
    if candidate_id not in allowance.registered_urls:
        raise FetchRejected("Only server-registered approved/discovered URL identities may be fetched")
    if allowance.pages >= allowance.max_pages or not 0 < timeout <= 30:
        raise FetchRejected("Public retrieval allowance is exhausted")
    allowance.pages += 1  # failed attempts also consume the bounded transport allowance
    deadline = time.monotonic() + timeout
    url = allowance.registered_urls[candidate_id]
    client = transport or SocketPublicTransport()
    for redirect in range(4):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchUnavailable('Source redirect deadline exhausted')
        target = validate_target(url, resolver, resolution_timeout=min(30, remaining))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchUnavailable('Source DNS deadline exhausted')
        response = client.get(target, timeout=remaining, byte_limit=allowance.byte_limit)
        if response.status in {301, 302, 303, 307, 308}:
            if redirect == 3 or not response.headers.get("location"):
                raise FetchRejected("Redirect allowance exhausted")
            url = urljoin(target.url, response.headers["location"])
            continue
        if response.status != 200:
            raise FetchUnavailable("Public source returned an unavailable status")
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type not in {"text/plain", "text/html", "application/xhtml+xml"} or response.headers.get("content-encoding", "identity") != "identity":
            raise FetchRejected("Public evidence supports bounded uncompressed text only")
        if len(response.body) > allowance.byte_limit:
            raise FetchRejected("Public response exceeds its byte allowance")
        try:
            decoded = response.body.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise FetchRejected("Source is not UTF-8 text") from exc
        if "\x00" in decoded:
            raise FetchRejected("Source text contains unsupported NUL characters")
        if content_type == "text/html":
            parser = TextExtractor()
            parser.feed(decoded)
            if "<" not in decoded:
                parser.close()
            decoded = "".join(parser.parts)
        elif content_type == "application/xhtml+xml":
            handler = XHTMLTextExtractor()
            parser = DefusedExpatParser(namespaceHandling=True, forbid_dtd=True, forbid_entities=True, forbid_external=True)
            parser.setContentHandler(handler)
            try:
                parser.feed(decoded)
                parser.close()
            except (SAXException, DefusedXmlException):
                raise FetchRejected("Public XHTML is malformed or unsupported") from None
            decoded = "".join(handler.parts)
        text = "\n".join(line.strip() for line in decoded.replace("\r\n", "\n").splitlines() if line.strip())
        if not text:
            raise FetchUnavailable("Public source contains no usable text")
        group = "origin_" + hashlib.sha256(target.hostname.encode()).hexdigest()
        return RetrievedSource(target.url, text, hashlib.sha256(text.encode()).hexdigest(), datetime.now(timezone.utc).isoformat(), group)
    raise FetchRejected("Redirect limit exceeded")


def exact_passages(source: RetrievedSource, *, maximum: int = 24, size: int = 2000, terms=()) -> list[dict]:
    if not 1 <= maximum <= 24 or not 1 <= size <= 2000:
        raise FetchRejected("Passage packet exceeds supported bounds")
    # Local relevance and exception coverage never send private requirements to
    # the search provider. Preserve exact canonical offsets and bytes; ranking
    # is a heuristic for inspection, not evidence that a proposition is true.
    stopwords = {"the", "and", "for", "with", "this", "that", "must", "should", "from", "have", "into", "will"}
    words = set(re.findall(r"[\w-]{3,}", " ".join(str(term) for term in terms).lower())) - stopwords
    exception = re.compile(r"\b(?:exclud\w*|excep\w*|unless|limitation\w*|restriction\w*|additional\s+(?:fee|charge)|not\s+(?:included|covered|available)|non.?refundable)\b", re.I)
    stride = max(1, size - min(200, size // 5))
    total_windows = max(1, (len(source.text) + stride - 1) // stride)
    sampling = max(1, (total_windows + 2047) // 2048)
    complete_packet = len(source.text) <= size * maximum
    starts = set(range(0, len(source.text), size)) if complete_packet else set(range(0, len(source.text), stride * sampling))
    if not complete_packet:
        starts.add(max(0, len(source.text) - size))
    # Anchor windows at exceptions so a late qualification is not discarded
    # merely because it follows a long navigation or introduction section.
    for match in ([] if complete_packet else list(exception.finditer(source.text))[:512]):
        starts.add(max(0, min(match.start() - size // 3, len(source.text) - size)))
    candidates = []
    for start in sorted(starts):
        text = source.text[start:start + size]
        tokens = set(re.findall(r"[\w-]{3,}", text.lower()))
        matched = words & tokens
        score = len(matched) * 3 + min(3, len(exception.findall(text))) * 5
        candidates.append((score, start, text, matched))
    selected, covered = [], set()
    while candidates and len(selected) < maximum:
        best = max(candidates, key=lambda item: (item[0] + 3 * len(item[3] - covered), -item[1]))
        selected.append(best)
        covered |= best[3]
        begin, end = best[1], best[1] + len(best[2])
        # Do not spend scarce slots on largely duplicated overlapping windows.
        candidates = [item for item in candidates if item is not best and max(0, min(end, item[1] + len(item[2])) - max(begin, item[1])) < min(len(best[2]), len(item[2])) / 2]
    selected.sort(key=lambda item: item[1])
    intervals = sorted((start, start + len(text)) for _, start, text, _ in selected)
    inspected, previous_end = 0, 0
    for begin, end in intervals:
        inspected += max(0, end - max(begin, previous_end))
        previous_end = max(previous_end, end)
    return [{"text": text, "locator": f"extracted_text[{start}:{start + len(text)}]", "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "scope": "Selected exact extracted passage; relevance is heuristic, semantic support remains unchecked and omitted text is not verified.",
        "verification_status": "not_checked", "source_content_hash": source.content_hash, "source_text_length": len(source.text),
        "selected_start": start, "selected_end": start + len(text), "selection_method": "bounded_requirement_exception_coverage_v1",
        "uninspected_characters": max(0, len(source.text) - inspected)} for _, start, text, _ in selected]
