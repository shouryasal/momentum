"""Talking to Ollama from inside WSL2 — detection, model list, pull, and the guidance
that makes the usual failure fixable.

The hard part is not the API, it is the network. Ollama runs on the **Windows host**
(0.34.1 with ``llama3.1:8b`` on this machine); the jobs run inside WSL2. In NAT mode
``127.0.0.1`` inside WSL is not the host, so the default install — which binds
``127.0.0.1`` on Windows — is unreachable no matter how correct the client is. Three
addresses are worth trying, in this order:

1. ``http://127.0.0.1:11434`` — right when Ollama runs inside WSL, or when Windows 11
   *mirrored* networking is on (then loopback really is shared);
2. the **WSL default gateway** from ``/proc/net/route`` — the host's address on the NAT
   network, which is what actually works in the default NAT setup;
3. the ``/etc/resolv.conf`` nameserver — the same host under the older DNS layout;
4. ``host.docker.internal`` — present when Docker Desktop installed it.

Every candidate is probed with ``GET /api/version`` on a short timeout, and any address
that is neither loopback nor RFC-1918/link-local private is **rejected**: a local model
endpoint reachable over the public internet is a misconfiguration, not a feature.

When nothing answers, :func:`guidance` returns the exact PowerShell the owner has to run
on the Windows side (``OLLAMA_HOST=0.0.0.0`` plus a firewall rule, or mirrored
networking), because "Ollama not detected" without those commands is a dead end.

Stdlib + httpx only, and every network entry point takes an injectable client so the
tests never open a socket.
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

__all__ = [
    "DEFAULT_PORT",
    "PROBE_TOKENS",
    "Detection",
    "OllamaError",
    "ProbeResult",
    "default_probe",
    "detect",
    "expand_candidates",
    "guidance",
    "is_local_url",
    "list_models",
    "probe_url",
    "pull",
    "resolv_nameserver",
    "show_model",
    "wsl_default_gateway",
]

DEFAULT_PORT = 11434
PROBE_TIMEOUT_S = 1.5

#: Symbolic probe entries resolved at detection time against the host's own files.
PROBE_TOKENS: tuple[str, ...] = ("wsl_gateway", "resolv_nameserver")

PROC_NET_ROUTE = Path("/proc/net/route")
RESOLV_CONF = Path("/etc/resolv.conf")

#: Hostnames that are local by definition even though they are not IP literals.
_LOCAL_HOSTS = frozenset({"localhost", "host.docker.internal", "ip6-localhost"})


class OllamaError(Exception):
    """A local-model endpoint problem worth surfacing in the UI."""


def default_probe() -> list[str]:
    return ["http://127.0.0.1:11434", "wsl_gateway", "resolv_nameserver",
            "http://host.docker.internal:11434"]


# --------------------------------------------------------------------------- host lookup


def wsl_default_gateway(route_text: str | None = None) -> str | None:
    """The Windows host's address on the WSL NAT network, from ``/proc/net/route``.

    The file is a table of hex, little-endian, per interface. The default route is the
    row whose Destination is all zeroes; its Gateway is the host.
    """
    if route_text is None:
        try:
            route_text = PROC_NET_ROUTE.read_text()
        except OSError:
            return None
    for line in route_text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 3 or parts[1] != "00000000":
            continue
        raw = parts[2]
        if len(raw) != 8:
            continue
        try:
            packed = int(raw, 16)
        except ValueError:
            continue
        octets = [(packed >> shift) & 0xFF for shift in (0, 8, 16, 24)]
        if not any(octets):
            continue
        return ".".join(str(o) for o in octets)
    return None


def resolv_nameserver(resolv_text: str | None = None) -> str | None:
    """The first nameserver in ``/etc/resolv.conf`` — the host under the DNS layout."""
    if resolv_text is None:
        try:
            resolv_text = RESOLV_CONF.read_text()
        except OSError:
            return None
    for line in resolv_text.splitlines():
        m = re.match(r"^\s*nameserver\s+(\S+)", line)
        if m:
            return m.group(1)
    return None


def is_local_url(url: str) -> bool:
    """True only for loopback, link-local or RFC-1918 addresses (and known local names).

    A local provider that answers on a public address is refused: the router would then
    be sending prompts off this machine without anybody having said so.
    """
    try:
        host = urlparse(url).hostname
    except ValueError:
        return False
    if not host:
        return False
    if host.lower() in _LOCAL_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(ip.is_loopback or ip.is_private or ip.is_link_local)


def expand_candidates(
    probe: Iterable[str],
    *,
    route_text: str | None = None,
    resolv_text: str | None = None,
    port: int = DEFAULT_PORT,
) -> list[str]:
    """Resolve the configured probe list into concrete, de-duplicated base URLs.

    Symbolic entries that cannot be resolved (no ``/proc/net/route`` on a non-WSL host,
    say) simply drop out; non-local URLs are dropped too.
    """
    out: list[str] = []
    for entry in probe:
        raw = str(entry).strip()
        if not raw:
            continue
        if raw == "wsl_gateway":
            host = wsl_default_gateway(route_text)
            url = f"http://{host}:{port}" if host else None
        elif raw == "resolv_nameserver":
            host = resolv_nameserver(resolv_text)
            url = f"http://{host}:{port}" if host else None
        else:
            url = raw if "://" in raw else f"http://{raw}:{port}"
        if url is None:
            continue
        url = url.rstrip("/")
        if not is_local_url(url):
            continue
        if url not in out:
            out.append(url)
    return out


# --------------------------------------------------------------------------- probing


@dataclass(frozen=True)
class ProbeResult:
    url: str
    ok: bool
    version: str | None = None
    error: str | None = None
    latency_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"url": self.url, "ok": self.ok, "version": self.version,
                "error": self.error, "latency_ms": self.latency_ms}


@dataclass
class Detection:
    base_url: str | None = None
    version: str | None = None
    results: list[ProbeResult] = field(default_factory=list)
    cached: bool = False

    @property
    def ok(self) -> bool:
        return self.base_url is not None

    def as_dict(self) -> dict[str, Any]:
        return {"base_url": self.base_url, "version": self.version, "ok": self.ok,
                "cached": self.cached, "results": [r.as_dict() for r in self.results]}


def _client(client: Any | None, timeout: float) -> tuple[Any, bool]:
    if client is not None:
        return client, False
    import httpx

    return httpx.Client(timeout=timeout), True


def probe_url(url: str, *, client: Any | None = None,
              timeout: float = PROBE_TIMEOUT_S) -> ProbeResult:
    """``GET {url}/api/version`` on a short timeout. Never raises."""
    if not is_local_url(url):
        return ProbeResult(url=url, ok=False, error="refused: not a local address")
    http, owned = _client(client, timeout)
    import time as _time

    started = _time.monotonic()
    try:
        resp = http.get(f"{url.rstrip('/')}/api/version", timeout=timeout)
        elapsed = int((_time.monotonic() - started) * 1000)
        if resp.status_code != 200:
            return ProbeResult(url=url, ok=False, error=f"HTTP {resp.status_code}",
                               latency_ms=elapsed)
        version = None
        try:
            version = (resp.json() or {}).get("version")
        except (ValueError, json.JSONDecodeError):
            version = None
        return ProbeResult(url=url, ok=True, version=version, latency_ms=elapsed)
    except Exception as e:  # noqa: BLE001 — any transport failure is just "not here"
        return ProbeResult(url=url, ok=False, error=f"{type(e).__name__}: {e}",
                           latency_ms=int((_time.monotonic() - started) * 1000))
    finally:
        if owned:
            http.close()


def detect(
    probe: Iterable[str] | None = None,
    *,
    client: Any | None = None,
    route_text: str | None = None,
    resolv_text: str | None = None,
    timeout: float = PROBE_TIMEOUT_S,
    port: int = DEFAULT_PORT,
) -> Detection:
    """Probe every candidate in order and keep the first that answers."""
    candidates = expand_candidates(probe if probe is not None else default_probe(),
                                   route_text=route_text, resolv_text=resolv_text,
                                   port=port)
    results: list[ProbeResult] = []
    for url in candidates:
        result = probe_url(url, client=client, timeout=timeout)
        results.append(result)
        if result.ok:
            return Detection(base_url=url, version=result.version, results=results)
    return Detection(base_url=None, version=None, results=results)


# --------------------------------------------------------------------------- model API


def list_models(base_url: str, *, client: Any | None = None,
                timeout: float = 10.0) -> list[dict[str, Any]]:
    """``GET /api/tags`` — the models this endpoint already has."""
    http, owned = _client(client, timeout)
    try:
        resp = http.get(f"{base_url.rstrip('/')}/api/tags", timeout=timeout)
        resp.raise_for_status()
        payload = resp.json() or {}
    except Exception as e:  # noqa: BLE001
        raise OllamaError(f"cannot list models at {base_url}: {e}") from e
    finally:
        if owned:
            http.close()
    models = payload.get("models") or []
    return [
        {
            "name": m.get("name"),
            "size": m.get("size"),
            "parameter_size": ((m.get("details") or {}).get("parameter_size")),
            "quantization": ((m.get("details") or {}).get("quantization_level")),
            "family": ((m.get("details") or {}).get("family")),
            "modified_at": m.get("modified_at"),
        }
        for m in models
        if isinstance(m, dict)
    ]


def show_model(base_url: str, name: str, *, client: Any | None = None,
               timeout: float = 10.0) -> dict[str, Any]:
    """``POST /api/show`` — capabilities of one model (context length, families)."""
    http, owned = _client(client, timeout)
    try:
        resp = http.post(f"{base_url.rstrip('/')}/api/show", json={"model": name},
                         timeout=timeout)
        resp.raise_for_status()
        payload = resp.json() or {}
    except Exception as e:  # noqa: BLE001
        raise OllamaError(f"cannot describe {name} at {base_url}: {e}") from e
    finally:
        if owned:
            http.close()
    info = payload.get("model_info") or {}
    ctx = next((v for k, v in info.items() if k.endswith("context_length")), None)
    return {
        "name": name,
        "families": (payload.get("details") or {}).get("families") or [],
        "parameter_size": (payload.get("details") or {}).get("parameter_size"),
        "max_ctx": int(ctx) if isinstance(ctx, (int, float)) else None,
        "capabilities": payload.get("capabilities") or [],
    }


def pull(base_url: str, name: str, *, client: Any | None = None,
         timeout: float = 3600.0) -> Iterator[dict[str, Any]]:
    """``POST /api/pull`` streamed — yields each progress frame for the SSE topic."""
    http, owned = _client(client, timeout)
    try:
        with http.stream("POST", f"{base_url.rstrip('/')}/api/pull",
                         json={"model": name, "stream": True}, timeout=timeout) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except (ValueError, json.JSONDecodeError):
                    continue
    except OllamaError:
        raise
    except Exception as e:  # noqa: BLE001
        raise OllamaError(f"pull of {name} failed at {base_url}: {e}") from e
    finally:
        if owned:
            http.close()


# --------------------------------------------------------------------------- guidance


def guidance(detection: Detection | None = None, *,
             env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """What to do about a local model that cannot be reached, in copy-paste form.

    Two working fixes on Windows 11 and one check inside WSL. Returned as structured
    data so the AI & Models page can render it as labelled code blocks rather than a
    paragraph the owner has to decode at 2am.
    """
    gateway = wsl_default_gateway()
    reachable = bool(detection and detection.ok)
    tried = [r.url for r in (detection.results if detection else [])]
    return {
        "reachable": reachable,
        "base_url": detection.base_url if detection else None,
        "tried": tried,
        "wsl_gateway": gateway,
        "problem": (
            "Ollama answered." if reachable else
            "Ollama runs on the Windows host; inside WSL2's NAT network 127.0.0.1 is "
            "not the host, and Ollama binds 127.0.0.1 by default — so nothing here can "
            "reach it until the host either listens on all interfaces or WSL uses "
            "mirrored networking."
        ),
        "options": [
            {
                "title": "Bind Ollama to all interfaces (works on any WSL mode)",
                "shell": "PowerShell (Windows, as Administrator)",
                "commands": [
                    '[Environment]::SetEnvironmentVariable("OLLAMA_HOST", "0.0.0.0", "User")',
                    'Get-Process ollama* | Stop-Process -Force',
                    'Start-Process "$env:LOCALAPPDATA\\Programs\\Ollama\\ollama app.exe"',
                    'New-NetFirewallRule -DisplayName "Ollama from WSL" -Direction Inbound '
                    '-Protocol TCP -LocalPort 11434 -Action Allow '
                    '-RemoteAddress 172.16.0.0/12,192.168.0.0/16',
                ],
                "then": (
                    f"Earn will then find it at http://{gateway}:{DEFAULT_PORT}"
                    if gateway else
                    "Earn will then find it on the WSL default gateway."
                ),
            },
            {
                "title": "Mirrored networking (Windows 11 22H2+; 127.0.0.1 becomes shared)",
                "shell": "PowerShell (Windows)",
                "commands": [
                    'Add-Content -Path "$env:USERPROFILE\\.wslconfig" '
                    '-Value "[wsl2]`nnetworkingMode=mirrored"',
                    "wsl --shutdown",
                ],
                "then": "After the restart http://127.0.0.1:11434 works from inside WSL.",
            },
            {
                "title": "Check from inside WSL",
                "shell": "bash (Ubuntu)",
                "commands": [
                    "curl -s --max-time 2 http://127.0.0.1:11434/api/version",
                    "curl -s --max-time 2 http://$(ip route show default | awk '{print $3}')"
                    ":11434/api/version",
                ],
                "then": "Whichever answers is the base_url to pin in models.yaml.",
            },
        ],
        "note": (
            "An explicit providers.ollama.base_url must stay loopback or private; a "
            "public address is refused at load."
        ),
        "env_hint": (env or {}).get("OLLAMA_HOST"),
    }
