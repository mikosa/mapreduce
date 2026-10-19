# Application Dossier: `inventory-service`

> **Synthesized by**: `local:mock-model` | **Supporting Chunks**: `1` | **Extracted Facts**: `6`

## 1. How the Application Functions

- [FUNCTION] Ticket ID: INC001115 `[csv:tickets/servicenow_incidents.csv#INC001115]`
- [FUNCTION] Tagged Application/CI: INV-SVC `[csv:tickets/servicenow_incidents.csv#INC001115]`
- [FUNCTION] Description: Inventory counts drifted for high-demand SKUs. `[csv:tickets/servicenow_incidents.csv#INC001115]`

## 2. Architecture & Technical Stack

- [ARCHITECTURE] [via `KAFKA`] Short Description: Duplicate stock deduction on retried Kafka events `[csv:tickets/servicenow_incidents.csv#INC001115]`
- [ARCHITECTURE] [via `KAFKA`] Technical / Work Notes: Inventory Service consumes reservation events from Checkout Service via Kafka topic inventory.reservations.v1 and stores stock levels in Oracle 19c database inv_stock_db. `[csv:tickets/servicenow_incidents.csv#INC001115]`
- [ARCHITECTURE] Comments: Added idempotency key check on order_id in Oracle table STOCK_RESERVATIONS. `[csv:tickets/servicenow_incidents.csv#INC001115]`

## 3. Upstream & Downstream Dependencies

*No upstream or downstream application links discovered yet.*

### Dependency Notes
*No upstream or downstream dependencies found.*

## 4. Operational Failure Modes & Incomplete Documentation Gaps

*No operational failure modes or incomplete documentation stubs recorded.*

---

## Provenance & Source Index

- `csv:tickets/servicenow_incidents.csv#INC001115` — `tickets/servicenow_incidents.csv` (Ticket INC001115)
