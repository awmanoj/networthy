"""Read-only check: does any stored holding vanish before it reaches the screen?

Run after deploying the merge fix:

    docker exec networthy python /app/scripts/verify_holdings.py

Writes nothing and changes nothing. It re-runs the real read path
(`main.merge_sources`) over the real rows and compares the two totals — the same
reconcile-against-itself idea the CAS parser uses, applied to the database.

A non-zero "hidden" figure means rows are being dropped on the way to the page.
That was the folio bug: two folios of one scheme share an ISIN and a name, and
before the fix all but the first were discarded silently.
"""

from __future__ import annotations

import sys
from collections import defaultdict

sys.path.insert(0, "/app")

from app import main as m            # noqa: E402
from app import storage              # noqa: E402

ALL_CLASSES = {
    "mutual_fund", "direct_equity", "gold", "silver", "private_equity",
    "corporate_bond", "govt_bond", "nps", "etf", "unknown",
}


def main() -> int:
    storage.init_db()
    users = storage.list_users()
    print(f"{len(users)} account(s)\n")
    worst = 0.0

    for user in users:
        imports = storage.list_networth_holdings(user.id, ALL_CLASSES)
        cas = storage.latest_holdings_by_class(user.id, ALL_CLASSES)
        if not imports and not cas:
            continue

        stored = sum((h.get("value") or 0.0) for h in imports) + \
                 sum((h.get("value") or 0.0) for h in cas)
        merged = m.merge_sources(imports, cas)
        shown = sum((h.get("value") or 0.0) for h in merged)

        by_source: dict[str, int] = defaultdict(int)
        for h in imports:
            by_source[h.get("source") or "?"] += 1
        by_source["nsdl-cas"] += len(cas)

        # Rows sharing an ISIN or name *within one source* — legitimate duplicates
        # (two folios, two demat accounts) and exactly what used to be dropped.
        dupes: dict[tuple, list] = defaultdict(list)
        for h in imports:
            key = (h.get("source"), h.get("isin") or m._norm_name(h.get("name")))
            dupes[key].append(h)
        repeated = {k: v for k, v in dupes.items() if len(v) > 1}

        hidden = stored - shown
        flag = "OK" if abs(hidden) < 1.0 else "*** ROWS ARE BEING HIDDEN ***"
        print(f"{user.email}")
        print(f"  rows        : {dict(by_source)}")
        print(f"  stored      : Rs {stored:>16,.0f}")
        print(f"  rendered    : Rs {shown:>16,.0f}")
        print(f"  difference  : Rs {hidden:>16,.0f}   <- cross-source duplicates, expected")
        if repeated:
            print(f"  same-source repeats (these MUST all survive):")
            for (src, key), rows in repeated.items():
                total = sum((r.get('value') or 0.0) for r in rows)
                names = {r.get("name") for r in rows}
                print(f"    {src}: {len(rows)} rows, Rs {total:,.0f} — {list(names)[0][:48]}")
                kept = [r for r in merged if (r.get("isin") or m._norm_name(r.get("name"))) == key]
                print(f"      -> {len(kept)} of {len(rows)} reach the page"
                      f"  {'OK' if len(kept) == len(rows) else '*** DROPPED ***'}")
                if len(kept) != len(rows):
                    worst += total - sum((r.get('value') or 0.0) for r in kept)
        print(f"  verdict     : {flag}\n")

    if worst:
        print(f"MONEY MISSING FROM THE SCREEN: Rs {worst:,.0f}")
        return 1
    print("No stored holding is being dropped on the way to the page.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
