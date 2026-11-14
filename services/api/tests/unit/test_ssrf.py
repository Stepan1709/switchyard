import ipaddress

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.core import ssrf
from app.core.ssrf import SSRFError

HOSTS = ["mock_llm", "ollama"]


def ip(text: str):
    return ipaddress.ip_address(text)


def fake(*answers: str):
    """A resolver that returns the given addresses."""

    async def resolver(host: str, port: int):
        return [ip(a) for a in answers]

    return resolver


# ---- classification ----
@pytest.mark.parametrize("addr", ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111"])
def test_public_addresses(addr):
    assert ssrf.classify(ip(addr)) == "public"


@pytest.mark.parametrize(
    "addr", ["10.0.0.5", "172.16.3.4", "172.18.0.2", "192.168.1.1", "127.0.0.1", "::1", "fc00::1"]
)
def test_private_addresses(addr):
    assert ssrf.classify(ip(addr)) == "private"


@pytest.mark.parametrize(
    "addr",
    [
        "169.254.169.254",  # AWS / GCP / Azure metadata
        "169.254.0.1",
        "100.100.100.200",  # Alibaba metadata (carrier-grade NAT range)
        "0.0.0.0",
        "224.0.0.1",
        "255.255.255.255",
        "fe80::1",
        "fd00:ec2::254",  # AWS IPv6 metadata
        "::ffff:169.254.169.254",  # IPv4-mapped form of the metadata address
        "64:ff9b::a9fe:a9fe",  # NAT64 embedding 169.254.169.254
        "2002:a9fe:a9fe::1",  # 6to4 embedding 169.254.169.254
        "::",
    ],
)
def test_blocked_addresses(addr):
    assert ssrf.classify(ip(addr)) == "blocked"


@given(st.integers(min_value=0, max_value=2**32 - 1))
def test_classification_is_consistent_with_the_stdlib(value):
    address = ipaddress.IPv4Address(value)
    kind = ssrf.classify(address)
    if kind == "public":
        assert address.is_global and not address.is_link_local and not address.is_multicast
    if address.is_link_local or address.is_multicast or address.is_unspecified:
        assert kind == "blocked"
    if address.is_loopback or address.is_private:
        assert kind != "public"


# ---- URL policy ----
def parse(url, local=False):
    return ssrf.parse_base_url(url, is_local=local, local_hosts=HOSTS)


def test_valid_urls():
    t = parse("https://api.example.com/v1/")
    assert (t.scheme, t.host, t.port, t.base_path) == ("https", "api.example.com", 443, "/v1")
    local = parse("http://MOCK_LLM:8003/v1", local=True)
    assert (local.host, local.port) == ("mock_llm", 8003)
    assert parse("https://api.example.com.").host == "api.example.com"  # trailing dot


@pytest.mark.parametrize(
    "url",
    [
        "ftp://api.example.com/",
        "file:///etc/passwd",
        "gopher://api.example.com/",
        "javascript:alert(1)",
        "https://user:pass@api.example.com/v1",
        "https://user@api.example.com/v1",
        "https://api.example.com/v1?x=1",
        "https://api.example.com/v1#frag",
        "https:///v1",
        "https://",
        "http://api.example.com/v1",  # non-local must use https
        "https://api.example.com:99999/v1",
        "https://api.example.com:abc/v1",
        "https://metadata.google.internal/",
        "https://metadata/",
        "",
    ],
)
def test_forbidden_urls(url):
    with pytest.raises(SSRFError):
        parse(url)


def test_local_provider_must_be_on_the_allowlist():
    with pytest.raises(SSRFError, match="allowlist"):
        parse("http://some-other-host:8000/v1", local=True)
    with pytest.raises(SSRFError, match="allowlist"):
        parse("http://169.254.169.254/v1", local=True)  # an IP literal is not on the list either


# ---- resolution ----
async def check(host_ip, *, local=False, url="https://api.example.com/v1"):
    target = parse(url, local=local)
    return await ssrf.resolve_checked(target, is_local=local, resolver=fake(*host_ip))


async def test_public_host_is_accepted():
    assert str(await check(["93.184.216.34"])) == "93.184.216.34"


async def test_private_answer_is_refused_for_non_local_providers():
    with pytest.raises(SSRFError, match="private"):
        await check(["10.1.2.3"])


async def test_private_answer_is_accepted_for_allowlisted_local_provider():
    got = await check(["172.18.0.5"], local=True, url="http://mock_llm:8003/v1")
    assert str(got) == "172.18.0.5"


@pytest.mark.parametrize("local", [False, True])
async def test_metadata_range_is_refused_even_for_local_providers(local):
    url = "http://mock_llm:8003/v1" if local else "https://api.example.com/v1"
    with pytest.raises(SSRFError, match="forbidden"):
        await check(["169.254.169.254"], local=local, url=url)


async def test_one_bad_address_in_a_mixed_answer_refuses_the_host():
    with pytest.raises(SSRFError, match="forbidden"):
        await check(["93.184.216.34", "169.254.169.254"])


async def test_ip_literal_needs_no_dns_and_is_still_checked():
    async def no_dns(host, port):
        raise AssertionError("DNS must not be used for a literal")

    target = parse("https://8.8.8.8/v1")
    assert str(await ssrf.resolve_checked(target, is_local=False, resolver=no_dns)) == "8.8.8.8"
    with pytest.raises(SSRFError):
        await ssrf.resolve_checked(parse("https://[fe80::1]/v1"), is_local=False, resolver=no_dns)
    with pytest.raises(SSRFError):
        await ssrf.resolve_checked(parse("https://127.0.0.1/v1"), is_local=False, resolver=no_dns)


async def test_unresolvable_host_is_an_ssrf_error():
    async def nothing(host, port):
        return []

    with pytest.raises(SSRFError):
        await ssrf.resolve_checked(
            parse("https://nope.example.com/"), is_local=False, resolver=nothing
        )


# ---- DNS rebinding: the answer changes between two calls ----
async def test_every_call_resolves_again_so_a_changed_answer_is_caught():
    answers = [["93.184.216.34"], ["127.0.0.1"]]

    async def rebinding(host, port):
        return [ip(a) for a in answers.pop(0)]

    first = await ssrf.prepare(
        "https://rebind.example.com/v1",
        "/models",
        is_local=False,
        local_hosts=HOSTS,
        resolver=rebinding,
    )
    assert "93.184.216.34" in first.url
    with pytest.raises(SSRFError):
        await ssrf.prepare(
            "https://rebind.example.com/v1",
            "/models",
            is_local=False,
            local_hosts=HOSTS,
            resolver=rebinding,
        )


# ---- the outgoing request ----
async def test_request_goes_to_the_checked_address_with_the_original_host_header():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["host"] = str(request.url), request.headers["host"]
        seen["sni"] = request.extensions.get("sni_hostname")
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response, body = await ssrf.safe_request(
            client, "GET", "https://api.example.com/v1", "/models",
            is_local=False, local_hosts=HOSTS, resolver=fake("93.184.216.34"),
        )  # fmt: skip
    assert response.status_code == 200 and b"ok" in body
    assert seen["url"] == "https://93.184.216.34/v1/models"  # connected to the IP ...
    assert seen["host"] == "api.example.com"  # ... but addressed by name
    assert seen["sni"] == "api.example.com"  # ... and TLS is verified against the name


async def test_ipv6_target_is_bracketed_and_http_has_no_sni():
    prepared = await ssrf.prepare(
        "http://ollama:11434/v1",
        "/models",
        is_local=True,
        local_hosts=HOSTS,
        resolver=fake("fd12::5"),
    )
    assert prepared.url == "http://[fd12::5]:11434/v1/models"
    assert prepared.headers == {"host": "ollama:11434"} and prepared.extensions == {}


async def test_redirects_are_not_followed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as c:
        response, _ = await ssrf.safe_request(
            c, "GET", "https://api.example.com/v1", "/models",
            is_local=False, local_hosts=HOSTS, resolver=fake("93.184.216.34"),
        )  # fmt: skip
    assert response.status_code == 302  # returned as is, never fetched


async def test_oversized_response_is_cut_off():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 5000)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(ssrf.ResponseTooLarge):
            await ssrf.safe_request(
                c, "GET", "https://api.example.com/v1", "/models",
                is_local=False, local_hosts=HOSTS, resolver=fake("93.184.216.34"), max_bytes=1000,
            )  # fmt: skip
