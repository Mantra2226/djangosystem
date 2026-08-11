# Django Manufacturing ERP System

A full-stack Enterprise Resource Planning (ERP) system built with **Django 5.2** for small-to-mid-scale manufacturing operations. The system covers end-to-end workflows across **Procurement**, **Production**, **Inventory**, **Sales & Dispatch**, **Finance**, and **Executive Reporting**—unified under a single Django Admin interface with a RESTful JSON API layer for frontend integration.

---

## Table of Contents

- [Features](#features)
- [Technology Stack](#technology-stack)
- [Project Structure](#project-structure)
- [Data Model Architecture](#data-model-architecture)
- [Two-Stage Manufacturing Flow](#two-stage-manufacturing-flow)
- [Hybrid Inventory Engine](#hybrid-inventory-engine)
- [MRP Shortage Resolution](#mrp-shortage-resolution)
- [RESTful JSON API](#restful-json-api)
- [Executive Reporting Dashboard](#executive-reporting-dashboard)
- [Django Admin Customizations](#django-admin-customizations)
- [Getting Started](#getting-started)
- [Running Tests](#running-tests)
- [Configuration Reference](#configuration-reference)
- [License](#license)

---

## Features

### Procurement & Supply Chain
- **Supplier Management** — Maintain supplier directory with contact info.
- **Purchase Orders (POs)** — Auto-numbered POs (`PO-YYYY-NNNNN`) with line items, unit pricing, and automatic delivery status tracking (`DRAFT` → `SENT` → `PARTIAL` → `RECEIVED`).
- **Procurement Orders** — Track individual raw material deliveries against POs, with automatic AVCO (Average Weighted Cost) recalculation on receipt.
- **Purchase Invoices** — Supplier billing with partial payment tracking and automatic `PAID` / `PARTIAL` / `UNPAID` status transitions.
- **Supplier OTIF Metrics** — On-Time In-Full delivery percentage calculated across all delivered procurements.

### Production & Manufacturing
- **Bills of Materials (BOMs)** — Multi-level recipe management with circular dependency detection. Supports `RAW` and `INTERMEDIATE` components. Enforces single active BOM per product.
- **Work Orders** — Auto-coded (`WOC-NNNN`) production blueprints with step-by-step instruction sequences, employee assignments, BOM locking, and automated status state machine (`IN_PROGRESS` → `COMPLETED` / `CANCELLED`).
- **Production Orders** — Batch run records (`POC-NNNN`) with pre-run MRP stock availability checks. Automatically transitions to `ON_HOLD_SHORTAGE` when inventory is insufficient.
- **Two-Stage Manufacturing** — Automated Stage 1 Bulk Intermediate → Stage 2 Packaging flow with sequence lock validation, auto-spawning parent orders, and dynamic yield auto-scaling.
- **Material Variance Records** — Auto-calculated per-component variance analysis (Expected vs Actual), efficiency rates, financial impact, and `FAVOURABLE` / `UNFAVOURABLE` / `EXACT` classification.
- **Work Order Material Lines** — Per-component actual consumption tracking with incremental delta deduction from inventory.

### Inventory Management
- **Multi-Location Tracking** — Product stock tracked by warehouse location with unique `(product, location)` constraints.
- **AVCO Costing** — Moving weighted average cost automatically recalculated on every procurement receipt.
- **Stock Allocation & Reservation** — BOM-driven stock reservation on `IN_PROGRESS` work orders, with Phase 2 incremental actual consumption and Phase 3 reconciliation releasing unused allocations.
- **Stock Transactions** — Full audit trail of every stock movement (`RECEIPT`, `PRODUCTION_OUTPUT`, `PRODUCTION_CONSUMPTION`, `SHIPMENT`, `ADJUSTMENT`).
- **Automatic Inventory Creation** — Finished goods automatically receive an Inventory record on product creation.

### Sales & Dispatch
- **Customer Management** — Customer directory with contact and shipping information.
- **Sales Orders** — Auto-numbered (`SO-YYYYMM-NNNN`) with line items and automatic status management (`draft` → `approved` → `partially_dispatched` → `completed`).
- **Dispatch Records** — Auto-coded (`DISP-NNNN`) shipment tracking with stock deduction on `shipped` / `delivered` status. Validates inventory availability before dispatch. Automatically syncs parent Sales Order status.
- **Sales Invoices** — Auto-numbered (`SINV-YYYYMM-NNNN`) with auto-calculated totals from dispatch quantities × selling prices. Supports partial payments with automatic status transitions.
- **Returns** — Customer return processing with QC inspection workflow (`PENDING` → `APPROVED` → `REJECTED`), automatic inventory restocking on approval, and Sales Invoice credit adjustments.

### Finance
- **Finance Entries** — Revenue and Expense ledger with category enforcement (`SALES`, `LABOR`, `OVERHEAD`, `PROCUREMENT`, `CUSTOMER_REFUND`, `LOSS`). Cross-reference validation prevents invalid category/type combinations.
- **Sales Invoice Payments** — Multiple payment methods (`CASH`, `CARD`, `TRANSFER`) with reference number enforcement for electronic payments.
- **Purchase Payments** — Supplier payment tracking (`CASH`, `TRANSFER`, `CHEQUE`) with overpayment prevention and automatic invoice status updates.

### Reporting & Analytics
- **Profit & Loss (P&L)** — Revenue, COGS, operating expenses, gross profit, net income, and gross margin percentage.
- **Cost of Goods Manufactured (COGM)** — Direct material cost, scrap/variance impact, estimated labor cost, total COGM.
- **Yield & Scrap Analytics** — Production yield rates, variance classification breakdowns, machine/workstation utilization.
- **Inventory Health** — Total inventory valuation, low-stock alerts (threshold ≤ 10 units), supplier OTIF delivery percentage.
- **A/R & A/P Aging** — Accounts Receivable and Payable aging buckets (Current 0–30, 31–60, 61–90, 90+ days).

---

## Technology Stack

| Layer | Technology |
|---|---|
| **Backend Framework** | Django 5.2 (Python) |
| **Database** | SQLite 3 (development) — swappable to PostgreSQL/MySQL |
| **Admin Interface** | Django Admin with custom inlines, actions, and JS widgets |
| **API Layer** | Vanilla Django views returning `JsonResponse` (no DRF dependency) |
| **Serialization** | Custom `core/serializers.py` (lightweight, zero-dependency) |
| **Middleware** | Custom `ApiExceptionMiddleware` for standardized API error responses |
| **Frontend** | HTML5, Vanilla CSS, Vanilla JavaScript (`fetch()` API client) |
| **Reporting** | SQL-level `aggregate()` / `annotate()` via Django ORM |

---

## Project Structure

```
djangosystem/
├── manage.py                          # Django management script
├── db.sqlite3                         # SQLite database file
├── djangosystem/                      # Project configuration
│   ├── settings.py                    # Django settings (middleware, apps, DB)
│   ├── urls.py                        # Root URL configuration
│   └── wsgi.py                        # WSGI entry point
├── core/                              # Main application
│   ├── models.py                      # 20+ domain models (~2,100 lines)
│   ├── admin.py                       # Django Admin customizations (~900 lines)
│   ├── views.py                       # Form views + API endpoints
│   ├── urls.py                        # URL routing (forms, reports, API)
│   ├── serializers.py                 # Object-to-dict serializers & validators
│   ├── middleware.py                  # ApiExceptionMiddleware
│   ├── reports.py                     # Executive reporting analytics engine
│   ├── services.py                    # MRP engine & BOM explosion services
│   ├── signals.py                     # Django signals
│   ├── tests.py                       # Unit test suite (20+ test cases)
│   ├── migrations/                    # 31 database migrations
│   ├── templates/core/               # Django HTML templates
│   │   ├── reports_dashboard.html     # Executive reporting dashboard
│   │   ├── home.html, index.html      # Landing pages
│   │   ├── *_form.html                # Data entry forms
│   │   └── ...
│   └── static/                        # Static assets
│       ├── styles.css                 # Global stylesheet
│       ├── scripts.js                 # Global JS utilities
│       ├── procurement_product_filter.js  # PO→Product dynamic filter
│       └── core/js/api.js             # Vanilla JS API client (APIClient class)
```

---

## Data Model Architecture

The system is built on **20+ interconnected Django models** organized into five functional domains:

### Procurement Domain
| Model | Purpose |
|---|---|
| `Supplier` | Supplier directory (name, contact info) |
| `PurchaseOrder` | Multi-item purchase orders with auto-numbered codes |
| `PurchaseOrderItem` | Line items on a PO (product, qty ordered/received, unit price) |
| `ProcurementOrder` | Individual deliveries against POs, triggers AVCO recalculation |

### Production Domain
| Model | Purpose |
|---|---|
| `Product` | Product master data with types (`RAW`, `FINISHED`, `INTERMEDIATE`), auto-generated SKUs |
| `BillOfMaterial` | Recipe definitions with single-active-per-product enforcement |
| `BOMItem` | Recipe ingredients with quantity-per-unit and circular dependency checks |
| `WorkOrder` | Production blueprints with BOM locking, status state machine, and inventory engine |
| `WorkOrderInstruction` | Step-by-step manufacturing instructions with machine assignments |
| `WorkOrderMaterialLine` | Per-component actual vs expected consumption tracking |
| `ProductionOrder` | Batch run records with MRP pre-checks and status sync |
| `MaterialVarianceRecord` | Auto-calculated variance analysis (quantity, cost, efficiency) |
| `Employee` | Workforce directory with auto-coded employee IDs and hourly rates |

### Inventory Domain
| Model | Purpose |
|---|---|
| `Inventory` | Multi-location stock ledger with AVCO unit cost and valuation |
| `StockTransaction` | Complete audit trail of every stock movement |

### Sales Domain
| Model | Purpose |
|---|---|
| `Customer` | Customer directory (name, contact, shipping address) |
| `SalesOrder` | Multi-item customer orders with automatic status tracking |
| `SalesOrderItem` | Line items with dispatched quantity tracking |
| `DispatchRecord` | Shipment records with stock deduction and delivery tracking |
| `SalesInvoice` | Auto-calculated invoices with partial payment support |
| `SalesInvoicePayments` | Individual payment entries with method & reference tracking |
| `Return` | Customer returns with QC workflow and auto-restocking |

### Finance Domain
| Model | Purpose |
|---|---|
| `PurchaseInvoice` | Supplier billing with auto-status from payment tracking |
| `PurchasePayment` | Outgoing payments with overpayment prevention |
| `FinanceEntry` | General ledger entries (Revenue/Expense) with category validation |

---

## Two-Stage Manufacturing Flow

The system implements an automated two-stage manufacturing pipeline for packaging transformations (Bulk Intermediate → Finished Goods):

```
┌─────────────────────────────────────────────────────────┐
│  STAGE 1: Bulk Intermediate Mixing                      │
│  (Auto-spawned parent WorkOrder)                        │
│                                                         │
│  • Automatically created when a Finished Good WO        │
│    has an INTERMEDIATE component in its BOM             │
│  • Calculates bulk requirement: target_qty × BOM ratio  │
│  • Linked as parent_work_order on the packaging WO      │
└─────────────────────┬───────────────────────────────────┘
                      │ On COMPLETED
                      ▼
┌─────────────────────────────────────────────────────────┐
│  DYNAMIC YIELD AUTO-SCALING                             │
│  sync_child_packaging_expectations()                    │
│                                                         │
│  • Propagates actual bulk yield to child packaging      │
│    material lines (quantity_expected)                   │
│  • Prevents negative inventory from physical variance   │
└─────────────────────┬───────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────┐
│  STAGE 2: Packaging Operations                          │
│  (Original WorkOrder)                                   │
│                                                         │
│  • SEQUENCE LOCK: Cannot start/complete until Stage 1   │
│    parent_work_order.status == 'COMPLETED'              │
│  • Raises ValidationError with clear operator messaging │
└─────────────────────────────────────────────────────────┘
```

### Key Guardrails
- **Sequence Lock Validation** (`WorkOrder.clean()`) — Prevents packaging operations from executing before bulk mixing is complete.
- **Auto-Spawning** (`WorkOrder.save()`) — Automatically creates the parent bulk WorkOrder and calculates intermediate material requirements.
- **Dynamic Yield Scaling** (`sync_child_packaging_expectations()`) — Updates child packaging material expectations to match actual bulk output, preventing allocation crashes from physical yield variance.

---

## Hybrid Inventory Engine

The `WorkOrder.process_inventory()` method implements a three-phase inventory management engine:

| Phase | Trigger | Action |
|---|---|---|
| **Phase 1: Allocation** | Status → `IN_PROGRESS` | Reserves BOM-calculated stock from `quantity_available` into `quantity_allocated` |
| **Phase 2: Consumption** | During production | Deducts incremental actual consumption deltas from allocated pool |
| **Phase 3: Reconciliation** | Status → `COMPLETED` | Releases unused allocations, adds finished goods output, logs stock transactions |

Each phase includes safety gates (`is_inventory_allocated`, `is_inventory_updated`) to prevent duplicate processing.

---

## MRP Shortage Resolution

The system includes a Material Requirements Planning (MRP) engine (`core/services.py`) that:

1. **Explodes multi-level BOMs** recursively down to raw material requirements.
2. **Evaluates stock shortages** per component with detailed shortage analysis.
3. **Provides resolution pathways** accessible via Django Admin action buttons:

| Resolution | Component Type | Action |
|---|---|---|
| `raw_autodraft_po` | Raw Material | Auto-drafts a Purchase Order to the component's supplier |
| `raw_direct_procurement` | Raw Material | Spawns a direct Procurement Order |
| `raw_hold_inbound` | Raw Material | Holds production pending inbound PO delivery |
| `intermediate_build` | Intermediate | Spawns a child sub-assembly WorkOrder + ProductionOrder |
| `intermediate_hold_active` | Intermediate | Links to an active intermediate production run |
| `intermediate_partial_batch` | Intermediate | Down-scales the production batch to available stock |

---

## RESTful JSON API

All API endpoints return standardized JSON payloads:

```json
// Success
{ "status": "success", "data": [...] }

// Error (via ApiExceptionMiddleware)
{ "status": "error", "message": "...", "errors": {...} }
```

### Endpoints

| Method | URL | Description |
|---|---|---|
| `GET` | `/api/products/` | List all products (filter by `?type=RAW\|FINISHED\|INTERMEDIATE`) |
| `POST` | `/api/products/` | Create a product (validated via `ProductSerializer`) |
| `GET` | `/api/work-orders/` | List all Work Orders with material lines |
| `GET` | `/api/inventory/` | List all inventory stock levels |
| `GET` | `/api/production-orders/` | List all Production Orders |
| `GET` | `/api/sales-orders/` | List all Sales Orders with items |
| `GET` | `/api/procurements/` | List all Procurement Orders |

### API Middleware

`ApiExceptionMiddleware` (`core/middleware.py`) intercepts exceptions on `/api/` routes and AJAX requests, returning structured JSON error responses instead of Django HTML error pages.

### Client-Side API Module

`static/core/js/api.js` provides an `APIClient` JavaScript class for browser-side consumption:

```javascript
const api = new APIClient();

// GET request
const products = await api.get('/api/products/?type=FINISHED');

// POST request with automatic CSRF token injection
const newProduct = await api.post('/api/products/', {
    name: 'Widget',
    product_type: 'FINISHED',
    category: 'Widgets',
    unit_of_measurement: 'pcs',
    selling_price: 29.99
});
```

Features: automatic CSRF token extraction from cookies, standardized promise handling, and animated floating toast notifications.

---

## Executive Reporting Dashboard

Accessible at `/reports/` (staff-only), the reporting dashboard provides consolidated operational metrics powered by `core/reports.py`:

| Report | Metrics |
|---|---|
| **Profit & Loss** | Total Revenue, Sales Revenue, COGS, Gross Profit, Operating Expenses, Net Income, Gross Margin % |
| **Cost of Goods Manufactured** | Raw Material Cost, Scrap/Variance Impact, Labor Cost (standard rate), Total COGM |
| **Yield & Scrap** | Completed Runs, Target vs Produced Quantity, Yield Rate %, Variance Breakdowns, Machine Utilization |
| **Inventory Health** | Total SKUs, Total Valuation, Low-Stock Alerts (≤10 units), Supplier OTIF % |
| **A/R Aging** | Receivable balances bucketed by age (0–30, 31–60, 61–90, 90+ days) |
| **A/P Aging** | Payable balances bucketed by age (0–30, 31–60, 61–90, 90+ days) |

All reports support optional date range filtering via `?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD` query parameters.

---

## Django Admin Customizations

The Django Admin interface (`core/admin.py`) includes extensive customizations:

- **Optimized Querysets** — `select_related()` and `prefetch_related()` on all admin list views to eliminate N+1 queries.
- **CSV Export Action** — Generic `export_as_csv` action available on all model admin pages.
- **Inline Editing** — `PurchaseOrderItem` inline on PurchaseOrders, `WorkOrderInstruction` and `WorkOrderMaterialLine` inlines on WorkOrders, `SalesOrderItem` inline on SalesOrders, payment inlines on invoices.
- **Dynamic Product Filter** — Client-side JS (`procurement_product_filter.js`) dynamically filters the Product dropdown on Procurement Order forms based on the selected Purchase Order.
- **MRP Shortage Dashboard** — Read-only shortage analysis panel on Production Order change forms with one-click resolution action buttons.
- **Work Order Viewer** — Detailed read-only production summary with material lines, variance records, and instruction steps.

---

## Getting Started

### Prerequisites

- Python 3.10+
- pip

### Installation

1. **Clone the repository:**
   ```bash
   git clone <repository-url>
   cd djangosystem
   ```

2. **Create and activate a virtual environment:**
   ```bash
   python -m venv .venv

   # Windows
   .venv\Scripts\activate

   # macOS / Linux
   source .venv/bin/activate
   ```

3. **Install dependencies:**
   ```bash
   pip install django
   ```

4. **Apply database migrations:**
   ```bash
   python manage.py migrate
   ```

5. **Create a superuser for Django Admin access:**
   ```bash
   python manage.py createsuperuser
   ```

6. **Run the development server:**
   ```bash
   python manage.py runserver
   ```

7. **Access the application:**
   - **Django Admin:** [http://127.0.0.1:8000/admin/](http://127.0.0.1:8000/admin/)
   - **Reports Dashboard:** [http://127.0.0.1:8000/reports/](http://127.0.0.1:8000/reports/)
   - **API Endpoints:** [http://127.0.0.1:8000/api/products/](http://127.0.0.1:8000/api/products/)

---

## Running Tests

Execute the full test suite:

```bash
python manage.py test
```

The test suite (`core/tests.py`) includes:

- **MRP Engine Tests** — BOM explosion, shortage evaluation, resolution pathways.
- **Two-Stage Manufacturing Tests** — Sequence lock validation, auto-spawning, yield scaling.
- **Reporting Engine Tests** — P&L, COGM, and aging bucket calculations.
- **API & Serializer Tests** — Serialization accuracy, payload validation, endpoint responses, middleware error trapping.

---

## Configuration Reference

Key settings in `djangosystem/settings.py`:

| Setting | Value | Notes |
|---|---|---|
| `DEBUG` | `True` | Set to `False` in production |
| `DATABASES` | SQLite3 | Swap to PostgreSQL for production workloads |
| `MIDDLEWARE` | Includes `core.middleware.ApiExceptionMiddleware` | Handles `/api/` error responses |
| `INSTALLED_APPS` | Includes `django.contrib.humanize`, `core` | Humanize for template number formatting |
| `STATIC_URL` | `/static/` | Serve via `collectstatic` in production |

### Environment Considerations

- **Secret Key:** Replace the development `SECRET_KEY` with a securely generated key for production.
- **Allowed Hosts:** Configure `ALLOWED_HOSTS` with your production domain(s).
- **Database:** For production, migrate to PostgreSQL or MySQL and configure connection pooling.

---

## License

This project is proprietary. All rights reserved.
