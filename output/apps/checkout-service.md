# Application Dossier: `checkout-service`

> **Synthesized by**: `local:mock-model` | **Supporting Chunks**: `5` | **Extracted Facts**: `12`

## 1. How the Application Functions

- [FUNCTION] Ticket ID: INC001089 `[csv:tickets/servicenow_incidents.csv#INC001089]`
- [FUNCTION] Tagged Application/CI: CHKOUT `[csv:tickets/servicenow_incidents.csv#INC001089]`
- [FUNCTION] Short Description: High latency on checkout completion when Inventory Service lags `[csv:tickets/servicenow_incidents.csv#INC001089]`

## 2. Architecture & Technical Stack

- [ARCHITECTURE] [via `POSTGRES`] The service stores active shopping carts and session state in Redis cluster on port 6379 (`redis-carts.internal`) and persists finalized checkout records into PostgreSQL 15 database `checkout_prod_db` on port 5432. `[md:confluence/commerce/checkout-service/architecture/storage-and-calls.md#s1]`
- [ARCHITECTURE] [via `KAFKA`] It also publishes order reservation events to Kafka topic `inventory.reservations.v1` consumed by `inventory-service`. `[md:confluence/commerce/checkout-service/architecture/storage-and-calls.md#s2]`
- [ARCHITECTURE] [via `KAFKA`] Description: Checkout Service threads blocked waiting on Kafka acks. `[csv:tickets/servicenow_incidents.csv#INC001089]`
- [ARCHITECTURE] [via `KAFKA`] Comments: Switched Kafka producer to async batching with linger.ms=10. `[csv:tickets/servicenow_incidents.csv#INC001089]`

## 3. Upstream & Downstream Dependencies

```mermaid
flowchart LR
    checkout_service[["checkout-service"]]
    order_orchestrator["order-orchestrator"] -->|"KAFKA"| checkout_service
    checkout_service -->|"GRPC"| payment_gateway["payment-gateway"]
```

| Direction | Related Application | Protocol / Mechanism | Evidence & Details | Source Citation |
| :--- | :--- | :--- | :--- | :--- |
| **Upstream (Caller)** | `order-orchestrator` | `KAFKA` | Technical / Work Notes: Checkout Service receives upstream cart submit requests from Order Orchestrator over REST API. When publishing to Kafka topic inventory.reservations.v1 with acks=all, broker latency caused HikariCP pool exhaustion on checkout_prod_db. | `csv:tickets/servicenow_incidents.csv#INC001089` |
| **Downstream (Target)** | `payment-gateway` | `GRPC` | During finalization, the service calls `payment-gateway` via synchronous gRPC on port 9090 to authorize the customer's card. | `md:confluence/commerce/checkout-service/architecture/storage-and-calls.md#s2` |

### Dependency Notes
- [UPSTREAM] (Related App: `order-orchestrator`) [via `KAFKA`] Technical / Work Notes: Checkout Service receives upstream cart submit requests from Order Orchestrator over REST API. When publishing to Kafka topic inventory.reservations.v1 with acks=all, broker latency caused HikariCP pool exhaustion on checkout_prod_db. `[csv:tickets/servicenow_incidents.csv#INC001089]`
- [DOWNSTREAM] (Related App: `payment-gateway`) [via `GRPC`] During finalization, the service calls `payment-gateway` via synchronous gRPC on port 9090 to authorize the customer's card. `[md:confluence/commerce/checkout-service/architecture/storage-and-calls.md#s2]`

## 4. Operational Failure Modes & Incomplete Documentation Gaps

- [FAILURE_MODE] Connection pooling uses HikariCP with `maximumPoolSize=40` and `connectionTimeout=3000ms`. `[md:confluence/commerce/checkout-service/architecture/storage-and-calls.md#s1]`
- [INCOMPLETE_GAP] Incomplete section 'Database & Caching Configuration > Failover & Read Replicas' in page 'Database & Caching Configuration' (confluence/commerce/checkout-service/architecture/storage-and-calls.md): 'TODO: Fill in failover promotion procedure after Q3 DR drill.' `[md:confluence/commerce/checkout-service/architecture/storage-and-calls.md#s3]`
- [INCOMPLETE_GAP] Incomplete/stub Confluence page 'Disaster Recovery Runbook' at 'confluence/commerce/checkout-service/runbooks/disaster-recovery.md' (only 4 content words: 'TBD - page under construction.') `[md:confluence/commerce/checkout-service/runbooks/disaster-recovery.md#stub]`
---

## 5. Dynamic Topic Deep-Dives

### Topic: HikariCP connection pool and Kafka producer latency *(Generated 2026-10-19 04:29:40)*

- [Finding] Ticket ID: INC001089 `[csv:tickets/servicenow_incidents.csv#INC001089]`
- [Finding] Tagged Application/CI: CHKOUT `[csv:tickets/servicenow_incidents.csv#INC001089]`
- [Finding] Short Description: High latency on checkout completion when Inventory Service lags `[csv:tickets/servicenow_incidents.csv#INC001089]`
- [Finding] Description: Checkout Service threads blocked waiting on Kafka acks. `[csv:tickets/servicenow_incidents.csv#INC001089]`
- [Finding] Technical / Work Notes: Checkout Service receives upstream cart submit requests from Order Orchestrator over REST API. When publishing to Kafka topic inventory.reservations.v1 with acks=all, broker latency caused HikariCP pool exhaustion on checkout_prod_db. `[csv:tickets/servicenow_incidents.csv#INC001089]`
- [Finding] Comments: Switched Kafka producer to async batching with linger.ms=10. `[csv:tickets/servicenow_incidents.csv#INC001089]`
- [Finding] The service stores active shopping carts and session state in Redis cluster on port 6379 (`redis-carts.internal`) and persists finalized checkout records into PostgreSQL 15 database `checkout_prod_db` on port 5432. `[md:confluence/commerce/checkout-service/architecture/storage-and-calls.md#s1]`
- [Finding] Connection pooling uses HikariCP with `maximumPoolSize=40` and `connectionTimeout=3000ms`. `[md:confluence/commerce/checkout-service/architecture/storage-and-calls.md#s1]`
- [Finding] During finalization, the service calls `payment-gateway` via synchronous gRPC on port 9090 to authorize the customer's card. `[md:confluence/commerce/checkout-service/architecture/storage-and-calls.md#s2]`
- [Finding] It also publishes order reservation events to Kafka topic `inventory.reservations.v1` consumed by `inventory-service`. `[md:confluence/commerce/checkout-service/architecture/storage-and-calls.md#s2]`

### Topic: HikariCP connection pool and Kafka producer latency *(Generated 2026-10-19 04:45:42)*

- [Finding] Ticket ID: INC001089 `[csv:tickets/servicenow_incidents.csv#INC001089]`
- [Finding] Tagged Application/CI: CHKOUT `[csv:tickets/servicenow_incidents.csv#INC001089]`
- [Finding] Short Description: High latency on checkout completion when Inventory Service lags `[csv:tickets/servicenow_incidents.csv#INC001089]`
- [Finding] Description: Checkout Service threads blocked waiting on Kafka acks. `[csv:tickets/servicenow_incidents.csv#INC001089]`
- [Finding] Technical / Work Notes: Checkout Service receives upstream cart submit requests from Order Orchestrator over REST API. When publishing to Kafka topic inventory.reservations.v1 with acks=all, broker latency caused HikariCP pool exhaustion on checkout_prod_db. `[csv:tickets/servicenow_incidents.csv#INC001089]`
- [Finding] Comments: Switched Kafka producer to async batching with linger.ms=10. `[csv:tickets/servicenow_incidents.csv#INC001089]`
- [Finding] The service stores active shopping carts and session state in Redis cluster on port 6379 (`redis-carts.internal`) and persists finalized checkout records into PostgreSQL 15 database `checkout_prod_db` on port 5432. `[md:confluence/commerce/checkout-service/architecture/storage-and-calls.md#s1]`
- [Finding] Connection pooling uses HikariCP with `maximumPoolSize=40` and `connectionTimeout=3000ms`. `[md:confluence/commerce/checkout-service/architecture/storage-and-calls.md#s1]`
- [Finding] During finalization, the service calls `payment-gateway` via synchronous gRPC on port 9090 to authorize the customer's card. `[md:confluence/commerce/checkout-service/architecture/storage-and-calls.md#s2]`
- [Finding] It also publishes order reservation events to Kafka topic `inventory.reservations.v1` consumed by `inventory-service`. `[md:confluence/commerce/checkout-service/architecture/storage-and-calls.md#s2]`

---

## Provenance & Source Index

- `csv:tickets/servicenow_incidents.csv#INC001089` — `tickets/servicenow_incidents.csv` (Ticket INC001089)
- `md:confluence/commerce/checkout-service/architecture/storage-and-calls.md#s1` — `confluence/commerce/checkout-service/architecture/storage-and-calls.md` (Database & Caching Configuration > Primary Database)
- `md:confluence/commerce/checkout-service/architecture/storage-and-calls.md#s2` — `confluence/commerce/checkout-service/architecture/storage-and-calls.md` (Database & Caching Configuration > Downstream Calls)
- `md:confluence/commerce/checkout-service/architecture/storage-and-calls.md#s3` — `confluence/commerce/checkout-service/architecture/storage-and-calls.md` (Database & Caching Configuration > Failover & Read Replicas)
- `md:confluence/commerce/checkout-service/runbooks/disaster-recovery.md#stub` — `confluence/commerce/checkout-service/runbooks/disaster-recovery.md` ((Entire Page Stub))
