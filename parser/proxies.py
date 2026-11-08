import os
from urllib.parse import unquote, urlsplit


def read_proxy(name: str, allowed: set[str]):
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        url = urlsplit(raw)
        valid = (url.scheme in allowed and url.hostname and url.port
                 and url.path in ("", "/") and not url.query and not url.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise SystemExit(f"В {name} укажите адрес прокси с протоколом, хостом и портом.")
    return url


def telegram_proxy() -> dict | None:
    url = read_proxy("TELEGRAM_PROXY", {"http", "socks4", "socks5"})
    if url is None:
        return None
    return {
        "proxy_type": url.scheme, "addr": url.hostname, "port": url.port, "rdns": True,
        "username": unquote(url.username) if url.username is not None else None,
        "password": unquote(url.password) if url.password is not None else None,
    }
