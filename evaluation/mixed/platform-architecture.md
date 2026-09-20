# Platform architecture

## Overview
The billing platform is a set of small services that share one PostgreSQL database and one message queue. Customers reach it through a public API, and an internal admin tool talks to the same API. Each service is deployed on its own and can be scaled without touching the others.

## Tech stack
| Area | Tool |
|---|---|
| Language | Python 3.12 |
| Web framework | FastAPI and Uvicorn |
| Database | PostgreSQL 16 |
| Queue | Redis |
| Package manager | uv |
| Tests | pytest |
| Packaging | Docker images |

## Services
The invoice service creates and stores invoices and exposes them through the API. The payment service talks to the payment provider and records the result. The notification service sends emails and text messages and never blocks the other services, because it only reads from the queue. All three run as Docker containers on the same hosts.

## Database
One PostgreSQL database holds all invoice and payment data. Each service owns its own tables and never reads the tables of another service directly. Schema changes go through migrations that must be backwards compatible for one release, so that a rolling deployment never meets a schema it cannot read.

## Queue and jobs
Redis holds the queue for background work such as sending invoices by email and retrying failed payments. Jobs must be idempotent, because a job can be delivered twice after a worker crash. Failed jobs are retried five times with growing pauses and then moved to a dead-letter list that an engineer reviews every morning.

## Observability
Every service writes structured logs and exposes metrics in the same format. A request identifier travels with each call from the public API to every service behind it, so one customer complaint can be followed through the whole system. Alerts are based on the error rate and the latency of the public API, not on the state of single servers.

## Security
All traffic between services stays inside a private network. Secrets are injected as environment variables at start-up and never stored in an image. Access to the admin tool needs two-factor authentication, and every change made through it is written to an audit log.
