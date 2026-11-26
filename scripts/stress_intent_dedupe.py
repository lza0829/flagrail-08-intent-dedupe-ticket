#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""意图去重键 —— 外部 HTTP 自动化压测 / 核证脚本（仅标准库）。

覆盖：
  1) 同票号重放（幂等、不抬世代）
  2) 异票号再发（默认「世代顶替」：旧票作废、新票唯一存活；结局可区分）
  3) 双票号真并行提交（Barrier 同时发出）→ 存活已下发至多一张
  4) 回读归属唯一，只挂新生效票（旧票号回读自动改挂）
  5) 核证接口契约：意图键 / 存活票 / 作废票 / valid

用法：
  python3 scripts/stress_intent_dedupe.py [BASE_URL]
环境变量：BASE_URL（默认 http://127.0.0.1:11800）
退出码 0 全过，1 有失败。
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

BASE = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("BASE_URL", "http://127.0.0.1:11800")).rstrip("/")

PASS, FAIL = 0, 0
LINES = []


def log(ok, name, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"PASS  {name}" + (f"  :: {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"FAIL  {name}" + (f"  :: {detail}" if detail else ""))
    LINES.append((ok, name, detail))


def call(method, path, body=None, timeout=30):
    # 路径/查询里的中文做百分号编码（保留 URL 结构符与已有转义）
    path = urllib.parse.quote(path, safe="/?=&%+:;,@")
    url = BASE + path
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"_raw": raw}
    except Exception as e:  # noqa: BLE001
        return 0, {"_error": f"{type(e).__name__}: {e}"}


def wait_health(seconds=40):
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        st, js = call("GET", "/api/健康", timeout=5)
        if st == 200 and js and js.get("正常"):
            print(f"health OK at {BASE}")
            return True
        last = (st, js)
        time.sleep(1)
    print("server not healthy:", last)
    return False


def certify(version, bucket):
    q = urllib.parse.urlencode({"version": version, "bucket": bucket})
    st, js = call("GET", f"/api/intent-dedupe/verify?{q}")
    assert st == 200, f"核证 HTTP {st}: {js}"
    for k in ("意图键", "存活票", "作废票", "valid"):
        assert k in js, f"核证缺少字段 {k}: {list(js)}"
    return js


def setup_intent(uid, tag):
    key = f"stress.http.{tag}.{uid}"
    version = f"{key}@1"
    bucket = f"bucket-{tag}-{uid}"
    st, _ = call("POST", "/api/入轨", {"批次编号": f"b-{tag}-{uid}", "版本": [
        {"开关键": key, "版本号": 1, "版本键": version, "载荷": {"enabled": True}}
    ]})
    assert st == 200, f"入轨失败 {st}"
    st, _ = call("POST", "/api/分桶", {
        "桶键": bucket, "开关键": key, "桶类型": "百分比", "规则": {"percent": 10}})
    assert st == 200, f"分桶失败 {st}"
    return version, bucket


def issue(no, version, bucket):
    return call("POST", "/api/下发", {"票号": no, "版本键": version, "桶键": bucket})


def main():
    if not wait_health():
        return 1

    uid = uuid.uuid4().hex[:8]

    # ---------- 1+2 顺序：同票重放 / 异票再发，探测结局可区分 ----------
    v1, b1 = setup_intent(uid, "seq")
    tA, tB = f"票-http-seq-{uid}-A", f"票-http-seq-{uid}-B"

    s1, r1 = issue(tA, v1, b1)
    s1b, r1b = issue(tA, v1, b1)
    c = certify(v1, b1)
    log(s1 == 200 and s1b == 200 and r1b.get("幂等") is True
        and c["valid"] and len(c["存活票"]) == 1 and c["存活票号"] == [tA]
        and c["当前意图世代"] == 1 and len(c["作废票"]) == 0,
        "同票号重放幂等、不抬世代、仍唯一存活",
        f"status={s1}/{s1b} 存活={c['存活票号']} gen={c['当前意图世代']}")

    s2, r2 = issue(tB, v1, b1)
    c2 = certify(v1, b1)
    旧作废 = tA in c2["作废票号"]
    存活数 = len(c2["存活票号"])
    口径 = c2.get("口径") or (r2 or {}).get("口径")
    log(s2 == 200 and c2["valid"] and 存活数 == 1 and c2["存活票号"] == [tB] and 旧作废,
        "异票号再发：新票唯一存活、旧票作废（世代顶替结局）",
        f"口径={口径} 有效已下发票数={存活数} 旧票作废={旧作废} 存活={c2['存活票号']} 作废={c2['作废票号']}")
    print(f"  [探测区分] 异票号二次后 -> 有效已下发={存活数} 张, 旧票是否作废={旧作废} "
          f"(拒新口径则新票非2xx且旧票仍存活；本实现默认世代顶替)")

    # ---------- 3 全新意图：双票号真并行 ----------
    v2, b2 = setup_intent(uid, "par")
    p1, p2 = f"票-http-par-{uid}-P1", f"票-http-par-{uid}-P2"
    barrier = __import__("threading").Barrier(2)

    def parallel_issue(no):
        barrier.wait()
        return issue(no, v2, b2)

    with ThreadPoolExecutor(max_workers=2) as ex:
        rp1 = ex.submit(parallel_issue, p1)
        rp2 = ex.submit(parallel_issue, p2)
        a = rp1.result(timeout=40)
        b = rp2.result(timeout=40)

    cp = certify(v2, b2)
    存活n = len(cp["存活票号"])
    log(存活n <= 1 and cp["valid"] and 存活n == 1,
        "双票号并行提交后存活已下发至多一张",
        f"HTTP={a[0]}/{b[0]} 存活={cp['存活票号']} 作废={cp['作废票号']} valid={cp['valid']}")

    survivor = cp["存活票号"][0] if 存活n == 1 else None
    loser = p2 if survivor == p1 else p1

    # ---------- 4 回读归属唯一 ----------
    if survivor:
        sR1, jR1 = call("POST", "/api/回读", {"票号": survivor, "边缘键": "edge-http-ok"})
        sR2, jR2 = call("POST", "/api/回读", {"票号": loser, "边缘键": "edge-http-old"})
        cR = certify(v2, b2)
        attrs = set(cR.get("回读归属集合") or [])
        log(sR1 == 200 and sR2 == 200 and cR["valid"]
            and attrs == {survivor} and cR.get("回读归属票号") == survivor
            and (jR2 or {}).get("改挂") is True,
            "回读归属唯一，只挂新生效票（旧票号回读改挂）",
            f"survivor={survivor} attrs={attrs} 旧票改挂={(jR2 or {}).get('改挂')}")

        st, rows = call("GET", "/api/回读证")
        mine = [r for r in rows if r.get("意图键") == cR["意图键"]] if st == 200 and isinstance(rows, list) else []
        canonical = {r.get("票号") for r in mine}
        log(st == 200 and canonical <= {survivor} and len(mine) >= 2,
            "回读证列表中该意图规范票号只挂存活票",
            f"rows={len(mine)} canonical={canonical}")
    else:
        log(False, "回读归属唯一（缺少存活票，跳过）")

    # ---------- 5 核证契约 + 全局 valid ----------
    st, allc = call("GET", "/api/核证?范围=全部")
    log(st == 200 and all(k in allc for k in ("意图键", "存活票", "作废票", "valid"))
        and allc.get("valid") is True,
        "全局核证 valid=true 且契约字段齐全",
        f"intents={allc.get('意图数')} 全局存活={allc.get('存活票数')} 问题={allc.get('问题')}")

    print(f"\n==== 结果：{PASS} 通过 / {FAIL} 失败  BASE={BASE} ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print("ASSERT ERROR:", e)
        sys.exit(2)
