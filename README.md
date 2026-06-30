# I-ODSS v7 — Intelligent Operational Decision Support System
### CDCS230 Final Year Project | FastAPI + Vanilla JS

---

## Quick Start

### Windows — double-click run.bat
```
run.bat
```

### Manual
```bash
cd backend
python -m pip install -r requirements.txt
python -m uvicorn main:app --reload --port 8000
```

Open: **http://localhost:8000**

---

## Login Credentials (Demo)

| Email | Password |
|---|---|
| seller@iodss.com | demo1234 |
| admin@iodss.com | admin123 |

---

## What's new in v7

### 1. Explainable AI (XAI) Rationalization Layer
Every prescriptive output now carries **two** explanations side by side:

- `xai` — the formula-level breakdown (kept exactly as before, for the thesis
  defense / examiner walkthrough). Example: `"ROP=7.4: daily demand 1.15 u/day
  × lead 5 days + safety stock 1.7 units..."`
- `plain_explain` — a new narrative, jargon-free translation aimed at a seller
  with no data science background. Example: `"This item sells at a velocity
  of 1.1 unit/day. Because your supplier takes 5 days to ship new inventory,
  you will run out of stock in about 5 days if you do not reorder now."`

This applies to **Inventory, Pricing, Bundles, and Forecast** — every
endpoint that previously returned only a technical `xai` field now also
returns `plain_explain`. The UI shows the plain-language box (💡 amber,
"Why this matters") above the technical XAI box (purple) everywhere a
recommendation is displayed, so non-technical sellers read the story first
and can still drill into the math if they want to.

### 2. "Fast-Action, Deep-Verification" Single-Scroll Dashboard
The Dashboard page is now explicitly split into two labelled layers on one
continuous scroll, instead of being a flat stack of widgets:

- **Execution layer** (top) — the four colour-coded action cards render the
  moment a file finishes parsing, plus a single 💡 callout surfacing the
  most urgent plain-language insight (e.g. the most critical stockout) right
  under the cards. A seller can act from this layer alone, no scrolling.
- **Evidence layer** (below, same page) — KPI tiles and the weekly
  sales / top-SKU charts that let a seller scroll down and verify the
  numbers behind the cards above, building trust in the system before they
  commit to a decision.

The dedicated Inventory / Pricing / Bundles / Forecast tabs in the sidebar
still exist for full deep-dive detail and are unchanged in their own
single-scroll structure (KPIs → list/chart → recommendations).

### 3. Bold "Profitable? Yes/No" Badges
Wherever the system makes a margin-affecting recommendation, there is now an
explicit, high-contrast badge — not just numbers the seller has to interpret
themselves:

- **`✓ Profitable recommendation`** (green) or **`✗ Margin loss warning`**
  (red), shown in:
  - the **Promo Simulator** results (headline badge above the metric grid)
  - every row in the **Pricing** action card
  - every pair in the **Bundles** action card

The bundle badge uses a real margin model (40% gross margin baseline, same
as the promo simulator) comparing the margin given up on the suggested
discount against the *incremental* attach-rate revenue the bundle effect
creates (lift, not raw confidence, drives the incremental term — so a pair
with lift=1.0 literally cannot be profitable, which is the correct
behaviour: no lift means no causal bundle effect to monetise).

---

## Carried over from v6 (unchanged)

### Flexible Column Mapper
Accepts exports from **Shopee, Lazada, TikTok Shop** or any custom CSV.
Auto-maps header variations:
- SKU: `sku`, `product_name`, `item_name`, `SellerSKU`, `ProductName`, `product id`…
- Date: `date`, `order_date`, `created_at`, `waktu pesanan`, `timestamp`…
- Quantity: `quantity`, `qty`, `units`, `jumlah`, `kuantiti`…
- Price: `price`, `unit_price`, `selling_price`, `harga`, `UnitPrice`…

### Real-time Inline Validation Errors
- HTTP 422 with exact error details shown inline in the upload card
- Row-level errors: `"Row 45: UnitPrice must be a decimal number, got 'N/A'"`
- Missing column errors show a column name guide automatically
- Non-fatal row warnings shown as a collapsible list

### FP-Growth Basket Analysis
- Orders grouped by OrderID → shopping baskets
- Co-occurrence fallback ensures bundle cards never render blank
- Lift ≥ 1.2 threshold, Cross-PED per pair
- **Use `data_bundling_test.csv`** for full FP-Growth results (89% multi-item
  order rate — verified to surface 4 real pairs, not the fallback path)

### RAM-Only Architecture
- Zero disk writes of transaction data
- All parsed rows stored in a single in-memory `STORE` dict
- Overwritten on every new upload (no accumulation, no persistence)

---

## A note on dependencies (why requirements.txt is still minimal)

An earlier round of feedback suggested adding `pandas`, `numpy`,
`SQLAlchemy`, `statsmodels`, `mlxtend`, `scikit-learn`, `python-jose`, and
`bcrypt` to `requirements.txt`. **These were deliberately not added**,
because nothing in `main.py` imports them — the entire backend (CSV parsing,
ROP, PED, FP-Growth co-occurrence counting, the forecast heuristic) is
hand-written using only the Python standard library (`csv`, `math`,
`collections.defaultdict`, `itertools.combinations`). This was an explicit
architectural choice made earlier in the project specifically to satisfy the
thesis's RAM-only / minimal-footprint constraint and to keep `pip install`
fast and dependency-conflict-free for examiners running the system cold.
Adding those packages without using them would only slow down setup and
risk version conflicts on examiners' machines, with zero functional benefit.
If a future version genuinely adopts `statsmodels` for real SARIMA fitting
or `mlxtend` for textbook FP-Growth, the corresponding import should be
added to `requirements.txt` **at that time**, not preemptively.

---

## Sample Files (in frontend/static/samples/)

| File | Use Case |
|---|---|
| `data_bundling_test.csv` | **Best for demo** — 120 orders, multi-SKU per order, tests all 4 action cards including FP-Growth bundles |
| `data_electronics.csv` | Electronics & Peripherals — 150 rows, 8 SKUs |
| `data_fashion.csv` | Fashion & Apparel — 100 rows, 8 SKUs |
| `data_beauty.csv` | Health & Beauty — 100 rows, 8 SKUs |

Download from inside the app via the **Requirements** button.

---

## Project Structure

```
iodss7/
├── run.bat
├── README.md
├── backend/
│   ├── main.py          ← FastAPI — routes, mapper, validation, XAI layer
│   └── requirements.txt
└── frontend/
    ├── templates/
    │   ├── login.html     ← Dark theme, password strength meter, register tab
    │   └── dashboard.html ← Single-scroll SPA — Execution/Evidence layers,
    │                          insight callouts, profit badges, EDA, promo sim
    └── static/
        └── samples/
            ├── data_bundling_test.csv
            ├── data_electronics.csv
            ├── data_fashion.csv
            └── data_beauty.csv
```

---

## Algorithms

| Algorithm | Formula | Notes |
|---|---|---|
| ROP | `d × L + safety_stock` | Lead times per category |
| SARIMA | `(1,1,1)(1,1,0)[7]` | Heuristic weekly trend + seasonality |
| FP-Growth | Lift ≥ 1.2, Conf ≥ 0.25 | Basket grouping by OrderID |
| PED | `%ΔQ / %ΔP` | Category benchmarks + velocity modifier |
| Promo Sim | PED × discount% = volume uplift | 40% gross margin assumed |
| Bundle profitability | `incremental_attach ≥ discount%` | incremental_attach = conf − conf/lift |

---

## API Endpoints

| Method | Endpoint | Description | New v7 fields |
|---|---|---|---|
| POST | `/api/login` | Authenticate | — |
| POST | `/api/upload` | Upload & validate CSV (flexible mapper) | — |
| GET | `/api/status` | Check if data is loaded | — |
| GET | `/api/metrics` | KPI summary | — |
| GET | `/api/inventory` | ROP per SKU | `plain_explain`, `days_until_stockout` |
| GET | `/api/forecast` | Demand forecast | `plain_explain` |
| GET | `/api/bundles` | FP-Growth basket pairs | `plain_explain`, `is_profitable` |
| GET | `/api/pricing` | PED pricing | `plain_explain`, `is_profitable` |
| GET | `/api/sales-trend` | Weekly unit sales | — |
| GET | `/api/top-skus` | Top SKUs by revenue | — |
| GET | `/api/category-breakdown` | Revenue by category | — |
| GET | `/api/regional` | Revenue by region | — |
| GET | `/api/sku-list` | All SKUs (for promo simulator) | — |
| POST | `/api/simulate/promo` | Promo discount simulator | `is_profitable` (unchanged, already present in v6) |
