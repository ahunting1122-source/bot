"""``python -m app.cli`` 入口。等价于 ``orbit``。"""

from app.cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
