import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from app import voice as vc

rows = vc.describe_all(data_dir="data")
print("rows", len(rows))
for r in rows:
    flag = "OK " if r["available"] else "-- "
    print(r["kind"].ljust(14) + str(r["name"]).ljust(14) + flag + str(r["describe"])[:90].replace("\n", " "))
