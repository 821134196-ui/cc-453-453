# -*- coding: utf-8 -*-
"""电子价签批量改价与回滚核验系统 —— SQLite 数据层。

所有表只使用 SQLite 标准库；价格版本(price_versions)是系统期望价的事实来源，
价签表(tags)上的 displayed_price 是价签实际显示价，任何时候两者都被保留。
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根
DATA_DIR = os.path.join(BASE_DIR, "data")
IMPORT_DIR = os.path.join(DATA_DIR, "imports")
PHOTO_DIR = os.path.join(DATA_DIR, "photos")
DB_PATH = os.path.join(DATA_DIR, "esl.db")

# 价格允许浮动范围（相对当前实际生效价），越限即 PRICE_LIMIT
MIN_PRICE_RATIO = 0.30
MAX_PRICE_RATIO = 3.00


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_conn(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.makedirs(IMPORT_DIR, exist_ok=True)
    os.makedirs(PHOTO_DIR, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL                     -- operator / manager / inspector
);

CREATE TABLE IF NOT EXISTS stores (
    id TEXT PRIMARY KEY,                   -- S001
    name TEXT NOT NULL,
    region TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shelves (
    id TEXT PRIMARY KEY,                   -- SH-01-03
    store_id TEXT NOT NULL REFERENCES stores(id),
    zone TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id TEXT PRIMARY KEY,                   -- 100001
    name TEXT NOT NULL,
    barcode TEXT UNIQUE NOT NULL,
    spec TEXT NOT NULL,
    category TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tags (
    id TEXT PRIMARY KEY,                   -- ESL 编号
    store_id TEXT NOT NULL REFERENCES stores(id),
    shelf_id TEXT NOT NULL REFERENCES shelves(id),
    product_id TEXT NOT NULL REFERENCES products(id),
    status TEXT NOT NULL DEFAULT 'online', -- online / offline
    displayed_price REAL,                  -- 价签实际显示价（回执成功后更新）
    displayed_version_id INTEGER,          -- 实际显示版本
    updated_at TEXT,
    UNIQUE(store_id, product_id)           -- 一门店一商品一价签
);

-- 价格版本：系统期望价的完整历史，改价/促销/回滚都只追加新版本
CREATE TABLE IF NOT EXISTS price_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    price REAL NOT NULL,
    change_type TEXT NOT NULL,             -- regular / promo / restore / rollback
    promo_start TEXT,
    promo_end TEXT,
    effective_at TEXT NOT NULL,           -- 期望生效时间
    source_batch_id INTEGER,
    supersedes_version_id INTEGER,
    target_version_id INTEGER,             -- rollback 版本明确指向的历史版本
    created_at TEXT NOT NULL
);

-- 当前期望指针（每门店商品一行），指向 price_versions
CREATE TABLE IF NOT EXISTS current_prices (
    store_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    current_version_id INTEGER NOT NULL,
    PRIMARY KEY (store_id, product_id)
);

CREATE TABLE IF NOT EXISTS import_lists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,             -- IMP-yyyymmddHHMMSS
    filename TEXT NOT NULL,
    submitted_by INTEGER NOT NULL REFERENCES users(id),
    submitted_at TEXT NOT NULL,
    note TEXT,
    status TEXT NOT NULL DEFAULT 'submitted',
        -- submitted / validated / rejected / approved / batched / discarded
    validated_at TEXT,
    approved_by INTEGER REFERENCES users(id),
    approved_at TEXT,
    reject_reason TEXT
);

CREATE TABLE IF NOT EXISTS import_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    import_list_id INTEGER NOT NULL REFERENCES import_lists(id),
    line_no INTEGER NOT NULL,
    store_id TEXT,
    product_id TEXT,
    old_price REAL,
    new_price REAL,                        -- BAD_NUMBER 行允许为空
    effective_at TEXT,
    promo_start TEXT,
    promo_end TEXT,
    check_status TEXT NOT NULL,            -- ok / DUPLICATE_PRODUCT / PRODUCT_NOT_FOUND /
                                           -- STORE_NOT_FOUND / PRICE_LIMIT / TAG_OFFLINE /
                                           -- PROMO_OVERLAP / INVALID_TIME / BATCH_CONFLICT / BAD_NUMBER
    check_message TEXT,
    expected_version_id INTEGER
);

CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,             -- B-...
    import_list_id INTEGER REFERENCES import_lists(id),
    batch_type TEXT NOT NULL,              -- price_change / promo_restore / emergency_rollback
    store_id TEXT,                         -- 回滚批次针对单店
    product_id TEXT,
    target_version_id INTEGER,             -- 回滚目标版本
    status TEXT NOT NULL DEFAULT 'pending',
        -- pending / active / done / partial / failed / cancelled
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    activated_at TEXT,
    finished_at TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS batch_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    store_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    tag_id TEXT,
    old_price REAL,
    new_price REAL NOT NULL,
    version_id INTEGER,                    -- 本明细对应的新版本
    promo_start TEXT,
    promo_end TEXT,
    dispatch_status TEXT NOT NULL DEFAULT 'waiting', -- waiting / success / failed
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    acked_at TEXT,
    UNIQUE(batch_id, store_id, product_id)
);

CREATE TABLE IF NOT EXISTS dispatch_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_item_id INTEGER NOT NULL REFERENCES batch_items(id),
    attempt INTEGER NOT NULL,
    result TEXT NOT NULL,                  -- success / failed
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inspections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,             -- INS-...
    tag_id TEXT NOT NULL REFERENCES tags(id),
    store_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    expected_price REAL NOT NULL,          -- 系统期望价
    actual_price REAL,                     -- 现场录入实际价
    result TEXT NOT NULL,                  -- consistent / inconsistent / offline
    photo_path TEXT,
    inspector TEXT,
    checked_at TEXT NOT NULL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS rollbacks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,             -- RB-...
    batch_id INTEGER REFERENCES batches(id),
    store_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    from_version_id INTEGER NOT NULL,
    target_version_id INTEGER NOT NULL,   -- 明确指向的历史版本
    status TEXT NOT NULL,                  -- success / failed / idempotent_skip
    executed_by TEXT,
    executed_at TEXT NOT NULL,
    note TEXT
);

CREATE INDEX IF NOT EXISTS idx_pv_store_product ON price_versions(store_id, product_id);
CREATE INDEX IF NOT EXISTS idx_bi_status ON batch_items(dispatch_status);
CREATE INDEX IF NOT EXISTS idx_tags_lookup ON tags(store_id, product_id);
"""

# ---------------- 演示数据 ----------------

DEMO_USERS = [
    (1, "u_op", "运营员·小周", "operator"),
    (2, "u_mgr", "审批经理·老陈", "manager"),
    (3, "u_insp", "巡检员·小吴", "inspector"),
]

DEMO_STORES = [
    ("S001", "华润街店", "华东一区"),
    ("S002", "滨江路店", "华东一区"),
]

DEMO_SHELVES = [
    ("SH-01-01", "S001", "主通道"), ("SH-01-02", "S001", "生鲜区"),
    ("SH-01-03", "S001", "粮油区"), ("SH-01-04", "S001", "日化区"),
    ("SH-02-01", "S002", "主通道"), ("SH-02-02", "S002", "饮料区"),
]

DEMO_PRODUCTS = [
    ("100001", "农夫山泉饮用天然水 550ml", "6921168509256", "24瓶/箱", "饮料"),
    ("100002", "可口可乐汽水 330ml",      "6901939621021", "24罐/箱", "饮料"),
    ("100003", "金龙鱼黄金比例调和油 5L",  "6901234567890", "1桶",     "粮油"),
    ("100004", "伊利金典纯牛奶 250ml*12",  "6907992103524", "1提",     "乳品"),
    ("100005", "舒肤佳纯白清香香皂 115g",  "6903148070027", "3块装",   "日化"),
    ("100006", "康师傅红烧牛肉面 五连包",  "6920152400019", "5包/提",  "速食"),
]

# tag_id, store, shelf, product, 初始显示价, 初始状态
DEMO_TAGS = [
    ("ESL-0001", "S001", "SH-01-01", "100001", 2.00, "online"),
    ("ESL-0002", "S001", "SH-01-01", "100002", 3.50, "online"),
    ("ESL-0003", "S001", "SH-01-03", "100003", 69.90, "online"),
    ("ESL-0004", "S001", "SH-01-02", "100004", 65.00, "online"),
    ("ESL-0005", "S001", "SH-01-04", "100005", 12.90, "offline"),  # 离线价签
    ("ESL-0006", "S001", "SH-01-02", "100006", 13.50, "online"),
    ("ESL-0007", "S002", "SH-02-01", "100001", 2.00, "online"),
    ("ESL-0008", "S002", "SH-02-02", "100002", 3.50, "online"),
]

RESULT_LABELS = {
    "ok": "校验通过",
    "DUPLICATE_PRODUCT": "重复商品",
    "PRODUCT_NOT_FOUND": "商品不存在",
    "STORE_NOT_FOUND": "门店不存在",
    "PRICE_LIMIT": "价格越限",
    "TAG_OFFLINE": "价签离线",
    "PROMO_OVERLAP": "促销时段重叠",
    "INVALID_TIME": "生效时间无效",
    "BATCH_CONFLICT": "批次互斥冲突",
    "BAD_NUMBER": "价格格式错误",
}


def init_db(db_path: str | None = None, seed_time: datetime | None = None) -> str:
    """建库并写入演示数据。重复调用会重置（演示系统）。"""
    path = db_path or DB_PATH
    if os.path.exists(path):
        os.remove(path)
    conn = get_conn(path)
    conn.executescript(SCHEMA)
    cur = conn.cursor()
    cur.executemany("INSERT INTO users(id,username,display_name,role) VALUES (?,?,?,?)", DEMO_USERS)
    cur.executemany("INSERT INTO stores VALUES (?,?,?)", DEMO_STORES)
    cur.executemany("INSERT INTO shelves VALUES (?,?,?)", DEMO_SHELVES)
    cur.executemany("INSERT INTO products VALUES (?,?,?,?,?)", DEMO_PRODUCTS)
    base = seed_time or datetime.now()
    ts0 = (base - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    for tag_id, store_id, _shelf, product_id, price, status in DEMO_TAGS:
        cur.execute(
            "INSERT INTO price_versions(store_id,product_id,price,change_type,effective_at,created_at)"
            " VALUES (?,?,?, 'regular', ?, ?)",
            (store_id, product_id, price, ts0, ts0),
        )
        vid = cur.lastrowid
        cur.execute(
            "INSERT INTO tags(id,store_id,shelf_id,product_id,status,displayed_price,"
            "displayed_version_id,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (tag_id, store_id, _shelf, product_id, status, price, vid, ts0),
        )
        cur.execute(
            "INSERT INTO current_prices(store_id,product_id,current_version_id) VALUES (?,?,?)",
            (store_id, product_id, vid),
        )
    conn.commit()
    conn.close()
    return path


if __name__ == "__main__":
    p = init_db()
    print("数据库已初始化:", p)
