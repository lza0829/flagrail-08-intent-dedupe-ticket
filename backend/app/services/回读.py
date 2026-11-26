import json
from datetime import datetime, timezone

from app.db import 连接
from app.modules import 意图去重键 as 意图核


def 此刻() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def 记回读(体: dict) -> dict:
    # 回读归属唯一票：同意图世代顶替后，旧票号回读自动改挂当前存活新票，
    # 规范列「票号 / 归属票号」只挂新生效票；原报票号留审计。
    意图核._确保()
    严格 = bool(体.get("严格") or 体.get("strict") or 体.get("拒旧世代"))
    return 意图核._独占事务(lambda conn: _记回读事务(conn, 体, 严格))


def _记回读事务(conn, 体: dict, 严格: bool) -> dict:
    票号 = 体["票号"]
    归属 = 意图核.解析回读归属(conn, 票号, 严格)
    ticket = 归属["target"]
    target_no = 归属["target_no"]
    key = 归属["key"]
    gen = 归属["gen"]
    rehang = bool(归属["rehang"])

    证号 = 体.get("证号") or f"证-{target_no}-{体['边缘键']}"
    所见 = 体.get("所见版本键") or ticket["版本键"]
    结果 = 体.get("结果", "确认")
    if 结果 == "确认" and 所见 != ticket["版本键"]:
        结果 = "偏差"

    conn.execute(
        """INSERT INTO 回读证(证号, 票号, 边缘键, 所见版本键, 结果, 回读时, 说明,
             意图键, 意图世代, 归属票号, 原报票号, 改挂)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(证号) DO UPDATE SET
             票号=excluded.票号,
             所见版本键=excluded.所见版本键, 结果=excluded.结果,
             回读时=excluded.回读时, 说明=excluded.说明,
             意图键=excluded.意图键, 意图世代=excluded.意图世代,
             归属票号=excluded.归属票号, 改挂=excluded.改挂,
             原报票号=COALESCE(NULLIF(回读证.原报票号,''), excluded.原报票号)""",
        (
            证号, target_no, 体["边缘键"], 所见, 结果, 此刻(), 体.get("说明", ""),
            key, gen, target_no, 票号, 1 if rehang else 0,
        ),
    )
    return {
        "证号": 证号,
        "结果": 结果,
        "票号": target_no,
        "归属票号": target_no,
        "原报票号": 票号,
        "意图键": key,
        "意图世代": gen,
        "改挂": rehang,
        "严格": 严格,
    }


def 装回读夹具(体: dict) -> dict:
    批次 = 体.get("批次编号") or f"回读-{此刻()}"
    结果 = []
    for item in 体.get("回读") or []:
        结果.append(记回读(item))
    conn = 连接()
    try:
        报告 = {"类型": "回读", "结果": 结果}
        conn.execute(
            "INSERT OR REPLACE INTO 装载批次(编号, 时刻, 报告) VALUES (?,?,?)",
            (批次, 此刻(), json.dumps(报告, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()
    return {"批次编号": 批次, "报告": 报告}
