"""One-request HTTP transport with an absolute deadline through DNS and TLS.

Inactivity timeouts alone permit a server to drip headers indefinitely. Every
socket operation here receives only the remaining request allowance. DNS calls
are bounded at the caller and in outstanding count because libc resolution is
not interruptible. The transport performs no retries or environment proxy use.
"""

from contextlib import contextmanager
import socket
import threading
import time

import httpcore
import httpx


_DNS_CAPACITY = threading.BoundedSemaphore(3)


def _remaining(deadline, timeout=None):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise httpcore.ReadTimeout("Provider total deadline exceeded")
    return min(remaining, timeout) if timeout is not None else remaining


def _addresses(host, port, deadline):
    if not _DNS_CAPACITY.acquire(timeout=_remaining(deadline)):
        raise httpcore.ConnectTimeout("Provider DNS capacity unavailable")
    finished, outcome = threading.Event(), []

    def resolve():
        try:
            outcome.append(list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))))
        except BaseException as error:
            outcome.append(error)
        finally:
            _DNS_CAPACITY.release()
            finished.set()

    try:
        threading.Thread(target=resolve, daemon=True, name="round-robin-provider-dns").start()
    except RuntimeError as exc:
        _DNS_CAPACITY.release()
        raise httpcore.ConnectError("Provider DNS worker unavailable") from exc
    if not finished.wait(_remaining(deadline)):
        raise httpcore.ConnectTimeout("Provider DNS total deadline exceeded")
    if isinstance(outcome[0], BaseException) or not outcome[0]:
        raise httpcore.ConnectError("Provider DNS resolution unavailable")
    return outcome[0]


class _DeadlineStream(httpcore.NetworkStream):
    def __init__(self, stream, deadline):
        self.stream, self.deadline = stream, deadline

    def read(self, max_bytes, timeout=None):
        return self.stream.read(max_bytes, timeout=_remaining(self.deadline, timeout))

    def write(self, buffer, timeout=None):
        # sendall applies its timeout to the entire write, including partial
        # writes, whereas resetting a timeout in a send loop renews allowance.
        sock = self.stream.get_extra_info("socket")
        try:
            sock.settimeout(_remaining(self.deadline, timeout))
            sock.sendall(buffer)
        except socket.timeout as exc:
            raise httpcore.WriteTimeout("Provider write deadline exceeded") from exc
        except OSError as exc:
            raise httpcore.WriteError("Provider transport write unavailable") from exc

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        stream = self.stream.start_tls(ssl_context, server_hostname, timeout=_remaining(self.deadline, timeout))
        return _DeadlineStream(stream, self.deadline)

    def get_extra_info(self, info):
        return self.stream.get_extra_info(info)

    def close(self):
        self.stream.close()


class _DeadlineBackend(httpcore.NetworkBackend):
    def __init__(self, deadline):
        self.deadline = deadline
        self.backend = httpcore.SyncBackend()

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        addresses = _addresses(host, port, self.deadline)
        failure = None
        for address in addresses:
            try:
                stream = self.backend.connect_tcp(address, port, timeout=_remaining(self.deadline, timeout), local_address=local_address, socket_options=socket_options)
                return _DeadlineStream(stream, self.deadline)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                failure = exc
        raise failure


@contextmanager
def _httpx_errors():
    try:
        yield
    except httpcore.TimeoutException as exc:
        raise httpx.TimeoutException("Provider total deadline exceeded") from exc
    except httpcore.ConnectError as exc:
        raise httpx.ConnectError("Provider connection unavailable") from exc
    except httpcore.NetworkError as exc:
        raise httpx.NetworkError("Provider transport unavailable") from exc
    except httpcore.ProtocolError as exc:
        raise httpx.ProtocolError("Provider transport protocol invalid") from exc
    except TimeoutError as exc:
        raise httpx.TimeoutException("Provider total deadline exceeded") from exc
    except OSError as exc:
        raise httpx.NetworkError("Provider transport unavailable") from exc


def _close_with_httpx_errors(resource):
    with _httpx_errors():
        resource.close()


@contextmanager
def preserve_primary_http_error(manager):
    # Track exceptions from this exact body, rather than unrelated exceptions
    # the caller may currently be handling in an outer context.
    primary = None
    try:
        with manager as value:
            try:
                yield value
            except BaseException as exc:
                primary = exc
                raise
    except (httpx.HTTPError, OSError) as cleanup:
        if primary is None or cleanup is primary:
            raise
        raise primary.with_traceback(primary.__traceback__) from None


class _ResponseStream(httpx.SyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    def __iter__(self):
        with _httpx_errors():
            yield from self.stream

    def close(self):
        _close_with_httpx_errors(self.stream)


class DeadlineHTTPTransport(httpx.BaseTransport):
    def __init__(self, deadline, *, ssl_context=None):
        self.pool = httpcore.ConnectionPool(ssl_context=ssl_context, max_connections=1, max_keepalive_connections=0, retries=0, network_backend=_DeadlineBackend(deadline))

    def handle_request(self, request):
        core_request = httpcore.Request(method=request.method, url=httpcore.URL(scheme=request.url.raw_scheme, host=request.url.raw_host, port=request.url.port, target=request.url.raw_path), headers=request.headers.raw, content=request.stream, extensions=request.extensions)
        with _httpx_errors():
            response = self.pool.handle_request(core_request)
        return httpx.Response(response.status, headers=response.headers, stream=_ResponseStream(response.stream), extensions=response.extensions)

    def close(self):
        _close_with_httpx_errors(self.pool)
