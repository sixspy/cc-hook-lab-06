from __future__ import annotations
import json
from pathlib import Path
from app.db import 夹具目录, 连接
from app.services.时间 import 此刻


def 列表() -> dict:
    files = []
    if 夹具目录.is_dir():
        for p in sorted(夹具目录.glob("*.json")):
            files.append(p.stem)
    return {"夹具": files}


def 读取(name: str) -> dict:
    p = 夹具目录 / f"{name}.json"
    if not p.is_file():
        # try Chinese names
        for q in 夹具目录.glob("*.json"):
            if q.stem == name or name in q.stem:
                p = q
                break
    if not p.is_file():
        raise FileNotFoundError(name)
    return json.loads(p.read_text(encoding="utf-8"))


def 武装超时注入(事件号: str = "*", 拒因: str | None = None, 超时秒: int = 5) -> dict:
    """把「注入投递超时夹具」的注入块写进故障注入表，供投递超时闸读取。"""
    全局 = not 事件号 or 事件号 in ("*", "全局")
    键 = "全局:投递超时" if 全局 else f"事件:{事件号}"
    目标 = "*" if 全局 else 事件号
    载荷 = json.dumps({
        "投递超时": True, "超时秒": int(超时秒 or 5),
        "拒因": 拒因 or f"投递超时：夹具注入，对端 {超时秒 or 5} 秒内未确认",
    }, ensure_ascii=False)
    conn = 连接()
    try:
        conn.execute(
            "INSERT INTO 故障注入(键,事件号,种类,载荷,启用,时刻) VALUES (?,?, '投递超时', ?, 1, ?) "
            "ON CONFLICT(键) DO UPDATE SET 事件号=excluded.事件号, 载荷=excluded.载荷, "
            "启用=1, 时刻=excluded.时刻",
            (键, 目标, 载荷, 此刻()),
        )
        conn.commit()
        return {"键": 键, "事件号": 目标, "种类": "投递超时", "已武装": True}
    finally:
        conn.close()


def 解除注入(事件号: str | None = None) -> dict:
    conn = 连接()
    try:
        if 事件号:
            conn.execute("UPDATE 故障注入 SET 启用=0 WHERE 事件号=? OR 键=?",
                         (事件号, f"事件:{事件号}"))
        else:
            conn.execute("UPDATE 故障注入 SET 启用=0")
        conn.commit()
        return {"已解除": True, "事件号": 事件号 or "全部"}
    finally:
        conn.close()
