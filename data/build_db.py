# -*- coding: utf-8 -*-
"""
生成店铺演示数据库 data/shop.db（可重复执行，每次全量重建）。

表结构：
  customers   顾客（脱敏手机号、会员等级、积分）
  products    商品（SKU、价格、库存、标签、评分）
  orders      订单（状态、物流、签收时间、售后备注）
  order_items 订单明细
  tickets     工单（初始为空，由客服 Agent 运行时写入）
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "shop.db"

CUSTOMERS = [
    # customer_id, name, phone, vip_level(0普通/1银卡/2金卡), points, created_at
    ("C001", "张伟", "138****2211", 1, 860,  "2025-11-02 10:15"),
    ("C002", "李娜", "159****0325", 0, 320,  "2026-03-18 20:41"),
    ("C003", "王芳", "137****8846", 0, 150,  "2026-08-09 12:03"),
    ("C004", "赵磊", "186****5103", 2, 2400, "2025-06-21 09:30"),
    ("C005", "陈静", "135****7742", 0, 90,   "2026-09-30 18:22"),
    ("C006", "刘洋", "150****3398", 0, 10,   "2026-10-24 21:05"),
]

PRODUCTS = [
    # sku, name, category, price, stock, tags(json数组字符串), rating, status, description
    ("SKU-1001", "星声 Buds Pro 无线降噪耳机", "音频", 299.0, 45,
     '["降噪","运动","IPX5防水","蓝牙5.3","长续航"]', 4.8, "在售",
     "主动降噪+通透模式，IPX5防水防汗，适合通勤与跑步，单次续航8小时。"),
    ("SKU-1002", "星声 Buds Air 半入耳无线耳机", "音频", 129.0, 80,
     '["轻便","通话降噪","蓝牙5.3"]', 4.6, "在售",
     "半入耳设计佩戴无压感，通话降噪清晰，适合通勤日常使用。"),
    ("SKU-1003", "澳韵 Mini 蓝牙音箱", "音频", 159.0, 30,
     '["便携","IPX7防水","12小时续航"]', 4.7, "在售",
     "巴掌大小便携音箱，IPX7防水可淋浴间使用，续航12小时。"),
    ("SKU-1004", "星声 Buds Youth 入门无线耳机", "音频", 79.0, 120,
     '["高性价比","运动","IPX4防水","轻便"]', 4.4, "在售",
     "入门运动蓝牙耳机，IPX4防水防汗，挂耳式佩戴稳固，适合跑步健身。"),
    ("SKU-2001", "安途 20000mAh 快充充电宝", "充电", 139.0, 60,
     '["22.5W快充","双向Type-C","数显电量"]', 4.7, "在售",
     "22.5W双向快充，数显电量，额定能量约74Wh可随身登机（不可托运）。"),
    ("SKU-2002", "安途 10000mAh 迷你充电宝", "充电", 79.0, 90,
     '["轻薄","可登机","18W快充"]', 4.6, "在售",
     "口红大小轻薄机身，18W快充，日常应急首选。"),
    ("SKU-2003", "安途 65W 氮化镓充电器", "充电", 99.0, 55,
     '["GaN","多口","折叠插脚"]', 4.8, "在售",
     "65W双C口氮化镓，可充手机/平板/轻薄本，折叠插脚便携。"),
    ("SKU-2004", "编织数据线三合一（1.5m）", "充电", 29.0, 200,
     '["三合一","Type-C","耐拉扯"]', 4.5, "在售",
     "Type-C/Lightning/Micro三接头尼龙编织线，1.5米耐拉扯。"),
    ("SKU-3001", "律动 Fit 智能手环", "智能穿戴", 199.0, 40,
     '["心率监测","50米防水","14天续航"]', 4.6, "在售",
     "全天心率监测，50米防水可游泳，典型使用14天续航。"),
    ("SKU-3002", "律动 Watch S 智能手表", "智能穿戴", 599.0, 25,
     '["GPS","血氧监测","蓝牙通话","100+运动模式"]', 4.7, "在售",
     "内置GPS与血氧监测，支持蓝牙通话，100+运动模式。"),
    ("SKU-3003", "律动 Fit 青春版手环", "智能穿戴", 129.0, 70,
     '["心率监测","30天续航","高性价比"]', 4.5, "在售",
     "基础心率监测+睡眠分析，30天超长续航。"),
    ("SKU-4001", "云憩超声波加湿器 3L", "家居", 89.0, 8,
     '["3L大容量","静音","缺水断电"]', 4.5, "在售",
     "3L水箱超声波加湿，36dB静音，缺水自动断电。建议使用纯净水。"),
    ("SKU-4002", "云憩智能香薰机", "家居", 129.0, 22,
     '["APP控制","四档雾量","氛围灯"]', 4.6, "在售",
     "APP远程控制，四档雾量，暖色氛围灯。"),
    ("SKU-4003", "云憩直流变频循环扇", "家居", 199.0, 0,
     '["DC变频","12档风速","遥控"]', 4.7, "在售",
     "DC变频电机静音省电，12档风速，附带遥控器。"),
    ("SKU-5001", "气囊防摔透明手机壳", "手机配件", 39.0, 300,
     '["气囊防摔","不发黄","多机型"]', 4.4, "在售",
     "四角气囊缓震，高透材质不易发黄，覆盖主流机型。"),
    ("SKU-5002", "高清钢化膜两片装", "手机配件", 19.0, 500,
     '["高清","疏油层"," easy贴"]', 4.5, "在售",
     "0.33mm高清钢化膜，疏油防指纹，两片装含贴膜工具。"),
    ("SKU-6001", "颈部按摩仪 Nano", "生活电器", 199.0, 15,
     '["脉冲按摩","15分钟定时","Type-C充电"]', 4.3, "在售",
     "低频脉冲颈部按摩，15分钟自动定时。心脏起搏器佩戴者及孕妇禁用。"),
    ("SKU-6002", "便携电动打泡剃须刀", "生活电器", 149.0, 18,
     '["便携","IPX6水洗","60分钟续航"]', 4.5, "在售",
     "旋转双刀头打泡剃须，IPX6全身水洗，60分钟续航。"),
]

ORDERS = [
    # order_no, customer_id, status, total_amount, payment_time, ship_time,
    # carrier, tracking_no, logistics_status, logistics_updated_at, logistics_note,
    # receipt_time, after_sale_note, receiver_name, receiver_phone, address, created_at
    ("SO2610120001", "C001", "已签收", 318.0,
     "2026-10-12 21:18", "2026-10-13 10:02", "中通", "ZT77321009854",
     "已签收", "2026-10-14 14:36", "本人签收", "2026-10-14 14:36", "",
     "张伟", "138****2211", "上海市浦东新区张江路88号2栋501", "2026-10-12 21:15"),
    ("SO2610150002", "C002", "已签收", 168.0,
     "2026-10-15 12:40", "2026-10-16 09:20", "中通", "ZT77331542870",
     "已签收", "2026-10-17 11:02", "驿站代收", "2026-10-17 11:02", "",
     "李娜", "159****0325", "北京市海淀区中关村大街1号A座1603", "2026-10-15 12:38"),
    ("SO2610180003", "C003", "已发货", 199.0,
     "2026-10-18 19:22", "2026-10-19 08:47", "中通", "ZT75312345678",
     "停滞", "2026-10-20 18:40", "包裹到达【武汉转运中心】，此后无新的物流更新",
     None, "",
     "王芳", "137****8846", "湖北省武汉市洪山区珞狮路165号", "2026-10-18 19:20"),
    ("SO2610080004", "C004", "已退款", 89.0,
     "2026-10-08 15:33", "2026-10-09 10:12", "圆通", "YT53112200876",
     "已签收", "2026-10-11 12:20", "本人签收", "2026-10-11 12:20",
     "2026-10-16 申请仅退款（加湿器不出雾，质量问题）；2026-10-20 退款审核通过并原路退回，退款已完成。",
     "赵磊", "186****5103", "广州市天河区体育西路103号1201", "2026-10-08 15:31"),
    ("SO2610160005", "C005", "售后中", 159.0,
     "2026-10-16 22:05", "2026-10-17 11:30", "顺丰", "SF1330224481560",
     "已签收", "2026-10-19 15:44", "本人签收", "2026-10-19 15:44",
     "2026-10-22 申请换货（音箱充电异常，质量问题）；换货处理中，替换件已于2026-10-24 通过顺丰 SF1330224987300 寄出。",
     "陈静", "135****7742", "成都市高新区天府三街199号8-602", "2026-10-16 22:03"),
    ("SO2610240006", "C001", "待发货", 39.0,
     "2026-10-24 20:11", None, "", "",
     "", None, "", None, "",
     "张伟", "138****2211", "上海市浦东新区张江路88号2栋501", "2026-10-24 20:09"),
    ("SO2610250007", "C006", "待付款", 599.0,
     None, None, "", "",
     "", None, "", None, "",
     "刘洋", "150****3398", "杭州市西湖区文三路477号华星科技大厦608", "2026-10-25 09:02"),
    ("SO2609090008", "C002", "已签收", 139.0,
     "2026-09-09 13:26", "2026-09-10 09:15", "中通", "ZT77099887766",
     "已签收", "2026-09-11 10:50", "本人签收", "2026-09-11 10:50", "",
     "李娜", "159****0325", "北京市海淀区中关村大街1号A座1603", "2026-09-09 13:24"),
]

ORDER_ITEMS = [
    # order_no, sku, qty, unit_price, item_status
    ("SO2610120001", "SKU-1001", 1, 299.0, "正常"),
    ("SO2610120001", "SKU-5002", 1, 19.0,  "正常"),
    ("SO2610150002", "SKU-2001", 1, 139.0, "正常"),
    ("SO2610150002", "SKU-2004", 1, 29.0,  "正常"),
    ("SO2610180003", "SKU-3001", 1, 199.0, "正常"),
    ("SO2610080004", "SKU-4001", 1, 89.0,  "已退款"),
    ("SO2610160005", "SKU-1003", 1, 159.0, "售后中"),
    ("SO2610240006", "SKU-5001", 1, 39.0,  "正常"),
    ("SO2610250007", "SKU-3002", 1, 599.0, "正常"),
    ("SO2609090008", "SKU-2001", 1, 139.0, "正常"),
]

DDL = """
DROP TABLE IF EXISTS customers;
CREATE TABLE customers (
    customer_id TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    phone       TEXT,
    vip_level   INTEGER DEFAULT 0,
    points      INTEGER DEFAULT 0,
    created_at  TEXT
);

DROP TABLE IF EXISTS products;
CREATE TABLE products (
    sku         TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    category    TEXT,
    price       REAL,
    stock       INTEGER,
    tags        TEXT,
    rating      REAL,
    status      TEXT,
    description TEXT
);

DROP TABLE IF EXISTS orders;
CREATE TABLE orders (
    order_no              TEXT PRIMARY KEY,
    customer_id           TEXT NOT NULL,
    status                TEXT,
    total_amount          REAL,
    payment_time          TEXT,
    ship_time             TEXT,
    carrier               TEXT,
    tracking_no           TEXT,
    logistics_status      TEXT,
    logistics_updated_at  TEXT,
    logistics_note        TEXT,
    receipt_time          TEXT,
    after_sale_note       TEXT,
    receiver_name         TEXT,
    receiver_phone        TEXT,
    address               TEXT,
    created_at            TEXT
);

DROP TABLE IF EXISTS order_items;
CREATE TABLE order_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no    TEXT NOT NULL,
    sku         TEXT NOT NULL,
    qty         INTEGER,
    unit_price  REAL,
    item_status TEXT DEFAULT '正常'
);

DROP TABLE IF EXISTS tickets;
CREATE TABLE tickets (
    ticket_id     TEXT PRIMARY KEY,
    order_no      TEXT,
    customer_id   TEXT,
    type          TEXT,
    priority      TEXT,
    description   TEXT,
    status        TEXT DEFAULT '新建',
    created_at    TEXT,
    resolved_note TEXT
);
"""


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(DDL)
        conn.executemany(
            "INSERT INTO customers VALUES (?,?,?,?,?,?)", CUSTOMERS)
        conn.executemany(
            "INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?)", PRODUCTS)
        conn.executemany(
            "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ORDERS)
        conn.executemany(
            "INSERT INTO order_items (order_no, sku, qty, unit_price, item_status) "
            "VALUES (?,?,?,?,?)", ORDER_ITEMS)
        conn.commit()
        for table in ("customers", "products", "orders", "order_items", "tickets"):
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table:<12} {n} rows")
        print(f"OK: {DB_PATH}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
