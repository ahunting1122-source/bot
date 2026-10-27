"""Orbit 命令行入口（对标 Hermes 的 ``hermes`` CLI）。

四层分工，边界要守住：

- ``spec.py``     命令的**声明式**注册表：名字 / 帮助 / 选项 / 子命令 / 归类 / 对标优先级。
- ``context.py``  命令运行时上下文：配置、存储、异步桥、输出格式。
- ``main.py``     把注册表编译成 argparse，并统一分发、统一错误出口。
- ``commands/``   每个功能域一个模块，**只写业务**，不写 argparse 样板。

为什么是中央注册表而不是各模块自己 ``add_parser``：

1. ``orbit --help`` 本身就是功能面。对齐 Hermes 时，命令清单必须能被一处列举、
   一处核对——``scripts/parity_report.py`` 直接读这张表生成对照矩阵，
   代码与文档不会各说各话。
2. 归类（core/ops/platform/…）与对标优先级（P0/P1/P2）是**元数据**，不进 argparse，
   但必须和命令放在一起，否则必然漂移。
3. 子命令层级（``orbit config get``）在 argparse 里是嵌套 subparser，手写容易漏
   help、漏别名、漏默认值；声明式之后"加一条命令"= 加一个 ``Command``。
"""

#: 导入命令包 = 填满注册表。放在 ``__init__`` 而不是 ``main()`` 里，是为了让
#: "``import app.cli`` 之后注册表就是完整可读的"成为硬契约——测试与
#: ``scripts/parity_report.py`` 都依赖这一点，漏了就会读到空表。
from app.cli import commands as _commands  # noqa: E402,F401  （导入即注册）
from app.cli.spec import REGISTRY, Command, Option, Registry

__all__ = ["REGISTRY", "Command", "Option", "Registry"]
