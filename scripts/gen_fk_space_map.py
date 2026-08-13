#!/usr/bin/env python3
"""gen_fk_space_map.py — N1: emit fk_space_map.json from live information_schema.

Classifies every `*_idfr` column in taxo/meta/org_n/purpus_user by whether it
has a live FK, and where it points:
  master  FK -> taxo.master.id           (DB-enforced)
  tenant  FK -> some other table         (DB-enforced)
  none    no FK at all                   (naming convention only, unenforced --
                                           this is what N1's naming gate protects)

Query is the one verified live and documented in
02a Belongity_Domain_Taxonomy_Entity_Html_Guides/Part67_N1_IdSpace_Column_Classification.html
§5 -- GROUP BY column identity de-dupes the known defect (a column with two FK
definitions to the same parent was counted twice by the naive LEFT JOIN).

DB credentials: same convention as taxo_lint.py --data (org secrets
TAXO_DB_HOST/PORT/USER/PASSWORD, or --db-json/--db-env for local runs).
Read-only against information_schema -- no user tables touched, no writes.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

QUERY = """
SELECT
  c.TABLE_SCHEMA, c.TABLE_NAME, c.COLUMN_NAME,
  kcu.REFERENCED_TABLE_SCHEMA, kcu.REFERENCED_TABLE_NAME,
  CASE
    WHEN kcu.REFERENCED_TABLE_SCHEMA = 'taxo' AND kcu.REFERENCED_TABLE_NAME = 'master' THEN 'master'
    WHEN kcu.REFERENCED_TABLE_NAME IS NOT NULL THEN 'tenant'
    ELSE 'none'
  END AS space
FROM information_schema.COLUMNS c
LEFT JOIN information_schema.KEY_COLUMN_USAGE kcu
  ON kcu.TABLE_SCHEMA = c.TABLE_SCHEMA
  AND kcu.TABLE_NAME = c.TABLE_NAME
  AND kcu.COLUMN_NAME = c.COLUMN_NAME
  AND kcu.REFERENCED_TABLE_NAME IS NOT NULL
WHERE RIGHT(c.COLUMN_NAME, 5) = '_idfr'
  AND c.TABLE_SCHEMA IN ('taxo','meta','org_n','purpus_user')
GROUP BY c.TABLE_SCHEMA, c.TABLE_NAME, c.COLUMN_NAME
ORDER BY c.TABLE_SCHEMA, c.TABLE_NAME, c.COLUMN_NAME
"""


# Deliberately duplicated from taxo_lint.py's _load_db_creds (10 lines) rather
# than imported -- keeps this script runnable standalone regardless of CWD/
# sys.path in whatever CI step invokes it.
def _load_db_creds(args):
    if args.db_json:
        with open(args.db_json) as fh:
            env = json.load(fh)[args.db_env]
        return dict(host=env["host"], port=int(env.get("port", 3306)),
                    user=env["user"], password=env["password"])
    host = os.environ.get("TAXO_DB_HOST")
    if not host:
        sys.exit("FATAL: no DB credentials -- pass --db-json/--db-env or set "
                 "TAXO_DB_HOST/PORT/USER/PASSWORD env vars.")
    return dict(host=host, port=int(os.environ.get("TAXO_DB_PORT", "3306")),
                user=os.environ.get("TAXO_DB_USER", ""),
                password=os.environ.get("TAXO_DB_PASSWORD", ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-json", help="Path to bc_mysql_envs.json style creds file.")
    ap.add_argument("--db-env", default="sandbox", help="Env key inside --db-json.")
    ap.add_argument("--out", default="fk_space_map.json")
    args = ap.parse_args()

    import pymysql
    creds = _load_db_creds(args)
    conn = pymysql.connect(connect_timeout=20, **creds)
    cur = conn.cursor()
    cur.execute(QUERY)
    rows = cur.fetchall()
    conn.close()

    columns = {}
    counts = {"master": 0, "tenant": 0, "none": 0}
    for table_schema, table_name, column_name, ref_schema, ref_table, space in rows:
        key = f"{table_schema}.{table_name}.{column_name}"
        entry = {"space": space}
        if ref_schema and ref_table:
            entry["referenced"] = f"{ref_schema}.{ref_table}"
        columns[key] = entry
        counts[space] += 1

    out = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "sandbox information_schema -- see Part67 §5 for the query",
        "counts": {**counts, "total_distinct": len(columns)},
        "columns": columns,
    }
    with open(args.out, "w") as fh:
        json.dump(out, fh, indent=2, sort_keys=True)

    print(f"fk_space_map.json written: {len(columns)} columns "
          f"(master={counts['master']} tenant={counts['tenant']} none={counts['none']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
