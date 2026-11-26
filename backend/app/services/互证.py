from __future__ import annotations
from app.db import 连接
from app.services.时间 import 此刻


def 空成功扫描(conn) -> list[dict]:
    """已投递却没有任何尝试行 = 空成功壳；有行但无成功行 = 无成功尝试。"""
    rows = conn.execute(
        """
        SELECT e.事件号, e.事件类型, e.端点键, e.状态,
               (SELECT COUNT(*) FROM 投递尝试 a WHERE a.事件号=e.事件号) AS 尝试数,
               (SELECT COUNT(*) FROM 投递尝试 a
                  WHERE a.事件号=e.事件号 AND a.状态='成功') AS 成功数
          FROM 事件 e
         WHERE e.状态='已投递'
         ORDER BY e.更新时 DESC
        """
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if int(d["尝试数"] or 0) == 0:
            d["类别"] = "空成功"
            d["问题"] = "事件标已投递，但投递尝试表无行（空成功壳抬水位）"
            out.append(d)
        elif int(d["成功数"] or 0) == 0:
            d["类别"] = "无成功尝试"
            d["问题"] = "事件标已投递，却没有一条成功尝试行"
            out.append(d)
    return out


def 票账联查(conn):
    """影子归档轨 vs 正式轨（超时尝试 + 旁路凭证）同票核对。"""
    影子 = {r["票号"] for r in conn.execute(
        "SELECT DISTINCT 票号 FROM 归档壳 WHERE 票号 IS NOT NULL AND 票号<>''").fetchall()}
    正式 = {r["票号"] for r in conn.execute(
        "SELECT DISTINCT 票号 FROM 投递尝试 WHERE 是否超时=1 AND 票号<>''").fetchall()}
    try:
        正式 |= {r["票号"] for r in conn.execute(
            "SELECT DISTINCT 票号 FROM 超时凭证 WHERE 票号 IS NOT NULL AND 票号<>''").fetchall()}
    except Exception:
        pass
    影子无正式 = sorted(影子 - 正式)
    正式无影子 = sorted(正式 - 影子)
    联查 = []
    for tk in sorted(影子 | 正式):
        联查.append({
            "票号": tk,
            "在影子归档": tk in 影子,
            "在正式流水": tk in 正式,
            "同票": tk in 影子 and tk in 正式,
        })
    return {
        "影子票数": len(影子), "正式票数": len(正式),
        "影子无正式": 影子无正式, "正式无影子": 正式无影子,
        "同票": not 影子无正式 and not 正式无影子,
        "联查": 联查,
    }


def 跑一次() -> dict:
    t = 此刻()
    conn = 连接()
    try:
        总 = conn.execute("SELECT COUNT(*) AS c FROM 事件").fetchone()["c"]
        已 = conn.execute("SELECT COUNT(*) AS c FROM 事件 WHERE 状态='已投递'").fetchone()["c"]
        死 = conn.execute("SELECT COUNT(*) AS c FROM 事件 WHERE 状态='死信'").fetchone()["c"]
        待 = conn.execute("SELECT COUNT(*) AS c FROM 事件 WHERE 状态 IN ('待投','投递中')").fetchone()["c"]
        匣 = conn.execute("SELECT COUNT(*) AS c FROM 死信 WHERE 状态='待处置'").fetchone()["c"]
        超时数 = conn.execute(
            "SELECT COUNT(*) AS c FROM 投递尝试 WHERE 是否超时=1 OR 说明 LIKE '%超时%'"
        ).fetchone()["c"]

        空成功明细 = 空成功扫描(conn)
        空成功数 = sum(1 for x in 空成功明细 if x["类别"] == "空成功")
        无成功数 = sum(1 for x in 空成功明细 if x["类别"] == "无成功尝试")
        票 = 票账联查(conn)

        旧口径对齐 = bool(死 == 匣 and 已 + 死 + 待 <= 总)
        对齐 = bool(旧口径对齐 and 空成功数 == 0 and 无成功数 == 0 and 票["同票"])

        原因 = []
        if not 旧口径对齐:
            原因.append("死信匣与事件死信数不一致或水位异常")
        if 空成功数:
            原因.append(f"{空成功数} 张空成功壳（已投递却无尝试行）被当成已投，必须剔出水位")
        if 无成功数:
            原因.append(f"{无成功数} 条已投递事件无成功尝试行")
        if not 票["同票"]:
            原因.append(
                f"影子与正式票不同票（影子无正式 {len(票['影子无正式'])}、"
                f"正式无影子 {len(票['正式无影子'])}）")
        说明 = "；".join(原因) if 原因 else "事件/投递/死信口径对齐，无空成功，影子与正式同票"

        conn.execute(
            "INSERT INTO 互证批次(时刻,事件总数,已投递数,死信数,待投数,是否对齐,说明,"
            "空成功数,无成功尝试数,超时数,影子票数,正式票数,同票) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t, 总, 已, 死, 待, 1 if 对齐 else 0, 说明,
             空成功数, 无成功数, 超时数, 票["影子票数"], 票["正式票数"], 1 if 票["同票"] else 0),
        )
        conn.commit()
        return {
            "时刻": t,
            "事件总数": 总,
            "已投递数": 已,
            "死信数": 死,
            "待投数": 待,
            "死信匣待处置": 匣,
            "是否对齐": bool(对齐),
            "是否全部对齐": bool(对齐),
            "同票": 票["同票"],
            "说明": 说明,
            "空成功数": 空成功数,
            "无成功尝试数": 无成功数,
            "超时数": 超时数,
            "影子票数": 票["影子票数"],
            "正式票数": 票["正式票数"],
            "空成功明细": 空成功明细,
            "联查": 票["联查"],
            "影子无正式": 票["影子无正式"],
            "正式无影子": 票["正式无影子"],
            "明细": {"总": 总, "已": 已, "死": 死, "待": 待, "匣": 匣,
                    "空成功": 空成功数, "无成功尝试": 无成功数, "超时": 超时数,
                    "影子票": 票["影子票数"], "正式票": 票["正式票数"]},
        }
    finally:
        conn.close()


# 模块别名：互证全扫空桩接线时可直接调用
全扫 = 跑一次
对账 = 跑一次
