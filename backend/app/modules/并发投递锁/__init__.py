"""并发投递锁（hook-06 争用救绿服务）。

两类原语：
1. 事件互斥锁 获取锁/尝试获取锁/释放锁/同锁 —— 同一事件串行落账，跨线程同锁。
2. 单飞合并 进场/等待/成功/失败 —— 并发双击/超时重试只让牵头落一次账，
   跟随者等待并复用牵头结果（同票），从根上消除“双壳”。

另在投递服务内用 SQLite BEGIN IMMEDIATE + 投递锁/幂等票表做跨连接/跨进程兜底。
"""
from __future__ import annotations

import contextlib
import threading
import uuid

NAME = "并发投递锁"

_互斥守卫 = threading.Lock()
_事件锁: dict[str, dict] = {}

_单飞守卫 = threading.Lock()
_在飞: dict[str, dict] = {}


def 新令牌() -> str:
    return f"LOCK-{uuid.uuid4().hex[:12]}"


def _对象(事件号: str) -> dict:
    with _互斥守卫:
        obj = _事件锁.get(事件号)
        if obj is None:
            obj = {"锁": threading.Lock(), "令牌": None}
            _事件锁[事件号] = obj
        return obj


def 尝试获取锁(事件号: str, 令牌: str | None = None):
    """非阻塞拿锁。拿到返回令牌；已被占用返回 False。"""
    obj = _对象(事件号)
    tok = 令牌 or 新令牌()
    if obj["锁"].acquire(blocking=False):
        with _互斥守卫:
            obj["令牌"] = tok
        return tok
    return False


def 获取锁(事件号: str, 等待秒: float = 10.0) -> str:
    """阻塞拿同一把事件锁，超时抛 TimeoutError。"""
    obj = _对象(事件号)
    tok = 新令牌()
    if obj["锁"].acquire(blocking=True, timeout=等待秒):
        with _互斥守卫:
            obj["令牌"] = tok
        return tok
    raise TimeoutError(f"事件 {事件号} 的投递锁被占用，等待 {等待秒}s 仍未释放")


def 释放锁(事件号: str, 令牌: str | None = None) -> bool:
    obj = _事件锁.get(事件号)
    if obj is None:
        return False
    if 令牌 is not None and obj.get("令牌") not in (None, 令牌):
        return False
    try:
        obj["锁"].release()
    except RuntimeError:
        return False
    with _互斥守卫:
        if obj.get("令牌") == 令牌 or 令牌 is None:
            obj["令牌"] = None
    return True


@contextlib.contextmanager
def 同锁(事件号: str, 等待秒: float = 10.0):
    tok = 获取锁(事件号, 等待秒)
    try:
        yield tok
    finally:
        释放锁(事件号, tok)


def 进场(键: str):
    """单飞：首个调用为 ('牵头', box)，重叠期内的并发调用为 ('跟随', box)。"""
    with _单飞守卫:
        box = _在飞.get(键)
        if box is None:
            box = {"事件": threading.Event(), "结果": None, "异常": None}
            _在飞[键] = box
            return "牵头", box
        return "跟随", box


def 等待结果(box: dict, 超时秒: float = 30.0):
    if not box["事件"].wait(超时秒):
        raise TimeoutError("等待同锁牵头落账超时")
    if box["异常"] is not None:
        raise box["异常"]
    return box["结果"]


# 兼容别名
等待 = 等待结果


def 牵头成功(键: str, box: dict, 结果) -> None:
    box["结果"] = 结果
    box["事件"].set()
    with _单飞守卫:
        if _在飞.get(键) is box:
            _在飞.pop(键, None)


def 牵头失败(键: str, box: dict, 异常: BaseException) -> None:
    box["异常"] = 异常
    box["事件"].set()
    with _单飞守卫:
        if _在飞.get(键) is box:
            _在飞.pop(键, None)


# 兼容短名
成功 = 牵头成功
失败 = 牵头失败


def 活跃单飞() -> list[str]:
    with _单飞守卫:
        return list(_在飞.keys())


def 活跃锁() -> list[dict]:
    out = []
    with _互斥守卫:
        for k, v in _事件锁.items():
            held = v.get("令牌") is not None
            if held:
                out.append({"事件号": k, "锁令牌": v.get("令牌"), "状态": "持有中"})
    return out


class 投递锁:
    """面向对象别名：with 投递锁(事件号) as lock: ..."""

    def __init__(self, 事件号: str, 等待秒: float = 10.0):
        self.事件号 = 事件号
        self.等待秒 = 等待秒
        self.令牌 = None

    def __enter__(self):
        self.令牌 = 获取锁(self.事件号, self.等待秒)
        return self

    def __exit__(self, *exc):
        释放锁(self.事件号, self.令牌)
        return False

    @staticmethod
    def 尝试(事件号: str):
        return 尝试获取锁(事件号)
