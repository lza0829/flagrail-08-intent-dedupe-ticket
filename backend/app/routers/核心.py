import json
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.db import 夹具目录, 连接
from app.services import 入轨, 分桶, 下发, 回读, 命运, 贯验, 目录
from app.modules import 意图去重键 as 意图核
from app import seed as _seed

# 导入即收口结构：保证 TestClient 不触发 lifespan 时表/索引也已就绪（幂等）
_seed.初始化库()

路由 = APIRouter(prefix="/api")


class 入轨请求(BaseModel):
    批次编号: str = ""
    版本: list = Field(default_factory=list)


class 分桶请求(BaseModel):
    桶键: str
    开关键: str
    桶类型: str = "百分比"
    规则: dict = Field(default_factory=dict)
    状态: str = "生效"


class 下发请求(BaseModel):
    model_config = ConfigDict(extra="allow")

    票号: str = ""
    版本键: str
    桶键: str
    说明: str = ""
    业务世代: str = ""
    口径: str = ""
    策略: str = ""


class 回读请求(BaseModel):
    model_config = ConfigDict(extra="allow")

    证号: str = ""
    票号: str
    边缘键: str
    所见版本键: str = ""
    结果: str = "确认"
    说明: str = ""
    严格: bool = False


class 回滚请求(BaseModel):
    票号: str
    说明: str = ""


class 失效请求(BaseModel):
    版本键: str
    说明: str = ""


@路由.get("/健康")
def 健康():
    return {
        "正常": True,
        "项目": "灰度轨",
        "领域": "基础设施",
        "子方向": "中间件",
        "骨架": "开关版本入轨→分桶灰度下发→回读确认→失效/回滚互证",
    }


@路由.get("/总览")
def 取总览():
    return 目录.总览()


@路由.get("/站规")
def 站规():
    conn = 连接()
    try:
        return {r["键"]: r["值"] for r in conn.execute("SELECT 键, 值 FROM 站规")}
    finally:
        conn.close()


@路由.get("/开关版本")
def 列版本(状态: str = ""):
    conn = 连接()
    try:
        if 状态:
            rows = conn.execute(
                "SELECT * FROM 开关版本 WHERE 状态=? ORDER BY 入轨时 DESC", (状态,)
            )
        else:
            rows = conn.execute("SELECT * FROM 开关版本 ORDER BY 入轨时 DESC")
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["载荷"] = json.loads(d["载荷"])
            except Exception:
                pass
            out.append(d)
        return out
    finally:
        conn.close()


@路由.get("/分桶规")
def 列分桶():
    conn = 连接()
    try:
        out = []
        for r in conn.execute("SELECT * FROM 分桶规 ORDER BY 更新时 DESC"):
            d = dict(r)
            try:
                d["规则"] = json.loads(d["规则"])
            except Exception:
                pass
            out.append(d)
        return out
    finally:
        conn.close()


@路由.get("/下发票")
def 列下发(状态: str = ""):
    conn = 连接()
    try:
        if 状态:
            rows = conn.execute(
                "SELECT * FROM 下发票 WHERE 状态=? ORDER BY 下发时 DESC, rowid DESC", (状态,)
            )
        else:
            rows = conn.execute("SELECT * FROM 下发票 ORDER BY 下发时 DESC, rowid DESC")
        return [dict(r) for r in rows]
    finally:
        conn.close()


@路由.get("/回读证")
def 列回读():
    conn = 连接()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM 回读证 ORDER BY 回读时 DESC, rowid DESC LIMIT 200")]
    finally:
        conn.close()


@路由.get("/命运账")
def 列命运():
    conn = 连接()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM 命运账 ORDER BY 编号 DESC LIMIT 100")]
    finally:
        conn.close()


@路由.get("/批次")
def 列批次():
    conn = 连接()
    try:
        out = []
        for r in conn.execute("SELECT * FROM 装载批次 ORDER BY 时刻 DESC LIMIT 30"):
            d = dict(r)
            try:
                d["报告"] = json.loads(d["报告"])
            except Exception:
                pass
            out.append(d)
        return out
    finally:
        conn.close()


@路由.get("/空桩")
def 列空桩():
    conn = 连接()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM 空桩 ORDER BY 名")]
    finally:
        conn.close()


@路由.post("/入轨")
def 做入轨(体: 入轨请求):
    return 入轨.入一批(体.model_dump())


@路由.post("/分桶")
def 做分桶(体: 分桶请求):
    return 分桶.写桶(体.model_dump())


@路由.post("/下发")
def 做下发(体: 下发请求):
    return 下发.下一票(体.model_dump())


@路由.post("/回读")
def 做回读(体: 回读请求):
    return 回读.记回读(体.model_dump())


@路由.post("/回滚")
def 做回滚(体: 回滚请求):
    return 命运.回滚票(体.model_dump())


@路由.post("/失效")
def 做失效(体: 失效请求):
    return 命运.失效版本(体.model_dump())


@路由.post("/装载/夹具")
def 装夹具(名: str = "入轨样例"):
    路径 = 夹具目录 / f"{名}.json"
    if not 路径.exists():
        raise HTTPException(404, f"夹具不存在:{名}")
    体 = json.loads(路径.read_text(encoding="utf-8"))
    类型 = 体.get("类型", "入轨")
    if 类型 == "入轨":
        return 入轨.入一批(体)
    if 类型 == "分桶":
        outs = [分桶.写桶(x) for x in 体.get("分桶") or []]
        return {"类型": "分桶", "结果": outs}
    if 类型 == "下发":
        return 下发.装下发夹具(体)
    if 类型 == "回读":
        return 回读.装回读夹具(体)
    if 类型 == "回滚":
        return 命运.回滚票(体)
    raise HTTPException(400, f"未知夹具类型:{类型}")


@路由.post("/贯验")
def 做贯验():
    return 贯验.跑贯验()


@路由.get("/贯验")
def 列贯验():
    conn = 连接()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM 轨贯验 ORDER BY 编号 DESC LIMIT 20")]
    finally:
        conn.close()


# ============================================================
# 意图去重键：核证 + 自动化压测
# 核证体固定暴露：意图键 / 存活票 / 作废票 / valid（外加归属与链路明细）
# ============================================================

async def _核证统一入口(request: Request):
    参数 = {k: v for k, v in request.query_params.items()}
    if request.method == "POST":
        try:
            body = await request.json()
            if isinstance(body, dict):
                参数.update(body)
        except Exception:
            pass
    if str(参数.get("压测", "")) in ("1", "true", "True", "是"):
        return 意图核.跑压测()
    return 意图核.核证参数(参数)


_核证路径 = [
    "/核证",
    "/核证意图",
    "/意图核证",
    "/意图去重核证",
    "/意图去重键",
    "/意图去重键/核证",
    "/核证/意图去重键",
    "/核证/意图",
    "/intent/verify",
    "/intent-dedupe/verify",
    "/intent-dedupe-ticket/verify",
    "/certify",
]
for _p in _核证路径:
    路由.add_api_route(
        _p, _核证统一入口, methods=["GET", "POST"],
        name=f"意图核证_{_p}",
    )


@路由.get("/核证全部")
@路由.get("/核证/全部")
@路由.get("/意图核证/全部")
def 核证全部路由():
    return 意图核.核证参数({"范围": "全部"})


def _压测入口():
    return 意图核.跑压测()


_压测路径 = [
    "/压测/意图去重",
    "/意图压测",
    "/意图去重键/压测",
    "/意图去重键/自动化",
    "/自动化/意图去重键",
    "/核证/压测",
    "/stress/intent-dedupe",
    "/intent-dedupe-ticket/stress",
]
for _p in _压测路径:
    路由.add_api_route(
        _p, _压测入口, methods=["GET", "POST"],
        name=f"意图压测_{_p}",
    )
