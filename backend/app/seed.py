from datetime import datetime, timezone

from app.db import 连接

建表 = """
CREATE TABLE IF NOT EXISTS 站规 (
  键 TEXT PRIMARY KEY,
  值 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS 开关版本 (
  版本键 TEXT PRIMARY KEY,
  开关键 TEXT NOT NULL,
  版本号 INTEGER NOT NULL,
  载荷 TEXT NOT NULL DEFAULT '{}',
  状态 TEXT NOT NULL DEFAULT '已入轨',
  入轨时 TEXT NOT NULL,
  说明 TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS 分桶规 (
  桶键 TEXT PRIMARY KEY,
  开关键 TEXT NOT NULL,
  桶类型 TEXT NOT NULL,
  规则 TEXT NOT NULL DEFAULT '{}',
  状态 TEXT NOT NULL DEFAULT '生效',
  更新时 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS 下发票 (
  票号 TEXT PRIMARY KEY,
  开关键 TEXT NOT NULL,
  版本键 TEXT NOT NULL,
  桶键 TEXT NOT NULL,
  状态 TEXT NOT NULL DEFAULT '待下发',
  下发时 TEXT NOT NULL DEFAULT '',
  世代 INTEGER NOT NULL DEFAULT 1,
  说明 TEXT NOT NULL DEFAULT '',
  意图键 TEXT NOT NULL DEFAULT '',
  意图世代 INTEGER NOT NULL DEFAULT 0,
  业务世代 TEXT NOT NULL DEFAULT '',
  前缘票号 TEXT NOT NULL DEFAULT '',
  顶替票号 TEXT NOT NULL DEFAULT '',
  作废时 TEXT NOT NULL DEFAULT '',
  作废原因 TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS 回读证 (
  证号 TEXT PRIMARY KEY,
  票号 TEXT NOT NULL,
  边缘键 TEXT NOT NULL,
  所见版本键 TEXT NOT NULL DEFAULT '',
  结果 TEXT NOT NULL,
  回读时 TEXT NOT NULL,
  说明 TEXT NOT NULL DEFAULT '',
  意图键 TEXT NOT NULL DEFAULT '',
  意图世代 INTEGER NOT NULL DEFAULT 0,
  归属票号 TEXT NOT NULL DEFAULT '',
  原报票号 TEXT NOT NULL DEFAULT '',
  改挂 INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS 命运账 (
  编号 INTEGER PRIMARY KEY AUTOINCREMENT,
  对象类型 TEXT NOT NULL,
  对象键 TEXT NOT NULL,
  命运 TEXT NOT NULL,
  时刻 TEXT NOT NULL,
  说明 TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS 装载批次 (
  编号 TEXT PRIMARY KEY,
  时刻 TEXT NOT NULL,
  报告 TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS 轨贯验 (
  编号 INTEGER PRIMARY KEY AUTOINCREMENT,
  时刻 TEXT NOT NULL,
  版本数 INTEGER NOT NULL,
  下发数 INTEGER NOT NULL,
  回读数 INTEGER NOT NULL,
  命运数 INTEGER NOT NULL,
  是否通过 INTEGER NOT NULL,
  说明 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS 空桩 (
  名 TEXT PRIMARY KEY,
  状态 TEXT NOT NULL DEFAULT '空桩'
);
"""

# 旧库补列（CREATE TABLE IF NOT EXISTS 不会给已存在的表加列）
补列 = {
    "下发票": {
        "世代": "INTEGER NOT NULL DEFAULT 1",
        "意图键": "TEXT NOT NULL DEFAULT ''",
        "意图世代": "INTEGER NOT NULL DEFAULT 0",
        "业务世代": "TEXT NOT NULL DEFAULT ''",
        "前缘票号": "TEXT NOT NULL DEFAULT ''",
        "顶替票号": "TEXT NOT NULL DEFAULT ''",
        "作废时": "TEXT NOT NULL DEFAULT ''",
        "作废原因": "TEXT NOT NULL DEFAULT ''",
    },
    "回读证": {
        "意图键": "TEXT NOT NULL DEFAULT ''",
        "意图世代": "INTEGER NOT NULL DEFAULT 0",
        "归属票号": "TEXT NOT NULL DEFAULT ''",
        "原报票号": "TEXT NOT NULL DEFAULT ''",
        "改挂": "INTEGER NOT NULL DEFAULT 0",
    },
}


def 此刻() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _已有列(conn, 表: str) -> set:
    return {r["name"] for r in conn.execute(f'PRAGMA table_info("{表}")').fetchall()}


def _算意图键(版本键: str, 桶键: str, 业务世代: str = "") -> str:
    业务世代 = (业务世代 or "").strip()
    if 业务世代:
        return f"{版本键}|{桶键}|{业务世代}"
    return f"{版本键}|{桶键}"


def 迁移意图收口(conn) -> None:
    """补列 → 回填意图键/世代 → 收口历史双活 → 建部分唯一索引。幂等。"""
    for 表, 列s in 补列.items():
        现有 = _已有列(conn, 表)
        for 名, ddl in 列s.items():
            if 名 not in 现有:
                conn.execute(f'ALTER TABLE "{表}" ADD COLUMN "{名}" {ddl}')

    # 回填意图键
    for r in conn.execute(
        "SELECT rowid AS rid, 版本键, 桶键, 业务世代 FROM 下发票 WHERE COALESCE(意图键,'')=''"
    ).fetchall():
        key = _算意图键(r["版本键"], r["桶键"], r["业务世代"] or "")
        conn.execute("UPDATE 下发票 SET 意图键=? WHERE rowid=?", (key, r["rid"]))

    # 回填意图世代（按意图内 rowid 稠密编号），并与旧「世代」列对齐
    keys = [r["意图键"] for r in conn.execute(
        "SELECT DISTINCT 意图键 FROM 下发票 WHERE COALESCE(意图键,'')<>''"
    ).fetchall()]
    for key in keys:
        rows = conn.execute(
            "SELECT rowid AS rid FROM 下发票 WHERE 意图键=? ORDER BY 世代 ASC, rowid ASC",
            (key,),
        ).fetchall()
        for i, r in enumerate(rows, 1):
            conn.execute(
                "UPDATE 下发票 SET 意图世代=?, 世代=? WHERE rowid=?",
                (i, i, r["rid"]),
            )

    # 收口历史脏数据：同一意图多张「已下发」只留世代最大（rowid 兜底最新）的一张
    双活 = conn.execute(
        """SELECT 意图键, COUNT(*) AS c FROM 下发票
           WHERE 状态='已下发' AND COALESCE(意图键,'')<>''
           GROUP BY 意图键 HAVING c > 1"""
    ).fetchall()
    now = 此刻()
    for g in 双活:
        key = g["意图键"]
        lives = conn.execute(
            """SELECT 票号 FROM 下发票 WHERE 意图键=? AND 状态='已下发'
               ORDER BY 意图世代 DESC, rowid DESC""",
            (key,),
        ).fetchall()
        存活 = lives[0]["票号"]
        for bad in lives[1:]:
            旧 = bad["票号"]
            conn.execute(
                """UPDATE 下发票 SET 状态='已作废', 作废时=?,
                   作废原因='启动迁移：同一意图存在多张已下发，收口为唯一存活',
                   顶替票号=? WHERE 票号=?""",
                (now, 存活, 旧),
            )
            conn.execute(
                """UPDATE 回读证 SET 原报票号=票号
                   WHERE 票号=? AND COALESCE(原报票号,'')=''""",
                (旧,),
            )
            conn.execute(
                """UPDATE 回读证 SET 票号=?, 归属票号=?, 改挂=1
                   WHERE 票号=?""",
                (存活, 存活, 旧),
            )
            conn.execute(
                "INSERT INTO 命运账(对象类型, 对象键, 命运, 时刻, 说明) VALUES (?,?,?,?,?)",
                ("下发票", 旧, "作废", now, f"迁移收口：意图 {key} 重复已下发，存活票 {存活}"),
            )

    # 硬约束兜底：每个意图键至多一张已下发
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS "ux_下发票_意图唯一存活"
           ON "下发票"("意图键") WHERE "状态"='已下发'"""
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS "ix_下发票_意图键" ON "下发票"("意图键", "意图世代")'
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS "ix_回读证_意图" ON "回读证"("意图键", "归属票号")'
    )


def 初始化库() -> None:
    conn = 连接()
    try:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        conn.executescript(建表)
        迁移意图收口(conn)
        if conn.execute("SELECT COUNT(*) AS c FROM 站规").fetchone()["c"] == 0:
            for k, v in {
                "站名": "灰度轨",
                "领域": "基础设施",
                "子方向": "中间件",
                "默认灰度比": "10",
                "回读齐套策略": "全边缘确认才可晋升",
                "骨架": "开关版本入轨→分桶灰度下发→回读确认→失效/回滚互证",
                "意图收口口径": "世代顶替",
            }.items():
                conn.execute("INSERT INTO 站规(键, 值) VALUES (?, ?)", (k, v))
        else:
            conn.execute(
                "INSERT OR IGNORE INTO 站规(键, 值) VALUES ('意图收口口径', '世代顶替')"
            )
        for name in [
            "分桶比例冲突", "名单与百分比互斥", "回读超时旁置", "半截下发回滚",
            "双版本同键并发", "回读偏差拒升全量", "环境桶隔离", "失效后幽灵回读",
            "下发票幂等键", "回滚窗互斥", "变体载荷契约", "边缘时钟漂移",
            "分桶重切", "全量晋升闸", "影子轨对照", "配置世代戳",
            "回读仲裁策略", "失效写闸", "跨环境串扰", "轨贯验三联",
        ]:
            conn.execute(
                "INSERT OR IGNORE INTO 空桩(名, 状态) VALUES (?, '空桩')", (name,)
            )
        # 本题空桩：意图去重键 —— 标记为已实现（两种口径默认世代顶替）
        conn.execute(
            "INSERT OR IGNORE INTO 空桩(名, 状态) VALUES ('意图去重键', '已实现')"
        )
        conn.execute(
            "UPDATE 空桩 SET 状态='已实现' WHERE 名 IN ('意图去重键', '下发票幂等键')"
        )
        conn.commit()
    finally:
        conn.close()
