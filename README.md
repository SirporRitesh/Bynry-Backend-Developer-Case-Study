# Bynry-Backend-Developer-Case-Study

---

## Part 1 — Code review, production impact, and what I changed
### Issues and impact
1) Missing input validation

Impact: KeyError or 500 crash on malformed requests ->bad UX and unreliable API.

2) Incorrect money handling (floats / no Decimal)

Impact: Rounding/precision errors in valuations or billing; financial inaccuracy.

3) No transactional integrity across product + inventory

Impact: Partial writes (product created but inventory insert fails) ->inconsistent state / orphaned products.

4) SKU uniqueness not handled gracefully and not normalized

Impact: Case/whitespace collisions (abc vs ABC), race conditions, and ugly DB errors exposed to clients.

5) Incorrect data model assumption about warehouse

Impact: Violates requirement that products can exist in multiple warehouses.

6) No warehouse existence validation

Impact: Inventory rows may reference non-existent warehouses -> broken joins/reports and data integrity issues.

7) No audit trail for inventory seed

Impact: No traceability for how stock was seeded -> hurts analytics, reconciliation, and debugging.

8) Possible duplicate inventory rows (product + warehouse)

Impact: Duplicate rows produce incorrect stock totals and unreliable alerts.

9) Poor error handling and logging

Impact: Hard to debug in production and poor observability; clients receive unclear errors.

### Fixes I implemented 
1) Validation & friendly errors

- request.get_json(silent=True), data.get(), required-field checks for name, sku, price.

- Decimal parsing for price; non-negative integer check for initial_quantity.

- Return 400 for invalid requests.

2) Money correctness

- Convert price with Decimal(...).quantize(Decimal('0.01')).

- Keep Numeric(10,2) in DB model.

3) SKU normalization + proactive check

- normalized_sku = sku.strip().upper().

- Product.query.filter_by(sku=normalized_sku).first() to return 409 early.

- Keep DB UNIQUE on SKU and catch IntegrityError to handle races.

4) Single transaction

- Wrap creation of Product, optional Inventory, and InventoryHistory in with db.session.begin(): to ensure atomicity.

5) Warehouse existence check

- Validate warehouse_id before inserting inventory.

6) Inventory history seed

- If initial_quantity > 0, create an InventoryHistory record (reason="Initial stock").

7) Error handling & logging

- Catch IntegrityError (409) and generic exceptions (500), rollback, and log for observability.

8) Code comments / conventions

- Document InventoryHistory.change_amount convention (negative = sale), and other important behaviors.

*Additional context handled: implemented behavior consistent with the extra context: products in multiple warehouses, SKU uniqueness, Decimal price handling, and optional fields handled correctly.

---

## Part 2 — Database schema, questions for product team, and design decisions
Schema (tables, key columns, types, relationships)

1) company

- id INT PK

- name VARCHAR(255) NOT NULL

2) warehouse

- id INT PK

- company_id INT NOT NULL ->FK company(id)

- name VARCHAR(255) NOT NULL

- Relationship: multiple warehouses ->one company (companies can have multiple warehouses)

3) product

- id INT PK

- name VARCHAR(255) NOT NULL

- sku VARCHAR(100) NOT NULL UNIQUE

- price NUMERIC(10,2) NOT NULL

- reorder_threshold INT NOT NULL DEFAULT 10

4) supplier

- id INT PK

- name VARCHAR(255) NOT NULL

- contact_email VARCHAR(255) NULL

5) product_supplier (junction)

- product_id INT ->FK product(id) (PK part)

- supplier_id INT ->FK supplier(id) (PK part)

- is_primary BOOLEAN DEFAULT FALSE

- Relationship: many-to-many; is_primary flags preferred supplier

6) inventory

- id INT PK

- product_id INT NOT NULL ->FK product(id)

- warehouse_id INT NOT NULL ->FK warehouse(id)

- quantity INT NOT NULL DEFAULT 0

- Constraint: UNIQUE(product_id, warehouse_id) to prevent duplicates

7) inventory_history

- id INT PK

- inventory_id INT NOT NULL ->FK inventory(id)

- change_amount INT NOT NULL (positive = add, negative = remove/sale)

- reason VARCHAR(255)

- created_at DATETIME (UTC) DEFAULT now

- Index: (inventory_id, created_at) for time-window queries

### Questions I'd ask the product team
Q1) Threshold settings: per-product, per-warehouse, or dynamic by product type / velocity?

Q2) Product types & bundles: are bundles virtual (computed) or tracked as prebuilt SKUs?

Q3) Reservation model: do we need reserved_quantity for unshipped orders?

Q4) Supplier selection policy: use is_primary only or choose by lead time/cost/availability?

Q5) Inventory reasons taxonomy: fixed enum vs free text for reason?

Q6) Retention & deletion policy: history retention period, soft deletes, GDPR concerns?

Q7) Scale & SLA: expected products/transactions/day and QPS for indexing and partitioning decisions.

Q8) Currency model: single currency or multi-currency pricing required?

### Design decisions
1) SKU UNIQUE + normalization

- Rationale: enforce global uniqueness; normalization prevents case/space variants; handle races by DB constraint + catching IntegrityError.

2) UNIQUE(product_id, warehouse_id) on inventory

- Rationale: prevents duplicate inventory rows which would break aggregations and alerts.

3) FK constraints

- Rationale: maintain referential integrity and prevent orphaned rows.

4) Indexes

- inventory_history(inventory_id, created_at) — speeds recent-sales queries (critical for alerts).

- inventory(warehouse_id), inventory(product_id) — efficient joins/filters.

- product_supplier(product_id, is_primary) — fast primary supplier lookup.

5) Numeric(10,2) for price

- Rationale: fixed-point for monetary accuracy.

6) Inventory history table

- Rationale: enables auditing and computing usage (e.g., 30-day sales); avoids destructive overwrites.

7) Atomic transactions

- Rationale: prevent partial states and orphaned data.

## Part 3 — Low-stock alerts: business rules, edge cases, and approach
### Business rules implemented
1) Threshold varies by product — use product.reorder_threshold.

2) Alert only for products with recent sales activity — require negative inventory_history.change_amount within last 30 days.

3) Multi-warehouse support — consider all warehouses owned by company_id.

4) Include supplier info — fetch product_supplier where is_primary = true.

### Edge cases and handling 
1) No recent sales in 30 days: filtered out by subquery — no alert.

2) Zero average daily usage: avoid divide-by-zero; days_until_stockout = null.

3) Missing supplier: return supplier: null in response.

4) Negative inventory: allowed if business permits; flag as exceptional; recommend forbidding on order flow.

5) Duplicate inventory rows (historical): prevented going forward by UNIQUE constraint; require migration to fix legacy data.

6) Race on SKU insertion: proactive check may race; DB UNIQUE + IntegrityError catch is final authority.

### Approach — step-by-step (what the endpoint does)
1) Compute thirty_days_ago = utc_now - 30 days.

2) Subquery: select distinct inventory_id from inventory_history where created_at >= thirty_days_ago AND change_amount < 0 (sales).

3) Query Inventory join Product and Warehouse (filter warehouse.company_id == company_id), inner-join the subquery so only inventories with recent sales are included, and filter Inventory.quantity < Product.reorder_threshold. Left-join to product_supplier and supplier to attach supplier info.

4) For each result: sum negative change_amount over last 30 days -> total_usage; compute avg_daily_usage = total_usage / 30; if avg_daily_usage > 0, days_until_stockout = floor(current_stock / avg_daily_usage), else null.

5) Serialize to required JSON fields and return total_alerts.

