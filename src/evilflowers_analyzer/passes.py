"""How many times the current run's ``extract`` message has been delivered (issue #3).

A worker that dies during layout (OOM) or is stopped (scale-in) leaves the message
unacknowledged, and Redis delivers it again, marked ``redelivered``; nothing caps
that loop on its own. The count lives in the broker's Redis, keyed by run:

- a first delivery (a new message from the catalog: upload, retry, backfill) starts at 1;
- a redelivery adds one.

Counting is best effort: if Redis cannot be reached the pass counts as the first,
so a broken counter never stops a book.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

KEY = "evilflowers_analyzer:passes:{run_id}"
#: Longer than any book takes, short enough that keys of abandoned runs go away.
TTL_SECONDS = 7 * 24 * 3600


def count(client: Any, run_id: str, redelivered: bool) -> int:
    """This delivery's pass number, from 1."""
    key = KEY.format(run_id=run_id)
    try:
        if not redelivered:
            client.set(key, 1, ex=TTL_SECONDS)
            return 1
        passes = int(client.incr(key))
        client.expire(key, TTL_SECONDS)
        return passes
    except Exception:
        logger.warning("Cannot count passes of run %s; counting this one as the first", run_id, exc_info=True)
        return 1
