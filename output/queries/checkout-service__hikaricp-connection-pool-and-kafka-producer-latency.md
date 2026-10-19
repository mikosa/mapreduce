# Topic Deep-Dive: HikariCP connection pool and Kafka producer latency

- **Application Scope**: `checkout-service`
- **Synthesized By**: `local:mock-model`
- **Supporting Chunks**: `csv:tickets/servicenow_incidents.csv#INC001089, md:confluence/commerce/checkout-service/architecture/storage-and-calls.md#s1, md:confluence/commerce/checkout-service/architecture/storage-and-calls.md#s2`

---

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
