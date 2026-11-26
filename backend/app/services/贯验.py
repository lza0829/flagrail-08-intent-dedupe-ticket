from datetime import datetime, timezone

from app.db import 连接


def 此刻() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def 跑贯验() -> dict:
    conn = 连接()
    try:
        版本数 = conn.execute("SELECT COUNT(*) FROM 开关版本").fetchone()[0]
        下发数 = conn.execute("SELECT COUNT(*) FROM 下发票").fetchone()[0]
        回读数 = conn.execute("SELECT COUNT(*) FROM 回读证").fetchone()[0]
        命运数 = conn.execute("SELECT COUNT(*) FROM 命运账").fetchone()[0]

        问题 = []
        # 已下发票必须有版本
        for r in conn.execute("SELECT 票号, 版本键 FROM 下发票 WHERE 状态='已下发'"):
            if not conn.execute(
                "SELECT 1 FROM 开关版本 WHERE 版本键=?", (r["版本键"],)
            ).fetchone():
                问题.append(f"悬空下发:{r['票号']}")
        # 确认回读必须对应已下发/已回滚票
        for r in conn.execute("SELECT 证号, 票号, 所见版本键, 结果 FROM 回读证 WHERE 结果='确认'"):
            t = conn.execute("SELECT * FROM 下发票 WHERE 票号=?", (r["票号"],)).fetchone()
            if not t:
                问题.append(f"悬空回读:{r['证号']}")
            elif t["版本键"] != r["所见版本键"] and t["状态"] == "已下发":
                问题.append(f"回读版本拧花:{r['证号']}")
        # 已回滚票应有命运条
        for r in conn.execute("SELECT 票号 FROM 下发票 WHERE 状态='已回滚'"):
            if not conn.execute(
                "SELECT 1 FROM 命运账 WHERE 对象类型='下发票' AND 对象键=? AND 命运='回滚'",
                (r["票号"],),
            ).fetchone():
                问题.append(f"回滚无命运:{r['票号']}")
        # 意图去重键：同一意图至多一张有效已下发（禁止双活）
        for r in conn.execute(
            """SELECT 意图键, COUNT(*) AS c FROM 下发票
               WHERE 状态='已下发' AND COALESCE(意图键,'')<>''
               GROUP BY 意图键 HAVING c > 1"""
        ):
            问题.append(f"意图双活:{r['意图键']}x{r['c']}")
        # 回读归属：挂在规范票号上的归属票必须是当前已下发存活票
        for r in conn.execute(
            """SELECT h.证号, COALESCE(NULLIF(h.归属票号,''), h.票号) AS 归属,
                      i.状态 AS 状态
               FROM 回读证 h LEFT JOIN 下发票 i ON i.票号 = COALESCE(NULLIF(h.归属票号,''), h.票号)
               WHERE i.票号 IS NOT NULL"""
        ):
            if r["状态"] not in ("已下发", "已回滚", "回滚中"):
                问题.append(f"回读挂非存活票:{r['证号']}->{r['归属']}({r['状态']})")

        通过 = 1 if not 问题 else 0
        说明 = "齐" if 通过 else "; ".join(问题)
        conn.execute(
            """INSERT INTO 轨贯验(时刻, 版本数, 下发数, 回读数, 命运数, 是否通过, 说明)
               VALUES (?,?,?,?,?,?,?)""",
            (此刻(), 版本数, 下发数, 回读数, 命运数, 通过, 说明),
        )
        conn.commit()
        return {
            "是否通过": bool(通过),
            "版本数": 版本数,
            "下发数": 下发数,
            "回读数": 回读数,
            "命运数": 命运数,
            "说明": 说明,
        }
    finally:
        conn.close()
