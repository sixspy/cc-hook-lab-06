from __future__ import annotations
import hashlib
import hmac
import json
import uuid

from app.db import 连接
from app.services.时间 import 此刻
from app.services import 签名目录
from app.modules import 并发投递锁 as 同锁模块
from app.modules import 投递超时闸 as 超时闸

# 调用方可能使用的超时别名（HTTP body / 关键字参数）
_超时别名 = ("超时", "强制超时", "投递超时", "timeout", "Timeout", "is_timeout")
_票别名 = ("幂等键", "票", "请求键", "idempotency_key", "Idempotency-Key", "key")


def _签名(密钥版本: str, 事件号: str, 摘要: str) -> str:
    raw = f"{密钥版本}|{事件号}|{摘要}".encode("utf-8")
    key = f"hook-secret-{密钥版本}".encode("utf-8")
    return hmac.new(key, raw, hashlib.sha256).hexdigest()[:24]


def _真(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "是", "超时")
    return bool(v)


def _取别名(kw: dict, 别名):
    for k in 别名:
        v = kw.get(k)
        if v not in (None, ""):
            return v
    return None


# ---------------------------------------------------------------------------
# 空成功回刷核验 / 拒绝闸（工程形态 A：旁路凭证 + 拒空成功回刷）
# ---------------------------------------------------------------------------
def 核验_已投递必有成功尝试() -> list[dict]:
    """返回所有“已投递却没有成功尝试行”的事件（空成功壳 / 无成功尝试）。"""
    conn = 连接()
    try:
        rows = conn.execute(
            """
            SELECT e.事件号, e.事件类型, e.状态,
                   (SELECT COUNT(*) FROM 投递尝试 a WHERE a.事件号 = e.事件号) AS 尝试数,
                   (SELECT COUNT(*) FROM 投递尝试 a
                      WHERE a.事件号 = e.事件号 AND a.状态 = '成功') AS 成功数,
                   (SELECT MAX(a.是否超时) FROM 投递尝试 a WHERE a.事件号 = e.事件号) AS 有超时
              FROM 事件 e
             WHERE e.状态 = '已投递'
            """
        ).fetchall()
        违规: list[dict] = []
        for r in rows:
            d = dict(r)
            if int(d["尝试数"] or 0) == 0:
                违规.append({
                    "事件号": d["事件号"], "类别": "空成功",
                    "问题": "事件标已投递，但投递尝试表无行（空成功壳抬水位）",
                })
            elif int(d["成功数"] or 0) == 0:
                违规.append({
                    "事件号": d["事件号"], "类别": "无成功尝试",
                    "问题": "事件标已投递，却没有一条成功尝试行",
                })
        return 违规
    finally:
        conn.close()


def 拒空成功回刷(事件号: str) -> dict:
    """回刷“已投递”前的硬闸：没有成功尝试行就拒绝。"""
    违规 = [v for v in 核验_已投递必有成功尝试() if v["事件号"] == 事件号]
    if 违规:
        raise ValueError("拒绝空成功回刷：" + 违规[0]["问题"])
    return {"事件号": 事件号, "允许回刷": True}


# ---------------------------------------------------------------------------
# 双写两轨（工程形态 B）：正式失败尝试之外，把“超时成功壳”关进只读归档轨
# 该函数是可 monkeypatch 的写入缝（原子性测试会替换它制造中途失败）
# ---------------------------------------------------------------------------
def 写超时归档壳(conn, 壳号: str, 事件号: str, 尝试号: str, 票号: str,
                锁令牌: str, 壳JSON: str, 拒因: str, t: str) -> None:
    conn.execute(
        "INSERT INTO 归档壳(壳号,事件号,尝试号,票号,锁令牌,壳JSON,拒因,轨,入档时) "
        "VALUES (?,?,?,?,?,?,?, '超时只读归档', ?)",
        (壳号, 事件号, 尝试号, 票号, 锁令牌, 壳JSON, 拒因, t),
    )


def 写旁路凭证(conn, 凭证号: str, 事件号: str, 尝试号: str, 票号: str,
              锁令牌: str, 拒因: str, 超时秒: int, 命运: str, t: str) -> None:
    conn.execute(
        "INSERT INTO 超时凭证(凭证号,事件号,尝试号,票号,锁令牌,拒因,超时秒,命运,时刻) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (凭证号, 事件号, 尝试号, 票号, 锁令牌, 拒因, 超时秒, 命运, t),
    )


def _落锁台账(conn, 事件号: str, 票号: str, 锁令牌: str, t: str, 状态: str = "已同锁落账") -> None:
    conn.execute(
        "INSERT INTO 投递锁(事件号,票号,令牌,状态,持有时,更新时) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(事件号) DO UPDATE SET 票号=excluded.票号, 令牌=excluded.令牌, "
        "状态=excluded.状态, 更新时=excluded.更新时",
        (事件号, 票号, 锁令牌, 状态, t, t),
    )


def _落幂等票(conn, 票键, 事件号: str, rep: dict, t: str) -> None:
    if not 票键:
        return
    conn.execute(
        "INSERT INTO 幂等票(票键,事件号,状态,响应JSON,时刻,更新时) VALUES (?,?, '已完成', ?,?,?) "
        "ON CONFLICT(票键) DO UPDATE SET 事件号=excluded.事件号, 状态='已完成', "
        "响应JSON=excluded.响应JSON, 更新时=excluded.更新时",
        (票键, 事件号, json.dumps(rep, ensure_ascii=False), t, t),
    )


def _重读幂等票(票键):
    if not 票键:
        return None
    conn = 连接()
    try:
        row = conn.execute("SELECT * FROM 幂等票 WHERE 票键=?", (票键,)).fetchone()
        if row and row["状态"] == "已完成" and row["响应JSON"]:
            rep = json.loads(row["响应JSON"])
            rep["同票"] = True
            rep["复用票"] = True
            return rep
        return None
    finally:
        conn.close()


def 投递一次(事件号: str, 强制失败: bool = False, 注入超时: bool = False, **kw) -> dict:
    """对事件执行一次签名投递。

    并发安全：同一事件走并发投递锁的单飞合并 —— 并发超时重试/双击只生一壳，
    跟随者拿到牵头者同票结果。顺序重试不重叠，仍每次新增一次尝试。
    """
    超时显式 = _真(注入超时) or any(_真(kw.get(k)) for k in _超时别名 if k in kw)
    幂等键 = _取别名(kw, _票别名)

    # 顺序同票重放（不重叠的重复请求）
    old = _重读幂等票(幂等键)
    if old is not None:
        return old

    单飞键 = f"投递:{事件号}"
    身份, box = 同锁模块.进场(单飞键)
    if 身份 == "跟随":
        return 同锁模块.等待结果(box)
    try:
        rep = _投递核心(事件号, bool(强制失败), 超时显式, 幂等键)
        同锁模块.牵头成功(单飞键, box, rep)
        return rep
    except BaseException as e:  # noqa: BLE001 - 需把异常同票传给跟随者
        同锁模块.牵头失败(单飞键, box, e)
        raise


def _投递核心(事件号: str, 强制失败: bool, 超时显式: bool, 幂等键) -> dict:
    t = 此刻()
    尝试号 = f"TRY-{uuid.uuid4().hex[:10]}"
    壳号 = f"SHELL-{uuid.uuid4().hex[:10]}"
    凭证号 = f"CRED-{uuid.uuid4().hex[:10]}"
    票号 = 幂等键 or f"TICKET-{uuid.uuid4().hex[:12]}"

    with 同锁模块.同锁(事件号, 等待秒=10.0) as 互斥令牌:
        conn = 连接()
        # 该连接改手工事务，确保 BEGIN IMMEDIATE 起到 COMMIT 跨表同成同败
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            # 跨进程/多 worker 兜底：拿到写锁后在事务内复查同票。
            # 入口的 _重读幂等票 与本次复查之间存在窗口；BEGIN IMMEDIATE 已把写者串行化，
            # 故后到者拿到锁时若见同票已完成，直接同票复用，绝不二次落尝试行（不双壳）。
            if 幂等键:
                已落票 = conn.execute(
                    "SELECT 状态,响应JSON FROM 幂等票 WHERE 票键=?", (幂等键,)
                ).fetchone()
                if 已落票 and 已落票["状态"] == "已完成" and 已落票["响应JSON"]:
                    old = json.loads(已落票["响应JSON"])
                    old["同票"] = True
                    old["复用票"] = True
                    conn.execute("ROLLBACK")  # 仅为释放 BEGIN IMMEDIATE 写锁，未做任何写入
                    return old
            ev = conn.execute("SELECT * FROM 事件 WHERE 事件号=?", (事件号,)).fetchone()
            if not ev:
                raise ValueError("无此事件")
            if ev["状态"] in ("已投递", "死信"):
                raise ValueError(f"事件状态为{ev['状态']}，不可再投")
            ep = conn.execute("SELECT * FROM 投递端点 WHERE 端点键=?", (ev["端点键"],)).fetchone()
            if not ep:
                raise ValueError("端点缺失")
            if int(ep["启用"]) != 1:
                raise ValueError("端点停用")

            规 = 签名目录.签名规()
            最大 = int(规.get("最大重试") or 3)
            sig = _签名(ep["密钥版本"], 事件号, ev["载荷摘要"])

            超时, info = 超时闸.是否超时(事件号, ev["事件类型"], 超时显式, conn=conn, 规=规)

            失败次数 = conn.execute(
                "SELECT COUNT(*) AS c FROM 投递尝试 WHERE 事件号=? AND 状态='失败'", (事件号,)
            ).fetchone()["c"]

            强制规则失败 = 强制失败 or str(ev["事件类型"]).endswith(".fail")

            # ---------------- 成功轨 ----------------
            if not 超时 and not 强制规则失败:
                conn.execute(
                    "INSERT INTO 投递尝试(尝试号,事件号,端点键,签名摘要,状态,响应码,说明,投递时,"
                    "是否超时,票号,锁令牌) VALUES (?,?,?,?,?,?,?,?,0,?,?)",
                    (尝试号, 事件号, ev["端点键"], sig, "成功", 200, "投递成功", t, 票号, 互斥令牌),
                )
                conn.execute("UPDATE 事件 SET 状态=?, 更新时=? WHERE 事件号=?", ("已投递", t, 事件号))
                conn.execute("INSERT INTO 操作留痕(时刻,操作,事件号,详情) VALUES (?,?,?,?)",
                             (t, "投递成功", 事件号, 尝试号))
                rep = {
                    "尝试号": 尝试号, "状态": "成功", "事件状态": "已投递", "签名摘要": sig,
                    "超时": False, "超时标记": "", "票号": 票号, "锁令牌": 互斥令牌,
                    "命运": "已投递",
                    "中文说明": "已有成功尝试行，对端确认，事件标记已投递（账实相符）。",
                }
                _落锁台账(conn, 事件号, 票号, 互斥令牌, t)
                _落幂等票(conn, 幂等键, 事件号, rep, t)
                conn.commit()
                return rep

            # ---------------- 失败 / 超时轨 ----------------
            新失败 = 失败次数 + 1
            进匣 = 新失败 >= 最大
            if 超时:
                拒因 = info["拒因"]
                超时秒 = int(info["超时秒"])
                响应码 = int(info["响应码"])
                说明 = f"【超时】{拒因}"
                操作 = "投递超时"
            else:
                拒因 = "投递失败：对端未确认"
                超时秒 = int(规.get("超时秒") or 5)
                响应码 = 500
                说明 = "投递失败"
                操作 = "投递失败"

            if 进匣:
                事件状态 = "死信"
                命运 = ("超时重试耗尽，已进死信匣；超时不得标已投递。" if 超时
                        else "失败重试耗尽，已进死信匣。")
            else:
                事件状态 = "投递中"
                命运 = ("超时已记为一次失败，事件留投递中待重试；超时不得标已投递。" if 超时
                        else "投递失败已记账，事件留投递中待重试。")

            conn.execute(
                "INSERT INTO 投递尝试(尝试号,事件号,端点键,签名摘要,状态,响应码,说明,投递时,"
                "是否超时,票号,锁令牌) VALUES (?,?,?,?, '失败', ?,?,?, ?,?,?)",
                (尝试号, 事件号, ev["端点键"], sig, 响应码, 说明, t,
                 1 if 超时 else 0, 票号, 互斥令牌),
            )

            if 超时:
                # 工程形态 A：旁路凭证记下超时拒因
                写旁路凭证(conn, 凭证号, 事件号, 尝试号, 票号, 互斥令牌,
                           拒因, 超时秒, "进死信" if 进匣 else "记失败待重试", t)
                # 工程形态 B：被拒的“空成功壳”双写到只读归档轨，绝不回刷水位
                空壳 = {
                    "尝试号": 尝试号, "状态": "成功", "事件状态": "已投递",
                    "签名摘要": sig, "超时": True, "是空成功壳": True, "已拒回刷": True,
                    "拒因": 拒因, "票号": 票号,
                }
                写超时归档壳(conn, 壳号, 事件号, 尝试号, 票号, 互斥令牌,
                             json.dumps(空壳, ensure_ascii=False), 拒因, t)

            if 进匣:
                dl = f"DL-{uuid.uuid4().hex[:10]}"
                末次原因 = 拒因 if 超时 else "重试耗尽"
                # 重投后再次失败：旧死信行可能已是「已重投」，须重新打开为待处置，
                # 否则事件回了死信、死信匣却无待处置行，重投链被「死信已处置」卡死。
                conn.execute(
                    "INSERT INTO 死信(死信号,事件号,端点键,失败次数,末次原因,状态,入匣时) "
                    "VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(事件号) DO UPDATE SET 死信号=excluded.死信号, "
                    "端点键=excluded.端点键, 失败次数=excluded.失败次数, "
                    "末次原因=excluded.末次原因, 状态='待处置', 入匣时=excluded.入匣时",
                    (dl, 事件号, ev["端点键"], 新失败, 末次原因, "待处置", t),
                )
                conn.execute("UPDATE 事件 SET 状态=?, 更新时=? WHERE 事件号=?", ("死信", t, 事件号))
            else:
                conn.execute("UPDATE 事件 SET 状态=?, 更新时=? WHERE 事件号=?", ("投递中", t, 事件号))

            conn.execute("INSERT INTO 操作留痕(时刻,操作,事件号,详情) VALUES (?,?,?,?)",
                         (t, 操作, 事件号,
                          (f"超时失败{新失败}/{最大}" if 超时 else f"失败{新失败}/{最大}")))

            rep = {
                "尝试号": 尝试号, "状态": "失败", "事件状态": 事件状态,
                "失败次数": 新失败, "签名摘要": sig,
                "超时": bool(超时), "超时标记": "超时" if 超时 else "",
                "响应码": 响应码, "说明": 说明, "拒因": 拒因 if 超时 else "",
                "票号": 票号, "锁令牌": 互斥令牌, "同票": True,
                "命运": 事件状态, "中文说明": 命运,
                "凭证号": 凭证号 if 超时 else "", "壳号": 壳号 if 超时 else "",
            }
            _落锁台账(conn, 事件号, 票号, 互斥令牌, t)
            _落幂等票(conn, 幂等键, 事件号, rep, t)
            conn.commit()
            return rep
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()
