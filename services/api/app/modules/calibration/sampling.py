"""Choosing which recorded requests to replay. Pure functions: no database, no clock.

The sample must be

* free of duplicates (the same prompt asked a thousand times would count a thousand times);
* reproducible: the same pool and the same seed give the same sample, so a run can be
  explained and repeated;
* representative: `stratified` keeps the mix of short/long prompts and with/without tools that
  the pool has, so a small sample does not accidentally contain only easy requests.
"""

import random
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class PoolItem:
    """What is known about a candidate request without reading its body."""

    request_id: uuid.UUID
    source: str  # "traffic" | "labeled"
    dedup_key: str  # equal keys = the same prompt
    prompt_chars: int  # size of the request (a stand-in for the token count)
    has_tools: bool


def deduplicate(items: Sequence[PoolItem]) -> list[PoolItem]:
    """Keep one item per dedup key. Labelled examples win over recorded traffic (they carry a
    known-good answer); among equals the smallest id wins, so the result does not depend on the
    order the database returned the rows in."""
    best: dict[str, PoolItem] = {}
    for item in sorted(items, key=lambda i: (i.source != "labeled", str(i.request_id))):
        best.setdefault(item.dedup_key, item)
    return sorted(best.values(), key=lambda i: str(i.request_id))


def pick_random(items: Sequence[PoolItem], n: int, seed: int) -> list[PoolItem]:
    ordered = sorted(items, key=lambda i: str(i.request_id))  # a fixed order before sampling
    if n >= len(ordered):
        return ordered
    return sorted(random.Random(seed).sample(ordered, n), key=lambda i: str(i.request_id))


def _length_bucket(chars: int, low: int, high: int) -> int:
    return 0 if chars <= low else (1 if chars <= high else 2)


def _terciles(values: list[int]) -> tuple[int, int]:
    ordered = sorted(values)
    return ordered[len(ordered) // 3], ordered[(2 * len(ordered)) // 3]


def _allocate(sizes: dict[tuple[bool, int], int], n: int) -> dict[tuple[bool, int], int]:
    """Split n picks between strata: one each first (so no kind of request is missing), the
    rest in proportion to what is left in each stratum (largest remainder method)."""
    keys = sorted(sizes)
    quota = dict.fromkeys(keys, 0)
    if n >= len(keys):
        quota = dict.fromkeys(keys, 1)
    remaining = n - sum(quota.values())
    spare = {k: sizes[k] - quota[k] for k in keys}
    while remaining > 0 and any(spare.values()):
        total_spare = sum(spare.values())
        shares = {k: remaining * spare[k] / total_spare for k in keys}
        added = 0
        for k in keys:
            take = min(int(shares[k]), spare[k])
            quota[k] += take
            spare[k] -= take
            added += take
        if added == 0:  # only fractions are left: hand out one by one, largest remainder first
            by_fraction = sorted(keys, key=lambda k: (-(shares[k] - int(shares[k])), k))
            for k in by_fraction:
                if remaining - added <= 0:
                    break
                if spare[k] > 0:
                    quota[k] += 1
                    spare[k] -= 1
                    added += 1
        remaining -= added
    return quota


def pick_stratified(items: Sequence[PoolItem], n: int, seed: int) -> list[PoolItem]:
    ordered = sorted(items, key=lambda i: str(i.request_id))
    if n >= len(ordered):
        return ordered
    if not ordered:
        return []
    low, high = _terciles([i.prompt_chars for i in ordered])
    strata: dict[tuple[bool, int], list[PoolItem]] = defaultdict(list)
    for item in ordered:
        strata[(item.has_tools, _length_bucket(item.prompt_chars, low, high))].append(item)
    quota = _allocate({k: len(v) for k, v in strata.items()}, n)
    chosen: list[PoolItem] = []
    for key in sorted(strata):
        # a separate generator per stratum: changing one stratum does not reshuffle the others
        rng = random.Random(f"{seed}:{key}")
        chosen.extend(rng.sample(strata[key], quota[key]))
    return sorted(chosen, key=lambda i: str(i.request_id))


def choose(items: Sequence[PoolItem], n: int, strategy: str, seed: int) -> list[PoolItem]:
    """Deduplicate the pool and pick up to n items with the given strategy."""
    pool = deduplicate(items)
    if strategy == "stratified":
        return pick_stratified(pool, n, seed)
    return pick_random(pool, n, seed)
