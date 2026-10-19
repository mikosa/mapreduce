# Application Dossier: `payment-gateway`

> **Synthesized by**: `local:mock-model` | **Supporting Chunks**: `4` | **Extracted Facts**: `10`

## 1. How the Application Functions

- [FUNCTION] Processes credit card authorizations, captures, and asynchronous settlement webhooks from external banking networks. `[md:confluence/payments/payment-gateway/webhook-processing.md#s1]`
- [FUNCTION] Ticket ID: INC001042 `[csv:tickets/servicenow_incidents.csv#INC001042]`
- [FUNCTION] Tagged Application/CI: PAY-GW `[csv:tickets/servicenow_incidents.csv#INC001042]`
- [FUNCTION] Description: Customers unable to complete card payments during peak flash sale. `[csv:tickets/servicenow_incidents.csv#INC001042]`

## 2. Architecture & Technical Stack

- [ARCHITECTURE] Deployed on AWS EKS in namespace `payments-prod` using Go 1.22 worker pods. `[md:confluence/payments/payment-gateway/webhook-processing.md#s1]`
- [ARCHITECTURE] [via `KAFKA`] Failed settlement messages are pushed to Kafka topic `dlq-payments-settlement` with a 5-retry exponential backoff (initial delay 2s, max delay 60s). `[md:confluence/payments/payment-gateway/webhook-processing.md#s2]`
- [ARCHITECTURE] [via `REST`] Comments: Increased REST timeout between payment-gateway and fraud-engine to 1500ms and enabled circuit breaker fallback. `[csv:tickets/servicenow_incidents.csv#INC001042]`

## 3. Upstream & Downstream Dependencies

```mermaid
flowchart LR
    payment_gateway[["payment-gateway"]]
    checkout_service["checkout-service"] -->|"GRPC"| payment_gateway
    payment_gateway -->|"REST"| fraud_engine["fraud-engine"]
```

| Direction | Related Application | Protocol / Mechanism | Evidence & Details | Source Citation |
| :--- | :--- | :--- | :--- | :--- |
| **Upstream (Caller)** | `checkout-service` | `GRPC` | [Inferred from checkout-service docs/tickets] checkout-service sends/calls payment-gateway: During finalization, the service calls `payment-gateway` via synchronous gRPC on port 9090 to authorize the customer's card. | `md:confluence/commerce/checkout-service/architecture/storage-and-calls.md#s2` |
| **Downstream (Target)** | `fraud-engine` | `REST` | Technical / Work Notes: Checked pod logs in payments-prod. PAY-GW calls Fraud Engine via REST POST /v2/score before authorizing cards. Fraud Engine returned HTTP 503 due to Redis connection exhaustion. | `csv:tickets/servicenow_incidents.csv#INC001042` |

### Dependency Notes
- [UPSTREAM] (Related App: `checkout-service`) [via `GRPC`] [Inferred from checkout-service docs/tickets] checkout-service sends/calls payment-gateway: During finalization, the service calls `payment-gateway` via synchronous gRPC on port 9090 to authorize the customer's card. `[md:confluence/commerce/checkout-service/architecture/storage-and-calls.md#s2]`
- [DOWNSTREAM] (Related App: `fraud-engine`) [via `REST`] Technical / Work Notes: Checked pod logs in payments-prod. PAY-GW calls Fraud Engine via REST POST /v2/score before authorizing cards. Fraud Engine returned HTTP 503 due to Redis connection exhaustion. `[csv:tickets/servicenow_incidents.csv#INC001042]`

## 4. Operational Failure Modes & Incomplete Documentation Gaps

- [FAILURE_MODE] Short Description: 503 Gateway Timeout during card authorization `[csv:tickets/servicenow_incidents.csv#INC001042]`

---

## Provenance & Source Index

- `csv:tickets/servicenow_incidents.csv#INC001042` — `tickets/servicenow_incidents.csv` (Ticket INC001042)
- `md:confluence/commerce/checkout-service/architecture/storage-and-calls.md#s2` — `confluence/commerce/checkout-service/architecture/storage-and-calls.md` (Database & Caching Configuration > Downstream Calls)
- `md:confluence/payments/payment-gateway/webhook-processing.md#s1` — `confluence/payments/payment-gateway/webhook-processing.md` (Webhook & Settlement Processing > Functional Overview)
- `md:confluence/payments/payment-gateway/webhook-processing.md#s2` — `confluence/payments/payment-gateway/webhook-processing.md` (Webhook & Settlement Processing > Dead Letter Queue & Retry Policy)
