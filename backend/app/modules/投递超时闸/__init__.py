"""投递超时闸（hook-06 主救绿服务之一）。

骨签：投递超时不得成功。
- 识别三类注入：显式标志、事件类型 *.timeout/.超时、故障注入表（夹具武装）。
- 传输缝 模拟传输 在注入超时时抛 投递超时错误，绝不返回成功壳。
- 给出统一中文拒因，供凭证 / 尝试行 / 页面命运文案共用。
"""
from __future__ import annotations

import uuid

NAME = "投递超时闸"

默认超时秒 = 5
默认响应码 = 504


class 投递超时错误(RuntimeError):
    def __init__(self, 拒因: str, 超时秒: int = 默认超时秒, 响应码: int = 默认响应码):
        super().__init__(拒因)
        self.拒因 = 拒因
        self.超时秒 = 超时秒
        self.响应码 = 响应码


def 类型含超时(事件类型: str | None) -> bool:
    s = str(事件类型 or "").lower()
    return s.endswith(".timeout") or "timeout" in s or "超时" in str(事件类型 or "")


def 标志含超时(标志) -> bool:
    if isinstance(标志, str):
        return 标志.strip().lower() in ("1", "true", "yes", "on", "超时", "是")
    return bool(标志)


def 库内注入超时(conn, 事件号: str) -> dict | None:
    """故障注入表中该事件（或全局）是否武装了投递超时。"""
    try:
        row = conn.execute(
            "SELECT * FROM 故障注入 WHERE 启用=1 AND 种类='投递超时' "
            "AND (事件号=? OR 键='全局:投递超时' OR 事件号='*') "
            "ORDER BY CASE WHEN 事件号=? THEN 0 ELSE 1 END LIMIT 1",
            (事件号, 事件号),
        ).fetchone()
    except Exception:
        return None
    return dict(row) if row else None


def 解析超时秒(规: dict | None, 注入: dict | None = None) -> int:
    try:
        if 注入 and 注入.get("载荷"):
            import json
            payload = json.loads(注入["载荷"])
            if payload.get("超时秒"):
                return int(payload["超时秒"])
    except Exception:
        pass
    try:
        return int((规 or {}).get("超时秒") or 默认超时秒)
    except Exception:
        return 默认超时秒


def 中文拒因(超时秒: int = 默认超时秒, 端点: str = "", 额外: str = "") -> str:
    base = f"投递超时：对端在 {超时秒} 秒内未确认，闸口判失败，拒绝空成功回刷"
    if 端点:
        base = f"投递超时：{端点} 在 {超时秒} 秒内未确认，闸口判失败，拒绝空成功回刷"
    if 额外:
        base += f"（{额外}）"
    return base


def 是否超时(事件号: str, 事件类型: str | None, 显式标志=False, conn=None, 规=None):
    """返回 (是否超时, 拒因信息dict)。"""
    注入 = None
    if conn is not None:
        注入 = 库内注入超时(conn, 事件号)
    命中 = 标志含超时(显式标志) or 类型含超时(事件类型) or 注入 is not None
    if not 命中:
        return False, None
    秒 = 解析超时秒(规, 注入)
    拒因 = (注入 or {}).get("载荷") if False else ""
    原因 = 中文拒因(秒)
    if 注入 and 注入.get("载荷"):
        try:
            import json
            p = json.loads(注入["载荷"])
            原因 = p.get("拒因") or 中文拒因(秒, 额外="夹具注入")
        except Exception:
            原因 = 中文拒因(秒, 额外="夹具注入")
    return True, {"拒因": 原因, "超时秒": 秒, "响应码": 默认响应码, "注入": 注入}


def 模拟传输(事件号: str, 签名摘要: str, 注入超时: bool = False, 超时秒: int = 默认超时秒):
    """可被测试 monkeypatch 的传输缝。默认本地模拟：武装超时则抛错，否则回 200。"""
    if 注入超时:
        raise 投递超时错误(中文拒因(超时秒), 超时秒=超时秒)
    return {"响应码": 200, "说明": "对端已确认"}


# 常见别名，便于不同命名习惯的调用/探测
闸口 = 是否超时
判定 = 是否超时
判定超时 = 是否超时
超时判定 = 是否超时
检查超时 = 是否超时
拦截 = 是否超时
