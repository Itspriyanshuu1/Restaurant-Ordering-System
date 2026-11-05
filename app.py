from flask import (
    Flask, render_template, request, jsonify,
    redirect, url_for, session
)
import sqlite3
import json
import os
from pathlib import Path
from functools import wraps
from collections import Counter
from datetime import date, timedelta

app = Flask(__name__)

DATABASE = Path(__file__).parent / "urban_crave.db"

app.secret_key = os.environ.get(
    "FLASK_SECRET_KEY", "change-this-secret-key"
)
ADMIN_PASSWORD = os.environ.get(
    "ADMIN_PASSWORD", "change-this-admin-password"
)

ORDER_STATUSES = [
    "Pending",
    "Preparing",
    "Out for Delivery",
    "Delivered",
    "Cancelled"
]


def get_db_connection():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database():
    connection = get_db_connection()

    connection.execute("""
        CREATE TABLE IF NOT EXISTS menu_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            category TEXT NOT NULL,
            price REAL NOT NULL,
            image TEXT,
            description TEXT
        )
    """)

    connection.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_name TEXT NOT NULL,
            phone TEXT NOT NULL,
            address TEXT NOT NULL,
            items TEXT NOT NULL,
            total REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'Pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    connection.commit()
    connection.close()


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/menu", methods=["GET"])
def get_menu():
    connection = get_db_connection()
    items = connection.execute(
        "SELECT * FROM menu_items ORDER BY id"
    ).fetchall()
    connection.close()

    return jsonify([dict(item) for item in items])


@app.route("/api/order", methods=["POST"])
def create_order():
    data = request.get_json(silent=True) or {}

    name = str(data.get("name", "")).strip()
    phone = str(data.get("phone", "")).strip()
    address = str(data.get("address", "")).strip()
    items = data.get("items")
    total = data.get("total")

    if not name or not phone or not address:
        return jsonify({
            "error": "Please provide all delivery details."
        }), 400

    if not isinstance(items, list) or not items:
        return jsonify({"error": "Your cart is empty."}), 400

    try:
        total = float(total)
        if total <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid order total."}), 400

    connection = get_db_connection()

    try:
        cursor = connection.execute("""
            INSERT INTO orders
            (customer_name, phone, address, items, total)
            VALUES (?, ?, ?, ?, ?)
        """, (
            name, phone, address, json.dumps(items), total
        ))

        connection.commit()
        order_id = cursor.lastrowid

    except sqlite3.Error:
        connection.rollback()
        return jsonify({
            "error": "Could not save your order."
        }), 500

    finally:
        connection.close()

    return jsonify({
        "message": "Order saved successfully!",
        "order_id": order_id
    }), 201


def admin_required(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        return function(*args, **kwargs)
    return wrapper


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None

    if request.method == "POST":
        password = request.form.get("password", "")

        if password == ADMIN_PASSWORD:
            session["admin_logged_in"] = True
            return redirect(url_for("admin_dashboard"))

        error = "Incorrect password. Please try again."

    return render_template("admin_login.html", error=error)


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_logged_in", None)
    return redirect(url_for("admin_login"))


def get_item_name(item):
    """Support common item formats used by the shopping cart."""
    if isinstance(item, str):
        return item

    if isinstance(item, dict):
        return str(
            item.get("name")
            or item.get("title")
            or item.get("dish")
            or "Unknown item"
        )

    return "Unknown item"


def get_item_quantity(item):
    if isinstance(item, dict):
        try:
            quantity = int(item.get("quantity", item.get("qty", 1)))
            return max(quantity, 1)
        except (TypeError, ValueError):
            return 1

    return 1


def build_analytics(orders):
    today = date.today()
    dates = [today - timedelta(days=i) for i in range(6, -1, -1)]
    daily_values = {day.isoformat(): 0.0 for day in dates}
    daily_counts = {day.isoformat(): 0 for day in dates}

    popular_items = Counter()
    revenue = 0.0
    delivered_revenue = 0.0
    active_orders = 0

    for order in orders:
        status = order["status"]

        # Cancelled orders are excluded from sales figures.
        if status != "Cancelled":
            revenue += float(order["total"] or 0)
            active_orders += 1

            order_date = str(order["created_at"] or "")[:10]

            if order_date in daily_values:
                daily_values[order_date] += float(order["total"] or 0)
                daily_counts[order_date] += 1

            for item in order["items_list"]:
                popular_items[get_item_name(item)] += get_item_quantity(item)

        if status == "Delivered":
            delivered_revenue += float(order["total"] or 0)

    total_orders = len(orders)
    pending_orders = sum(
        1 for order in orders
        if order["status"] == "Pending"
    )
    delivered_orders = sum(
        1 for order in orders
        if order["status"] == "Delivered"
    )

    average_order_value = (
        revenue / active_orders if active_orders else 0
    )

    return {
        "total_orders": total_orders,
        "pending_orders": pending_orders,
        "delivered_orders": delivered_orders,
        "total_revenue": revenue,
        "delivered_revenue": delivered_revenue,
        "average_order_value": average_order_value,
        "active_orders": active_orders,
        "cancelled_orders": sum(
            1 for order in orders
            if order["status"] == "Cancelled"
        ),
        "chart_labels": [day.strftime("%d %b") for day in dates],
        "chart_revenue": [
            round(daily_values[day.isoformat()], 2) for day in dates
        ],
        "chart_orders": [
            daily_counts[day.isoformat()] for day in dates
        ],
        "popular_labels": [
            name for name, count in popular_items.most_common(5)
        ],
        "popular_counts": [
            count for name, count in popular_items.most_common(5)
        ]
    }


@app.route("/admin")
@admin_required
def admin_dashboard():
    connection = get_db_connection()

    rows = connection.execute(
        "SELECT * FROM orders ORDER BY id DESC"
    ).fetchall()

    connection.close()

    orders = []

    for row in rows:
        order = dict(row)

        try:
            parsed_items = json.loads(order["items"])
            order["items_list"] = (
                parsed_items if isinstance(parsed_items, list) else []
            )
        except (json.JSONDecodeError, TypeError):
            order["items_list"] = []

        orders.append(order)

    stats = build_analytics(orders)

    return render_template(
        "admin.html",
        orders=orders,
        stats=stats,
        statuses=ORDER_STATUSES
    )


@app.route("/admin/update-status/<int:order_id>", methods=["POST"])
@admin_required
def update_order_status(order_id):
    status = request.form.get("status", "")

    if status not in ORDER_STATUSES:
        return "Invalid order status.", 400

    connection = get_db_connection()

    cursor = connection.execute(
        "UPDATE orders SET status = ? WHERE id = ?",
        (status, order_id)
    )

    connection.commit()
    connection.close()

    if cursor.rowcount == 0:
        return "Order not found.", 404

    return redirect(url_for("admin_dashboard"))


if __name__ == "__main__":
    initialize_database()
    app.run(debug=True)