"""
jybot.http -- tiny JSON HTTP helper built on the standard library.

The market-discovery and order-book code only need simple GET requests with a
short timeout and JSON decoding. Using ``urllib`` (stdlib) keeps the dry-run /
simulation path dependency-free. ``requests``/``httpx`` are NOT required.

A lightweight retry wrapper handles transient errors and HTTP 429 rate limits.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple, Union

from jybot.log import logger

_DEFAULT_HEADERS = {
    "User-Agent": "jybot-polymarket-5m/2.0",
    "Accept": "application/json",
}


class HttpError(Exception):
    """Raised for non-retryable HTTP failures."""

    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


def _encode_params(params: Optional[Dict[str, Any]]) -> str:
    """Encode query params; repeats array values as ?k=a&k=b (Gamma style)."""
    if not params:
        return ""
    pairs: List[Tuple[str, str]] = []
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            pairs.append((key, "true" if value else "false"))
        elif isinstance(value, (list, tuple)):
            for item in value:
                pairs.append((key, str(item)))
        else:
            pairs.append((key, str(value)))
    return urllib.parse.urlencode(pairs)


def get_json(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 20.0,
    retries: int = 3,
    backoff: float = 1.5,
) -> Union[Dict[str, Any], List[Any]]:
    """GET a URL and decode JSON, with retry on 429/5xx/network errors."""
    query = _encode_params(params)
    full_url = f"{url}?{query}" if query else url

    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(full_url, headers=_DEFAULT_HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = ""
            try:
                body = exc.read().decode("utf-8", "ignore")[:200]
            except Exception:
                pass
            # 429 (rate limit) and 5xx are retryable.
            if status == 429 or 500 <= status < 600:
                wait = backoff ** attempt
                logger.warning(
                    f"[http] {status} on {url} (attempt {attempt}/{retries}) "
                    f"retrying in {wait:.1f}s"
                )
                last_exc = HttpError(status, body)
                time.sleep(wait)
                continue
            raise HttpError(status, body)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            wait = backoff ** attempt
            logger.warning(
                f"[http] network error on {url}: {exc} "
                f"(attempt {attempt}/{retries}) retrying in {wait:.1f}s"
            )
            last_exc = exc
            time.sleep(wait)
        except json.JSONDecodeError as exc:
            last_exc = exc
            logger.warning(f"[http] bad JSON from {url}: {exc}")
            break

    raise HttpError(0, f"request failed after {retries} attempts: {last_exc}")


def get_json_safe(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 20.0,
    retries: int = 3,
) -> Optional[Union[Dict[str, Any], List[Any]]]:
    """Like :func:`get_json` but returns ``None`` instead of raising."""
    try:
        return get_json(url, params=params, timeout=timeout, retries=retries)
    except Exception as exc:
        logger.warning(f"[http] giving up on {url}: {exc}")
        return None
