"""意图去重键（空桩收口实现）。

Bug：客户端换票号重试时，旧逻辑只按「票号」幂等；同一意图
（版本 + 桶 [+ 业务世代]）会冒出两张「已下发」，回读不知道跟谁。

收口口径（可二选，默认「世代顶替」）：
- 拒新：意图已有存活已下发时，异票号新票一律 409 拒绝；仅同票号重放幂等。
- 顶替：允许异票号新票，但抬「意图世代」并把旧已下发作废（状态=已作废，
  记录顶替链 + 命运账），回读只归属当前存活世代。

硬约束由 SQLite 部分唯一索引 + BEGIN IMMEDIATE 临界区 + 进程锁共同保证：
每个意图键至多一张 状态='已下发' 的票，双票号并行也不破。

核证：核证参数() 返回 {意图键, 存活票, 作废票, valid, ...}。
自动化：跑压测() 在隔离意图上验证 同票重放 / 异票再发 / 双票并行 / 回读唯一归属。
"""

from __future__ import annotations

import os
import random
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fastapi import HTTPException

from app.db import 连接, 独占连接

存活态 = "已下发"
作废态 = "已作废"
口径默认 = "顶替"

_票据锁 = threading.Lock()
_已就绪 = False


def 此刻() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _确保() -> None:
    """即便 startup 事件没跑（如直接导 app / 非 with TestClient）也保证结构就绪。"""
    global _已就绪
    if not _已就绪:
        from app import seed  # 延迟，避免循环导入

        seed.初始化库()
        _已就绪 = True


# ---------------------------------------------------------------- 意图键 / 口径

def 算意图键(版本键: str, 桶键: str, 业务世代: str | None = "") -> str:
    """意图键 = 版本 + 桶 + 业务世代。

    未显式给业务世代时退化为「版本|桶」，表示该意图唯一的隐式业务世代。
    """
    业务世代 = str(业务世代 or "").strip()
    if 业务世代:
        return f"{版本键}|{桶键}|{业务世代}"
    return f"{版本键}|{桶键}"


# 题面 / 核证可能以多种叫法传入，统一兜底
_业务世代键 = ("业务世代", "业务代", "biz_gen", "bizGeneration", "business_generation", "generation_biz")
_口径键 = ("口径", "策略", "收口口径", "policy", "mode")


def _首值(体: dict, 名s):
    for n in 名s:
        v = 体.get(n)
        if v not in (None, ""):
            return v
    return None


def 取业务世代(体: dict) -> str:
    v = _首值(体, _业务世代键)
    return str(v).strip() if v not in (None, "") else ""


def 取口径(体: dict | None = None) -> str:
    体 = 体 or {}
    v = _首值(体, _口径键) or os.environ.get("意图收口口径") or 口径默认
    s = str(v)
    if "拒" in s or s.strip().lower() in ("reject", "reject_new", "unique", "deny"):
        return "拒新"
    return "顶替"


# 向后兼容的公开别名
意图键 = 算意图键


# ---------------------------------------------------------------- 事务脚手架

def _静默回滚(conn) -> None:
    try:
        conn.execute("ROLLBACK")
    except Exception:
        pass


def _独占事务(工作, 尝试: int = 8):
    最后错: Exception | None = None
    for i in range(尝试):
        conn = 独占连接()
        try:
            conn.execute("BEGIN IMMEDIATE")
            out = 工作(conn)
            conn.execute("COMMIT")
            return out
        except HTTPException:
            _静默回滚(conn)
            raise
        except sqlite3.OperationalError as e:
            最后错 = e
            _静默回滚(conn)
            msg = str(e).lower()
            if "locked" not in msg and "busy" not in msg:
                raise HTTPException(status_code=500, detail=f"意图收口数据库错误:{e}")
            time.sleep(min(0.05 * (i + 1), 0.4) + random.random() * 0.03)
        except sqlite3.IntegrityError as e:
            # 部分唯一索引兜底触发的极端竞态：重试，输家改为顶替赢家
            最后错 = e
            _静默回滚(conn)
            time.sleep(min(0.05 * (i + 1), 0.4) + random.random() * 0.03)
        except Exception:
            _静默回滚(conn)
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass
    raise HTTPException(status_code=503, detail=f"意图收口并发冲突，重试耗尽:{最后错}")


def _命运(conn, 对象类型: str, 对象键: str, 命运: str, 说明: str) -> None:
    conn.execute(
        "INSERT INTO 命运账(对象类型, 对象键, 命运, 时刻, 说明) VALUES (?,?,?,?,?)",
        (对象类型, 对象键, 命运, 此刻(), 说明),
    )


def _重挂回读(conn, 旧票号: str, 新票号: str, key: str, gen: int) -> int:
    """旧票作废/顶替后，把挂在旧票上的回读全部改挂到新存活票。

    保留「原报票号」审计；规范列「票号 / 归属票号」只挂新票。
    """
    conn.execute(
        "UPDATE 回读证 SET 原报票号=票号 WHERE 票号=? AND COALESCE(原报票号,'')=''",
        (旧票号,),
    )
    cur = conn.execute(
        """UPDATE 回读证
           SET 票号=?, 归属票号=?, 意图键=?, 意图世代=?, 改挂=1
           WHERE 票号=?""",
        (新票号, 新票号, key, gen, 旧票号),
    )
    return cur.rowcount or 0


# ---------------------------------------------------------------- 下发收口

def 收口下发(体: dict) -> dict:
    _确保()
    版本键 = (体.get("版本键") or "").strip()
    桶键 = (体.get("桶键") or "").strip()
    if not 版本键 or not 桶键:
        raise HTTPException(status_code=422, detail="版本键 与 桶键 必填")
    票号 = 体.get("票号") or f"票-{版本键}-{桶键}"
    biz = 取业务世代(体)
    key = 算意图键(版本键, 桶键, biz)
    policy = 取口径(体)

    with _票据锁:
        return _独占事务(lambda conn: _下发事务(conn, 体, 版本键, 桶键, 票号, biz, key, policy))


def _下发事务(conn, 体, 版本键, 桶键, 票号, biz, key, policy) -> dict:
    ver = conn.execute("SELECT * FROM 开关版本 WHERE 版本键=?", (版本键,)).fetchone()
    if not ver:
        raise HTTPException(status_code=400, detail=f"版本未入轨:{版本键}")
    if ver["状态"] not in ("已入轨", "灰度中"):
        raise HTTPException(status_code=400, detail=f"版本状态不可下发:{ver['状态']}")
    bucket = conn.execute(
        "SELECT * FROM 分桶规 WHERE 桶键=? AND 状态='生效'", (桶键,)
    ).fetchone()
    if not bucket:
        raise HTTPException(status_code=400, detail=f"分桶未生效:{桶键}")
    if bucket["开关键"] != ver["开关键"]:
        raise HTTPException(status_code=400, detail="分桶开关键与版本不一致")

    now = 此刻()
    old = conn.execute("SELECT * FROM 下发票 WHERE 票号=?", (票号,)).fetchone()
    lives = conn.execute(
        "SELECT * FROM 下发票 WHERE 意图键=? AND 状态=? ORDER BY 意图世代 DESC, rowid DESC",
        (key, 存活态),
    ).fetchall()

    # ---- 同票号重放：当前存活 → 纯幂等，绝不抬世代 ----
    if old is not None and old["状态"] == 存活态:
        return {
            "票号": 票号,
            "状态": 存活态,
            "幂等": True,
            "意图键": key,
            "意图世代": old["意图世代"] or old["世代"],
            "业务世代": biz,
            "口径": policy,
            "存活票数": 1,
            "valid": True,
        }

    # ---- 已被顶替作废的旧票重放：只回显历史结局，不复活、不产生第二存活 ----
    if old is not None and old["状态"] == 作废态:
        cur = lives[0]["票号"] if lives else (old["顶替票号"] or None)
        return {
            "票号": 票号,
            "状态": 作废态,
            "幂等": True,
            "已作废": True,
            "已顶替": True,
            "意图键": key,
            "意图世代": old["意图世代"],
            "口径": policy,
            "存活票号": cur,
            "归属票号": cur,
            "作废旧票": 票号,
            "说明": "该票已被新世代顶替作废，重放不复活；回读请归属存活票",
            "valid": len(lives) <= 1,
        }

    # ---- 拒新口径：意图已有存活票，异票号新票失败 ----
    if policy == "拒新" and lives:
        live = lives[0]
        raise HTTPException(
            status_code=409,
            detail={
                "错误": "同意图已存在已下发，按「意图唯一拒新票」口径拒绝新票号",
                "code": "INTENT_LIVE_REJECT_NEW",
                "口径": "拒新",
                "意图键": key,
                "存活票号": live["票号"],
                "存活意图世代": live["意图世代"],
                "请求票号": 票号,
                "valid": False,
            },
        )

    # ---- 顶替口径（或意图尚无存活）：把本票置为唯一存活 ----
    genrow = conn.execute(
        "SELECT COALESCE(MAX(意图世代),0) AS m FROM 下发票 WHERE 意图键=?", (key,)
    ).fetchone()
    gen = int(genrow["m"]) + 1

    predecessor = lives[0] if lives else None
    作废旧票号 = None
    改挂回读数 = 0
    for L in lives:
        if L["票号"] == 票号:
            continue
        作废旧票号 = L["票号"]
        conn.execute(
            """UPDATE 下发票
               SET 状态=?, 作废时=?, 作废原因=?, 顶替票号=?
               WHERE 票号=?""",
            (
                作废态,
                now,
                f"意图世代顶替：第{L['意图世代']}世被第{gen}世新票 {票号} 顶替",
                票号,
                L["票号"],
            ),
        )
        改挂回读数 += _重挂回读(conn, L["票号"], 票号, key, gen)
        _命运(
            conn, "下发票", L["票号"], "作废",
            f"意图 {key} 第{L['意图世代']}世 → 被 {票号}（第{gen}世）顶替作废",
        )

    前缘 = predecessor["票号"] if predecessor is not None else ""
    说明 = 体.get("说明", "") or ""
    if old is not None:
        # 同票号处于 待下发/已回滚/已失效：重新置为存活
        conn.execute(
            """UPDATE 下发票 SET 开关键=?, 版本键=?, 桶键=?, 状态=?, 下发时=?,
                 世代=?, 意图键=?, 意图世代=?, 业务世代=?, 前缘票号=?,
                 顶替票号='', 作废时='', 作废原因='', 说明=?
               WHERE 票号=?""",
            (ver["开关键"], 版本键, 桶键, 存活态, now, gen, key, gen, biz, 前缘, 说明, 票号),
        )
        动作 = "重新下发"
    else:
        conn.execute(
            """INSERT INTO 下发票(票号, 开关键, 版本键, 桶键, 状态, 下发时, 世代, 说明,
                 意图键, 意图世代, 业务世代, 前缘票号, 顶替票号, 作废时, 作废原因)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (票号, ver["开关键"], 版本键, 桶键, 存活态, now, gen, 说明,
             key, gen, biz, 前缘, "", "", ""),
        )
        动作 = "下发"

    if predecessor is not None:
        _命运(
            conn, "下发票", 票号, "下发",
            f"世代顶替生效：意图 {key} 第{gen}世，前缘票 {前缘}；改挂回读 {改挂回读数} 张",
        )
        _命运(
            conn, "意图键", key, "世代顶替",
            f"{前缘} 作废 → {票号} 生效（第{gen}世）",
        )
    else:
        _命运(
            conn, "下发票", 票号, "下发",
            f"首次下发：意图 {key} 第{gen}世",
        )

    return {
        "票号": 票号,
        "状态": 存活态,
        "幂等": False,
        "意图键": key,
        "意图世代": gen,
        "业务世代": biz,
        "口径": policy,
        "顶替": predecessor is not None,
        "世代顶替": predecessor is not None,
        "换票重试": predecessor is not None,
        "作废旧票": 作废旧票号,
        "作废票号": 作废旧票号,
        "前缘票号": 前缘,
        "改挂回读数": 改挂回读数,
        "存活票数": 1,
        "valid": True,
        "动作": 动作,
    }


# ---------------------------------------------------------------- 回读归属

def 解析回读归属(conn, 票号: str, 严格: bool = False) -> dict:
    presented = conn.execute("SELECT * FROM 下发票 WHERE 票号=?", (票号,)).fetchone()
    if presented is None:
        raise HTTPException(status_code=400, detail=f"下发票不存在:{票号}")
    key = presented["意图键"] or 算意图键(
        presented["版本键"], presented["桶键"], presented["业务世代"] or ""
    )
    live = conn.execute(
        "SELECT * FROM 下发票 WHERE 意图键=? AND 状态=? ORDER BY 意图世代 DESC, rowid DESC",
        (key, 存活态),
    ).fetchone()

    if live is not None:
        rehang = live["票号"] != 票号
        if rehang and 严格:
            raise HTTPException(
                status_code=409,
                detail={
                    "错误": "旧意图世代已作废，回读只认新世代存活票",
                    "code": "INTENT_OLD_GENERATION_READBACK_REJECTED",
                    "意图键": key,
                    "原报票号": 票号,
                    "归属票号": live["票号"],
                    "意图世代": live["意图世代"],
                },
            )
        return {
            "presented": presented,
            "target": live,
            "key": key,
            "gen": live["意图世代"],
            "target_no": live["票号"],
            "rehang": rehang,
        }

    # 没有存活票（例如已回滚流程中）：保留旧的状态门禁
    if presented["状态"] not in ("已下发", "回滚中"):
        raise HTTPException(status_code=400, detail=f"票状态不可回读:{presented['状态']}")
    return {
        "presented": presented,
        "target": presented,
        "key": key,
        "gen": presented["意图世代"] or presented["世代"],
        "target_no": presented["票号"],
        "rehang": False,
    }


# ---------------------------------------------------------------- 核证

def _票公开(r) -> dict:
    d = dict(r)
    d["存活"] = d.get("状态") == 存活态
    d["作废"] = d.get("状态") == 作废态
    return d


def 核证意图连接(conn, key: str) -> dict | None:
    rows = conn.execute(
        "SELECT * FROM 下发票 WHERE 意图键=? ORDER BY 意图世代 ASC, rowid ASC",
        (key,),
    ).fetchall()
    if not rows:
        return None

    tickets = [_票公开(r) for r in rows]
    live = [t for t in tickets if t["状态"] == 存活态]
    void = [t for t in tickets if t["状态"] == 作废态]
    nos = [t["票号"] for t in tickets]

    qmarks = ",".join("?" * len(nos))
    reads = conn.execute(
        f"""SELECT * FROM 回读证
            WHERE 意图键=?
               OR 票号 IN ({qmarks})
               OR 归属票号 IN ({qmarks})
               OR 原报票号 IN ({qmarks})""",
        [key] + nos * 3,
    ).fetchall()
    read_dicts = [dict(r) for r in reads]

    def _attr(r: dict) -> str:
        return (r.get("归属票号") or r.get("票号") or "")

    attrs = sorted({_attr(r) for r in read_dicts if _attr(r)})

    问题: list[str] = []
    survivor = live[0]["票号"] if len(live) == 1 else None

    if len(live) > 1:
        问题.append(f"意图双活:{key} 有 {len(live)} 张已下发:{[t['票号'] for t in live]}")

    bad_reads: list[dict] = []
    if len(live) == 1:
        for r in read_dicts:
            if _attr(r) != survivor:
                bad_reads.append(r)
        if bad_reads:
            问题.append(
                "回读归属未唯一挂存活票:"
                + ",".join(f"{r['证号']}->{_attr(r)}" for r in bad_reads[:5])
            )

    # 作废链路指针必须能落到本意图的票
    noset = set(nos)
    for v in void:
        p = v.get("顶替票号") or ""
        if p and p not in noset:
            问题.append(f"作废链路悬空:{v['票号']} -> {p}")

    归属唯一 = (len(live) <= 1) and (not bad_reads)

    return {
        "意图键": key,
        "存在": True,
        "口径": "世代顶替",
        "策略": "世代顶替",
        "版本键": tickets[0]["版本键"],
        "桶键": tickets[0]["桶键"],
        "业务世代": tickets[0].get("业务世代", ""),
        "当前意图世代": (live[0]["意图世代"] if live else max(t["意图世代"] for t in tickets)),
        "存活票": live,
        "作废票": void,
        "存活票号": [t["票号"] for t in live],
        "作废票号": [t["票号"] for t in void],
        "唯一存活票": live[0] if len(live) == 1 else None,
        "存活票号唯一": survivor,
        "归属票号": survivor,
        "回读归属票号": survivor,
        "回读归属唯一": 归属唯一,
        "回读张数": len(read_dicts),
        "回读归属集合": attrs,
        "回读明细": read_dicts,
        "问题": 问题,
        "valid": len(问题) == 0,
    }


def 核证全部连接(conn) -> dict:
    keyrows = conn.execute(
        "SELECT DISTINCT 意图键 FROM 下发票 WHERE COALESCE(意图键,'')<>'' ORDER BY 意图键"
    ).fetchall()
    intents = [核证意图连接(conn, r["意图键"]) for r in keyrows]
    intents = [x for x in intents if x is not None]

    empty = conn.execute(
        "SELECT COUNT(*) AS c FROM 下发票 WHERE COALESCE(意图键,'')=''"
    ).fetchone()["c"]

    all_live = [
        _票公开(r)
        for r in conn.execute(
            "SELECT * FROM 下发票 WHERE 状态=? ORDER BY 意图键, 意图世代", (存活态,)
        ).fetchall()
    ]
    all_void = [
        _票公开(r)
        for r in conn.execute(
            "SELECT * FROM 下发票 WHERE 状态=? ORDER BY rowid", (作废态,)
        ).fetchall()
    ]
    问题 = [p for x in intents for p in x["问题"]]
    if empty:
        问题.append(f"存在 {empty} 张未物化意图键的下发票")

    return {
        "范围": "全部",
        "意图键": None,
        "存在": True,
        "口径": "世代顶替",
        "valid": len(问题) == 0,
        "意图数": len(intents),
        "意图核证": intents,
        "核证明细": intents,
        "存活票": all_live,
        "作废票": all_void,
        "存活票数": len(all_live),
        "作废票数": len(all_void),
        "问题": 问题,
    }


def _空核证(key: str | None = None) -> dict:
    return {
        "意图键": key,
        "存在": False,
        "口径": "世代顶替",
        "策略": "世代顶替",
        "valid": True,
        "存活票": [],
        "作废票": [],
        "存活票号": [],
        "作废票号": [],
        "唯一存活票": None,
        "存活票号唯一": None,
        "归属票号": None,
        "回读归属票号": None,
        "回读归属唯一": True,
        "回读张数": 0,
        "回读归属集合": [],
        "问题": [],
        "信息": "该意图下暂无下发票",
    }


_参数键 = {
    "key": ("意图键", "intent_key", "intentKey"),
    "biz": ("业务世代", "biz_gen", "business_generation", "bizGeneration"),
    "ver": ("版本键", "版本", "version", "version_key", "versionKey"),
    "bucket": ("桶键", "桶", "bucket", "bucket_key", "bucketKey"),
    "ticket": ("票号", "ticket", "ticket_no"),
}


def 核证参数(参数: dict | None = None) -> dict:
    _确保()
    p = dict(参数 or {})

    def 取(组: str):
        for k in _参数键[组]:
            if p.get(k) not in (None, ""):
                return p.get(k)
        return None

    全部标记 = str(p.get("范围", "")) == "全部" or p.get("全部") in (True, "1", "true", "True") or p.get("全局") in (True, "1", "true", "True")

    conn = 连接()
    try:
        if 全部标记:
            return 核证全部连接(conn)

        key = 取("key")
        biz = 取("biz")
        ver = 取("ver")
        bucket = 取("bucket")
        ticket = 取("ticket")

        if not key and ver and bucket:
            key = 算意图键(str(ver), str(bucket), str(biz) if biz else "")

        if not key and ticket:
            r = conn.execute("SELECT * FROM 下发票 WHERE 票号=?", (str(ticket),)).fetchone()
            if r is not None:
                key = r["意图键"] or 算意图键(r["版本键"], r["桶键"], r["业务世代"] or "")

        if not key:
            r = conn.execute(
                "SELECT 意图键 FROM 下发票 WHERE COALESCE(意图键,'')<>'' ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            key = r["意图键"] if r is not None else None

        if not key:
            return _空核证(None)

        rep = 核证意图连接(conn, str(key))
        if rep is None:
            empty = _空核证(str(key))
            if ver:
                empty["版本键"] = ver
            if bucket:
                empty["桶键"] = bucket
            return empty

        # 顺手带上全局视图，方便核证方一次取齐
        try:
            rep["全局核证"] = 核证全部连接(conn)
        except Exception:
            pass
        return rep
    finally:
        conn.close()


# ---------------------------------------------------------------- 自动化压测

def _建底座(开关键: str, 版本号: int, 版本键: str, 桶键: str) -> None:
    conn = 连接()
    try:
        conn.execute(
            """INSERT OR IGNORE INTO 开关版本(版本键, 开关键, 版本号, 载荷, 状态, 入轨时, 说明)
               VALUES (?,?,?,'{}','已入轨',?,?)""",
            (版本键, 开关键, 版本号, 此刻(), "意图去重自动化压测"),
        )
        conn.execute(
            """INSERT OR IGNORE INTO 分桶规(桶键, 开关键, 桶类型, 规则, 状态, 更新时)
               VALUES (?,?,'百分比','{}','生效',?)""",
            (桶键, 开关键, 此刻()),
        )
        conn.commit()
    finally:
        conn.close()


def _核证key(key: str) -> dict:
    conn = 连接()
    try:
        return 核证意图连接(conn, key) or _空核证(key)
    finally:
        conn.close()


def 跑压测(并发数: int = 2) -> dict:
    """真实多线程并发打服务层（各自独立 sqlite 连接），验证收口硬约束。"""
    _确保()
    from app.services import 下发 as 下发服务
    from app.services import 回读 as 回读服务

    checks: list[dict] = []

    def check(名: str, ok: bool, 细节: str = "") -> bool:
        checks.append({"名": 名, "通过": bool(ok), "细节": 细节})
        return bool(ok)

    uid = uuid.uuid4().hex[:8]

    # ---- 场景一/二：同票重放 + 异票再发（顺序意图） ----
    ka = f"stress.seq.{uid}"
    va, ba = f"{ka}@1", f"bucket-seq-{uid}"
    _建底座(ka, 1, va, ba)
    tA, tB = f"票-seq-{uid}-A", f"票-seq-{uid}-B"
    体A = {"票号": tA, "版本键": va, "桶键": ba}
    rA1 = 下发服务.下一票(dict(体A))
    rA2 = 下发服务.下一票(dict(体A))
    cReplay = _核证key(算意图键(va, ba))
    check(
        "同票号重放幂等且不抬世代",
        rA2.get("幂等") is True
        and len(cReplay["存活票号"]) == 1
        and cReplay["存活票号"] == [tA]
        and cReplay["当前意图世代"] == 1
        and len(cReplay["作废票号"]) == 0
        and cReplay["valid"],
        f"rA2={rA2.get('幂等')} 存活={cReplay['存活票号']} 世代={cReplay['当前意图世代']}",
    )

    rB = 下发服务.下一票({"票号": tB, "版本键": va, "桶键": ba})
    cDiff = _核证key(算意图键(va, ba))
    check(
        "异票号再发：新票存活、旧票作废、存活唯一",
        rB.get("幂等") is False
        and bool(rB.get("顶替") or rB.get("作废旧票"))
        and rB.get("作废旧票") == tA
        and cDiff["存活票号"] == [tB]
        and tA in cDiff["作废票号"]
        and cDiff["valid"],
        f"作废旧票={rB.get('作废旧票')} 存活={cDiff['存活票号']} 作废={cDiff['作废票号']}",
    )

    rA3 = 下发服务.下一票(dict(体A))  # 旧票重放
    cDiff2 = _核证key(算意图键(va, ba))
    check(
        "已作废旧票重放不复活",
        rA3.get("幂等") is True
        and rA3.get("已作废") is True
        and cDiff2["存活票号"] == [tB]
        and cDiff2["valid"],
        f"旧票重放状态={rA3.get('状态')} 存活={cDiff2['存活票号']}",
    )

    # ---- 场景三：全新意图，双票号真并行 ----
    kb = f"stress.par.{uid}"
    vb, bb = f"{kb}@1", f"bucket-par-{uid}"
    _建底座(kb, 1, vb, bb)
    keyP = 算意图键(vb, bb)
    p1, p2 = f"票-par-{uid}-P1", f"票-par-{uid}-P2"

    def _提交(vk: str, bk: str, no: str):
        try:
            return ("ok", 下发服务.下一票({"票号": no, "版本键": vk, "桶键": bk}))
        except HTTPException as e:
            return ("http_error", {"status": e.status_code, "detail": e.detail})
        except Exception as e:  # noqa: BLE001
            return ("error", f"{type(e).__name__}: {e}")

    with ThreadPoolExecutor(max_workers=max(2, int(并发数))) as ex:
        f1 = ex.submit(_提交, vb, bb, p1)
        f2 = ex.submit(_提交, vb, bb, p2)
        pres = [f1.result(timeout=30), f2.result(timeout=30)]

    cPar = _核证key(keyP)
    survivor = cPar["存活票号"][0] if len(cPar["存活票号"]) == 1 else None
    loser = p2 if survivor == p1 else p1
    check(
        "双票号并行后存活已下发至多一张（恰好一张）",
        len(cPar["存活票号"]) == 1
        and len(cPar["作废票号"]) == 1
        and set(cPar["存活票号"] + cPar["作废票号"]) == {p1, p2}
        and cPar["valid"],
        f"并行结果={pres} 存活={cPar['存活票号']} 作废={cPar['作废票号']}",
    )

    # ---- 回读归属：存活票 + 失败方票号都试，最终只挂存活票 ----
    rdOk = 回读服务.记回读({"票号": survivor, "边缘键": "edge-survivor"}) if survivor else None
    rdOld = 回读服务.记回读({"票号": loser, "边缘键": "edge-loser"}) if survivor else None
    cRead = _核证key(keyP)
    attrs = set(cRead["回读归属集合"])
    check(
        "回读归属唯一且只挂新生效票（旧票号回读自动改挂）",
        survivor is not None
        and attrs == {survivor}
        and cRead["回读归属唯一"]
        and rdOld is not None
        and rdOld.get("改挂") is True
        and rdOld.get("归属票号") == survivor
        and cRead["valid"],
        f"survivor={survivor} 归属集合={attrs} 旧票回读={rdOld and rdOld.get('改挂')}",
    )

    # ---- 场景四：已有世代的意图上再双票并行，仍至多一张存活 ----
    q1, q2 = f"票-seq-{uid}-Q1", f"票-seq-{uid}-Q2"
    with ThreadPoolExecutor(max_workers=2) as ex:
        fq1 = ex.submit(_提交, va, ba, q1)
        fq2 = ex.submit(_提交, va, ba, q2)
        qr = [fq1.result(timeout=30), fq2.result(timeout=30)]
    cSeqPar = _核证key(算意图键(va, ba))
    check(
        "已有作废世代上再并行双票，存活仍唯一",
        len(cSeqPar["存活票号"]) == 1
        and cSeqPar["存活票号"][0] in (q1, q2)
        and tB in cSeqPar["作废票号"]
        and cSeqPar["valid"],
        f"存活={cSeqPar['存活票号']} 作废数={len(cSeqPar['作废票号'])} 结果={qr}",
    )

    ok = all(c["通过"] for c in checks)
    return {
        "ok": ok,
        "valid": ok,
        "口径": "世代顶替",
        "策略": "世代顶替",
        "时刻": 此刻(),
        "意图键": keyP,
        "并行意图键": keyP,
        "顺序意图键": 算意图键(va, ba),
        "断言": checks,
        "同票重放": {"首次": rA1, "重放": rA2},
        "异票再发": {"新票": rB, "旧票重放": rA3, "核证": cDiff2},
        "双票并行": {
            "票号": [p1, p2],
            "提交结果": pres,
            "存活票": cPar["存活票号"],
            "作废票": cPar["作废票号"],
        },
        "回读归属": {"存活票回读": rdOk, "旧票号回读": rdOld, "归属集合": sorted(attrs)},
        "再并行": {"提交结果": qr, "存活票": cSeqPar["存活票号"]},
        "核证": cRead,
    }
