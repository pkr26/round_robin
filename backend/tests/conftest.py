"""Fail-closed offline test profile; real-provider checks are not implemented.

Production credentials cause collection to stop before fixtures can erase them.
Tests may inject synthetic sentinel keys to exercise rejection paths. IP sockets
deny all destinations by default. Explicit ROUND_ROBIN_TEST_ALLOW_LOOPBACK=1
permits only loopback disposable services, including a post-resolution IP check.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from typing import Any

import pytest


PROVIDER_CREDENTIAL_NAMES = (
    "OPENAI_API_KEY",
    "OPENAI_ADMIN_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "AZURE_OPENAI_API_KEY",
    "BRAVE_SEARCH_API_KEY",
)


class OfflineNetworkBlocked(RuntimeError):
    pass


_SESSION_BOUNDARY = pytest.MonkeyPatch()


def pytest_sessionstart(session: pytest.Session) -> None:
    if any(os.environ.get(name) for name in PROVIDER_CREDENTIAL_NAMES):
        raise pytest.UsageError("Offline test profile refuses production provider credentials. Remove credentials before running tests; values are never printed.")
    # Install before collection: a test-module import must not perform network I/O.
    _install_network_boundary(_SESSION_BOUNDARY)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    _SESSION_BOUNDARY.undo()


def _loopback_host(host: Any) -> bool:
    if isinstance(host, bytes):
        try:
            host = host.decode("ascii")
        except UnicodeDecodeError:
            return False
    if host in {"localhost", "localhost."}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except (ValueError, TypeError):
        return False


def _check_host(host: Any) -> None:
    if os.environ.get("ROUND_ROBIN_TEST_ALLOW_LOOPBACK") != "1" or not _loopback_host(host):
        raise OfflineNetworkBlocked("offline test network boundary denied destination")


def _check_address(sock: socket.socket, address: Any) -> None:
    if sock.family in {socket.AF_INET, socket.AF_INET6}:
        if not isinstance(address, tuple) or not address:
            raise OfflineNetworkBlocked("offline test network boundary denied invalid destination")
        _check_host(address[0])
    elif sock.family != socket.AF_UNIX:
        raise OfflineNetworkBlocked("offline test network boundary denied socket family")


def _install_network_boundary(monkeypatch: pytest.MonkeyPatch):
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex
    sendto = socket.socket.sendto
    getaddrinfo = socket.getaddrinfo
    create_connection = socket.create_connection
    gethostbyname = socket.gethostbyname
    gethostbyname_ex = socket.gethostbyname_ex
    gethostbyaddr = socket.gethostbyaddr
    getnameinfo = socket.getnameinfo

    def checked_connect(sock, address):
        _check_address(sock, address)
        return connect(sock, address)

    def checked_connect_ex(sock, address):
        _check_address(sock, address)
        return connect_ex(sock, address)

    def checked_sendto(sock, data, *args):
        _check_address(sock, args[-1] if args else None)
        return sendto(sock, data, *args)

    def checked_getaddrinfo(host, *args, **kwargs):
        _check_host(host)
        results = getaddrinfo(host, *args, **kwargs)
        if any(not _loopback_host(result[4][0]) for result in results):
            raise OfflineNetworkBlocked("offline test network boundary denied resolved destination")
        return results

    def checked_create_connection(address, *args, **kwargs):
        _check_host(address[0])
        return create_connection(address, *args, **kwargs)

    def checked_gethostbyname(host):
        _check_host(host)
        result = gethostbyname(host)
        if not _loopback_host(result):
            raise OfflineNetworkBlocked("offline test network boundary denied resolved destination")
        return result

    def checked_gethostbyname_ex(host):
        _check_host(host)
        result = gethostbyname_ex(host)
        if any(not _loopback_host(address) for address in result[2]):
            raise OfflineNetworkBlocked("offline test network boundary denied resolved destination")
        return result

    def checked_gethostbyaddr(host):
        _check_host(host)
        return gethostbyaddr(host)

    def checked_getnameinfo(address, flags):
        _check_host(address[0])
        return getnameinfo(address, flags)

    monkeypatch.setattr(socket.socket, "connect", checked_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", checked_connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", checked_sendto)
    monkeypatch.setattr(socket, "getaddrinfo", checked_getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", checked_create_connection)
    monkeypatch.setattr(socket, "gethostbyname", checked_gethostbyname)
    monkeypatch.setattr(socket, "gethostbyname_ex", checked_gethostbyname_ex)
    monkeypatch.setattr(socket, "gethostbyaddr", checked_gethostbyaddr)
    monkeypatch.setattr(socket, "getnameinfo", checked_getnameinfo)
    if hasattr(socket.socket, "sendmsg"):
        sendmsg = socket.socket.sendmsg

        def checked_sendmsg(sock, buffers, ancdata=(), flags=0, address=None):
            if address is not None:
                _check_address(sock, address)
            elif sock.family not in {socket.AF_INET, socket.AF_INET6, socket.AF_UNIX}:
                raise OfflineNetworkBlocked("offline test network boundary denied socket family")
            return sendmsg(sock, buffers, ancdata, flags, address) if address is not None else sendmsg(sock, buffers, ancdata, flags)

        monkeypatch.setattr(socket.socket, "sendmsg", checked_sendmsg)
