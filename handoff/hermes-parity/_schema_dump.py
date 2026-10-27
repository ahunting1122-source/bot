"""Read-only schema dump helper used during Hermes->Orbit parity research."""

from __future__ import annotations

import sqlite3
import sys


def dump(path: str, tables_only: bool = False) -> None:
    conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    try:
        names = [
            row[0]
            for row in conn.execute(
                "select name from sqlite_master where type='table' order by name"
            )
        ]
        print(f"# {path}")
        for name in names:
            try:
                count = conn.execute(f'select count(*) from "{name}"').fetchone()[0]
            except sqlite3.Error:
                count = -1
            print(f"  {name:40s} rows={count}")
            if tables_only:
                continue
            try:
                ddl = conn.execute(
                    "select sql from sqlite_master where type='table' and name=?", (name,)
                ).fetchone()
            except sqlite3.Error:
                ddl = None
            if ddl and ddl[0]:
                for line in ddl[0].splitlines():
                    print("      " + line.strip())
    finally:
        conn.close()


if __name__ == "__main__":
    dump(sys.argv[1], tables_only="--tables" in sys.argv)
