# Database & Caching Configuration

## Primary Database
The service stores active shopping carts and session state in Redis cluster on port 6379 (`redis-carts.internal`) and persists finalized checkout records into PostgreSQL 15 database `checkout_prod_db` on port 5432.
Connection pooling uses HikariCP with `maximumPoolSize=40` and `connectionTimeout=3000ms`.

## Downstream Calls
During finalization, the service calls `payment-gateway` via synchronous gRPC on port 9090 to authorize the customer's card.
It also publishes order reservation events to Kafka topic `inventory.reservations.v1` consumed by `inventory-service`.

## Failover & Read Replicas
TODO: Fill in failover promotion procedure after Q3 DR drill.
