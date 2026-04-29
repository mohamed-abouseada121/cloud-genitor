"""
utils/pagination.py
───────────────────
Generic pagination helpers for each cloud provider's different patterns.
"""

from __future__ import annotations

from typing import Any, Callable, Generator, Optional


# ── AWS ───────────────────────────────────────────────────────────────────────

def aws_paginate(client: Any, method: str, result_key: str,
                 **kwargs: Any) -> Generator[Any, None, None]:
    """
    Yields individual items from an AWS paginated API call.

    Example
    -------
    for instance in aws_paginate(ec2, 'describe_instances',
                                 'Reservations', Filters=[...]):
        ...
    """
    paginator = client.get_paginator(method)
    for page in paginator.paginate(**kwargs):
        for item in page.get(result_key, []):
            yield item


# ── Alibaba ───────────────────────────────────────────────────────────────────

def alibaba_paginate(
    call_fn: Callable[..., Any],
    request_builder: Callable[[int, int], Any],
    result_extractor: Callable[[Any], list],
    total_extractor: Callable[[Any], int],
    page_size: int = 50,
) -> Generator[Any, None, None]:
    """
    Handles Alibaba's PageNumber / PageSize pattern.

    Parameters
    ----------
    call_fn          : e.g. client.describe_instances
    request_builder  : callable(page_number, page_size) → request object
    result_extractor : callable(response) → list of items
    total_extractor  : callable(response) → total count (int)
    """
    page = 1
    total = None
    fetched = 0

    while True:
        req = request_builder(page, page_size)
        resp = call_fn(req)
        items = result_extractor(resp)
        if total is None:
            total = total_extractor(resp)

        for item in items:
            yield item
            fetched += 1

        if fetched >= total or not items:
            break
        page += 1


# ── GCP ───────────────────────────────────────────────────────────────────────

def gcp_paginate(
    call_fn: Callable[..., Any],
    **kwargs: Any,
) -> Generator[Any, None, None]:
    """
    Handles GCP's next_page_token pattern on standard list/aggregated calls.
    Expects an iterable pager returned by the GCP SDK (already handles pagination).
    """
    pager = call_fn(**kwargs)
    for item in pager:
        yield item


# ── Azure ─────────────────────────────────────────────────────────────────────

def azure_paginate(paged_result: Any) -> Generator[Any, None, None]:
    """
    Azure SDK list calls return ItemPaged iterables — just iterate them.
    This wrapper exists for uniformity and future retry injection.
    """
    for item in paged_result:
        yield item


# ── OCI ───────────────────────────────────────────────────────────────────────

def oci_paginate(client: Any, method: str, **kwargs: Any) -> Generator[Any, None, None]:
    """
    Uses OCI's built-in pagination helper.
    """
    import oci  # type: ignore[import]
    response = oci.pagination.list_call_get_all_results(
        getattr(client, method), **kwargs
    )
    for item in response.data:
        yield item
