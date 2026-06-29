# Café Inventory & Replenishment — Design

A lightweight **store inventory layer** that owns each café's live stock counts and
uses **Unleashed Software** only to (a) raise replenishment requests as Sales
Orders and (b) detect fulfillment and read the **actual** delivered quantities
back into local stock.

> Scope: Ktown and Gardena cafés. Stores are Unleashed **customers**, not
> warehouses. The app never reads Stock On Hand for store inventory.

---

## 1. Recommended architecture

```
                          ┌─────────────────────────────────────────────┐
   Employee phone         │              Café Inventory App             │
   (QR / NFC scan)  ─────▶│  FastAPI + Jinja2 (server-rendered, mobile) │
                          │                                             │
   Manager / Warehouse ──▶│  Routers ─ Services ─ SQLAlchemy ─ Postgres │
   / Admin (browser)      │                  │                          │
                          │                  │ httpx (HMAC, backend)    │
                          └──────────────────┼──────────────────────────┘
                                             │
                       ┌─────────────────────┴───────────────────────┐
                       │  Completion detection (two paths):           │
                       │   • Webhook  POST /webhooks/unleashed        │
                       │   • Polling  APScheduler every N minutes     │
                       └─────────────────────┬───────────────────────┘
                                             ▼
                                   ┌────────────────────┐
                                   │ Unleashed Software │  (warehouse stock,
                                   │   REST API (HMAC)  │   sales orders,
                                   └────────────────────┘   fulfillment)
```

* **One small web service.** Server-rendered HTML keeps the employee scan flow a
  single fast page with no SPA build step. The same service exposes the manager,
  warehouse and admin screens, the webhook receiver, and runs the polling job.
* **Service layer owns all state changes.** Routes are thin; every inventory
  mutation goes through transaction-safe service functions that write a ledger
  row. `current_count` is never edited anywhere else.
* **Unleashed access is backend-only.** Credentials live in environment
  variables; the browser never sees them.

---

## 2. Recommended tech stack

| Concern        | Choice | Why |
|----------------|--------|-----|
| Language       | Python 3.11 | Simple, ubiquitous, easy to hire/maintain |
| Web framework  | **FastAPI** | Async-capable, typed, tiny, great local DX, OpenAPI built-in |
| UI             | **Jinja2 server-rendered + a little vanilla JS** | Sub-5-second scan page, mobile-friendly, no build pipeline |
| ORM            | **SQLAlchemy 2.0** | Transaction control, row locks, runs on SQLite *and* Postgres |
| DB             | **SQLite** (local) → **PostgreSQL** (cloud) | One `DATABASE_URL` switch; SQLite for instant local testing |
| HTTP client    | **httpx** | Clean, backend-only Unleashed calls |
| Scheduling     | **APScheduler** | In-process polling fallback, zero extra infra |
| Auth           | Signed-cookie sessions (Starlette) + PBKDF2 (stdlib) | No heavy deps; employee scan stays login-free |
| Tests          | **pytest** | Fast, SQLite-backed, fake Unleashed client |

Deploy as a single container to any PaaS (Render, Railway, Fly.io, Cloud Run,
App Service). Managed Postgres alongside. This is intentionally the *simplest
practical* stack for the MVP.

---

## 3. Why local store inventory, not Unleashed Stock On Hand

The stores **are not warehouses in Unleashed**, so Unleashed has no per-store
Stock On Hand to read — store-level SOH simply does not exist there. Even if it
did:

* Selling/transferring into a store in Unleashed would require modelling each
  store as a warehouse and tracking every café consumption event — exactly the
  heavyweight workflow we are avoiding.
* The real signal we care about is **"what left back storage for café use."**
  That is a store-local event Unleashed never sees.

So the app keeps its **own authoritative store counts**, decremented by employee
scans and incremented only by **confirmed fulfillment**. Unleashed stays the
source of truth for what it actually owns: warehouse stock, sales orders and
delivery. This cleanly separates "what the store has" (us) from "what the
warehouse shipped" (Unleashed) — and fixes the original prediction app, which
guessed need without knowing what each store actually had.

---

## 4. Full workflow (text diagram)

```
SETUP (admin, once)
  Stores ─ Products(code = Unleashed code) ─ StoreInventory(par, min, tag_id)
  Store → Unleashed Customer mapping ─ fulfilling warehouse code (env)

DAILY LOOP
  1. REMOVAL  employee scans /scan/{tag_id}
              → shows item + current count
              → taps qty (1/2/3/Case/Custom) → Submit
              → current_count -= qty           [InventoryTransaction STORE_REMOVAL]
              (no Unleashed call)

  2. GENERATE manager clicks "Generate Request" (or cutoff time)
              suggested = max(par - current_count, 0); include only > 0
              → DailyRequest(DRAFT) + DailyRequestLines   [DAILY_REQUEST_GENERATED]

  3. REVIEW   manager adjusts final qty (+ optional note) [REQUEST_OVERRIDE]

  4. SUBMIT   generate Guid + SourceId locally, persist, THEN POST
              → Unleashed Sales Order (status Parked, Customer=store, Warehouse=fulfilling)
              store Guid + OrderNumber + SourceId          [UNLEASHED_REQUEST_SUBMITTED]
              status DRAFT → SUBMITTED   (local counts unchanged)

  5. FULFILL  warehouse picks/packs/ships/completes the order *inside Unleashed*

  6. DETECT   webhook (preferred) or polling sees OrderStatus = Completed
              → re-fetch order by Guid
              → read ACTUAL shipped qty (Sales Shipments) — fallback: order lines
              → current_count += actual qty (idempotent) [UNLEASHED_RECEIPT]
              status SUBMITTED → COMPLETED → RECEIVED
```

Worked example (Oat Milk @ Ktown): 18 in storage → employee takes 2 → 16 → par
24 ⇒ suggested 8 → submit SO for 8 → warehouse ships 8 → completion detected →
count 16 + 8 = **24**. If only 7 were shipped, the count rises by **7**, never 8.

---

## 5. Database schema

The app owns these tables (see `app/models.py`). All money/identity that belongs
to Unleashed is referenced by code/Guid, not duplicated.

* **stores** — `store_code`, `store_name`, `unleashed_customer_code`,
  `unleashed_customer_guid?`, `active`.
* **products** — `product_code` (= Unleashed code), `unleashed_product_guid?`,
  `display_name`, `category`, `unit_of_measure`, `case_quantity`, `active`.
* **store_inventory** — `store_id`, `product_id`, **`current_count`**, `par_level`,
  `minimum_level`, `storage_location`, **`tag_id`** (unique), `active`.
  Unique on `(store_id, product_id)`.
* **inventory_transactions** — append-only ledger: `transaction_type`,
  `quantity_delta`, `quantity_before/after`, `source`, `employee_id?`,
  `daily_request_id?`, `daily_request_line_id?`, `unleashed_sales_order_guid?`,
  `unleashed_order_number?`, `unleashed_shipment_guid?`, **`idempotency_key?`
  (UNIQUE)**, `timestamp`, `note`.
* **daily_requests** — `store_id`, `request_date`, `status`, timestamps,
  `unleashed_sales_order_guid`, `unleashed_order_number`,
  **`unleashed_source_id` (UNIQUE)**, `error_message`.
* **daily_request_lines** — `product_id`, `sales_order_line_number?`,
  `current_count_at_generation`, `par_level`, `suggested_quantity`,
  `final_requested_quantity`, `fulfilled_quantity`, `received_into_store_count`,
  `status`, `notes`.
* **webhook_events** — `provider`, `event_type`, `event_notification_id`,
  `resource_guid`, `raw_payload`, `status`, timestamps. Unique on
  `(provider, event_notification_id)`.
* **users** — `name`, `email`, `password_hash`, `role`, `store_id?`, `active`.
* **settings** — non-secret key/value (secrets stay in env).

Two uniqueness constraints do the heavy lifting for correctness:
`inventory_transactions.idempotency_key` (no double receipts) and
`webhook_events.(provider, event_notification_id)` (no double webhook processing).

---

## 6. Main backend services (`app/services/`)

* **inventory_service** — the *only* place `current_count` changes. Row-locks the
  inventory row, writes a ledger row, updates the count, commits.
  `record_removal`, `record_count_adjustment`, `apply_receipt_line`
  (idempotent), `record_audit` (zero-delta ledger markers).
* **request_service** — `generate_daily_request` (par − count), `override_line`,
  `submit_to_unleashed` (Guid-first idempotent create + payload mapping).
* **receipt_service** — `process_completion`: re-fetch order, prefer Sales
  Shipments, fallback to order lines, apply each line idempotently, then
  **recompute** line/request roll-ups from the ledger (self-healing on retry).
* **webhook_service** — store + dedupe the event, then run `process_completion`.
* **sync_service** — `poll_open_requests`: sweep submitted-but-unreceived
  requests and run the same `process_completion`.

---

## 7. Main API / routes

Employee (no login):
* `GET /scan/{tag_id}` — scan page
* `POST /scan/{tag_id}` — no-JS submit · `POST /api/scan/{tag_id}` — JSON submit

Manager (`STORE_MANAGER`/`ADMIN`):
* `GET /manager/inventory` · `POST /manager/inventory/adjust`
* `GET /manager/requests` · `POST /manager/requests/generate`
* `GET /manager/requests/{id}` · `POST /manager/requests/{id}/line/{line_id}`
* `POST /manager/requests/{id}/submit`

Warehouse/Admin (`WAREHOUSE`/`ADMIN`):
* `GET /warehouse/requests`
* `POST /warehouse/requests/{id}/retry-submit`
* `POST /warehouse/requests/{id}/process-receipt` · `POST /warehouse/poll-now`

Admin (`ADMIN`): `GET/POST /admin/stores|products|inventory|users`,
`GET /admin/tags`, `GET /admin/settings`, `POST /admin/settings/test`.

Integration & ops: `POST /webhooks/unleashed`, `GET /healthz`,
`GET/POST /login`, `GET /logout`.

---

## 8. Page-by-page UI plan

* **Employee Scan** `/scan/{tag_id}` — large item name, store, current count, UoM,
  big quantity buttons **1 / 2 / 3 / Case / Custom**, one **Submit**, inline
  confirmation "Removed 2 Oat Milk. New count: 16." Default action is always
  STORE_REMOVAL. No login, no pickers, no search. Target < 5 s.
* **Manager Inventory** — current vs par vs min, below-par highlighted, suggested
  qty, last-updated, inline count correction.
* **Manager Daily Request** — list + **Generate**; detail page with editable final
  quantities, notes, and **Submit to Unleashed**.
* **Warehouse/Admin Sync Status** — every request with status, Unleashed order #,
  submitted/completed/received timestamps, error text, **Retry submit** /
  **Check-receive** / **Poll now**.
* **Admin Setup** — Stores, Products, Store Inventory/Par/Tags, Users, Unleashed
  Settings (status + test connection + webhook URL; **never** shows secrets).

---

## 9. Unleashed integration design

Verified against the Unleashed API docs (see Sources).

**Auth** — HMAC-SHA256 of the **query string only** (no `?`, no endpoint), key =
API key, Base64-encoded; empty query signs `""`. Headers `api-auth-id` +
`api-auth-signature`. Implemented in `app/integrations/unleashed.py`
(`compute_signature`); the same query string is used for signing and the URL so
they can't diverge. A bad signature returns 403.

**Endpoints used**
* `POST /SalesOrders/{guid}` — create the replenishment order. The **Guid is
  generated locally first** ⇒ idempotent (re-POST updates, never duplicates).
  Created with `OrderStatus = Parked` (the API only allows Parked/Completed on
  create), `Customer = store's customer`, `Warehouse = fulfilling warehouse`,
  `SourceId = CAFEAPP-DR-{id}`, and `SalesOrderLines` (LineNumber, ProductCode,
  OrderQuantity).
* `GET /SalesOrders/{guid}` — re-read to detect `OrderStatus = Completed`.
* `GET /SalesOrders/{page}?…` — polling sweep / lookups.
* `GET /SalesShipments?orderNumber=…` — **actual shipped quantities**: each line
  has `Product.ProductCode`, `SalesOrderLineNumber`, `ShipmentQty`.
* `GET /Customers`, `GET /Products` — admin lookups.

**Key facts that shaped the design**
* Stores map to **customers**; the order still needs a **fulfilling warehouse**
  (configurable, `UNLEASHED_FULFILL_WAREHOUSE_CODE`).
* Unleashed has **no partial update** — we create/read, never patch.
* Receipts use **Sales Shipments** (actual) by default; an order-line fallback is
  configurable for accounts where Completed ⇒ fully delivered.

---

## 10. Webhook + polling design

Both paths converge on the same idempotent `process_completion`, so they are
safe to run together (webhook for latency, polling as the safety net).

**Webhook (preferred)** — register `POST /webhooks/unleashed?secret=…` as a Sales
Order webhook subscription. Unleashed delivers a small payload (subscription id,
**event notification id**, event type, timestamp, **resource Guid**), retrying
failed deliveries for 72h. We: verify the shared secret → store the raw event →
dedupe on event notification id → find the local request by Guid → re-fetch and
run the receipt. We **always re-fetch**; the webhook only carries a Guid.

**Polling (fallback / always-on)** — APScheduler every `POLLING_INTERVAL_MINUTES`
sweeps requests in `SUBMITTED/COMPLETED/RECEIPT_ERROR` with a Guid, re-fetches
each, and runs the receipt when Completed. Manual **Poll now** button too.

---

## 11. Idempotency & duplicate prevention

* **Order creation** — Guid generated and persisted *before* the API call; retry
  re-POSTs the same Guid ⇒ Unleashed upserts, no duplicate order. `SourceId` is a
  second external anchor (unique locally).
* **Webhooks** — unique `(provider, event_notification_id)`; duplicates are
  detected on insert and ignored.
* **Receipts** — every applied unit carries a unique `idempotency_key`
  (`recv:shipline:{guid}` / `recv:ship:{shipGuid}:line:{n}:prod:{code}` /
  `recv:order:{soGuid}:line:{n}:prod:{code}`) stored on the ledger row with a
  UNIQUE constraint. Apply = fast-path check + insert; an `IntegrityError` race is
  caught and treated as duplicate. Inventory is therefore added **at most once**
  per shipment/order line across duplicate webhooks, polls and retries.
* **Roll-ups recomputed from the ledger** — line `fulfilled`/`received` and request
  status are derived from the sum of applied receipt transactions, so a crash
  mid-process self-heals on the next attempt instead of double-counting.
* **DB transactions / row locks** — `SELECT … FOR UPDATE` on Postgres; SQLite
  serialises writes. Each receipt line commits independently for partial-retry
  safety.

---

## 12. Error handling & retry

* **Submit fails** → request `SYNC_ERROR`, `error_message` saved, `SYNC_ERROR`
  ledger marker; **Retry submit** reuses the same Guid (no duplicate order).
* **Receipt fails** after completion → request `RECEIPT_ERROR`, `error_message`
  saved; retry re-runs the idempotent receipt (no double count).
* **Unleashed client errors** are wrapped in `UnleashedError` with **sanitized**
  messages (never headers/keys) and trimmed bodies; secrets are never logged.
* **Webhook** always returns quickly (200 once stored) so Unleashed doesn't retry
  issues we've already recorded; bad secret → 401, bad body → 400.
* **Scheduler / poll** swallow per-order errors so one bad order can't stop the
  sweep.

---

## 13. MVP build plan (phases)

* **Phase 0 — Foundations** ✅ config, DB, models, auth, seed.
* **Phase 1 — Employee scan** ✅ `/scan/{tag_id}`, removal, ledger, confirmation.
* **Phase 2 — Manager requests** ✅ generate (par−count), override, submit.
* **Phase 3 — Unleashed create** ✅ HMAC client, Guid-first Sales Order, mapping.
* **Phase 4 — Completion & receipt** ✅ webhook + polling, shipments/fallback,
  idempotent apply.
* **Phase 5 — Admin & status** ✅ setup pages, sync status, retries, tests.

Everything above is implemented in this repo. **Future:** dashboards, audit
counts, low-stock alerts, cutoff times, approval rules (P2); usage history &
recommended pars & forecasting (P3); scale/sensor support (P4).

---

## 14. Project structure

```
inventorysubmission/
├── DESIGN.md                  # this document
├── README.md                  # setup / run / deploy
├── requirements.txt · pyproject.toml · .env.example · .gitignore · Dockerfile
├── app/
│   ├── main.py                # FastAPI app, routers, scheduler lifespan
│   ├── config.py              # env-based settings (secrets here only)
│   ├── database.py · models.py · enums.py · security.py · templating.py · scheduler.py
│   ├── integrations/unleashed.py     # HMAC client (create/get order, shipments)
│   ├── services/                     # inventory · request · receipt · webhook · sync
│   ├── routers/                      # scan · manager · warehouse · admin · webhooks · auth
│   ├── templates/                    # base, scan, manager/, warehouse/, admin/, partials/
│   └── static/                       # styles.css, scan.js
├── scripts/                   # seed.py, generate_tags.py
└── tests/                     # signature, inventory, request, receipt, webhook
```

---

## Sources (Unleashed API, verified)

- Authentication — https://apidocs.unleashedsoftware.com/AuthenticationHelp
- Sales Orders — https://apidocs.unleashedsoftware.com/SalesOrders
- Sales Shipments — https://apidocs.unleashedsoftware.com/SalesShipments
- Webhooks — https://apidocs.unleashedsoftware.com/Webhooks
- Sales Order Statuses — https://support.unleashedsoftware.com/hc/en-us/articles/4402384779801-Sales-Order-Statuses
