import asyncio
from typing import Any, cast
from unittest.mock import MagicMock, patch

from map_miner.proxy import (
    DEFAULT_PROXY_BYPASS,
    DEFAULT_TOR_RENEW_COOLDOWN,
    ProxyRotator,
    _normalize_proxy,
    async_renew_tor_circuit_control,
    get_tor_rotating_proxy,
    renew_tor_circuit_control,
)


def test_normalize_proxy():
    # String proxies
    assert _normalize_proxy("http://proxy.example.com:8080") == {
        "server": "http://proxy.example.com:8080"
    }
    assert _normalize_proxy("   socks5://127.0.0.1:1080   ") == {
        "server": "socks5://127.0.0.1:1080"
    }
    assert _normalize_proxy("") is None
    assert _normalize_proxy("   ") is None

    # Dict proxies
    assert _normalize_proxy({"server": "http://proxy.example.com:8080"}) == {
        "server": "http://proxy.example.com:8080"
    }
    assert _normalize_proxy(
        {
            "server": "  http://proxy.example.com:8080  ",
            "bypass": "  localhost, *.local  ",
            "username": "user",
            "password": "pwd",
        }
    ) == {
        "server": "http://proxy.example.com:8080",
        "bypass": "localhost, *.local",
        "username": "user",
        "password": "pwd",
    }
    assert _normalize_proxy({"other_field": "val"}) is None

    # Invalid types
    assert _normalize_proxy(None) is None
    assert _normalize_proxy(12345) is None
    assert _normalize_proxy(["not", "dict", "or", "str"]) is None


def test_proxy_rotator_empty_and_invalid():
    rotator_none = ProxyRotator(None)
    assert rotator_none.total == 0
    assert rotator_none.get() is None
    assert rotator_none.renew() is None

    rotator_empty = ProxyRotator([])
    assert rotator_empty.total == 0
    assert rotator_empty.get() is None

    rotator_invalid_items = ProxyRotator(["", "   ", {"no_server": "val"}])
    assert rotator_invalid_items.total == 0
    assert rotator_invalid_items.get() is None

    rotator_unsupported = ProxyRotator(cast(Any, 12345))
    assert rotator_unsupported.total == 0
    assert rotator_unsupported.get() is None


def test_proxy_rotator_single():
    rotator_str = ProxyRotator("http://proxy1:8080")
    assert rotator_str.total == 1
    assert rotator_str.get() == {"server": "http://proxy1:8080"}
    assert rotator_str.get() == {"server": "http://proxy1:8080"}

    proxy_dict = {
        "server": "http://proxy2:8080",
        "username": "user",
        "password": "password",
    }
    rotator_dict = ProxyRotator(proxy_dict)
    assert rotator_dict.total == 1
    assert rotator_dict.get() == proxy_dict
    assert rotator_dict.get() == proxy_dict


def test_proxy_rotator_round_robin():
    proxies = [f"http://proxy{i}:8080" for i in range(1, 4)]
    rotator = ProxyRotator(proxies)
    assert rotator.total == 3

    assert rotator.get() == {"server": "http://proxy1:8080"}
    assert rotator.get() == {"server": "http://proxy2:8080"}
    assert rotator.get() == {"server": "http://proxy3:8080"}
    assert rotator.get() == {"server": "http://proxy1:8080"}
    assert rotator.get() == {"server": "http://proxy2:8080"}


def test_proxy_rotator_with_bypass():
    proxy_dict = {
        "server": "http://proxy1:8080",
        "bypass": DEFAULT_PROXY_BYPASS,
    }
    rotator = ProxyRotator(proxy_dict)
    assert rotator.total == 1
    assert rotator.get() == proxy_dict

    mixed_proxies = [
        {"server": "http://proxy1:8080", "bypass": DEFAULT_PROXY_BYPASS},
        {"server": "http://proxy2:8080", "bypass": "custom.domain.com"},
    ]
    rotator_mixed = ProxyRotator(mixed_proxies)
    assert rotator_mixed.total == 2
    assert rotator_mixed.get() == mixed_proxies[0]
    assert rotator_mixed.get() == mixed_proxies[1]


def test_proxy_rotator_renew():
    # Empty rotator
    rotator_empty = ProxyRotator()
    assert rotator_empty.renew() is None

    # Standard proxy pool rotation
    rotator_std = ProxyRotator(["http://p1:8080", "http://p2:8080"])
    first = rotator_std.get()
    assert first is not None
    assert first["server"] == "http://p1:8080"
    renewed = rotator_std.renew()
    assert renewed is not None
    assert renewed["server"] == "http://p2:8080"

    # Tor proxy renewal (127.0.0.1:9050)
    with patch(
        "map_miner.proxy.renew_tor_circuit_control", return_value=True
    ) as mock_renew:
        rotator_tor = ProxyRotator("socks5://127.0.0.1:9050")
        current = rotator_tor.get()
        assert current is not None
        assert current["server"] == "socks5://127.0.0.1:9050"

        renewed_tor = rotator_tor.renew(current)
        mock_renew.assert_called_once()
        assert renewed_tor is not None
        assert renewed_tor["server"] == "socks5://127.0.0.1:9050"
        assert "username" not in renewed_tor
        assert "password" not in renewed_tor
        assert renewed_tor["bypass"] == DEFAULT_PROXY_BYPASS

    # Tor proxy renewal (localhost:9050)
    with patch(
        "map_miner.proxy.renew_tor_circuit_control", return_value=True
    ) as mock_renew_lh:
        rotator_lh = ProxyRotator("socks5://localhost:9050")
        renewed_lh = rotator_lh.renew()
        mock_renew_lh.assert_called_once()
        assert renewed_lh is not None
        assert renewed_lh["server"] == "socks5://localhost:9050"
        assert "username" not in renewed_lh
        assert "password" not in renewed_lh


def test_renew_tor_circuit_control():
    with patch("socket.create_connection") as mock_conn:
        mock_sock = MagicMock()
        mock_conn.return_value.__enter__.return_value = mock_sock

        # Case 1: Success (both AUTHENTICATE and SIGNAL return 250)
        mock_sock.recv.side_effect = [b"250 OK\r\n", b"250 OK\r\n"]
        assert (
            renew_tor_circuit_control(
                host="127.0.0.1", port=9051, password="secret", min_cooldown=0.0
            )
            is True
        )
        mock_conn.assert_called_with(("127.0.0.1", 9051), timeout=2.0)
        mock_sock.sendall.assert_any_call(b'AUTHENTICATE "secret"\r\n')
        mock_sock.sendall.assert_any_call(b"SIGNAL NEWNYM\r\n")

        # Case 2: Auth failed (515 Authentication failed)
        mock_sock.recv.side_effect = [b"515 Authentication failed\r\n"]
        assert (
            renew_tor_circuit_control(host="127.0.0.1", port=9051, min_cooldown=0.0)
            is False
        )

        # Case 3: Signal failed (514 Command disabled)
        mock_sock.recv.side_effect = [b"250 OK\r\n", b"514 Command disabled\r\n"]
        assert (
            renew_tor_circuit_control(host="127.0.0.1", port=9051, min_cooldown=0.0)
            is False
        )

        # Case 4: Connection refused / timeout
        mock_conn.side_effect = OSError("Connection refused")
        assert (
            renew_tor_circuit_control(host="127.0.0.1", port=9051, min_cooldown=0.0)
            is False
        )


def test_get_tor_rotating_proxy():
    # Default parameters
    proxy1 = get_tor_rotating_proxy()
    assert proxy1["server"] == "socks5://127.0.0.1:9050"
    assert "username" not in proxy1
    assert "password" not in proxy1
    assert proxy1["bypass"] == DEFAULT_PROXY_BYPASS

    # Repeated calls should produce valid config without credentials
    proxy2 = get_tor_rotating_proxy()
    assert proxy2["server"] == "socks5://127.0.0.1:9050"
    assert "username" not in proxy2
    assert "password" not in proxy2

    # Custom server and bypass
    proxy3 = get_tor_rotating_proxy(
        server="socks5://custom-tor:9050", bypass="google.com"
    )
    assert proxy3["server"] == "socks5://custom-tor:9050"
    assert proxy3["bypass"] == "google.com"
    assert "username" not in proxy3
    assert "password" not in proxy3

    # Custom bypass empty string
    proxy4 = get_tor_rotating_proxy(server="socks5://localhost:9050", bypass="")
    assert proxy4["server"] == "socks5://localhost:9050"
    assert proxy4["bypass"] == ""
    assert "username" not in proxy4
    assert "password" not in proxy4


def test_normalize_proxy_socks_strips_credentials():
    # SOCKS5 with credentials should have username and password removed
    proxy = {
        "server": "socks5://127.0.0.1:1080",
        "username": "myuser",
        "password": "mypassword",
        "bypass": "localhost",
    }
    normalized = _normalize_proxy(proxy)
    assert normalized == {
        "server": "socks5://127.0.0.1:1080",
        "bypass": "localhost",
    }
    assert "username" not in normalized
    assert "password" not in normalized

    # SOCKS4 / SOCKS5H
    proxy_s4 = {
        "server": "socks4://127.0.0.1:1080",
        "username": "user4",
        "password": "pwd",
    }
    norm_s4 = _normalize_proxy(proxy_s4)
    assert norm_s4 == {"server": "socks4://127.0.0.1:1080"}
    assert "username" not in norm_s4
    assert "password" not in norm_s4

    # HTTP proxies should preserve username and password
    http_proxy = {
        "server": "http://127.0.0.1:8080",
        "username": "user",
        "password": "pwd",
    }
    assert _normalize_proxy(http_proxy) == http_proxy


def test_tor_renewal_cooldown():
    import map_miner.proxy as proxy_mod

    proxy_mod._last_tor_renew_time = 0.0

    with patch("socket.create_connection") as mock_conn:
        mock_sock = MagicMock()
        mock_conn.return_value.__enter__.return_value = mock_sock
        mock_sock.recv.side_effect = [b"250 OK\r\n", b"250 OK\r\n"]

        # Call 1: First renewal succeeds, connects via socket
        assert renew_tor_circuit_control(host="127.0.0.1", port=9051) is True
        assert mock_conn.call_count == 1

        # Call 2: Immediate second renewal within default 15s cooldown -> returns True without socket connection
        assert renew_tor_circuit_control(host="127.0.0.1", port=9051) is True
        assert mock_conn.call_count == 1

        # Call 3: Calling with min_cooldown=0.0 bypasses cooldown and connects via socket
        mock_sock.recv.side_effect = [b"250 OK\r\n", b"250 OK\r\n"]
        assert (
            renew_tor_circuit_control(host="127.0.0.1", port=9051, min_cooldown=0.0)
            is True
        )
        assert mock_conn.call_count == 2


def test_proxy_rotator_renew_cooldown():
    import map_miner.proxy as proxy_mod

    proxy_mod._last_tor_renew_time = 0.0

    with patch("map_miner.proxy.renew_tor_circuit_control") as mock_renew:
        rotator = ProxyRotator("socks5://127.0.0.1:9050")
        rotator.renew()
        mock_renew.assert_called_with(min_cooldown=DEFAULT_TOR_RENEW_COOLDOWN)


def test_async_renew_tor_circuit_control():
    with patch(
        "map_miner.proxy.renew_tor_circuit_control", return_value=True
    ) as mock_sync:
        res = asyncio.run(
            async_renew_tor_circuit_control(
                host="127.0.0.1", port=9051, min_cooldown=10.0
            )
        )
        assert res is True
        mock_sync.assert_called_once_with(
            host="127.0.0.1",
            port=9051,
            password="",
            min_cooldown=10.0,
        )


def test_proxy_rotator_async_renew():
    rotator = ProxyRotator(["http://p1:8080", "http://p2:8080"])
    first = rotator.get()
    assert first == {"server": "http://p1:8080"}
    renewed = asyncio.run(rotator.async_renew())
    assert renewed == {"server": "http://p2:8080"}


def test_default_proxy_bypass_domains():
    expected_domains = [
        "maps.gstatic.com",
        "*.gstatic.com",
        "fonts.googleapis.com",
        "fonts.gstatic.com",
        "apis.google.com",
        "ssl.gstatic.com",
    ]
    for domain in expected_domains:
        assert domain in DEFAULT_PROXY_BYPASS
    assert (
        DEFAULT_PROXY_BYPASS
        == "maps.gstatic.com,*.gstatic.com,fonts.googleapis.com,fonts.gstatic.com,apis.google.com,ssl.gstatic.com"
    )
