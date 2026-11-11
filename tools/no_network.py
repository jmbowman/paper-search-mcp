"""pytest plugin: refuse every non-local network connection and DNS lookup for the whole test run."""

import socket

_LOCAL = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}
_blocked: list[str] = []
_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex
_getaddrinfo = socket.getaddrinfo


def _check(address):
    if isinstance(address, tuple) and str(address[0]) not in _LOCAL:
        _blocked.append(str(address[0]))
        raise OSError(f"network blocked during tests: {address[0]}")


def _guarded_connect(self, address):
    _check(address)
    return _connect(self, address)


def _guarded_connect_ex(self, address):
    _check(address)
    return _connect_ex(self, address)


def _guarded_getaddrinfo(host, *args, **kwargs):
    if host is not None and str(host) not in _LOCAL:
        _blocked.append(str(host))
        raise socket.gaierror(f"DNS blocked during tests: {host}")
    return _getaddrinfo(host, *args, **kwargs)


socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
socket.getaddrinfo = _guarded_getaddrinfo


def pytest_terminal_summary(terminalreporter):
    hosts = sorted(set(_blocked))
    terminalreporter.write_line(f"no_network: blocked {len(_blocked)} attempt(s): {', '.join(hosts) or 'none'}")
