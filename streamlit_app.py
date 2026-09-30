"""
Intelligent Inventory System for Quick-Commerce Stores
------------------------------------------------------
Steps covered:
 1 Product registration        6 Expiry monitoring
 2 Automatic inventory update  7 Stock prediction (weighted moving average)
 3 Expected stock calculation  8 Automatic alerts
 4 Inventory drift detection   9 Dashboard (terminal + HTML export)
 5 Anomaly detection + likely reasons

Only the Python standard library is used. Run:  python smart_inventory.py
"""
import random
import sqlite3
import statistics as stats
from datetime import date, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS products(
    sku TEXT PRIMARY KEY, name TEXT, reorder_level INT, lead_time_days INT);
CREATE TABLE IF NOT EXISTS batches(
    id INTEGER PRIMARY KEY, sku TEXT, batch_no TEXT, expiry TEXT, qty_remaining INT);
CREATE TABLE IF NOT EXISTS txns(
    id INTEGER PRIMARY KEY, sku TEXT, batch_id INT, day TEXT, type TEXT, qty INT, note TEXT);
CREATE TABLE IF NOT EXISTS counts(
    id INTEGER PRIMARY KEY, sku TEXT, day TEXT, expected INT, actual INT, drift INT, reasons TEXT);
CREATE TABLE IF NOT EXISTS alerts(
    id INTEGER PRIMARY KEY, day TEXT, level TEXT, sku TEXT, message TEXT,
    UNIQUE(day, sku, message));
"""


class InventorySystem:
    def __init__(self, db=":memory:", today=None):
        self.db = sqlite3.connect(db)
        self.db.executescript(SCHEMA)
        self.today = today or date.today()

    # ------------------------------------------------------------ helpers
    def q(self, sql, args=()):
        return self.db.execute(sql, args).fetchall()

    def _log(self, sku, batch_id, ttype, qty, note=""):
        self.db.execute("INSERT INTO txns(sku,batch_id,day,type,qty,note) VALUES(?,?,?,?,?,?)",
                        (sku, batch_id, self.today.isoformat(), ttype, qty, note))
        self.db.execute("UPDATE batches SET qty_remaining = qty_remaining + ? WHERE id=?",
                        (qty, batch_id))
        self.db.commit()

    def _deduct_fefo(self, sku, qty, ttype, note="", allow_expired=False):
        """Deduct from batches with the earliest expiry first (FEFO)."""
        cond = "" if allow_expired else "AND expiry >= ?"
        args = (sku,) if allow_expired else (sku, self.today.isoformat())
        batches = self.q(f"SELECT id, qty_remaining FROM batches WHERE sku=? AND qty_remaining>0 "
                         f"{cond} ORDER BY expiry", args)
        if sum(b[1] for b in batches) < qty:
            return False
        for bid, rem in batches:
            take = min(rem, qty)
            self._log(sku, bid, ttype, -take, note)
            qty -= take
            if qty == 0:
                break
        return True

    def alert(self, level, sku, msg):
        try:
            self.db.execute("INSERT INTO alerts(day,level,sku,message) VALUES(?,?,?,?)",
                            (self.today.isoformat(), level, sku, msg))
            self.db.commit()
            print(f"  [{level}] {sku}: {msg}")          # replace with email/SMS/push
        except sqlite3.IntegrityError:
            pass                                          # already alerted today

    # ------------------------------------------------- Step 1: registration
    def register_product(self, sku, name, reorder_level=10, lead_time_days=2):
        self.db.execute("INSERT OR REPLACE INTO products VALUES(?,?,?,?)",
                        (sku, name, reorder_level, lead_time_days))
        self.db.commit()

    def receive_stock(self, sku, batch_no, qty, expiry):
        cur = self.db.execute("INSERT INTO batches(sku,batch_no,expiry,qty_remaining) VALUES(?,?,?,0)",
                              (sku, batch_no, expiry.isoformat()))
        self._log(sku, cur.lastrowid, "RECEIVE", qty, f"batch {batch_no}")

    # --------------------------------------- Step 2: automatic stock update
    def place_order(self, sku, qty):
        if self._deduct_fefo(sku, qty, "ORDER"):
            return True
        self.alert("HIGH", sku, f"Order for {qty} units cancelled - insufficient sellable stock")
        return False

    def customer_return(self, sku, qty):
        row = self.q("SELECT id FROM batches WHERE sku=? ORDER BY expiry DESC LIMIT 1", (sku,))
        if row:
            self._log(sku, row[0][0], "RETURN", qty)

    def record_damage(self, sku, qty, note="damaged"):
        self._deduct_fefo(sku, qty, "DAMAGE", note, allow_expired=True)

    def write_off_expired(self):
        for bid, sku, rem in self.q("SELECT id, sku, qty_remaining FROM batches "
                                    "WHERE expiry < ? AND qty_remaining > 0",
                                    (self.today.isoformat(),)):
            self._log(sku, bid, "EXPIRE", -rem, "auto write-off")

    # ------------------------------------ Step 3: expected stock calculation
    def expected_stock(self, sku):
        return self.q("SELECT COALESCE(SUM(qty),0) FROM txns WHERE sku=?", (sku,))[0][0]

    # ---------------------------------- Step 4: drift detection (stock count)
    def physical_count(self, sku, actual):
        expected = self.expected_stock(sku)
        drift = expected - actual                          # +ve = missing, -ve = surplus
        tol = max(2, int(0.05 * expected))
        reasons = []
        if abs(drift) > tol:
            reasons = self.suggest_reasons(sku, drift)     # Step 5
            self.alert("HIGH", sku, f"Inventory drift {drift:+d} (expected {expected}, counted "
                                    f"{actual}). Likely: {'; '.join(reasons)}")
        if drift:                                          # reconcile app stock with reality
            if drift > 0:
                self._deduct_fefo(sku, drift, "ADJUST", "count reconciliation", allow_expired=True)
            else:
                bid = self.q("SELECT id FROM batches WHERE sku=? ORDER BY expiry DESC LIMIT 1", (sku,))[0][0]
                self._log(sku, bid, "ADJUST", -drift, "count reconciliation")
        self.db.execute("INSERT INTO counts(sku,day,expected,actual,drift,reasons) VALUES(?,?,?,?,?,?)",
                        (sku, self.today.isoformat(), expected, actual, drift, "; ".join(reasons)))
        self.db.commit()
        return drift

    # ---------------------------------------- Step 5: anomaly detection / AI
    def suggest_reasons(self, sku, drift):
        week_ago = (self.today - timedelta(days=7)).isoformat()
        dmg = self.q("SELECT COUNT(*) FROM txns WHERE sku=? AND type='DAMAGE' AND day>=?",
                     (sku, week_ago))[0][0]
        stale = self.q("SELECT COUNT(*) FROM batches WHERE sku=? AND expiry<? AND qty_remaining>0",
                       (sku, self.today.isoformat()))[0][0]
        reasons = []
        if drift > 0:
            if stale:
                reasons.append("expired units discarded but not written off")
            if not dmg:
                reasons.append("unrecorded damage or unrecorded/offline sale (or theft)")
            if drift >= 9 and drift % 9 == 0:
                reasons.append("digit-transposition data-entry error")
            reasons.append("wrong quantity entered at goods receipt")
        else:
            reasons.append("customer return not recorded or receipt quantity under-entered")
        return reasons

    def daily_sales(self, sku, days):
        start = (self.today - timedelta(days=days - 1)).isoformat()
        rows = dict(self.q("SELECT day, -SUM(qty) FROM txns WHERE sku=? AND type='ORDER' AND day>=? "
                           "GROUP BY day", (sku, start)))
        return [rows.get((self.today - timedelta(days=days - 1 - i)).isoformat(), 0)
                for i in range(days)]

    def detect_sales_anomaly(self, sku, window=14, z_limit=2.5):
        """Z-score of today's sales vs previous days (spikes/drops in movement)."""
        s = self.daily_sales(sku, window)
        hist, latest = s[:-1], s[-1]
        if len(hist) < 5 or stats.pstdev(hist) == 0:
            return None
        z = (latest - stats.mean(hist)) / stats.pstdev(hist)
        return z if abs(z) >= z_limit else None

    # ---------------------------------------------- Step 6: expiry monitoring
    def expiry_report(self, warn_days=3):
        rows = self.q("SELECT sku, batch_no, expiry, qty_remaining FROM batches "
                      "WHERE qty_remaining>0 ORDER BY expiry")
        out = []
        for sku, batch, exp, qty in rows:
            left = (date.fromisoformat(exp) - self.today).days
            if left <= warn_days:
                out.append((sku, batch, exp, qty, left))
        return out

    # ----------------------------------------------- Step 7: stock prediction
    def predict(self, sku, window=7):
        """Weighted moving average (recent days weigh more) -> days until stock-out."""
        s = self.daily_sales(sku, window)
        weights = range(1, window + 1)
        avg = sum(w * v for w, v in zip(weights, s)) / sum(weights)
        stock = self.expected_stock(sku)
        return avg, (stock / avg if avg > 0 else float("inf"))

    # ------------------------------------------------- Step 8: alert engine
    def run_checks(self):
        for sku, name, reorder, lead in self.q("SELECT * FROM products"):
            stock = self.expected_stock(sku)
            avg, days_left = self.predict(sku)
            if stock <= reorder:
                self.alert("MEDIUM", sku, f"Low stock: {stock} units (reorder level {reorder})")
            if days_left <= lead + 1:
                self.alert("HIGH", sku, f"Predicted stock-out in {days_left:.1f} days "
                                        f"(avg demand {avg:.1f}/day, lead time {lead}d)")
            z = self.detect_sales_anomaly(sku)
            if z is not None:
                kind = "spike" if z > 0 else "drop"
                self.alert("MEDIUM", sku, f"Unusual sales {kind} today (z={z:+.1f}) - check demand/data")
        for sku, batch, exp, qty, left in self.expiry_report():
            msg = (f"Batch {batch} EXPIRED on {exp} with {qty} units still in stock" if left < 0
                   else f"Batch {batch}: {qty} units expire in {left} day(s) ({exp})")
            self.alert("HIGH" if left <= 1 else "MEDIUM", sku, msg)

    # --------------------------------------------------- Step 9: dashboard
    def _rows(self):
        rows = []
        for sku, name, reorder, lead in self.q("SELECT * FROM products"):
            stock = self.expected_stock(sku)
            avg, dl = self.predict(sku)
            exp = self.q("SELECT MIN(expiry) FROM batches WHERE sku=? AND qty_remaining>0", (sku,))[0][0]
            trend = "".join("▁▂▃▄▅▆▇█"[min(7, int(v * 7 / (max(self.daily_sales(sku, 7)) or 1)))]
                            for v in self.daily_sales(sku, 7))
            status = "OK" if stock > reorder and dl > lead + 1 else "ATTENTION"
            rows.append((sku, name, stock, round(avg, 1), "inf" if dl == float("inf") else f"{dl:.1f}",
                         exp or "-", trend, status))
        return rows

    def dashboard(self):
        print(f"\n{'=' * 92}\n INVENTORY DASHBOARD - {self.today}\n{'=' * 92}")
        print(f"{'SKU':<8}{'Product':<18}{'Stock':>6}{'Avg/day':>9}{'DaysLeft':>10}"
              f"{'Next expiry':>13}  {'7d trend':<9}{'Status'}")
        for r in self._rows():
            print(f"{r[0]:<8}{r[1]:<18}{r[2]:>6}{r[3]:>9}{r[4]:>10}{r[5]:>13}  {r[6]:<9}{r[7]}")
        print("\n Recent drift audits:")
        for sku, day, exp, act, drift, why in self.q(
                "SELECT sku, day, expected, actual, drift, reasons FROM counts ORDER BY id DESC LIMIT 5"):
            print(f"  {day} {sku}: expected {exp}, counted {act}, drift {drift:+d}"
                  + (f"  -> {why}" if why else ""))
        print("\n Latest alerts:")
        for day, lvl, sku, msg in self.q("SELECT day, level, sku, message FROM alerts "
                                         "ORDER BY id DESC LIMIT 8"):
            print(f"  {day} [{lvl}] {sku}: {msg}")

    def export_html(self, path="dashboard.html"):
        head = "".join(f"<th>{h}</th>" for h in
                       ["SKU", "Product", "Stock", "Avg/day", "Days left", "Next expiry", "7d trend", "Status"])
        body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in self._rows())
        alerts = "".join(f"<li><b>[{l}]</b> {s}: {m}</li>" for l, s, m in
                         self.q("SELECT level, sku, message FROM alerts ORDER BY id DESC LIMIT 15"))
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"<html><head><meta charset='utf-8'><title>Inventory</title><style>"
                    f"body{{font-family:sans-serif;margin:2rem}}table{{border-collapse:collapse}}"
                    f"td,th{{border:1px solid #ccc;padding:6px 10px}}th{{background:#f3f3f3}}"
                    f"</style></head><body><h2>Inventory Dashboard - {self.today}</h2>"
                    f"<table><tr>{head}</tr>{body}</table><h3>Alerts</h3><ul>{alerts}</ul></body></html>")
        return path


# =============================================================== demo run
def demo():
    random.seed(7)
    start = date.today() - timedelta(days=20)
    inv = InventorySystem(today=start)

    products = [("MILK1", "Milk 500ml", 20), ("BRD1", "Bread", 15), ("EGG1", "Eggs (6)", 15),
                ("CHP1", "Chips", 10), ("COLA", "Cola 750ml", 10)]
    shelf = {"MILK1": 5, "BRD1": 4, "EGG1": 12, "CHP1": 90, "COLA": 120}
    base_demand = {"MILK1": 12, "BRD1": 8, "EGG1": 7, "CHP1": 4, "COLA": 5}
    for sku, name, rl in products:
        inv.register_product(sku, name, rl)
        inv.receive_stock(sku, f"{sku}-B1", 150 if sku == "MILK1" else 120, start + timedelta(days=shelf[sku]))

    for d in range(21):
        inv.today = start + timedelta(days=d)
        inv.write_off_expired()
        for sku, _, _ in products:
            demand = max(0, int(random.gauss(base_demand[sku], 1.5)))
            if d == 20 and sku == "COLA":
                demand *= 6                                   # sudden spike
            inv.place_order(sku, demand)
        if d == 6:
            inv.customer_return("BRD1", 2)
        if d == 8:
            inv.record_damage("EGG1", 6, "broken tray")
        if d > 0 and d % 3 == 0:                              # regular supplier deliveries
            for sku, _, _ in products:
                qty = 60 if sku in ("MILK1", "BRD1", "EGG1") else 40
                inv.receive_stock(sku, f"{sku}-D{d}", qty, inv.today + timedelta(days=shelf[sku]))
        if d == 14:                                           # weekly physical audit
            for sku, _, _ in products:
                exp = inv.expected_stock(sku)
                lost = 18 if sku == "CHP1" else (0 if sku != "COLA" else 3)   # unrecorded loss
                inv.physical_count(sku, exp - lost)
        if d >= 12:
            inv.run_checks()

    inv.dashboard()
    print("\nHTML dashboard saved to:", inv.export_html())


if __name__ == "__main__":
    demo()
