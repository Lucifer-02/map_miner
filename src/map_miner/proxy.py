"""
Proxy management and Tor network integration for map-miner.
"""

import asyncio
import logging
import socket
import threading
import time
from collections.abc import Sequence
from typing import Any, cast

from playwright.async_api import ProxySettings

logger = logging.getLogger(__name__)

DEFAULT_PROXY_BYPASS = "maps.gstatic.com,*.gstatic.com,fonts.googleapis.com"
DEFAULT_TOR_RENEW_COOLDOWN: float = 15.0

_last_tor_renew_time: float = 0.0
_tor_renew_lock: threading.Lock = threading.Lock()


def renew_tor_circuit_control(
    host: str = "127.0.0.1",
    port: int = 9051,
    password: str | None = None,
    min_cooldown: float = DEFAULT_TOR_RENEW_COOLDOWN,
) -> bool:
    """Sends SIGNAL NEWNYM to Tor ControlPort to request a fresh circuit.

    Respects min_cooldown between successive renewal attempts to avoid
    spamming Tor daemon and circuit renewal storms.

    Args:
        host (str): Tor ControlPort host. Defaults to "127.0.0.1".
        port (int): Tor ControlPort port. Defaults to 9051.
        password (str | None): ControlPort authentication password. Defaults to None.
        min_cooldown (float): Minimum seconds required between renewals. Defaults to 15.0.

    Returns:
        bool: True if authenticated and SIGNAL NEWNYM accepted (250 OK) or within cooldown, False otherwise.
    """
    global _last_tor_renew_time
    now = time.monotonic()

    with _tor_renew_lock:
        if now - _last_tor_renew_time < min_cooldown:
            logger.debug(
                "Tor circuit renewal skipped: within cooldown (%.1fs < %.1fs).",
                now - _last_tor_renew_time,
                min_cooldown,
            )
            return True

    auth_cmd = f'AUTHENTICATE "{password or ""}"\r\n'.encode("ascii")
    signal_cmd = b"SIGNAL NEWNYM\r\n"
    try:
        with socket.create_connection((host, port), timeout=2.0) as s:
            s.sendall(auth_cmd)
            resp = s.recv(1024).decode("ascii", errors="ignore")
            if not resp.startswith("250"):
                logger.debug("Tor ControlPort AUTHENTICATE failed: %s", resp.strip())
                return False

            s.sendall(signal_cmd)
            resp = s.recv(1024).decode("ascii", errors="ignore")
            if not resp.startswith("250"):
                logger.debug("Tor ControlPort SIGNAL NEWNYM failed: %s", resp.strip())
                return False

            try:
                s.sendall(b"QUIT\r\n")
            except OSError:
                pass

            with _tor_renew_lock:
                _last_tor_renew_time = time.monotonic()

            logger.info("🔄 Tor circuit renewed via ControlPort (%s:%d).", host, port)
            return True
    except (OSError, TimeoutError) as e:
        logger.debug("Could not connect to Tor ControlPort %s:%d: %s", host, port, e)
        return False


async def async_renew_tor_circuit_control(
    host: str = "127.0.0.1",
    port: int = 9051,
    password: str | None = None,
    min_cooldown: float = DEFAULT_TOR_RENEW_COOLDOWN,
) -> bool:
    """Asynchronously sends SIGNAL NEWNYM to Tor ControlPort to request a fresh circuit."""
    return await asyncio.to_thread(
        renew_tor_circuit_control,
        host=host,
        port=port,
        password=password,
        min_cooldown=min_cooldown,
    )


def get_tor_rotating_proxy(
    server: str = "socks5://127.0.0.1:9050",
    bypass: str | None = DEFAULT_PROXY_BYPASS,
) -> ProxySettings:
    """Generates a Tor SOCKS5 proxy configuration.

    Note: Chromium and Playwright do not support authentication for SOCKS5 proxies
    (Passing username/password causes Chromium to crash or fail connection).
    Circuit renewal is handled via Tor ControlPort (SIGNAL NEWNYM) instead of
    SOCKS auth stream isolation.

    Args:
        server (str): SOCKS5 proxy URL. Defaults to "socks5://127.0.0.1:9050".
        bypass (str | None): Hosts to bypass proxy. Defaults to DEFAULT_PROXY_BYPASS.

    Returns:
        ProxySettings: Configured proxy settings dictionary without credentials.
    """
    proxy: ProxySettings = {
        "server": server,
    }
    if bypass is not None:
        proxy["bypass"] = bypass
    return proxy


def _normalize_proxy(proxy: Any) -> ProxySettings | None:
    """Normalizes proxy input into a valid Playwright ProxySettings dict."""
    if isinstance(proxy, dict) and "server" in proxy:
        norm: dict[str, Any] = dict(proxy)
        server_str = str(norm["server"]).strip()
        norm["server"] = server_str
        if "bypass" in norm and norm["bypass"] is not None:
            norm["bypass"] = str(norm["bypass"]).strip()

        # Chromium and Playwright do not support SOCKS authentication.
        # Strip credentials to prevent Playwright/Chromium crashes.
        lower_server = server_str.lower()
        if lower_server.startswith(("socks5://", "socks4://", "socks5h://")) and (
            "username" in norm or "password" in norm
        ):
            logger.warning(
                "SOCKS proxy credentials (%s) are not supported by Chromium/Playwright; removing username/password to avoid crashes.",
                server_str,
            )
            norm.pop("username", None)
            norm.pop("password", None)

        return cast(ProxySettings, norm)
    if isinstance(proxy, str) and proxy.strip():
        return {"server": proxy.strip()}
    return None


class ProxyRotator:
    """
    Manages proxy allocation with round-robin rotation support for single or multiple proxies.
    """

    def __init__(
        self,
        proxy: (
            ProxySettings
            | dict[str, Any]
            | Sequence[ProxySettings | dict[str, Any] | Any]
            | str
            | Sequence[str]
            | None
        ) = None,
    ) -> None:
        self._proxies: list[ProxySettings] = []
        if proxy is not None:
            if isinstance(proxy, (dict, str)):
                norm = _normalize_proxy(proxy)
                if norm:
                    self._proxies.append(norm)
            elif isinstance(proxy, Sequence):
                for item in proxy:
                    norm = _normalize_proxy(item)
                    if norm:
                        self._proxies.append(norm)
            else:
                logger.warning("Unsupported proxy configuration type: %s", type(proxy))
        self._index: int = 0

    @property
    def total(self) -> int:
        """Total number of valid configured proxies."""
        return len(self._proxies)

    def get(self) -> ProxySettings | None:
        """
        Returns the next proxy configuration using round-robin allocation,
        or None if no proxy is configured.
        """
        if not self._proxies:
            return None
        selected = self._proxies[self._index % len(self._proxies)]
        self._index = (self._index + 1) % len(self._proxies)
        return selected

    def renew(
        self,
        current_proxy: ProxySettings | None = None,
        min_cooldown: float = DEFAULT_TOR_RENEW_COOLDOWN,
    ) -> ProxySettings | None:
        """Renews or rotates proxy.

        If using Tor SOCKS proxy (127.0.0.1:9050 or localhost:9050), attempts ControlPort
        renewal respecting min_cooldown and generates a new isolated SOCKS credential. Otherwise,
        advances to the next proxy in the pool.

        Args:
            current_proxy (ProxySettings | None): Currently active proxy.
            min_cooldown (float): Minimum seconds required between Tor circuit renewals.
                Defaults to DEFAULT_TOR_RENEW_COOLDOWN (15.0s).

        Returns:
            ProxySettings | None: The renewed or next proxy configuration.
        """
        target = current_proxy
        if target is None and self._proxies:
            target = self._proxies[self._index % len(self._proxies)]

        server = target.get("server", "") if target else ""
        is_tor = "127.0.0.1:9050" in server or "localhost:9050" in server

        if is_tor:
            renew_tor_circuit_control(min_cooldown=min_cooldown)
            bypass = (target.get("bypass") if target else None) or DEFAULT_PROXY_BYPASS
            new_proxy = get_tor_rotating_proxy(
                server=server or "socks5://127.0.0.1:9050", bypass=bypass
            )
            if self._proxies:
                for idx, p in enumerate(self._proxies):
                    p_server = p.get("server", "")
                    if "127.0.0.1:9050" in p_server or "localhost:9050" in p_server:
                        self._proxies[idx] = new_proxy
                        break
            return new_proxy

        return self.get()

    async def async_renew(
        self,
        current_proxy: ProxySettings | None = None,
        min_cooldown: float = DEFAULT_TOR_RENEW_COOLDOWN,
    ) -> ProxySettings | None:
        """Asynchronously renews or rotates proxy."""
        return await asyncio.to_thread(
            self.renew, current_proxy=current_proxy, min_cooldown=min_cooldown
        )


__all__ = [
    "DEFAULT_PROXY_BYPASS",
    "DEFAULT_TOR_RENEW_COOLDOWN",
    "ProxyRotator",
    "_last_tor_renew_time",
    "_normalize_proxy",
    "_tor_renew_lock",
    "async_renew_tor_circuit_control",
    "get_tor_rotating_proxy",
    "renew_tor_circuit_control",
]
