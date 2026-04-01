"""
Plugin: Heard Nodes
Command: !heard

List nodes the radio has heard recently, drawn from the contacts cache
(last_advert_ts) and supplemented with live data from the in-memory
contacts snapshot (hop count, etc.).

Usage:
  !heard                  -- last 10 nodes heard (most recent first)
  !heard <N>              -- last N nodes (max 50)
  !heard <query>          -- filter by partial name or pubkey prefix
  !heard <N> <query>      -- last N matching query

Scope: DM only (direct). Privilege floor: 1 (any registered user).
"""

__version__ = "0.2.0"

__author__    = "Kameron Gasso"
__email__     = "kameron@gasso.org"
__copyright__ = "Copyright 2026, Kameron Gasso"
__license__   = "GPLv3"

import logging
import time

from core.database import PRIV_DEFAULT

logger = logging.getLogger(__name__)

_DEFAULT_N  = 10
_MAX_N      = 50


def _ago(ts: int) -> str:
    """Return a human-readable 'X ago' string for a Unix timestamp."""
    if not ts:
        return "never"
    diff = int(time.time()) - ts
    if diff < 0:
        return "just now"
    if diff < 60:
        return f"{diff}s ago"
    if diff < 3600:
        return f"{diff // 60}m ago"
    if diff < 86400:
        return f"{diff // 3600}h ago"
    return f"{diff // 86400}d ago"


def _fmt_row(row, contacts_snapshot: dict) -> str:
    """
    Format one heard-node line.

    Layout:  Name (prefix) | adv: Xm ago | heard: Xm ago | hops:N
    - "adv" = last_advert_ts (only shown when not NULL)
    - "heard" = most recent of last_seen_ts (any inbound msg) and last_dm_ts (DM only)
    - Hops comes from DB last_hops first; falls back to live contacts cache
      path_len if DB value is NULL (e.g. restart cleared it before ADV persisted).
    - Fields omitted entirely when unavailable.
    """
    prefix     = row["pubkey_prefix"]
    name       = row["display_name"] or prefix
    advert_ts  = row["last_advert_ts"]
    seen_ts    = row["last_seen_ts"]
    dm_ts      = row["last_dm_ts"]
    db_hops    = row["last_hops"]

    # "heard" = most recent inbound message (channel or DM)
    heard_ts = max(t for t in (seen_ts, dm_ts) if t) if any((seen_ts, dm_ts)) else None

    # Supplement hops from live cache if DB value missing
    hops = db_hops
    if hops is None:
        contact = contacts_snapshot.get(prefix, {})
        raw = contact.get("path_len")
        if raw is not None:
            try:
                hops = int(raw)
            except (TypeError, ValueError):
                hops = None

    parts = [f"{name} ({prefix})"]
    if advert_ts:
        parts.append(f"adv:{_ago(advert_ts)}")
    if heard_ts:
        parts.append(f"heard:{_ago(heard_ts)}")
    if hops is not None:
        parts.append(f"hops:{hops}")

    return " | ".join(parts)


def setup(dispatcher, config, db):

    async def cmd_heard(msg, args=""):
        raw = (args or msg.arg_str).strip()
        tokens = raw.split(None, 1) if raw else []

        # Argument parsing:
        #   !heard             -> n=10, query=None
        #   !heard 15          -> n=15, query=None
        #   !heard KG7ABC      -> n=10, query="KG7ABC"
        #   !heard 5 KG7ABC    -> n=5,  query="KG7ABC"
        n     = _DEFAULT_N
        query = None

        if tokens:
            if tokens[0].isdigit():
                n = min(int(tokens[0]), _MAX_N)
                query = tokens[1].strip() if len(tokens) > 1 else None
            else:
                query = raw  # entire arg string is the search term

        # Build SQL
        if query:
            q = f"%{query}%"
            rows = await db.fetchall(
                """SELECT pubkey_prefix, display_name,
                          last_advert_ts, last_seen_ts, last_dm_ts, last_hops
                   FROM users
                   WHERE (last_advert_ts IS NOT NULL
                          OR last_seen_ts IS NOT NULL
                          OR last_dm_ts IS NOT NULL)
                     AND (display_name LIKE ? OR pubkey_prefix LIKE ?)
                   ORDER BY max(
                       coalesce(last_advert_ts, 0),
                       coalesce(last_seen_ts, 0),
                       coalesce(last_dm_ts, 0)
                   ) DESC
                   LIMIT ?""",
                (q, q, n),
            )
        else:
            rows = await db.fetchall(
                """SELECT pubkey_prefix, display_name,
                          last_advert_ts, last_seen_ts, last_dm_ts, last_hops
                   FROM users
                   WHERE last_advert_ts IS NOT NULL
                      OR last_seen_ts IS NOT NULL
                      OR last_dm_ts IS NOT NULL
                   ORDER BY max(
                       coalesce(last_advert_ts, 0),
                       coalesce(last_seen_ts, 0),
                       coalesce(last_dm_ts, 0)
                   ) DESC
                   LIMIT ?""",
                (n,),
            )

        if not rows:
            if query:
                return f"No nodes heard matching '{query}'."
            return "No nodes heard yet."

        # Grab live contacts snapshot for hop count supplementation
        contacts_snapshot = dispatcher.get_contacts_snapshot()

        header = f"Heard ({len(rows)}"
        if query:
            header += f", filter: '{query}'"
        header += "):"

        lines = [header]
        for row in rows:
            lines.append("  " + _fmt_row(row, contacts_snapshot))

        return "\n".join(lines)

    dispatcher.register_command(
        "!heard", cmd_heard,
        help_text="List recently heard nodes from the contacts cache",
        usage_text=(
            "!heard              -- last 10 nodes heard\n"
            "!heard <N>          -- last N nodes (max 50)\n"
            "!heard <query>      -- search by name or node ID\n"
            "!heard <N> <query>  -- last N matching query"
        ),
        scope="direct", priv_floor=PRIV_DEFAULT,
        category="nodes", plugin_name="heard",
    )
