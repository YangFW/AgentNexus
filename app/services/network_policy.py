from __future__ import annotations

import ipaddress
import os
import re
import socket
from typing import TypeVar
from urllib.parse import urlsplit


_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})
_Error = TypeVar("_Error", bound=Exception)

_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_METADATA_HOSTS = frozenset(
    {
        "instance-data",
        "instance-data.ec2.internal",
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
    }
)
_LOCAL_HOSTS = frozenset({"ip6-localhost", "ip6-loopback", "localhost", "localhost.localdomain"})
_METADATA_ADDRESSES = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("fd00:ec2::254"),
    }
)


def env_flag(name: str, *, default: bool = False) -> bool:
    """Read a boolean environment flag using the platform's accepted values."""

    fallback = "true" if default else "false"
    return os.getenv(name, fallback).strip().lower() in _TRUTHY_VALUES


def outbound_network_enabled() -> bool:
    """Return whether this deployment permits application-level outbound traffic."""

    return env_flag("APP_ALLOW_OUTBOUND_NETWORK")


def require_outbound_network(
    capability: str,
    *,
    error_type: type[_Error] = RuntimeError,
) -> None:
    """Stop a network-backed capability before it opens a connection."""

    if outbound_network_enabled():
        return
    raise error_type(
        f"{capability}需要访问外部网络。当前部署已关闭出站网络，"
        "请由管理员设置 APP_ALLOW_OUTBOUND_NETWORK=true 后重启平台。"
    )


def validate_outbound_http_url(
    url: str,
    *,
    capability: str,
    allowlist_env: str,
    require_allowlist: bool = False,
    allow_non_public_when_allowlisted: bool = False,
    require_https_unless_explicitly_allowlisted: bool = False,
    allow_query: bool = True,
    error_type: type[_Error] = RuntimeError,
) -> str:
    """Validate a user-configured HTTP destination before a client sees it.

    Host allowlists contain comma-separated, exact hostnames or IP literals;
    schemes, paths and wildcard entries are deliberately unsupported.  DNS is
    checked on every call so a stored configuration cannot bypass validation
    by changing after it was saved.

    ``allow_non_public_when_allowlisted`` is intended only for explicitly
    trusted local model endpoints.  Remote MCP and generic HTTP tools must
    leave it disabled.
    """

    raw_url = str(url or "").strip()
    if not raw_url or any(ord(character) < 33 for character in raw_url) or "\\" in raw_url:
        raise error_type(f"{capability}地址必须是有效的 http(s) URL")
    try:
        parsed = urlsplit(raw_url)
        port = parsed.port
        hostname = _canonical_hostname(parsed.hostname or "")
    except (UnicodeError, ValueError) as exc:
        raise error_type(f"{capability}地址必须是有效的 http(s) URL") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (not allow_query and parsed.query)
    ):
        raise error_type(f"{capability}地址必须是有效的 http(s) URL")
    if hostname in _METADATA_HOSTS:
        raise error_type(f"{capability}地址不能指向云元数据服务")

    allowlist = _host_allowlist(allowlist_env, capability=capability, error_type=error_type)
    if require_allowlist and not allowlist:
        raise error_type(f"{capability}要求管理员配置非空的 {allowlist_env}")
    explicitly_allowed = bool(allowlist) and hostname in allowlist
    if allowlist and not explicitly_allowed:
        raise error_type(f"{capability}主机不在 {allowlist_env} 中")
    if (
        require_https_unless_explicitly_allowlisted
        and parsed.scheme.lower() != "https"
        and not explicitly_allowed
    ):
        raise error_type(f"{capability}默认要求使用 HTTPS；HTTP 主机必须显式加入 {allowlist_env}")
    if (
        hostname in _LOCAL_HOSTS or hostname.endswith(".localhost")
    ) and not (allow_non_public_when_allowlisted and explicitly_allowed):
        raise error_type(f"{capability}地址不能指向本机")

    addresses = _resolve_host_addresses(
        hostname,
        port or (443 if parsed.scheme.lower() == "https" else 80),
        capability=capability,
        error_type=error_type,
    )
    for address in addresses:
        if address in _METADATA_ADDRESSES:
            raise error_type(f"{capability}地址不能指向云元数据服务")
        if address.is_global and not address.is_multicast:
            continue
        if allow_non_public_when_allowlisted and explicitly_allowed:
            continue
        raise error_type(f"{capability}地址解析到了非公网 IP，已拒绝连接")
    return raw_url


def _host_allowlist(
    env_name: str,
    *,
    capability: str,
    error_type: type[_Error],
) -> frozenset[str]:
    entries: set[str] = set()
    for raw_entry in os.getenv(env_name, "").split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        if any(character in entry for character in "/?#@*\\"):
            raise error_type(f"{env_name} 只能填写逗号分隔的精确主机名或 IP")
        try:
            entries.add(_canonical_hostname(entry))
        except (UnicodeError, ValueError) as exc:
            raise error_type(f"{env_name} 包含无效主机名") from exc
    if "" in entries:
        raise error_type(f"{capability}的主机白名单配置无效")
    return frozenset(entries)


def _canonical_hostname(value: str) -> str:
    hostname = value.strip().rstrip(".").lower()
    if not hostname or "%" in hostname:
        raise ValueError("invalid hostname")
    bracketless = hostname[1:-1] if hostname.startswith("[") and hostname.endswith("]") else hostname
    try:
        return ipaddress.ip_address(bracketless).compressed.lower()
    except ValueError:
        pass
    ascii_hostname = bracketless.encode("idna").decode("ascii").lower()
    if len(ascii_hostname) > 253 or any(not _HOST_LABEL.fullmatch(label) for label in ascii_hostname.split(".")):
        raise ValueError("invalid hostname")
    return ascii_hostname


def _resolve_host_addresses(
    hostname: str,
    port: int,
    *,
    capability: str,
    error_type: type[_Error],
) -> frozenset[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            resolved = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise error_type(f"{capability}主机无法安全解析") from exc
        addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
        for item in resolved:
            try:
                addresses.add(ipaddress.ip_address(item[4][0].split("%", 1)[0]))
            except (IndexError, TypeError, ValueError) as exc:
                raise error_type(f"{capability}主机解析结果无效") from exc
        if not addresses:
            raise error_type(f"{capability}主机没有可用的 IP 地址")
        return frozenset(addresses)
    return frozenset({literal})
