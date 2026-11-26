import json

from app.db import 连接
from app.modules import 意图去重键 as 意图核


def 下一票(体: dict) -> dict:
    # 意图去重键收口：同意图（版本+桶+业务世代）唯一存活已下发，
    # 默认世代顶替口径，数据库部分唯一索引兜底双票并行。
    return 意图核.收口下发(体)


def 装下发夹具(体: dict) -> dict:
    批次 = 体.get("批次编号") or f"下发-{意图核.此刻()}"
    结果 = []
    for item in 体.get("下发") or []:
        结果.append(下一票(item))
    conn = 连接()
    try:
        报告 = {"类型": "下发", "结果": 结果}
        conn.execute(
            "INSERT OR REPLACE INTO 装载批次(编号, 时刻, 报告) VALUES (?,?,?)",
            (批次, 意图核.此刻(), json.dumps(报告, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()
    return {"批次编号": 批次, "报告": 报告}
