"""SQLite 数据层：连接管理、建表、演示数据。"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
IMPORT_DIR = DATA_DIR / "imports"
PHOTO_DIR = DATA_DIR / "photos"
DB_PATH = Path(os.environ.get("ESL_DB", DATA_DIR / "esl.db"))


def now() -> datetime:
    return datetime.now().replace(microsecond=0)


def iso(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"无法解析时间: {s}")


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS stores (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shelves (
    id INTEGER PRIMARY KEY,
    store_id INTEGER NOT NULL REFERENCES stores(id),
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    UNIQUE(store_id, code)
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('operator','manager'))
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    sku TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    spec TEXT NOT NULL,
    min_price REAL NOT NULL,
    max_price REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY,
    tag_no TEXT NOT NULL UNIQUE,
    store_id INTEGER NOT NULL REFERENCES stores(id),
    shelf_id INTEGER NOT NULL REFERENCES shelves(id),
    sku TEXT NOT NULL REFERENCES products(sku),
    online INTEGER NOT NULL DEFAULT 1,
    fail_next INTEGER NOT NULL DEFAULT 0,   -- 演示用：模拟下一次下发失败
    expected_price REAL NOT NULL,          -- 系统期望价（权威）
    display_price REAL NOT NULL,           -- 价签实际显示价（回执驱动）
    display_version INTEGER NOT NULL DEFAULT 0
);

-- 价格版本：每一次期望价变化都追加一行，永久保留
CREATE TABLE IF NOT EXISTS price_versions (
    id INTEGER PRIMARY KEY,
    tag_id INTEGER NOT NULL REFERENCES tags(id),
    version INTEGER NOT NULL,
    price REAL NOT NULL,
    effective_at TEXT NOT NULL,
    change_type TEXT NOT NULL CHECK(change_type IN ('normal','promo','restore','rollback')),
    batch_id INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(tag_id, version)
);

CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY,
    file_name TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    submitted_by INTEGER NOT NULL REFERENCES users(id),
    submitted_at TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    promo_end_at TEXT,
    status TEXT NOT NULL,                  -- validating/validated/rejected/approved/expired
    summary TEXT NOT NULL DEFAULT '{}',
    checked_at TEXT,
    checked_by INTEGER REFERENCES users(id),
    approved_at TEXT,
    approved_by INTEGER REFERENCES users(id),
    reject_reason TEXT
);

CREATE TABLE IF NOT EXISTS import_rows (
    id INTEGER PRIMARY KEY,
    import_id INTEGER NOT NULL REFERENCES imports(id),
    line_no INTEGER NOT NULL,
    tag_no TEXT,
    store_code TEXT,
    sku TEXT,
    new_price REAL,
    promo_end_at TEXT,
    status TEXT NOT NULL,                  -- ok / error / warning
    result_code TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    old_price REAL,
    tag_id INTEGER REFERENCES tags(id),
    UNIQUE(import_id, line_no)
);

CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY,
    no TEXT NOT NULL UNIQUE,
    import_id INTEGER REFERENCES imports(id),
    kind TEXT NOT NULL CHECK(kind IN ('normal','promo_restore','rollback')),
    status TEXT NOT NULL,                  -- pending/partial/succeeded/failed/rolled_back
    created_at TEXT NOT NULL,
    scheduled_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    created_by INTEGER REFERENCES users(id),
    promo_end_at TEXT,
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS batch_items (
    id INTEGER PRIMARY KEY,
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    tag_id INTEGER NOT NULL REFERENCES tags(id),
    store_id INTEGER NOT NULL,
    sku TEXT NOT NULL,
    target_version INTEGER NOT NULL,
    target_price REAL NOT NULL,
    old_version INTEGER,
    old_price REAL,
    item_type TEXT NOT NULL DEFAULT 'normal',  -- normal/promo/restore/rollback
    status TEXT NOT NULL,    -- waiting/success/failed/canceled
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    last_result_code TEXT NOT NULL DEFAULT '',
    ack_at TEXT,
    UNIQUE(batch_id, tag_id)
);

-- 互斥锁：生效中（waiting）的条目占用 (store, sku)
CREATE TABLE IF NOT EXISTS tag_locks (
    id INTEGER PRIMARY KEY,
    store_id INTEGER NOT NULL,
    sku TEXT NOT NULL,
    batch_item_id INTEGER NOT NULL REFERENCES batch_items(id),
    UNIQUE(store_id, sku)
);

CREATE TABLE IF NOT EXISTS rollbacks (
    id INTEGER PRIMARY KEY,
    batch_id INTEGER REFERENCES batches(id),
    reason TEXT NOT NULL,
    operator_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    status TEXT NOT NULL                    -- pending/succeeded/partial/no_op
);

CREATE TABLE IF NOT EXISTS rollback_items (
    id INTEGER PRIMARY KEY,
    rollback_id INTEGER NOT NULL REFERENCES rollbacks(id),
    tag_id INTEGER NOT NULL REFERENCES tags(id),
    from_version INTEGER NOT NULL,
    target_version INTEGER NOT NULL,
    target_price REAL NOT NULL,
    status TEXT NOT NULL,                  -- success/no_op/failed
    result_code TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS inspections (
    id INTEGER PRIMARY KEY,
    tag_no TEXT NOT NULL,
    tag_id INTEGER REFERENCES tags(id),
    observed_price REAL,
    result TEXT NOT NULL,                  -- match/mismatch/tag_not_found/no_photo
    expected_price REAL,
    display_price REAL,
    inspector TEXT NOT NULL DEFAULT '现场店员',
    photo_path TEXT,
    note TEXT NOT NULL DEFAULT '',
    inspected_at TEXT NOT NULL
);
"""


def connect(path: str | os.PathLike | None = None) -> sqlite3.Connection:
    p = Path(path) if path else DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


# ---------------------------------------------------------------- 演示数据

DEMO_USERS = [
    ("u_yang", "杨小运", "operator"),
    ("u_zhou", "周敏", "manager"),
    ("u_wu", "吴启航", "operator"),
]

DEMO_STORES = [
    ("S001", "中山路旗舰店"),
    ("S002", "滨江社区店"),
    ("S003", "高新区科技店"),
]

DEMO_SHELVES = [
    ("S001", "A", "A 区饮料货架"), ("S001", "B", "B 区零食货架"),
    ("S002", "A", "A 区饮料货架"), ("S002", "C", "C 区日用货架"),
    ("S003", "B", "B 区零食货架"), ("S003", "D", "D 区生鲜货架"),
]

DEMO_PRODUCTS = [
    # sku, 名称, 分类, 规格, 最低限价, 最高限价
    ("P1001", "冰露纯净水",   "饮料", "550ml",  1.00,  3.00),
    ("P1002", "可口可乐",     "饮料", "330ml",  2.00,  5.00),
    ("P1003", "康师傅红烧牛肉面", "速食", "105g", 3.00,  6.50),
    ("P1004", "乐事原味薯片", "零食", "70g",   4.00,  9.90),
    ("P1005", "伊利纯牛奶",   "乳品", "250ml", 2.50,  6.00),
    ("P1006", "农夫山泉NFC橙汁", "饮料", "300ml", 6.00, 15.00),
]

# tag_no, store, shelf, sku, 期望价=显示价, 是否在线
DEMO_TAGS = [
    ("ESL-A-0001", "S001", "A", "P1001", 2.00, 1),
    ("ESL-A-0002", "S001", "A", "P1002", 3.50, 1),
    ("ESL-B-0003", "S001", "B", "P1004", 6.90, 1),
    ("ESL-A-0101", "S002", "A", "P1002", 3.50, 1),
    ("ESL-C-0102", "S002", "C", "P1005", 3.80, 1),
    ("ESL-B-0201", "S003", "B", "P1003", 4.50, 0),   # 长期离线
    ("ESL-D-0202", "S003", "D", "P1005", 3.80, 1),
    ("ESL-D-0203", "S003", "D", "P1006", 12.90, 1),
]


def init_db(conn: sqlite3.Connection, reset: bool = False) -> None:
    if reset:
        conn.execute("PRAGMA foreign_keys = OFF")
        for t in (
            "inspections", "rollback_items", "rollbacks", "tag_locks",
            "batch_items", "batches", "import_rows", "imports",
            "price_versions", "tags", "products", "users", "shelves", "stores",
        ):
            conn.execute(f"DROP TABLE IF EXISTS {t}")
        conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    if conn.execute("SELECT COUNT(*) c FROM stores").fetchone()["c"]:
        return
    _seed(conn)


def _seed(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO users(username, display_name, role) VALUES(?,?,?)", DEMO_USERS)
    conn.executemany(
        "INSERT INTO stores(code, name) VALUES(?,?)", DEMO_STORES)
    store_ids = {r["code"]: r["id"] for r in conn.execute("SELECT id, code FROM stores")}
    conn.executemany(
        "INSERT INTO shelves(store_id, code, name) VALUES((SELECT id FROM stores WHERE code=?),?,?)",
        DEMO_SHELVES,
    )
    conn.executemany(
        "INSERT INTO products(sku,name,category,spec,min_price,max_price) VALUES(?,?,?,?,?,?)",
        DEMO_PRODUCTS,
    )
    ver = 1
    for tag_no, scode, shcode, sku, price, online in DEMO_TAGS:
        shelf = conn.execute(
            "SELECT id FROM shelves WHERE store_id=? AND code=?",
            (store_ids[scode], shcode),
        ).fetchone()
        cur = conn.execute(
            """INSERT INTO tags(tag_no,store_id,shelf_id,sku,online,expected_price,
                                 display_price,display_version)
               VALUES(?,?,?,?,?,?,?,0)""",
            (tag_no, store_ids[scode], shelf["id"], sku, online, price, price),
        )
        conn.execute(
            """INSERT INTO price_versions(tag_id,version,price,effective_at,change_type,created_at)
               VALUES(?,1,?,?,'normal',?)""",
            (cur.lastrowid, price, iso(now() - timedelta(days=30)), iso(now() - timedelta(days=30))),
        )
        ver += 1
    conn.commit()


def reset_storage() -> None:
    """删除数据库文件与导入/照片目录下的演示产物。"""
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DB_PATH) + suffix)
        if p.exists():
            p.unlink()
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    for d in (IMPORT_DIR, PHOTO_DIR):
        for f in d.iterdir():
            if f.is_file():
                f.unlink()
