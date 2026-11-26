#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hook-06 争用核验：并发双请求打同一把投递锁。

做什么：
  1) 登录；2) 建一个 *.timeout 事件；3) 用栅栏让两个线程“同时”POST /api/投递，
     带同一个幂等票号；4) 核验：只有一条正式尝试行、两响应同尝试号/同票/同锁令牌，
     归档壳/旁路凭证各一张，并跑互证确认同票对齐。

即使两个 HTTP 请求在服务端被串行处理，同一幂等票的后到者也会读到已完成票并同票复用，
绝不二次落账；若时间上重叠，则由进程内单飞合并牵头/跟随。两道闸都保证“不双壳”。

用法：
  python3 并发双请求.py --base http://127.0.0.1:8500
  BASE_URL=http://127.0.0.1:8506 python3 并发双请求.py
"""
import argparse
import json
import os
import sys
import threading
import urllib.request
import urllib.error
from urllib.parse import quote


def _请求(base, method, path, body=None, cookie=None, timeout=15):
    # urllib 要求路径必须是 ASCII，中文路由名需百分号编码（保留 URL 语法字符）
    path = quote(path, safe="/?=&%")
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8")), r.headers
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8")), e.headers
        except Exception:
            return e.code, {}, e.headers


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("BASE_URL", "http://127.0.0.1:8506"))
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="123456")
    args = ap.parse_args()
    base = args.base

    st, login, hdr = _请求(base, "POST", "/api/登录",
                           {"账号": args.user, "密码": args.password})
    if st != 200:
        print("登录失败", st, login)
        return 2
    set_cookie = hdr.get("Set-Cookie", "")
    cookie = set_cookie.split(";", 1)[0] if set_cookie else ""
    print("登录 OK，Cookie:", cookie[:24], "...")

    st, ev, _ = _请求(base, "POST", "/api/事件/入队", {
        "事件类型": "concurrent.double.timeout",
        "端点键": "ep-billing",
        "载荷摘要": "lock-double-request",
        "优先级": "紧急",
    }, cookie)
    if st != 200:
        print("建事件失败", st, ev)
        return 2
    eid = ev["事件号"]
    票 = "HOOK06-CONC-" + eid[-8:]
    print("建事件:", eid, " 同票号:", 票)

    barrier = threading.Barrier(2)
    outs = [None, None]

    def worker(i):
        barrier.wait(timeout=5)
        outs[i] = _请求(base, "POST", "/api/投递",
                        {"事件号": eid, "注入超时": True, "幂等键": 票}, cookie)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    for i, o in enumerate(outs):
        code, rep, _ = o
        print(f"\n请求{i + 1}: HTTP {code}")
        print("  状态=%s 事件状态=%s 尝试号=%s" % (
            rep.get("状态"), rep.get("事件状态"), rep.get("尝试号")))
        print("  超时=%s 票号=%s 锁令牌=%s 同票=%s 复用票=%s" % (
            rep.get("超时"), rep.get("票号"), rep.get("锁令牌"),
            rep.get("同票"), rep.get("复用票")))
        print("  中文说明:", rep.get("中文说明"))

    _, detail, _ = _请求(base, "GET", f"/api/事件/{eid}", cookie=cookie)
    rows = detail.get("尝试", [])
    formal = [r for r in rows]
    archives = detail.get("归档壳", [])
    creds = detail.get("超时凭证", [])

    _, lockinfo, _ = _请求(base, "GET", "/api/锁", cookie=cookie)
    _, recon, _ = _请求(base, "POST", "/api/互证/跑", {}, cookie)

    尝试号集合 = {o[1].get("尝试号") for o in outs}
    锁集合 = {o[1].get("锁令牌") for o in outs}
    票集合 = {o[1].get("票号") for o in outs}

    print("\n================ 核账 ================")
    print("正式尝试行数:", len(formal), "（须为 1，>1 即双壳）")
    print("两响应尝试号集合:", 尝试号集合)
    print("两响应锁令牌集合:", 锁集合, " 票号集合:", 票集合)
    print("归档壳数:", len(archives), " 旁路凭证数:", len(creds))
    print("锁台账:", json.dumps(lockinfo, ensure_ascii=False))
    print("互证对齐:", recon.get("是否对齐"), " 同票:", recon.get("同票"),
          " 空成功数:", recon.get("空成功数"), " 说明:", recon.get("说明"))

    ok = (
        len(formal) == 1
        and len(尝试号集合) == 1
        and len(锁集合) == 1
        and len(票集合) == 1
        and all(o[1].get("状态") != "成功" for o in outs)
        and detail.get("状态") != "已投递"
    )
    print("\n结论:", "✅ 同锁同票，只生一壳，超时未成功" if ok else "❌ 仍有双壳/空成功")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
