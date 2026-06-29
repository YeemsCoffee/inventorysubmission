# ☕ Café Inventory & Replenishment (MVP)

A lightweight store-inventory app for café back storage, integrated with
**Unleashed Software**. Employees scan a QR/NFC tag when they pull stock for café
use; the app keeps each store's own live counts, generates daily replenishment
requests from **par − count**, submits them to Unleashed as **Sales Orders**, and
raises local stock back up by the **actual fulfilled quantity** once the order is
completed.

> Stores (Ktown, Gardena) are Unleashed **customers**, not warehouses. The app is
> the source of truth for store counts; Unleashed is the source of truth for
> warehouse stock, sales orders and fulfillment.

📐 Full design rationale, schema, workflow and integration details: **[DESIGN.md](DESIGN.md)**.

---

## The employee flow (the whole point)

1. **Scan** the QR / tap the NFC tag → opens `/scan/{tag_id}`
2. **See** the item name + current count
3. **Tap** a quantity — `1` `2` `3` `Case` `Custom`
4. **Submit** → "Removed 2 Oat Milk. New count: 16."

No login, no store/warehouse/action pickers, no search. Under 5 seconds.

---

## Quick start (local, SQLite)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # defaults to SQLite; fill Unleashed creds when ready
python -m scripts.seed        # stores, products, par levels, tags, demo logins

uvicorn app.main:app --reload
```

Open:

* Employee scan: <http://localhost:8000/scan/KTOWN-OATMILK>
* App (login): <http://localhost:8000/>  — seeded logins are printed by the seed
  script (e.g. `admin@yeemscoffee.com` / `admin123`). **Change these.**

Run the tests:

```bash
pytest
```

---

## Configuration (`.env`)

All settings — including the Unleashed credentials — are read from the
environment. **Secrets stay on the backend and are never exposed to the
browser.** See `.env.example` for the full list. Key ones:

| Variable | Purpose |
|----------|---------|
| `DATABASE_URL` | `sqlite:///./cafe_inventory.db` locally; `postgresql+psycopg2://…` in prod |
| `SECRET_KEY` | signs session cookies |
| `UNLEASHED_API_ID` / `UNLEASHED_API_KEY` | Unleashed API credentials (backend only) |
| `UNLEASHED_FULFILL_WAREHOUSE_CODE` | warehouse the orders ship **from** (stores aren't warehouses) |
| `UNLEASHED_CREATE_ORDER_STATUS` | `Parked` (API allows Parked/Completed on create) |
| `UNLEASHED_RECEIPT_USE_SHIPMENTS` | `true` = receive actual shipped qty; `false` = trust completed order lines |
| `UNLEASHED_WEBHOOK_SECRET` | shared secret for the webhook endpoint |
| `POLLING_ENABLED` / `POLLING_INTERVAL_MINUTES` | completion-polling fallback |

---

## Unleashed setup

1. **Create API credentials** in Unleashed (Integration → API Access) and put the
   ID/Key in `.env`. Admin → Unleashed Settings → **Test connection** verifies the
   HMAC signature.
2. **Map stores to customers** — set each store's `unleashed_customer_code` to the
   matching Unleashed customer (Admin → Stores).
3. **Match product codes** — each app product's `product_code` must equal the
   Unleashed product code (Admin → Products).
4. **Completion detection** — register a Sales Order **webhook** pointing at
   `https://<your-host>/webhooks/unleashed?secret=<UNLEASHED_WEBHOOK_SECRET>`
   (preferred), and/or leave polling on as a safety net. Both are idempotent.

---

## How completion → stock works

```
Submit  → Unleashed Sales Order (Parked), store Guid + OrderNumber + SourceId
Warehouse fulfils & Completes the order in Unleashed
Detect  → webhook or polling sees OrderStatus = Completed
Receive → read actual ShipmentQty (or order-line fallback) and add to local count
          (idempotent: never double-adds across duplicate webhooks/polls/retries)
```

Submitting **does not** change local counts — they rise only on confirmed
fulfillment.

---

## Deploy (container)

```bash
docker build -t cafe-inventory .
docker run -p 8000:8000 --env-file .env cafe-inventory
```

Point `DATABASE_URL` at managed Postgres, set a strong `SECRET_KEY`, real
Unleashed creds and `UNLEASHED_WEBHOOK_SECRET`, and run behind HTTPS. The MVP
uses `create_all` for tables; adopt Alembic migrations before evolving the schema
in production.

---

## Project layout

See **[DESIGN.md §14](DESIGN.md#14-project-structure)**. In short:
`app/services` (business logic, the only place counts change),
`app/integrations/unleashed.py` (HMAC client), `app/routers` (thin HTTP),
`app/templates` + `app/static` (mobile UI), `scripts/` (seed, tag URLs),
`tests/` (signature, removal, request, receipt + webhook idempotency).

## Security notes

* Unleashed keys exist only in backend env vars; the admin UI shows *whether*
  they're set, never their value. API errors are logged without secrets.
* Manager/Warehouse/Admin screens require login (role-checked). The employee scan
  page is intentionally open — the tag is the capability. For tighter control you
  can put it behind your store Wi-Fi/VPN or add a per-store PIN later.
