# -*- coding: utf-8 -*-
"""
10 万条真实感业务数据生成器。

默认生成到 SQLite（data/shop.db，与演示数据共存——演示行保留，业务量级行新增）；
--mysql 时建库建表并灌入 MySQL（需先在 .env 配置 MYSQL_PASSWORD）。

数据规模：
  products      800
  customers     20,000
  orders        100,000
  order_items   约 26 万（每单 1-3 件）

用法：
  python data/generate_big_data.py                # 生成到 SQLite
  python data/generate_big_data.py --mysql        # 建库建表并灌入 MySQL
  python data/generate_big_data.py --mysql --drop # 先删旧表再灌（全新灌入）
"""
import argparse
import json
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
random.seed(20261026)

N_PRODUCTS = 800
N_CUSTOMERS = 20_000
N_ORDERS = 100_000
BATCH = 5_000

CATS = {
    "音频": [("星声 Buds {ver}", 79, 799), ("澳韵 Mini 音箱 {gen}", 99, 399)],
    "充电": [("安途 快充充电宝 {mah}mAh", 79, 199), ("安途 GaN 充电器 {w}W", 69, 299)],
    "智能穿戴": [("律动 {series} 手环 {gen}", 129, 499), ("律动 Watch {gen}", 399, 1299)],
    "家居": [("云憩 {kind}", 89, 399), ("云憩 智能香薰 {gen}", 99, 299)],
    "手机配件": [("气囊防摔壳 {model}", 29, 69), ("钢化膜 {n}片装", 19, 49)],
    "生活电器": [("颈部按摩仪 {gen}", 149, 399), ("便携剃须刀 {gen}", 99, 299)],
}
TAG_POOL = {
    "音频": ["降噪", "运动", "防水", "蓝牙5.3", "长续航", "高音质"],
    "充电": ["快充", "可登机", "轻薄", "双口", "数显"],
    "智能穿戴": ["心率监测", "GPS", "血氧", "长续航", "运动模式"],
    "家居": ["静音", "大容量", "智能控制", "氛围灯"],
    "手机配件": ["防摔", "高清", "耐拉扯", "多机型"],
    "生活电器": ["便携", "水洗", "定时", "静音"],
}
SURNAMES = "王李张刘陈杨赵黄周吴徐孙马朱胡郭何高林郑谢罗宋唐韩冯于董萧程曹袁邓许傅沈曾彭吕"
GIVEN = ["伟", "芳", "娜", "敏", "静", "磊", "军", "洋", "勇", "艳", "杰", "涛", "明", "超", "秀英", "霞", "平", "刚", "桂英", "文轩", "雨欣", "子豪", "梦琪", "思远"]
CITIES = [("上海市", "浦东新区"), ("北京市", "海淀区"), ("广州市", "天河区"), ("深圳市", "南山区"),
          ("杭州市", "西湖区"), ("成都市", "高新区"), ("武汉市", "洪山区"), ("南京市", "江宁区"),
          ("西安市", "雁塔区"), ("重庆市", "渝北区"), ("苏州市", "工业园区"), ("长沙市", "岳麓区")]
CARRIERS = ["中通", "圆通", "韵达", "申通", "顺丰", "京东物流"]
STATUS_WEIGHTS = [("已签收", 62), ("已发货", 12), ("待发货", 8), ("待付款", 6),
                  ("售后中", 5), ("已退款", 5), ("已取消", 2)]
LOGISTICS_STATES = {"已签收": "已签收", "已发货": "运输中", "待发货": "", "待付款": "",
                    "售后中": "已签收", "已退款": "已签收", "已取消": ""}


def weighted_status() -> str:
    r = random.uniform(0, 100)
    acc = 0
    for s, w in STATUS_WEIGHTS:
        acc += w
        if r <= acc:
            return s
    return "已签收"


def gen_products() -> list[tuple]:
    rows, i = [], 0
    for cat, tpl_list in CATS.items():
        for tpl, lo, hi in tpl_list:
            for _ in range(N_PRODUCTS // (len(CATS) * len(tpl_list))):
                i += 1
                name = tpl.format(ver=random.choice("Pro Air Max Plus".split()),
                                  gen=random.randint(2, 9), mah=random.choice([10000, 20000, 30000]),
                                  w=random.choice([20, 30, 45, 65]), model=random.choice(["iPhone", "安卓通用", "通用"]),
                                  kind=random.choice(["超声波加湿器 3L", "直流循环扇", "桌面暖风机"]),
                                  series=random.choice(["Fit", "Band", "Air"]),
                                  n=random.choice([2, 3, 5]))
                price = round(random.uniform(lo, hi) / 10) * 10 - 1
                tags = random.sample(TAG_POOL[cat], k=random.randint(2, 4))
                rows.append((f"SKU-{9000 + i}", f"小铺严选 {name}", cat, price,
                             random.choices([0, 5, 30, 80, 200, 800],
                                            weights=[2, 3, 20, 30, 30, 15])[0],
                             json.dumps(tags, ensure_ascii=False),
                             round(random.uniform(4.0, 4.9), 1),
                             random.choices(["在售", "下架"], weights=[95, 5])[0],
                             f"{name}。{random.choice(TAG_POOL[cat])}设计，适合{random.choice(['通勤', '居家', '运动', '差旅'])}场景。"))
    return rows


def gen_customers() -> list[tuple]:
    rows = []
    base = datetime(2026, 1, 1)
    for i in range(1, N_CUSTOMERS + 1):
        name = random.choice(SURNAMES) + random.choice(GIVEN)
        phone = f"{random.choice('13578')}{''.join(str(random.randint(0, 9)) for _ in range(4))}****{''.join(str(random.randint(0, 9)) for _ in range(4))}"
        reg = base + timedelta(days=random.randint(0, 295), minutes=random.randint(0, 1440))
        rows.append((f"C{i:05d}", name, phone,
                     random.choices([0, 1, 2], weights=[80, 15, 5])[0],
                     random.randint(0, 5000), reg.strftime("%Y-%m-%d %H:%M")))
    return rows


def _order_row(i: int, cust: tuple, products: list[tuple]):
    created = datetime(2026, 1, 1) + timedelta(days=random.randint(0, 295),
                                               minutes=random.randint(0, 1439))
    status = weighted_status()
    city, district = random.choice(CITIES)
    address = f"{city}{district}{random.choice('幸福路和平街望江道科技园'.split())}{random.randint(1, 999)}号{random.choice('ABC')}{random.randint(1, 30)}"
    pay = ship = receipt = None
    carrier = track = logi_status = logi_note = ""
    logi_updated = None
    if status != "待付款":
        pay = created + timedelta(minutes=random.randint(1, 30))
    if status in ("已发货", "已签收", "售后中", "已退款"):
        ship = (pay or created) + timedelta(hours=random.randint(2, 40))
        carrier = random.choice(CARRIERS)
        track = f"{carrier[0]}T{''.join(str(random.randint(0, 9)) for _ in range(11))}"
        ship_days = random.randint(1, 5)
        receipt = ship + timedelta(days=ship_days, hours=random.randint(0, 20))
        if receipt > datetime(2026, 10, 26):
            receipt = None
            logi_status = random.choice(["运输中", "停滞"])
            logi_updated = ship + timedelta(days=1)
            logi_note = (f"包裹到达【{city}转运中心】，此后无新的物流更新"
                         if logi_status == "停滞" else "运输中，预计近期送达")
        else:
            logi_status = "已签收"
            logi_updated = receipt
            logi_note = random.choice(["本人签收", "驿站代收", "快递柜签收"])
    if status in ("待付款", "已取消"):
        carrier = track = logi_status = logi_note = ""
        pay = None
    stall = 1 if logi_status == "停滞" else 0
    n_items = random.choices([1, 2, 3], weights=[55, 30, 15])[0]
    items = random.sample(products, k=n_items)
    total = round(sum(p[3] for p in items), 2)
    return (f"SO{created.strftime('%Y%m%d')}{i:07d}", cust[0], status, total,
            fmt(pay), fmt(ship), carrier, track, logi_status, fmt(logi_updated),
            logi_note, fmt(receipt), "", cust[1], cust[2], address,
            fmt(created), stall), [(f"SO{created.strftime('%Y%m%d')}{i:07d}", p[0],
                                    1, p[3], {"售后中": "售后中", "已退款": "已退款"}.get(status, "正常"))
                                   for p in items]


def fmt(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mysql", action="store_true", help="灌入 MySQL（默认 SQLite）")
    ap.add_argument("--drop", action="store_true", help="MySQL 模式下先 DROP 旧表")
    args = ap.parse_args()

    t0 = time.time()
    print(f"生成数据: products={N_PRODUCTS} customers={N_CUSTOMERS} orders={N_ORDERS}")
    products = gen_products()
    customers = gen_customers()
    orders, items = [], []
    for i in range(1, N_ORDERS + 1):
        o, its = _order_row(i, random.choice(customers), products)
        orders.append(o)
        items.extend(its)
    stalled = sum(1 for o in orders if o[17] == 1)
    print(f"生成完成 {time.time()-t0:.1f}s | 明细 {len(items)} 条 | 物流停滞 {stalled} 单")

    if args.mysql:
        load_mysql(products, customers, orders, items, args.drop)
    else:
        load_sqlite(products, customers, orders, items)
    print(f"总计耗时 {time.time()-t0:.1f}s")


def _chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def load_sqlite(products, customers, orders, items):
    """SQLite：演示数据保留，业务量级行以 'B' 前缀编号并存（不与演示行冲突）。"""
    import sqlite3
    db = ROOT / "data" / "shop.db"
    big = str(db) + "-big"
    # 业务量级数据放独立文件 shop_big.db，通过 ATTACH 在查询端合并/切换
    conn = sqlite3.connect(big)
    conn.executescript("""
    DROP TABLE IF EXISTS products; DROP TABLE IF EXISTS customers;
    DROP TABLE IF EXISTS orders; DROP TABLE IF EXISTS order_items;
    CREATE TABLE products (sku TEXT PRIMARY KEY, name TEXT, category TEXT, price REAL,
        stock INTEGER, tags TEXT, rating REAL, status TEXT, description TEXT);
    CREATE TABLE customers (customer_id TEXT PRIMARY KEY, name TEXT, phone TEXT,
        vip_level INTEGER, points INTEGER, created_at TEXT);
    CREATE TABLE orders (order_no TEXT PRIMARY KEY, customer_id TEXT, status TEXT,
        total_amount REAL, payment_time TEXT, ship_time TEXT, carrier TEXT,
        tracking_no TEXT, logistics_status TEXT, logistics_updated_at TEXT,
        logistics_note TEXT, receipt_time TEXT, after_sale_note TEXT,
        receiver_name TEXT, receiver_phone TEXT, address TEXT, created_at TEXT,
        stalled INTEGER DEFAULT 0);
    CREATE TABLE order_items (id INTEGER PRIMARY KEY AUTOINCREMENT, order_no TEXT,
        sku TEXT, qty INTEGER, unit_price REAL, item_status TEXT);
    CREATE INDEX idx_orders_cust ON orders(customer_id);
    CREATE INDEX idx_orders_receipt ON orders(receipt_time);
    CREATE INDEX idx_orders_status ON orders(status);
    CREATE INDEX idx_items_order ON order_items(order_no);
    CREATE INDEX idx_items_sku ON order_items(sku);
    """)
    for batch in _chunks(products, BATCH):
        conn.executemany("INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?)", batch)
    for batch in _chunks(customers, BATCH):
        conn.executemany("INSERT INTO customers VALUES (?,?,?,?,?,?)", batch)
    print("写入 orders ...")
    for batch in _chunks(orders, BATCH):
        conn.executemany("INSERT INTO orders VALUES (%s)" % ",".join(["?"] * 18), batch)
    print("写入 order_items ...")
    for batch in _chunks(items, BATCH):
        conn.executemany("INSERT INTO order_items (order_no, sku, qty, unit_price, item_status) VALUES (?,?,?,?,?)", batch)
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    conn.close()
    print(f"SQLite 业务库就绪: {big} | orders={n}")
    print("提示：config.json database.backend 保持 sqlite 时，")
    print("      工具层会通过 ATTACH 读取该业务库（见 src/database.py 的 ensure_big_attached）。")


def load_mysql(products, customers, orders, items, drop):
    from src.llm import load_env_file
    load_env_file(ROOT / ".env")
    import os
    import pymysql
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    m = cfg["database"]["mysql"]
    pwd = os.environ.get(m["password_env"], "").strip()
    if not pwd:
        raise SystemExit(f"请先在 .env 里配置 {m['password_env']}=你的MySQL root密码")
    print(f"连接 MySQL {m['host']}:{m['port']} ...")
    server = pymysql.connect(host=m["host"], port=m["port"], user=m["user"],
                             password=pwd, charset="utf8mb4")
    with server.cursor() as cur:
        cur.execute("CREATE DATABASE IF NOT EXISTS %s CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci" % m["database"])
    server.select_db(m["database"])
    ddl = """
    {drop}CREATE TABLE IF NOT EXISTS products (
        sku VARCHAR(32) PRIMARY KEY, name VARCHAR(200) NOT NULL, category VARCHAR(50),
        price DECIMAL(10,2), stock INT, tags VARCHAR(500), rating DECIMAL(3,1),
        status VARCHAR(16), description TEXT) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    {drop}CREATE TABLE IF NOT EXISTS customers (
        customer_id VARCHAR(32) PRIMARY KEY, name VARCHAR(64), phone VARCHAR(32),
        vip_level INT, points INT, created_at VARCHAR(20)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    {drop}CREATE TABLE IF NOT EXISTS orders (
        order_no VARCHAR(32) PRIMARY KEY, customer_id VARCHAR(32), status VARCHAR(16),
        total_amount DECIMAL(10,2), payment_time VARCHAR(20), ship_time VARCHAR(20),
        carrier VARCHAR(32), tracking_no VARCHAR(32), logistics_status VARCHAR(20),
        logistics_updated_at VARCHAR(20), logistics_note VARCHAR(200), receipt_time VARCHAR(20),
        after_sale_note VARCHAR(500), receiver_name VARCHAR(64), receiver_phone VARCHAR(32),
        address VARCHAR(300), created_at VARCHAR(20), stalled TINYINT DEFAULT 0)
        ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    {drop}CREATE TABLE IF NOT EXISTS order_items (
        id BIGINT AUTO_INCREMENT PRIMARY KEY, order_no VARCHAR(32), sku VARCHAR(32),
        qty INT, unit_price DECIMAL(10,2), item_status VARCHAR(20),
        KEY idx_items_order (order_no), KEY idx_items_sku (sku)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    CREATE INDEX idx_orders_cust ON orders(customer_id);
    CREATE INDEX idx_orders_receipt ON orders(receipt_time);
    CREATE INDEX idx_orders_status ON orders(status);
    """
    drop = "DROP TABLE IF EXISTS order_items; DROP TABLE IF EXISTS orders; DROP TABLE IF EXISTS customers; DROP TABLE IF EXISTS products;\n" if drop else ""
    with server.cursor() as cur:
        for stmt in ddl.format(drop=drop).split(";"):
            if stmt.strip():
                try:
                    cur.execute(stmt)
                except pymysql.err.OperationalError as e:
                    if e.args[0] != 1061:  # 重复建索引忽略
                        raise
    def ins(sql, rows, label):
        print(f"MySQL 写入 {label} {len(rows)} 行 ...")
        with server.cursor() as cur:
            for batch in _chunks(rows, BATCH):
                cur.executemany(sql, batch)
            server.commit()
    try:
        ins("INSERT INTO products VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", products, "products")
        ins("INSERT INTO customers VALUES (%s,%s,%s,%s,%s,%s)", customers, "customers")
        ins("INSERT INTO orders VALUES (%s," + ",".join(["%s"] * 17) + ")", orders, "orders")
        ins("INSERT INTO order_items (order_no, sku, qty, unit_price, item_status) VALUES (%s,%s,%s,%s,%s)", items, "order_items")
    finally:
        server.close()
    print(f"MySQL 库 {m['database']} 灌入完成。将 config.json database.backend 改为 'mysql' 即生效。")


if __name__ == "__main__":
    main()
