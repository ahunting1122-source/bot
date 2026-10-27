"""命令模块集合。导入即注册——``main`` 在编译 parser 之前导入本包。

加新功能域的做法：新建 ``app/cli/commands/<域>.py``，在模块尾部 ``REGISTRY.add(...)``，
然后在这里加一行 import。不要在 ``main`` 里按需导入命令模块，否则 ``--help`` 会漏命令。
"""

from app.cli.commands import core

__all__ = ["core"]
