import streamlit as st
import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime, date, timedelta
from sklearn.ensemble import IsolationForest
import plotly.express as px

# ============================================================
# PAGE CONFIG
# ============================================================

st.set_page_config(
    page_title="Intelligent Inventory System",
    page_icon="📦",
    layout="wide"
)

DB_FILE = "inventory.db"


# ============================================================
# DATABASE CONNECTION
# ============================================================

def get_connection():
    return sqlite3.connect(DB_FILE, check_same_thread=False)


conn = get_connection()


# ============================================================
# DATABASE INITIALIZATION
# ============================================================

def initialize_database():

    cursor = conn.cursor()

    # --------------------------------------------------------
    # Products
    # --------------------------------------------------------

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT,
            price REAL DEFAULT 0,
            reorder_level INTEGER DEFAULT 10,
            created_at TEXT
        )
    """)

    # --------------------------------------------------------
    # Batches
    # --------------------------------------------------------

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER,
            batch_number TEXT,
            quantity INTEGER,
            expiry_date TEXT,
            created_at TEXT,
            FOREIGN KEY(product_id) REFERENCES products(id)
        )
    """)

    # --------------------------------------------------------
    # Inventory transactions
    # --------------------------------------------------------

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER,
            batch_id INTEGER,
            transaction_type TEXT,
            quantity INTEGER,
            transaction_date TEXT,
            notes TEXT,
            FOREIGN KEY(product_id) REFERENCES products(id),
            FOREIGN KEY(batch_id) REFERENCES batches(id)
        )
    """)

    # --------------------------------------------------------
    # Physical stock audits
    # --------------------------------------------------------

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS audits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER,
            expected_stock INTEGER,
            actual_stock INTEGER,
            difference INTEGER,
            drift_percentage REAL,
            audit_date TEXT,
            notes TEXT,
            FOREIGN KEY(product_id) REFERENCES products(id)
        )
    """)

    conn.commit()


initialize_database()


# ============================================================
# DATABASE HELPERS
# ============================================================

def get_products():

    return pd.read_sql_query(
        "SELECT * FROM products ORDER BY name",
        conn
    )


def get_batches():

    return pd.read_sql_query("""
        SELECT
            b.id,
            b.product_id,
            p.name AS product_name,
            b.batch_number,
            b.quantity,
            b.expiry_date,
            b.created_at
        FROM batches b
        JOIN products p
        ON b.product_id = p.id
        ORDER BY b.expiry_date
    """, conn)


def get_transactions():

    return pd.read_sql_query("""
        SELECT
            t.id,
            p.name AS product_name,
            t.batch_id,
            t.transaction_type,
            t.quantity,
            t.transaction_date,
            t.notes
        FROM transactions t
        JOIN products p
        ON t.product_id = p.id
        ORDER BY t.id DESC
    """, conn)


def get_audits():

    return pd.read_sql_query("""
        SELECT
            a.id,
            p.name AS product_name,
            a.expected_stock,
            a.actual_stock,
            a.difference,
            a.drift_percentage,
            a.audit_date,
            a.notes
        FROM audits a
        JOIN products p
        ON a.product_id = p.id
        ORDER BY a.id DESC
    """, conn)


# ============================================================
# PRODUCT FUNCTIONS
# ============================================================

def add_product(
    name,
    category,
    price,
    reorder_level
):

    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO products
        (
            name,
            category,
            price,
            reorder_level,
            created_at
        )
        VALUES (?, ?, ?, ?, ?)
    """, (
        name,
        category,
        price,
        reorder_level,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ))

    conn.commit()

    return cursor.lastrowid


# ============================================================
# BATCH FUNCTIONS
# ============================================================

def add_batch(
    product_id,
    batch_number,
    quantity,
    expiry_date
):

    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO batches
        (
            product_id,
            batch_number,
            quantity,
            expiry_date,
            created_at
        )
        VALUES (?, ?, ?, ?, ?)
    """, (
        product_id,
        batch_number,
        quantity,
        expiry_date,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ))

    batch_id = cursor.lastrowid

    conn.commit()

    return batch_id


def update_batch_quantity(batch_id, change):

    conn.execute("""
        UPDATE batches
        SET quantity = quantity + ?
        WHERE id = ?
    """, (
        change,
        batch_id
    ))

    conn.commit()


# ============================================================
# TRANSACTION FUNCTION
# ============================================================

def record_transaction(
    product_id,
    batch_id,
    transaction_type,
    quantity,
    notes=""
):

    conn.execute("""
        INSERT INTO transactions
        (
            product_id,
            batch_id,
            transaction_type,
            quantity,
            transaction_date,
            notes
        )
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        product_id,
        batch_id,
        transaction_type,
        quantity,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        notes
    ))

    conn.commit()


# ============================================================
# EXPECTED STOCK CALCULATION
# ============================================================

def calculate_expected_stock(product_id):

    batches = conn.execute("""
        SELECT COALESCE(SUM(quantity), 0)
        FROM batches
        WHERE product_id = ?
    """, (product_id,)).fetchone()[0]

    return int(batches)


# ============================================================
# TOTAL CURRENT STOCK
# ============================================================

def get_total_stock(product_id):

    result = conn.execute("""
        SELECT COALESCE(SUM(quantity), 0)
        FROM batches
        WHERE product_id = ?
    """, (product_id,)).fetchone()

    return int(result[0])


# ============================================================
# SALES HISTORY
# ============================================================

def get_sales_history(product_id):

    data = pd.read_sql_query("""
        SELECT
            DATE(transaction_date) AS sale_date,
            SUM(quantity) AS quantity
        FROM transactions
        WHERE product_id = ?
        AND transaction_type = 'Order'
        GROUP BY DATE(transaction_date)
        ORDER BY sale_date
    """, conn, params=(product_id,))

    return data


# ============================================================
# STOCK SHORTAGE PREDICTION
# ============================================================

def predict_stock_shortage(product_id):

    current_stock = get_total_stock(product_id)

    sales = get_sales_history(product_id)

    if sales.empty:

        return {
            "average_daily_sales": 0,
            "days_remaining": None,
            "predicted_date": None
        }

    # Last 30 days
    sales["sale_date"] = pd.to_datetime(
        sales["sale_date"]
    )

    recent_date = pd.Timestamp.today() - pd.Timedelta(
        days=30
    )

    sales = sales[
        sales["sale_date"] >= recent_date
    ]

    if sales.empty:

        return {
            "average_daily_sales": 0,
            "days_remaining": None,
            "predicted_date": None
        }

    total_sales = sales["quantity"].sum()

    days = max(
        (sales["sale_date"].max() -
         sales["sale_date"].min()).days,
        1
    )

    average_daily_sales = total_sales / days

    if average_daily_sales <= 0:

        return {
            "average_daily_sales": 0,
            "days_remaining": None,
            "predicted_date": None
        }

    days_remaining = current_stock / average_daily_sales

    predicted_date = (
        pd.Timestamp.today()
        + pd.Timedelta(days=days_remaining)
    )

    return {
        "average_daily_sales": average_daily_sales,
        "days_remaining": days_remaining,
        "predicted_date": predicted_date
    }


# ============================================================
# EXPIRY ALERTS
# ============================================================

def get_expiry_alerts():

    batches = get_batches()

    if batches.empty:
        return batches

    batches["expiry_date"] = pd.to_datetime(
        batches["expiry_date"]
    )

    today = pd.Timestamp.today()

    batches["days_to_expiry"] = (
        batches["expiry_date"] - today
    ).dt.days

    alerts = batches[
        (batches["quantity"] > 0)
        &
        (batches["days_to_expiry"] <= 7)
    ]

    return alerts


# ============================================================
# DRIFT DETECTION
# ============================================================

def detect_drift(
    expected_stock,
    actual_stock
):

    difference = expected_stock - actual_stock

    if expected_stock > 0:

        drift_percentage = (
            abs(difference)
            / expected_stock
        ) * 100

    else:

        drift_percentage = 0

    if drift_percentage >= 10:

        status = "CRITICAL"

    elif drift_percentage >= 5:

        status = "WARNING"

    else:

        status = "NORMAL"

    return (
