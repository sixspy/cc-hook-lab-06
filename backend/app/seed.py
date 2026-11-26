from datetime import datetime, timezone
from app.db import 连接

建表 = """
CREATE TABLE IF NOT EXISTS 用户 (
  账号 TEXT PRIMARY KEY,
  密码哈希 TEXT NOT NULL,
  角色 TEXT NOT NULL DEFAULT '管理员'
);
CREATE TABLE IF NOT EXISTS 签名规 (
  键 TEXT PRIMARY KEY,
  值 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS 投递端点 (
  端点键 TEXT PRIMARY KEY,
  名称 TEXT NOT NULL,
  回调URL TEXT NOT NULL,
  密钥版本 TEXT NOT NULL DEFAULT 'v1',
  启用 INTEGER NOT NULL DEFAULT 1,
  说明 TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS 事件 (
  事件号 TEXT PRIMARY KEY,
  事件类型 TEXT NOT NULL,
  端点键 TEXT NOT NULL DEFAULT '',
  载荷摘要 TEXT NOT NULL DEFAULT '',
  优先级 TEXT NOT NULL DEFAULT '普通',
  状态 TEXT NOT NULL DEFAULT '待投',
  入队时 TEXT NOT NULL,
  更新时 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS 投递尝试 (
  尝试号 TEXT PRIMARY KEY,
  事件号 TEXT NOT NULL,
  端点键 TEXT NOT NULL,
  签名摘要 TEXT NOT NULL DEFAULT '',
  状态 TEXT NOT NULL DEFAULT '失败',
  响应码 INTEGER NOT NULL DEFAULT 0,
  说明 TEXT NOT NULL DEFAULT '',
  投递时 TEXT NOT NULL,
  是否超时 INTEGER NOT NULL DEFAULT 0,
  票号 TEXT NOT NULL DEFAULT '',
  锁令牌 TEXT NOT NULL DEFAULT '',
  FOREIGN KEY (事件号) REFERENCES 事件(事件号),
  FOREIGN KEY (端点键) REFERENCES 投递端点(端点键)
);
CREATE TABLE IF NOT EXISTS 死信 (
  死信号 TEXT PRIMARY KEY,
  事件号 TEXT NOT NULL UNIQUE,
  端点键 TEXT NOT NULL,
  失败次数 INTEGER NOT NULL DEFAULT 0,
  末次原因 TEXT NOT NULL DEFAULT '',
  状态 TEXT NOT NULL DEFAULT '待处置',
  入匣时 TEXT NOT NULL,
  FOREIGN KEY (事件号) REFERENCES 事件(事件号)
);
CREATE TABLE IF NOT EXISTS 互证批次 (
  批次号 INTEGER PRIMARY KEY AUTOINCREMENT,
  时刻 TEXT NOT NULL,
  事件总数 INTEGER NOT NULL,
  已投递数 INTEGER NOT NULL,
  死信数 INTEGER NOT NULL,
  待投数 INTEGER NOT NULL,
  是否对齐 INTEGER NOT NULL,
  说明 TEXT NOT NULL,
  空成功数 INTEGER NOT NULL DEFAULT 0,
  无成功尝试数 INTEGER NOT NULL DEFAULT 0,
  超时数 INTEGER NOT NULL DEFAULT 0,
  影子票数 INTEGER NOT NULL DEFAULT 0,
  正式票数 INTEGER NOT NULL DEFAULT 0,
  同票 INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS 操作留痕 (
  编号 INTEGER PRIMARY KEY AUTOINCREMENT,
  时刻 TEXT NOT NULL,
  操作 TEXT NOT NULL,
  事件号 TEXT NOT NULL DEFAULT '',
  详情 TEXT NOT NULL DEFAULT ''
);
-- hook-06 旁路凭证：记下超时拒因，并作为拒空成功回刷的凭据
CREATE TABLE IF NOT EXISTS 超时凭证 (
  凭证号 TEXT PRIMARY KEY,
  事件号 TEXT NOT NULL,
  尝试号 TEXT NOT NULL DEFAULT '',
  票号 TEXT NOT NULL DEFAULT '',
  锁令牌 TEXT NOT NULL DEFAULT '',
  拒因 TEXT NOT NULL DEFAULT '',
  超时秒 INTEGER NOT NULL DEFAULT 5,
  命运 TEXT NOT NULL DEFAULT '记失败',
  时刻 TEXT NOT NULL
);
-- hook-06 双写两轨之只读归档轨：被拒的“超时成功壳”进这里，不进现行流水
CREATE TABLE IF NOT EXISTS 归档壳 (
  壳号 TEXT PRIMARY KEY,
  事件号 TEXT NOT NULL,
  尝试号 TEXT NOT NULL DEFAULT '',
  票号 TEXT NOT NULL DEFAULT '',
  锁令牌 TEXT NOT NULL DEFAULT '',
  壳JSON TEXT NOT NULL DEFAULT '{}',
  拒因 TEXT NOT NULL DEFAULT '',
  轨 TEXT NOT NULL DEFAULT '超时只读归档',
  入档时 TEXT NOT NULL
);
-- 幂等票：同一重试/双击票号并发，只落一次账，余者同票复用
CREATE TABLE IF NOT EXISTS 幂等票 (
  票键 TEXT PRIMARY KEY,
  事件号 TEXT NOT NULL DEFAULT '',
  状态 TEXT NOT NULL DEFAULT '处理中',
  响应JSON TEXT NOT NULL DEFAULT '',
  时刻 TEXT NOT NULL,
  更新时 TEXT NOT NULL
);
-- 投递锁台账：每个事件一把同锁，脚本/页面可读到同一锁令牌
CREATE TABLE IF NOT EXISTS 投递锁 (
  事件号 TEXT PRIMARY KEY,
  票号 TEXT NOT NULL DEFAULT '',
  令牌 TEXT NOT NULL DEFAULT '',
  状态 TEXT NOT NULL DEFAULT '处理中',
  持有时 TEXT NOT NULL,
  更新时 TEXT NOT NULL
);
-- 故障注入开关（夹具「注入投递超时」据此武装）
CREATE TABLE IF NOT EXISTS 故障注入 (
  键 TEXT PRIMARY KEY,
  事件号 TEXT NOT NULL DEFAULT '',
  种类 TEXT NOT NULL DEFAULT '投递超时',
  载荷 TEXT NOT NULL DEFAULT '',
  启用 INTEGER NOT NULL DEFAULT 1,
  时刻 TEXT NOT NULL
);
"""

# 旧库平滑补列（CREATE TABLE IF NOT EXISTS 不会给已存在的表加列）
补列 = {
    "投递尝试": [
        ("是否超时", "INTEGER NOT NULL DEFAULT 0"),
        ("票号", "TEXT NOT NULL DEFAULT ''"),
        ("锁令牌", "TEXT NOT NULL DEFAULT ''"),
    ],
    "互证批次": [
        ("空成功数", "INTEGER NOT NULL DEFAULT 0"),
        ("无成功尝试数", "INTEGER NOT NULL DEFAULT 0"),
        ("超时数", "INTEGER NOT NULL DEFAULT 0"),
        ("影子票数", "INTEGER NOT NULL DEFAULT 0"),
        ("正式票数", "INTEGER NOT NULL DEFAULT 0"),
        ("同票", "INTEGER NOT NULL DEFAULT 1"),
    ],
}


def 迁移库(conn) -> None:
    for 表, 列们 in 补列.items():
        已有 = {r["name"] for r in conn.execute(f"PRAGMA table_info({表})").fetchall()}
        for 列, 定义 in 列们:
            if 列 not in 已有:
                conn.execute(f"ALTER TABLE {表} ADD COLUMN {列} {定义}")


def 此刻() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def 初始化库() -> None:
    conn = 连接()
    conn.executescript(建表)
    迁移库(conn)
    conn.commit()
    if conn.execute("SELECT COUNT(*) AS c FROM 用户").fetchone()["c"] == 0:
        conn.execute("INSERT INTO 用户(账号,密码哈希,角色) VALUES (?,?,?)", ("admin", "123456", "管理员"))
    if conn.execute("SELECT COUNT(*) AS c FROM 签名规").fetchone()["c"] == 0:
        for k, v in {
            "最大重试": "3",
            "产品名": "投递台",
            "主链": "端点登记→事件入队→签名投递→失败重试与死信→投递账互证",
            "签名算法": "HMAC-SHA256",
            "超时秒": "5",
        }.items():
            conn.execute("INSERT INTO 签名规(键,值) VALUES (?,?)", (k, v))
    if conn.execute("SELECT COUNT(*) AS c FROM 投递端点").fetchone()["c"] == 0:
        for row in [
            ("ep-billing", "账单回调", "https://example.local/hooks/billing", "v1", 1, "账务"),
            ("ep-notify", "通知回调", "https://example.local/hooks/notify", "v1", 1, "通知"),
            ("ep-audit", "审计回调", "https://example.local/hooks/audit", "v2", 1, "审计"),
            ("ep-legacy", "旧版回调", "https://example.local/hooks/legacy", "v1", 0, "停用"),
            ("ep-partner", "合作方回调", "https://partner.local/ingest", "v1", 1, "外部"),
        ]:
            conn.execute(
                "INSERT INTO 投递端点(端点键,名称,回调URL,密钥版本,启用,说明) VALUES (?,?,?,?,?,?)",
                row,
            )
    if conn.execute("SELECT COUNT(*) AS c FROM 事件").fetchone()["c"] == 0:
        t = 此刻()
        for row in [
            ("EVT-demo-01", "invoice.paid", "ep-billing", "invoice=1001", "普通", "待投"),
            ("EVT-demo-02", "user.created", "ep-notify", "user=u42", "普通", "投递中"),
            ("EVT-demo-03", "order.refund", "ep-audit", "order=908", "紧急", "待投"),
            ("EVT-demo-04", "user.updated", "ep-notify", "user=u7", "低", "已投递"),
            ("EVT-demo-05", "invoice.failed", "ep-billing", "invoice=1002", "普通", "死信"),
        ]:
            conn.execute(
                "INSERT INTO 事件(事件号,事件类型,端点键,载荷摘要,优先级,状态,入队时,更新时) VALUES (?,?,?,?,?,?,?,?)",
                (*row, t, t),
            )
        conn.execute(
            "INSERT INTO 投递尝试(尝试号,事件号,端点键,签名摘要,状态,响应码,说明,投递时) VALUES (?,?,?,?,?,?,?,?)",
            ("TRY-demo-01", "EVT-demo-04", "ep-notify", "sig-demo", "成功", 200, "ok", t),
        )
        conn.execute(
            "INSERT INTO 死信(死信号,事件号,端点键,失败次数,末次原因,状态,入匣时) VALUES (?,?,?,?,?,?,?)",
            ("DL-demo-01", "EVT-demo-05", "ep-billing", 3, "连接超时", "待处置", t),
        )
        conn.execute(
            "INSERT INTO 操作留痕(时刻,操作,事件号,详情) VALUES (?,?,?,?)",
            (t, "种子", "", "初始化投递台样例"),
        )
    conn.commit()
    conn.close()
