"""Shared Elastic HTTP plumbing: per-thread session, endpoint URL, headers.

First slice of the elastic_client consolidation from
docs/CODE_REVIEW_2026-09.md §1 (the paginate/retry-ladder unification of
the six search_after loops is still to come and should be verified
against live Elastic one loop at a time).
"""
from __future__ import annotations

import threading

import requests
from requests.adapters import HTTPAdapter

_thread_local = threading.local()


def get_thread_session() -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is not None:
        return session
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=8, pool_maxsize=8, max_retries=0)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    _thread_local.session = session
    return session


def search_url(base: str, index_id: str) -> str:
    base = (base or "").rstrip("/")
    # If given a Kibana URL, convert to the ES endpoint instead of the proxy
    # (the proxy is often disabled).
    if "kb." in base:
        base = base.replace(".kb.", ".es.")
    return (
        f"{base}/{index_id}/_search"
        "?ignore_unavailable=true&allow_no_indices=true&request_cache=true"
    )


def api_headers(api_key: str) -> dict:
    """The auth/content headers every Elastic request sends. Previously
    spelled out inline at seven call sites."""
    return {
        "Content-Type": "application/json",
        "kbn-xsrf": "true",
        "Authorization": f"ApiKey {api_key}",
    }
