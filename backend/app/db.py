import os
import sqlite3
from pathlib import Path

数据目录 = Path(os.environ.get("数据目录", Path(__file__).resolve().parent.parent / "data"))
夹具目录 = Path(os.environ.get("夹具目录", Path(__file__).resolve().parent.parent.parent / "fixtures"))
库路径 = 数据目录 / "灰度轨.db"


def _配置(c: sqlite3.Connection) -> None:
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    # 并发双票提交时让写锁等待，而不是立刻 database is locked
    c.execute("PRAGMA busy_timeout = 10000")


def 连接() -> sqlite3.Connection:
    数据目录.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(库路径, timeout=10.0)
    _配置(c)
    return c


def 独占连接(timeout: float = 15.0) -> sqlite3.Connection:
    """自动提交模式 + 显式 BEGIN IMMEDIATE，用于意图收口临界区。

    这样「查存活票 → 作废旧票 → 插新存活票」在同一把写锁内完成，
    跨线程 / 跨进程都不会放出两张已下发；部分唯一索引是最后兜底。
    """
    数据目录.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(库路径, timeout=timeout, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
    return c
