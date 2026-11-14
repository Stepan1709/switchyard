"""SSRF protection for outgoing calls to provider addresses.

The address of a provider is entered by an administrator, so the server must not be usable as
a proxy into networks it can reach but the administrator should not: cloud metadata services,
link-local addresses, other hosts of the private network.

Rules
* only http/https, no credentials in the URL, no query or fragment;
* non-local providers must use https and must resolve only to public addresses;
* private addresses are allowed only for a provider explicitly marked `local` AND whose host
  name is in the configured allowlist (LOCAL_PROVIDER_HOSTS);
* cloud metadata and link-local ranges are rejected always, whatever the flags;
* the host name is resolved before every outgoing call, every resolved address is checked, and
  the connection is made to the checked address (not to the name again), so a DNS answer that
  changes between the check and the connection (DNS rebinding) cannot redirect the request;
* redirects are never followed (the shared client has follow_redirects=False).
"""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.core.errors import AppError

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

# Never reachable, even for local providers: cloud metadata, link-local, "this host" and
# similar ranges, plus address families that can tunnel to them (NAT64, 6to4).
_ALWAYS_BLOCKED = [
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "100.64.0.0/10",  # carrier-grade NAT; Alibaba Cloud metadata lives at 100.100.100.200
        "169.254.0.0/16",  # link-local, AWS/GCP/Azure metadata 169.254.169.254
        "192.0.0.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "fe80::/10",
        "ff00::/8",
        "fd00:ec2::/32",  # AWS IPv6 metadata
        "64:ff9b::/96",  # NAT64: embeds an IPv4 address
        "2002::/16",  # 6to4: embeds an IPv4 address
        "::/128",
    )
]
_BLOCKED_HOSTNAMES = {"metadata.google.internal", "metadata", "instance-data", "kubernetes.default"}


class SSRFError(AppError):
    status_code = 422
    title = "Provider address is not allowed"
    code = "ssrf_blocked"


@dataclass(frozen=True)
class Target:
    scheme: str
    host: str
    port: int
    base_path: str  # without a trailing slash, e.g. "/v1"

    @property
    def host_header(self) -> str:
        default = 443 if self.scheme == "https" else 80
        host = f"[{self.host}]" if ":" in self.host else self.host
        return host if self.port == default else f"{host}:{self.port}"


def _unwrap(ip: IPAddress) -> IPAddress:
    # ::ffff:169.254.169.254 is the same machine as 169.254.169.254: judge the IPv4 form.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def classify(ip: IPAddress) -> str:
    """'public', 'private' (allowed only for allowlisted local hosts) or 'blocked'."""
    ip = _unwrap(ip)
    if any(ip in net for net in _ALWAYS_BLOCKED):
        return "blocked"
    return "public" if ip.is_global else "private"


def parse_base_url(raw: str, *, is_local: bool, local_hosts: list[str]) -> Target:
    """Syntax and policy checks that need no DNS."""
    try:
        parts = urlsplit(raw.strip())
        port = parts.port
    except ValueError as exc:
        raise SSRFError("The URL is malformed") from exc
    if parts.scheme not in {"http", "https"}:
        raise SSRFError("Only http and https URLs are allowed")
    if not is_local and parts.scheme != "https":
        raise SSRFError("Non-local providers must use https")
    if parts.username is not None or parts.password is not None:
        raise SSRFError("Credentials in the URL are not allowed")
    if parts.query or parts.fragment:
        raise SSRFError("The URL must not contain a query string or fragment")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host or len(host) > 253:
        raise SSRFError("The URL has no valid host")
    if host in _BLOCKED_HOSTNAMES:
        raise SSRFError("This host name is not allowed")
    if is_local and host not in {h.lower() for h in local_hosts}:
        raise SSRFError("Host is not in the allowlist of local providers (LOCAL_PROVIDER_HOSTS)")
    if port is not None and not 1 <= port <= 65535:
        raise SSRFError("The port is out of range")
    port = port or (443 if parts.scheme == "https" else 80)
    return Target(parts.scheme, host, port, parts.path.rstrip("/"))


Resolver = Callable[[str, int], Awaitable[list[IPAddress]]]


async def system_resolver(host: str, port: int) -> list[IPAddress]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SSRFError("The host name cannot be resolved") from exc
    unique: dict[str, IPAddress] = {}
    for info in infos:
        address = str(info[4][0]).split("%")[0]  # drop an IPv6 zone id
        unique.setdefault(address, ipaddress.ip_address(address))
    return list(unique.values())


async def resolve_checked(
    target: Target, *, is_local: bool, resolver: Resolver = system_resolver
) -> IPAddress:
    """Resolve the host and return an address that is safe to connect to."""
    try:
        addresses = [ipaddress.ip_address(target.host)]  # an IP literal needs no DNS
    except ValueError:
        addresses = await resolver(target.host, target.port)
    if not addresses:
        raise SSRFError("The host name cannot be resolved")
    kinds = {classify(ip) for ip in addresses}
    # One bad address in the answer is enough to refuse: a mixed answer is how rebinding
    # tricks and misconfigurations look like.
    if "blocked" in kinds:
        raise SSRFError("The host resolves to a forbidden address range")
    if "private" in kinds and not is_local:
        raise SSRFError("The host resolves to a private address; mark the provider as local")
    return _unwrap(addresses[0])


@dataclass(frozen=True)
class PreparedRequest:
    url: str
    headers: dict[str, str]
    extensions: dict[str, Any]


async def prepare(
    base_url: str,
    path: str,
    *,
    is_local: bool,
    local_hosts: list[str],
    resolver: Resolver = system_resolver,
) -> PreparedRequest:
    """Validate the provider URL, resolve it, and build a request to the checked address."""
    target = parse_base_url(base_url, is_local=is_local, local_hosts=local_hosts)
    ip = await resolve_checked(target, is_local=is_local, resolver=resolver)
    ip_text = f"[{ip}]" if ip.version == 6 else str(ip)
    extensions: dict[str, Any] = {}
    if target.scheme == "https":
        # Certificate and SNI are checked against the real host name, not against the address.
        extensions["sni_hostname"] = target.host
    return PreparedRequest(
        url=f"{target.scheme}://{ip_text}:{target.port}{target.base_path}{path}",
        headers={"host": target.host_header},
        extensions=extensions,
    )


class ResponseTooLarge(Exception):
    pass


async def safe_request(
    client: httpx.AsyncClient,
    method: str,
    base_url: str,
    path: str,
    *,
    is_local: bool,
    local_hosts: list[str],
    headers: dict[str, str] | None = None,
    json: Any = None,
    request_timeout: httpx.Timeout | None = None,
    max_bytes: int = 1_048_576,
    resolver: Resolver = system_resolver,
) -> tuple[httpx.Response, bytes]:
    """Perform a request to a provider; returns the response and at most `max_bytes` of body."""
    prepared = await prepare(
        base_url, path, is_local=is_local, local_hosts=local_hosts, resolver=resolver
    )
    request = client.build_request(
        method,
        prepared.url,
        headers={**(headers or {}), **prepared.headers},
        json=json,
        timeout=request_timeout if request_timeout is not None else httpx.USE_CLIENT_DEFAULT,
        extensions=prepared.extensions,
    )
    response = await client.send(request, stream=True)
    try:
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > max_bytes:
                raise ResponseTooLarge
    finally:
        await response.aclose()
    return response, bytes(body)
