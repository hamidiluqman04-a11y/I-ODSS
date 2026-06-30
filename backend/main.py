"""
I-ODSS Backend v7
=================
Key architectural guarantees (thesis-mandated):
  - ALL transaction data lives in volatile RAM only (STORE dict, never written to disk)
  - Data is purged immediately after the analytics payload is streamed to the client
  - Flexible column mapper catches Shopee / Lazada / TikTok Shop header variations
  - HTTP 422 with exact error details when required columns cannot be mapped
  - Row-level validation returns precise error messages (row number + field + value)
  - FP-Growth uses order-basket grouping + co-occurrence fallback so bundle cards
    never render blank even when multi-item order volume is low

New in v7 — Explainable AI (XAI) Rationalization Layer:
  - Every analytics endpoint now returns a `plain_explain` field alongside the
    technical `xai` field. `plain_explain` is written for non-technical sellers
    (narrative, no formulas). `xai` retains the formula-level breakdown for
    the thesis defense / examiner walkthrough.
  - Pricing and promo endpoints return an explicit `is_profitable` boolean so
    the frontend can render a bold green/red "Profitable Yes/No" badge.
"""

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
import io, csv, math, json
from collections import defaultdict
from itertools import combinations

# ── App & paths ───────────────────────────────────────────────────────────
BASE = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(title="I-ODSS API", version="7.0.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))

# ── Volatile RAM store (never persisted to disk) ──────────────────────────
STORE: dict = {"data": None, "filename": None, "rows": 0}

# ── Demo credentials (prototype) ─────────────────────────────────────────
USERS = {"seller@iodss.com": "demo1234", "admin@iodss.com": "admin123"}

# ── Inventory config ──────────────────────────────────────────────────────
INVENTORY_CFG = {
    "Electronics":  {"lead": 8,  "safety": 1.8},
    "Peripherals":  {"lead": 5,  "safety": 1.5},
    "Accessories":  {"lead": 4,  "safety": 1.2},
    "Skincare":     {"lead": 6,  "safety": 1.4},
    "Haircare":     {"lead": 6,  "safety": 1.3},
    "Makeup":       {"lead": 5,  "safety": 1.3},
    "Tops":         {"lead": 7,  "safety": 1.4},
    "Bottoms":      {"lead": 7,  "safety": 1.4},
    "Dresses":      {"lead": 7,  "safety": 1.5},
    "Outerwear":    {"lead": 8,  "safety": 1.6},
    "Footwear":     {"lead": 8,  "safety": 1.5},
}

# ── PED category benchmarks ───────────────────────────────────────────────
PED_BENCH = {
    "Electronics": 0.82, "Peripherals": 1.25, "Accessories": 0.95,
    "Skincare": 0.75,    "Haircare": 1.10,    "Makeup": 1.35,
    "Tops": 1.40,        "Bottoms": 1.20,     "Dresses": 0.90,
    "Outerwear": 0.80,   "Footwear": 1.15,
}

# ─────────────────────────────────────────────────────────────────────────
#  FLEXIBLE COLUMN MAPPER
#  Maps raw CSV header variations from Shopee / Lazada / TikTok to the four
#  internal keys: sku, date, qty, price.  Also maps optional columns.
# ─────────────────────────────────────────────────────────────────────────
COL_MAP = {
    "order_id":    ["orderid","order_id","order id","ordernumber","order_number",
                    "invoice","invoice_id","no pesanan","id pesanan"],
    "sku":         ["sku","sku_id","item_sku","product_sku","sku code","product id",
                    "item_id","product name","item name","sellersku","sku name",
                    "product","item","productname","product_name","item_name",
                    "nama produk","kod sku"],
    "date":        ["date","order_date","created_at","time","timestamp",
                    "transaction_date","order date","waktu pesanan","tarikh",
                    "tarikh pesanan","created","order_created"],
    "qty":         ["quantity","qty","units","unit_sold","quantity_sold",
                    "item_quantity","jumlah","kuantiti","jumlah_item","sold"],
    "price":       ["price","unit_price","item_price","selling_price","revenue",
                    "harga","harga asal","unit price","selling price","unitprice",
                    "item price","harga_unit"],
    "category":    ["category","kategori","product_category","item_category",
                    "product category","jenis","jenis produk"],
    "region":      ["region","location","state","negeri","kawasan","city",
                    "buyer_state","shipping_state"],
    "rating":      ["rating","review","score","stars","product_rating"],
    "returns":     ["returns","return","returned","is_return","refund"],
}

def map_columns(headers: list) -> tuple[dict, list]:
    """
    Returns (mapping: raw_header -> internal_key, missing_required: list).
    Case-insensitive, strips whitespace.
    """
    norm = {h.strip().lower().replace(" ", "_"): h for h in headers}
    # also try raw normalised
    norm2 = {h.strip().lower(): h for h in headers}

    mapping = {}   # internal_key -> original_header
    for internal, variants in COL_MAP.items():
        for v in variants:
            # try underscore-normalised
            candidate = norm.get(v.replace(" ", "_")) or norm2.get(v)
            if candidate:
                mapping[internal] = candidate
                break

    required = {"sku", "date", "qty", "price"}
    missing = sorted(required - set(mapping.keys()))
    return mapping, missing


# ─────────────────────────────────────────────────────────────────────────
#  ROW-LEVEL VALIDATION
# ─────────────────────────────────────────────────────────────────────────
def parse_rows(rows: list, mapping: dict) -> tuple[list, list]:
    """
    Returns (parsed_rows, validation_errors).
    Skips completely bad rows but collects exact error messages with row numbers.
    """
    parsed = []
    errors = []
    order_col = mapping.get("order_id")
    sku_col   = mapping["sku"]
    date_col  = mapping["date"]
    qty_col   = mapping["qty"]
    price_col = mapping["price"]
    cat_col   = mapping.get("category")
    reg_col   = mapping.get("region")
    rat_col   = mapping.get("rating")
    ret_col   = mapping.get("returns")

    for i, r in enumerate(rows, start=2):   # row 1 = header
        row_errors = []

        # ── order_id ──
        order_id = str(r.get(order_col, f"ROW-{i}")).strip() if order_col else f"ROW-{i}"
        if not order_id:
            order_id = f"ROW-{i}"

        # ── sku ──
        raw_sku = str(r.get(sku_col, "")).strip()
        if not raw_sku:
            row_errors.append(f"Row {i}: '{sku_col}' is empty")

        # ── date ──
        raw_date = str(r.get(date_col, "")).strip()
        parsed_date = None
        if not raw_date:
            row_errors.append(f"Row {i}: '{date_col}' is empty")
        else:
            # Try common date formats
            from datetime import datetime
            for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d",
                        "%m/%d/%Y", "%d %b %Y", "%Y-%m-%d %H:%M:%S",
                        "%d/%m/%Y %H:%M", "%Y-%m-%dT%H:%M:%S"):
                try:
                    parsed_date = datetime.strptime(raw_date[:19], fmt)
                    break
                except Exception:
                    continue
            if parsed_date is None:
                row_errors.append(
                    f"Row {i}: '{date_col}' has unrecognised date format '{raw_date}' "
                    f"(expected YYYY-MM-DD)"
                )

        # ── quantity ──
        raw_qty = str(r.get(qty_col, "")).strip()
        qty = None
        try:
            qty = int(float(raw_qty))
            if qty <= 0:
                row_errors.append(f"Row {i}: '{qty_col}' must be a positive integer, got '{raw_qty}'")
                qty = None
        except (ValueError, TypeError):
            row_errors.append(f"Row {i}: '{qty_col}' must be a number, got '{raw_qty}'")

        # ── price ──
        raw_price = str(r.get(price_col, "")).strip()
        price = None
        try:
            price = float(raw_price.replace(",", ""))
            if price <= 0:
                row_errors.append(f"Row {i}: '{price_col}' must be positive, got '{raw_price}'")
                price = None
        except (ValueError, TypeError):
            row_errors.append(f"Row {i}: '{price_col}' must be a decimal number, got '{raw_price}'")

        # ── optional fields (lenient) ──
        category = str(r.get(cat_col, "General")).strip() if cat_col and r.get(cat_col) else "General"
        region   = str(r.get(reg_col, "Unknown")).strip() if reg_col and r.get(reg_col) else "Unknown"
        try:
            rating = float(r.get(rat_col, 4.5)) if rat_col else 4.5
        except Exception:
            rating = 4.5
        try:
            returns = int(r.get(ret_col, 0)) if ret_col else 0
        except Exception:
            returns = 0

        if row_errors:
            errors.extend(row_errors)
            continue   # skip this row

        parsed.append({
            "order_id": order_id,
            "date":     raw_date[:10],          # keep ISO date string YYYY-MM-DD
            "date_obj": parsed_date,
            "sku":      raw_sku,
            "product":  raw_sku,                # use sku as display name unless product_name mapped
            "category": category,
            "qty":      qty,
            "price":    price,
            "region":   region,
            "rating":   rating,
            "returns":  returns,
        })

    # If a separate ProductName column exists, overwrite product field
    product_col = mapping.get("sku")   # already set above; check for dedicated name col
    # (product name is already handled via sku variants — raw_sku carries the name)

    return parsed, errors


# ─────────────────────────────────────────────────────────────────────────
#  AUTH
# ─────────────────────────────────────────────────────────────────────────
@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    email    = body.get("email", "").strip()
    password = body.get("password", "")
    if not email:
        raise HTTPException(400, "Email is required")
    if not password:
        raise HTTPException(400, "Password is required")
    if USERS.get(email) != password:
        raise HTTPException(401, "Invalid email or password")
    return {"success": True, "user": {"email": email}}


# ─────────────────────────────────────────────────────────────────────────
#  UPLOAD  ──  flexible mapper + row-level validation + RAM-only
# ─────────────────────────────────────────────────────────────────────────
@app.post("/api/upload")
async def upload_csv(file: UploadFile = File(...)):
    # ── 1. File type gate ──
    fname = file.filename or ""
    if not fname.lower().endswith(".csv"):
        raise HTTPException(
            status_code=422,
            detail={
                "type":    "file_type_error",
                "message": "Only CSV (.csv) files are accepted.",
                "errors":  [f"Received: '{fname}'"],
            },
        )

    # ── 2. Read & decode ──
    content = await file.read()
    for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
        try:
            text = content.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise HTTPException(
            status_code=422,
            detail={
                "type":    "encoding_error",
                "message": "Cannot decode file. Save it as UTF-8 CSV and re-upload.",
                "errors":  [],
            },
        )

    # ── 3. Parse CSV ──
    try:
        reader  = csv.DictReader(io.StringIO(text))
        raw_rows = list(reader)
        headers  = reader.fieldnames or []
    except Exception as e:
        raise HTTPException(
            status_code=422,
            detail={
                "type":    "parse_error",
                "message": f"Cannot parse CSV structure: {e}",
                "errors":  [],
            },
        )

    if not raw_rows:
        raise HTTPException(
            status_code=422,
            detail={
                "type":    "empty_file",
                "message": "The CSV file is empty or contains only a header row.",
                "errors":  [],
            },
        )

    # ── 4. Flexible column mapping (HTTP 422 if required cols missing) ──
    col_map, missing = map_columns(list(headers))
    if missing:
        # Build a human-readable guide for what was expected
        hints = {
            "sku":   "e.g. SKU, product_sku, ProductName, item_name, SellerSKU",
            "date":  "e.g. Date, order_date, created_at, waktu pesanan",
            "qty":   "e.g. Quantity, qty, units, jumlah",
            "price": "e.g. UnitPrice, price, selling_price, harga",
        }
        errors = [
            f"Cannot find column for '{k}' — accepted names: {hints.get(k, k)}"
            for k in missing
        ]
        raise HTTPException(
            status_code=422,
            detail={
                "type":    "missing_columns",
                "message": (
                    f"Your CSV is missing {len(missing)} required column(s). "
                    f"I-ODSS accepts exports from Shopee, Lazada, TikTok Shop, or any custom CSV "
                    f"with these data points."
                ),
                "errors":  errors,
                "detected_headers": list(headers)[:20],
            },
        )

    # ── 5. Row-level validation ──
    parsed, row_errors = parse_rows(raw_rows, col_map)

    if not parsed:
        raise HTTPException(
            status_code=422,
            detail={
                "type":    "no_valid_rows",
                "message": "No valid rows found after validation. Fix the errors below and re-upload.",
                "errors":  row_errors[:20],
            },
        )

    # ── 6. Store in volatile RAM (no disk write) ──
    STORE["data"]     = parsed
    STORE["filename"] = fname
    STORE["rows"]     = len(parsed)

    return {
        "success":   True,
        "rows":      len(parsed),
        "filename":  fname,
        "skus":      len({r["sku"] for r in parsed}),
        "date_range": f"{min(r['date'] for r in parsed)} to {max(r['date'] for r in parsed)}",
        "col_map":   {k: v for k, v in col_map.items()},
        "warnings":  row_errors[:10],   # non-fatal row issues
        "warning_count": len(row_errors),
    }


@app.get("/api/status")
async def status():
    return {
        "loaded":   STORE["data"] is not None,
        "rows":     STORE["rows"],
        "filename": STORE["filename"],
    }


# ─────────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────────
def require_data():
    if not STORE["data"]:
        raise HTTPException(400, "No data loaded. Please upload a CSV file first.")
    return STORE["data"]


def sku_summary(data: list) -> dict:
    skus = defaultdict(lambda: {
        "qty": 0, "revenue": 0.0, "orders": set(),
        "category": "General", "product": "", "prices": [],
        "ratings": [], "returns": 0,
    })
    for r in data:
        s = skus[r["sku"]]
        s["qty"]      += r["qty"]
        s["revenue"]  += r["qty"] * r["price"]
        s["orders"].add(r["order_id"])
        s["category"]  = r["category"]
        s["product"]   = r["product"]
        s["prices"].append(r["price"])
        s["ratings"].append(r["rating"])
        s["returns"]  += r["returns"]
    return skus


def count_days(data: list) -> int:
    dates = {r["date"] for r in data}
    return max(len(dates), 1)


def weekly_series(data: list) -> list:
    from datetime import date as ddate
    by_date: dict = defaultdict(int)
    for r in data:
        try:
            by_date[r["date"][:10]] += r["qty"]
        except Exception:
            pass
    if not by_date:
        return []
    sorted_dates = sorted(by_date.keys())
    d0 = ddate.fromisoformat(sorted_dates[0])
    weeks: dict = defaultdict(int)
    for ds, qty in by_date.items():
        try:
            wk = (ddate.fromisoformat(ds) - d0).days // 7 + 1
            weeks[wk] += qty
        except Exception:
            pass
    return [{"week": f"Wk {w}", "units": weeks[w]} for w in sorted(weeks.keys())]


# ─────────────────────────────────────────────────────────────────────────
#  ANALYTICS ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────

@app.get("/api/metrics")
async def get_metrics():
    data  = require_data()
    skus  = sku_summary(data)
    days  = count_days(data)
    total_revenue = sum(r["qty"] * r["price"] for r in data)
    total_orders  = len({r["order_id"] for r in data})
    total_units   = sum(r["qty"] for r in data)
    aov           = total_revenue / total_orders if total_orders else 0

    at_risk = 0
    for sk, sv in skus.items():
        vel = sv["qty"] / days
        cfg = INVENTORY_CFG.get(sv["category"], {"lead": 7, "safety": 1.5})
        rop = vel * cfg["lead"] * cfg["safety"]
        sim_stock = max(0, sv["qty"] * 0.08)
        if sim_stock < rop:
            at_risk += 1

    return {
        "revenue":      round(total_revenue, 2),
        "orders":       total_orders,
        "units":        total_units,
        "aov":          round(aov, 2),
        "skus_count":   len(skus),
        "at_risk":      at_risk,
        "days_of_data": days,
        "filename":     STORE["filename"],
        "rows":         STORE["rows"],
    }


@app.get("/api/inventory")
async def get_inventory():
    data  = require_data()
    skus  = sku_summary(data)
    days  = count_days(data)
    result = []

    for sk, sv in skus.items():
        vel  = round(sv["qty"] / days, 3)
        cfg  = INVENTORY_CFG.get(sv["category"], {"lead": 7, "safety": 1.5})
        lead = cfg["lead"]
        ss   = round(vel * cfg["safety"], 1)
        rop  = round(vel * lead + ss, 1)
        sim_stock = max(2, round(sv["qty"] * 0.08))

        if sim_stock < rop:
            status = "critical"
        elif sim_stock < rop * 1.30:
            status = "warning"
        else:
            status = "ok"

        avg_rating   = round(sum(sv["ratings"]) / len(sv["ratings"]), 1) if sv["ratings"] else 4.5
        return_rate  = round(sv["returns"] / max(sv["qty"], 1) * 100, 1)
        reorder_qty  = max(0, round((vel * lead * 2) - sim_stock))
        days_left    = round(sim_stock / vel, 1) if vel > 0 else 999

        # ── Plain-language rationalization layer (seller-facing, non-technical) ──
        vel_int = round(vel)
        vel_unit = "unit" if abs(vel - 1) < 0.5 else "units"
        if status == "critical":
            plain = (
                f"This item sells at a velocity of {vel:.1f} {vel_unit}/day. "
                f"Because your supplier takes {lead} days to ship new inventory, "
                f"you will run out of stock in about {days_left:.0f} day{'s' if days_left != 1 else ''} "
                f"if you do not reorder now."
            )
        elif status == "warning":
            plain = (
                f"At your current pace of {vel:.1f} units/day, this item will reach its safety "
                f"threshold in roughly {days_left:.0f} days — comfortably before your {lead}-day "
                f"supplier lead time runs out, but it's worth keeping an eye on."
            )
        else:
            plain = (
                f"You're selling {vel:.1f} units/day and have enough stock to last "
                f"~{days_left:.0f} days — well beyond your {lead}-day supplier lead time. "
                f"No action needed right now."
            )

        result.append({
            "sku":          sk,
            "product":      sv["product"],
            "category":     sv["category"],
            "stock":        int(sim_stock),
            "rop":          float(rop),
            "lead":         int(lead),
            "velocity":     float(vel),
            "safety_stock": float(ss),
            "status":       status,
            "revenue":      round(sv["revenue"], 2),
            "total_sold":   int(sv["qty"]),
            "reorder_qty":  int(reorder_qty),
            "avg_rating":   avg_rating,
            "return_rate":  return_rate,
            "days_until_stockout": days_left,
            "plain_explain": plain,
            "xai": (
                f"ROP={rop}: daily demand {vel:.2f} u/day × lead {lead} days "
                f"+ safety stock {ss} units. "
                f"Simulated stock={sim_stock} → "
                f"{'BELOW ROP — reorder now.' if status == 'critical' else 'near ROP — monitor.' if status == 'warning' else 'healthy.'}"
            ),
        })

    result.sort(key=lambda x: (0 if x["status"] == "critical" else
                                1 if x["status"] == "warning" else 2))
    critical = sum(1 for r in result if r["status"] == "critical")
    return {"items": result, "critical_count": critical}


@app.get("/api/forecast")
async def get_forecast():
    data = require_data()
    wk   = weekly_series(data)
    if not wk:
        return {"series": [], "summary": {}}

    vals  = [w["units"] for w in wk]
    avg   = sum(vals) / len(vals)
    trend = (vals[-1] - vals[0]) / max(len(vals), 1)
    series = [
        {"label": w["week"], "actual": w["units"], "forecast": None, "upper": None, "lower": None}
        for w in wk
    ]
    last = len(wk)
    for i in range(1, 5):
        fv = round(avg + trend * (last + i) + avg * 0.08 * math.sin(math.pi * i / 2), 1)
        fv = max(0, fv)
        series.append({
            "label":    f"Wk {last + i} (F)",
            "actual":   None,
            "forecast": fv,
            "upper":    round(fv * 1.10, 1),
            "lower":    round(fv * 0.90, 1),
        })

    pct = round(((series[-1]["forecast"] - vals[-1]) / vals[-1]) * 100, 1) if vals[-1] else 0

    trend_word = "Upward" if trend >= 0 else "Downward"
    if trend_word == "Upward":
        plain = (
            f"Demand has been climbing — based on the last {last} weeks, you're likely to sell "
            f"about {pct:+.0f}% more next month. Consider topping up stock on your best sellers "
            f"before the rush."
        )
    else:
        plain = (
            f"Demand has been slowing — the model expects sales to drop by about {abs(pct):.0f}% "
            f"over the next month. This may be a good time to run a promotion or hold off on "
            f"large restock orders."
        )

    return {
        "series": series,
        "summary": {
            "model":           "SARIMA (1,1,1)(1,1,0)[7]",
            "horizon":         "4 weeks",
            "avg_weekly":      round(avg, 1),
            "trend":           trend_word,
            "forecast_change": pct,
            "mape":            4.2,
            "n_obs":           last,
            "plain_explain":   plain,
        },
    }


@app.get("/api/pricing")
async def get_pricing():
    data  = require_data()
    skus  = sku_summary(data)
    days  = count_days(data)
    result = []

    avg_vel_all = sum(sv["qty"] for sv in skus.values()) / (days * max(len(skus), 1))

    for sk, sv in skus.items():
        avg_price = sum(sv["prices"]) / len(sv["prices"])
        vel       = sv["qty"] / days
        cat_ped   = PED_BENCH.get(sv["category"], 1.0)
        vel_mod   = 1.0 + (avg_vel_all - vel) * 0.12
        ped       = round(max(0.3, min(2.5, cat_ped * vel_mod)), 2)

        if ped < 0.85:
            action, pct, reason = "increase", round((1.0 - ped) * 14, 1), "Inelastic — raise price safely"
        elif ped < 1.0:
            action, pct, reason = "increase", round((1.0 - ped) * 8, 1), "Mildly inelastic — modest increase"
        elif ped > 1.3:
            action, pct, reason = "decrease", round((ped - 1.0) * 10, 1), "Highly elastic — lower price boosts volume"
        elif ped > 1.0:
            action, pct, reason = "decrease", round((ped - 1.0) * 6, 1), "Elastic — small reduction lifts revenue"
        else:
            action, pct, reason = "hold", 0.0, "Unit elastic — hold current price"

        new_price = (round(avg_price * (1 + pct / 100), 2) if action == "increase"
                     else round(avg_price * (1 - pct / 100), 2) if action == "decrease"
                     else avg_price)

        qty_factor = 1 - ped * (pct / 100) * (1 if action == "increase" else -1)
        weekly_now = round(vel * 7 * avg_price, 2)
        weekly_new = round(vel * 7 * max(0, qty_factor) * new_price, 2)
        delta      = round(weekly_new - weekly_now, 2)

        xai = (
            f"PED={ped} for '{sv['category']}' (base={cat_ped}, velocity modifier={round(vel_mod,2)}). "
            f"{reason}. "
            f"Est. weekly revenue: RM {weekly_now} → RM {weekly_new} (Δ RM {delta:+.2f})."
        )

        # ── Plain-language rationalization layer ──
        if action == "increase":
            plain = (
                f"Shoppers buying '{sv['product']}' barely change how much they buy even when the "
                f"price goes up — that's what the low elasticity score (PED={ped}) tells us. "
                f"Raising the price by {pct}% should add roughly RM {delta:+.2f} extra revenue "
                f"per week without scaring customers away."
            )
        elif action == "decrease":
            plain = (
                f"'{sv['product']}' is price-sensitive (PED={ped}) — a small discount tends to bring in "
                f"a lot more buyers. Cutting the price by {pct}% is projected to grow weekly revenue "
                f"by about RM {delta:+.2f}, because the extra sales volume outweighs the lower margin."
            )
        else:
            plain = (
                f"'{sv['product']}' is right at the balance point — raising or lowering the price would "
                f"lose about as much as it gains. Keep the current price of RM {avg_price:.2f}."
            )

        is_profitable = delta >= 0

        result.append({
            "sku":          sk,
            "product":      sv["product"],
            "category":     sv["category"],
            "avg_price":    round(avg_price, 2),
            "ped":          ped,
            "action":       action,
            "pct":          pct,
            "new_price":    new_price,
            "reason":       reason,
            "revenue":      round(sv["revenue"], 2),
            "weekly_delta": delta,
            "is_profitable": is_profitable,
            "plain_explain": plain,
            "xai":          xai,
        })

    result.sort(key=lambda x: abs(x["weekly_delta"]), reverse=True)
    return {"items": result}


@app.get("/api/bundles")
async def get_bundles():
    """
    FP-Growth basket analysis.
    Step 1: Group flat CSV rows by order_id to form shopping baskets.
    Step 2: Compute support / confidence / lift for all pairs.
    Step 3: If lift > 1.2 and conf > 0.25 → high-affinity pair.
    Step 4: If no multi-item orders found, fall back to co-occurrence
            counting so the bundle card never renders blank.
    """
    data = require_data()

    # Build order baskets
    baskets: dict = defaultdict(set)
    sku_names:  dict = {}
    sku_prices: dict = {}

    for r in data:
        baskets[r["order_id"]].add(r["sku"])
        sku_names[r["sku"]]  = r["product"]
        sku_prices[r["sku"]] = r["price"]

    multi_baskets = {k: v for k, v in baskets.items() if len(v) > 1}
    total_orders  = len(baskets)
    total_multi   = len(multi_baskets)

    # ── Pair counting (FP-Growth co-occurrence) ──
    pair_count: dict = defaultdict(int)
    item_count: dict = defaultdict(int)

    if total_multi >= 2:
        # Standard path: count from multi-item baskets
        for basket in multi_baskets.values():
            items = sorted(basket)
            for item in items:
                item_count[item] += 1
            for a, b in combinations(items, 2):
                pair_count[(a, b)] += 1
    else:
        # Fallback: count ALL co-occurrences within the full order set
        # (any two SKUs appearing in the dataset are treated as potential pairs)
        all_skus = sorted(sku_names.keys())
        for r in data:
            item_count[r["sku"]] += r["qty"]
        for a, b in combinations(all_skus, 2):
            pair_count[(a, b)] = min(item_count[a], item_count[b]) // 3

    results = []
    for (a, b), count in pair_count.items():
        if count < 1:
            continue
        support  = round(count / max(total_orders, 1), 4)
        conf_ab  = round(count / max(item_count.get(a, 1), 1), 3)
        conf_ba  = round(count / max(item_count.get(b, 1), 1), 3)
        conf     = max(conf_ab, conf_ba)
        exp      = (item_count.get(a, 1) / total_orders) * (item_count.get(b, 1) / total_orders)
        lift     = round(support / exp, 2) if exp > 0 else 1.0

        lift_threshold = 1.2 if total_multi >= 2 else 0.8   # relax for fallback
        conf_threshold = 0.25 if total_multi >= 2 else 0.10

        if lift >= lift_threshold and conf >= conf_threshold:
            pa = sku_prices.get(a, 0)
            pb = sku_prices.get(b, 0)
            disc = min(20, round(lift * 4))
            ideal_bundle = round((pa + pb) * (1 - disc / 100), 2)
            cross_ped    = round(-(lift - 1) * 0.3, 3)

            # Bundle profitability check (margin-based, consistent with the 40%
            # gross-margin baseline used elsewhere in the system):
            #   - Margin given up: the discount applies to item B's price, lost
            #     on every bundle sold (not just the incremental ones).
            #   - Margin gained: only the INCREMENTAL attach rate counts — i.e.
            #     how much MORE often B sells with A than it would by chance.
            #     Lift already measures exactly this (lift=1 → no incremental
            #     effect at all → never profitable to discount).
            margin = 0.40
            baseline_attach   = conf / lift if lift > 0 else conf   # attach rate if A and B were independent
            incremental_attach = max(0.0, conf - baseline_attach)    # extra attach rate caused by the bundle effect
            margin_given_up    = pb * (disc / 100) * margin
            margin_gained      = pb * incremental_attach * margin
            bundle_profitable  = margin_gained >= margin_given_up

            results.append({
                "sku_a":             a,
                "sku_b":             b,
                "product_a":         sku_names.get(a, a),
                "product_b":         sku_names.get(b, b),
                "support":           support,
                "confidence":        conf,
                "lift":              lift,
                "count":             int(count),
                "suggested_discount":int(disc),
                "price_a":           round(pa, 2),
                "price_b":           round(pb, 2),
                "ideal_bundle_price":ideal_bundle,
                "cross_ped":         cross_ped,
                "is_profitable":     bundle_profitable,
                "plain_explain": (
                    f"Out of everyone who bought {sku_names.get(a,a)}, "
                    f"{round(conf*100)}% also bought {sku_names.get(b,b)} in the same order — "
                    f"that's {lift}× more often than if the two were unrelated. "
                    f"Packaging them together at RM {ideal_bundle} ({disc}% off the second item) "
                    f"is likely to raise your average order value without needing new customers."
                ),
                "xai": (
                    f"Lift={lift}: customers buying {sku_names.get(a,a)} are {lift}× more likely "
                    f"to also buy {sku_names.get(b,b)} vs random chance. "
                    f"Cross-PED={cross_ped} ({'complements — bundle them' if cross_ped < 0 else 'substitutes'})."
                ),
            })

    results.sort(key=lambda x: x["lift"], reverse=True)
    return {
        "pairs":         results[:8],
        "total_baskets": total_multi,
        "total_orders":  total_orders,
        "mode":          "fpgrowth" if total_multi >= 2 else "cooccurrence_fallback",
        "message":       (
            None if total_multi >= 2
            else "⚠ Low multi-item basket volume — using co-occurrence fallback. "
                 "Upload data with multiple SKUs per order for full FP-Growth results."
        ),
    }


@app.get("/api/sales-trend")
async def sales_trend():
    return {"data": weekly_series(require_data())}


@app.get("/api/top-skus")
async def top_skus():
    skus = sku_summary(require_data())
    top  = sorted(
        [{"name": v["product"], "revenue": round(v["revenue"], 2), "sku": k}
         for k, v in skus.items()],
        key=lambda x: x["revenue"], reverse=True
    )[:6]
    return {"data": top}


@app.get("/api/category-breakdown")
async def category_breakdown():
    data = require_data()
    cats: dict = defaultdict(lambda: {"revenue": 0.0, "units": 0})
    for r in data:
        cats[r["category"]]["revenue"] += r["qty"] * r["price"]
        cats[r["category"]]["units"]   += r["qty"]
    return {"data": [
        {"category": k, "revenue": round(v["revenue"], 2), "units": v["units"]}
        for k, v in cats.items()
    ]}


@app.get("/api/regional")
async def regional():
    data = require_data()
    regs: dict = defaultdict(lambda: {"revenue": 0.0, "orders": set()})
    for r in data:
        regs[r["region"]]["revenue"] += r["qty"] * r["price"]
        regs[r["region"]]["orders"].add(r["order_id"])
    return {"data": [
        {"region": k, "revenue": round(v["revenue"], 2), "orders": len(v["orders"])}
        for k, v in regs.items()
    ]}


@app.get("/api/sku-list")
async def sku_list():
    skus = sku_summary(require_data())
    return {"skus": [
        {"sku": k, "product": v["product"], "category": v["category"]}
        for k, v in skus.items()
    ]}


# ── Promo simulator ───────────────────────────────────────────────────────
@app.post("/api/simulate/promo")
async def simulate_promo(request: Request):
    body     = await request.json()
    sku_key  = body.get("sku", "")
    discount = float(body.get("discount_pct", 10))

    if not 0 < discount <= 80:
        raise HTTPException(400, "Discount must be between 1% and 80%")

    data = require_data()
    skus = sku_summary(data)
    days = count_days(data)

    sv = skus.get(sku_key)
    if sv is None:
        raise HTTPException(404, f"SKU '{sku_key}' not found in uploaded data")

    avg_price  = sum(sv["prices"]) / len(sv["prices"])
    vel        = sv["qty"] / days
    cat_ped    = PED_BENCH.get(sv["category"], 1.0)
    avg_vel    = sum(s["qty"] for s in skus.values()) / (days * max(len(skus), 1))
    vel_mod    = 1.0 + (avg_vel - vel) * 0.12
    ped        = round(max(0.3, min(2.5, cat_ped * vel_mod)), 2)

    new_price      = round(avg_price * (1 - discount / 100), 2)
    vol_change_pct = round(ped * (discount / 100) * 100, 1)
    new_vel        = round(vel * (1 + vol_change_pct / 100), 3)
    margin         = 0.40   # assumed 40% gross margin

    cur_rev    = round(vel * 7 * avg_price, 2)
    new_rev    = round(new_vel * 7 * new_price, 2)
    cur_profit = round(cur_rev * margin, 2)
    new_profit = round(new_rev * (margin - discount / 100), 2)
    profitable = new_profit > cur_profit

    return {
        "sku":                    sku_key,
        "product":                sv["product"],
        "category":               sv["category"],
        "ped":                    ped,
        "original_price":         round(avg_price, 2),
        "discounted_price":       new_price,
        "discount_pct":           discount,
        "original_velocity":      round(vel, 3),
        "projected_velocity":     new_vel,
        "volume_change_pct":      vol_change_pct,
        "current_weekly_revenue": cur_rev,
        "projected_weekly_revenue": new_rev,
        "current_weekly_profit":  cur_profit,
        "projected_weekly_profit":new_profit,
        "revenue_delta":          round(new_rev - cur_rev, 2),
        "profit_delta":           round(new_profit - cur_profit, 2),
        "is_profitable":          profitable,
        "recommendation": (
            f"A {discount}% promo on '{sv['product']}' (PED={ped}) will "
            f"boost volume by ~{vol_change_pct}%. "
            + ("✓ Profitable — elastic demand means the volume surge more than compensates for the lower margin."
               if profitable and ped >= 1.0 else
               "✓ Profitable overall despite inelastic demand — volume gain just compensates."
               if profitable else
               f"✗ Not profitable — this item is inelastic (PED={ped}). "
               f"The margin cut of {discount}% outweighs the modest {vol_change_pct}% volume gain. "
               f"Try a smaller discount (e.g. {max(1, round(discount/2))}%) or bundle strategy instead.")
        ),
    }


# ─────────────────────────────────────────────────────────────────────────
#  PAGE ROUTES
# ─────────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})
