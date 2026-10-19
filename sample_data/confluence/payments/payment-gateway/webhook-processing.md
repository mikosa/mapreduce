# Webhook & Settlement Processing

## Functional Overview
Processes credit card authorizations, captures, and asynchronous settlement webhooks from external banking networks.
Deployed on AWS EKS in namespace `payments-prod` using Go 1.22 worker pods.

## Dead Letter Queue & Retry Policy
Failed settlement messages are pushed to Kafka topic `dlq-payments-settlement` with a 5-retry exponential backoff (initial delay 2s, max delay 60s).
