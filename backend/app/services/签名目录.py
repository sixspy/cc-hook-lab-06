from __future__ import annotations
from app.db import 连接
from app.services import 校验


def 签名规() -> dict:
    conn = 连接()
    try:
        rows = conn.execute("SELECT 键,值 FROM 签名规").fetchall()
        return {r["键"]: r["值"] for r in rows}
    finally:
        conn.close()


def 改签名规(body: dict) -> dict:
    conn = 连接()
    try:
        for k, v in body.items():
            键 = 校验.非空("键", k)
            值 = 校验.非空("值", v)
            conn.execute(
                "INSERT INTO 签名规(键,值) VALUES (?,?) ON CONFLICT(键) DO UPDATE SET 值=excluded.值",
                (键, 值),
            )
        conn.commit()
        return 签名规()
    finally:
        conn.close()


def 概览() -> dict:
    conn = 连接()
    try:
        def c(sql):
            return conn.execute(sql).fetchone()[0]
        return {
            "端点数": c("SELECT COUNT(*) FROM 投递端点"),
            "启用端点": c("SELECT COUNT(*) FROM 投递端点 WHERE 启用=1"),
            "事件总数": c("SELECT COUNT(*) FROM 事件"),
            "待投": c("SELECT COUNT(*) FROM 事件 WHERE 状态='待投'"),
            "投递中": c("SELECT COUNT(*) FROM 事件 WHERE 状态='投递中'"),
            "已投递": c("SELECT COUNT(*) FROM 事件 WHERE 状态='已投递'"),
            "死信": c("SELECT COUNT(*) FROM 事件 WHERE 状态='死信'"),
            "尝试数": c("SELECT COUNT(*) FROM 投递尝试"),
            "成功尝试": c("SELECT COUNT(*) FROM 投递尝试 WHERE 状态='成功'"),
            "超时尝试": c("SELECT COUNT(*) FROM 投递尝试 WHERE 是否超时=1 OR 说明 LIKE '%超时%'"),
            "空成功": c("SELECT COUNT(*) FROM 事件 e WHERE e.状态='已投递' AND NOT EXISTS "
                       "(SELECT 1 FROM 投递尝试 a WHERE a.事件号=e.事件号)"),
            "归档壳": c("SELECT COUNT(*) FROM 归档壳"),
            "超时凭证": c("SELECT COUNT(*) FROM 超时凭证"),
            "死信匣": c("SELECT COUNT(*) FROM 死信 WHERE 状态='待处置'"),
            "签名规": 签名规(),
        }
    finally:
        conn.close()


# 事件命运中文案：页面“事件命运 / 中文说明”共用这一份账实同源口径
_命运底案 = {
    "待投": ("待投", "已入队，尚未产生正式尝试，可在投递流水签名投递。"),
    "投递中": ("投递中·待重试", "已占端点但未拿到成功确认；失败/超时均已记账，按退避重试，不标已投递。"),
    "已投递": ("已投递", "已有成功尝试行且对端确认，账实相符。"),
    "死信": ("死信", "失败或超时重试耗尽，已入死信匣，可重投或作废。"),
    "已作废": ("已作废", "该死信已经人工作废，事件结案。"),
}


def _事件命运(conn, d: dict) -> dict:
    eid = d["事件号"]
    stat = conn.execute(
        "SELECT COUNT(*) AS 总, "
        "SUM(CASE WHEN 状态='成功' THEN 1 ELSE 0 END) AS 成, "
        "SUM(CASE WHEN 是否超时=1 OR 说明 LIKE '%超时%' THEN 1 ELSE 0 END) AS 超 "
        "FROM 投递尝试 WHERE 事件号=?", (eid,)).fetchone()
    总 = int(stat["总"] or 0)
    成 = int(stat["成"] or 0)
    超 = int(stat["超"] or 0)
    d["尝试数"] = 总
    d["成功尝试数"] = 成
    d["超时次数"] = 超
    d["空成功"] = bool(d.get("状态") == "已投递" and 总 == 0)

    if d.get("状态") == "已投递" and 总 == 0:
        d["命运"] = "空成功·账实撕裂"
        d["命运色调"] = "danger"
        d["命运说明"] = "事件被标已投递，可投递尝试表没有任何行——这是空成功壳抬水位，互证必判撕裂，须拒回刷。"
    elif d.get("状态") == "已投递" and 成 == 0:
        d["命运"] = "已投递异常·无成功尝试"
        d["命运色调"] = "danger"
        d["命运说明"] = "事件标已投递，却没有成功尝试行，需互证核查。"
    elif d.get("状态") == "投递中" and 超 > 0:
        d["命运"] = "超时失败·待重试"
        d["命运色调"] = "warn"
        d["命运说明"] = "最近投递超时，闸口已记一次失败（或进死信），绝不标已投递；等待同锁重试。"
    else:
        命, 说 = _命运底案.get(d.get("状态"), (d.get("状态") or "—", ""))
        d["命运"] = 命
        d["命运色调"] = {"已投递": "ok", "死信": "danger", "已作废": "muted",
                        "投递中": "info", "待投": "warn"}.get(d.get("状态"), "muted")
        d["命运说明"] = 说
    return d


def 事件列表() -> list:
    conn = 连接()
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM 事件 ORDER BY 入队时 DESC").fetchall()]
        return [_事件命运(conn, d) for d in rows]
    finally:
        conn.close()


def 事件一条(事件号: str) -> dict | None:
    conn = 连接()
    try:
        row = conn.execute("SELECT * FROM 事件 WHERE 事件号=?", (事件号,)).fetchone()
        if not row:
            return None
        d = _事件命运(conn, dict(row))
        d["尝试"] = [dict(x) for x in conn.execute(
            "SELECT * FROM 投递尝试 WHERE 事件号=? ORDER BY 投递时 DESC", (事件号,)
        ).fetchall()]
        dl = conn.execute("SELECT * FROM 死信 WHERE 事件号=?", (事件号,)).fetchone()
        d["死信"] = dict(dl) if dl else None
        d["超时凭证"] = [dict(x) for x in conn.execute(
            "SELECT * FROM 超时凭证 WHERE 事件号=? ORDER BY 时刻 DESC", (事件号,)
        ).fetchall()]
        d["归档壳"] = [dict(x) for x in conn.execute(
            "SELECT * FROM 归档壳 WHERE 事件号=? ORDER BY 入档时 DESC", (事件号,)
        ).fetchall()]
        d["注入"] = [dict(x) for x in conn.execute(
            "SELECT * FROM 故障注入 WHERE 事件号=? AND 启用=1", (事件号,)
        ).fetchall()]
        return d
    finally:
        conn.close()


def 投递列表() -> list:
    conn = 连接()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM 投递尝试 ORDER BY 投递时 DESC LIMIT 200"
        ).fetchall()]
    finally:
        conn.close()


def 死信列表() -> list:
    conn = 连接()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM 死信 ORDER BY 入匣时 DESC").fetchall()]
    finally:
        conn.close()


def 端点列表() -> list:
    conn = 连接()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM 投递端点 ORDER BY 端点键").fetchall()]
    finally:
        conn.close()


def 互证列表() -> list:
    conn = 连接()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM 互证批次 ORDER BY 批次号 DESC LIMIT 50").fetchall()]
    finally:
        conn.close()


def 归档列表() -> list:
    """只读归档轨：被拒的超时成功壳；与现行流水(/api/投递 只读投递尝试)读集合分离。"""
    import json
    conn = 连接()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM 归档壳 ORDER BY 入档时 DESC LIMIT 200").fetchall()]
        for r in rows:
            try:
                r["壳对象"] = json.loads(r.get("壳JSON") or "{}")
            except Exception:
                r["壳对象"] = {}
            r["只读"] = True
            r["轨名"] = "超时只读归档（不计水位）"
        return rows
    finally:
        conn.close()


# 影子轨别名
影子列表 = 归档列表


def 凭证列表() -> list:
    conn = 连接()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM 超时凭证 ORDER BY 时刻 DESC LIMIT 200").fetchall()]
    finally:
        conn.close()


# 旁路凭证别名
旁路凭证列表 = 凭证列表


def 锁列表() -> list:
    conn = 连接()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM 投递锁 ORDER BY 更新时 DESC LIMIT 100").fetchall()]
    finally:
        conn.close()


def 注入列表() -> list:
    conn = 连接()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM 故障注入 ORDER BY 时刻 DESC LIMIT 100").fetchall()]
    finally:
        conn.close()
