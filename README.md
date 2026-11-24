# Invest IQ Backend API

This repository contains the read-only FastAPI service for querying Invest IQ
instruments, transactions, and portfolio summaries from PostgreSQL. It runs
locally with Uvicorn and is packaged for AWS Lambda as a container image.

## API endpoints

All data endpoints require the `X-API-Key` header. `GET /health` is
unauthenticated and does not connect to the database.

| Method and path | Description |
| --- | --- |
| `GET /health` | Liveness check |
| `GET /api/v1/instruments` | Paginated instrument list; optional `search` matches symbol or name |
| `GET /api/v1/instruments/{instrument_id}` | Instrument details and active transaction count |
| `GET /api/v1/transactions` | Paginated transactions; optional `symbol`, `exchange`, `type`, and `instrument_id` filters |
| `GET /api/v1/portfolio/holdings` | Non-zero net quantities and weighted average buy/sell prices, grouped by price currency |
| `GET /api/v1/portfolio/summary` | Transaction counts and recorded invested-amount totals |

List endpoints accept `limit` (default 100, maximum 500) and `offset` (default
0), and return `items`, `total`, `limit`, and `offset`. Transactions soft
deleted in the database are excluded.

The database schema does not record the currency of `invested_amount`, so
summary totals may combine currencies. The API does not fetch live market
prices.

## Run locally

From this directory, create and activate a Python 3.10+ virtual environment and
install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-api.txt
```

Set the connection string and a long random API key in the same terminal where
you will run the service:

```bash
export DATABASE_URL='postgresql://USER:URL_ENCODED_PASSWORD@HOST:5432/DATABASE'
export DB_SSLMODE=require
export API_KEY='YOUR_LONG_RANDOM_LOCAL_KEY'
uvicorn app:app --reload
```

For local PostgreSQL without TLS, set `DB_SSLMODE=disable`. URL-encode special
characters in the database password. Keep credentials and API keys private.
The API expects `public.instruments` and `public.transactions` to exist; it
does not create tables or import data. Each database transaction is set to
read-only.

Open Swagger UI at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs),
select **Authorize**, and enter the configured API key. OpenAPI JSON is at
`/openapi.json`; ReDoc is at `/redoc`.

Quick checks:

```bash
curl -i http://127.0.0.1:8000/health
curl -i -H "X-API-Key: $API_KEY" \
  'http://127.0.0.1:8000/api/v1/instruments?limit=10'
curl -i -H "X-API-Key: $API_KEY" \
  'http://127.0.0.1:8000/api/v1/transactions?type=BUY&limit=10'
```

## Deploy to AWS Lambda and Aurora Serverless

See [`AWS_DEPLOYMENT.md`](./AWS_DEPLOYMENT.md) for the step-by-step guide to
Aurora networking, database permissions, ECR image publishing, Lambda
configuration, API Gateway, and deployment testing.

The service reads `DATABASE_URL`, `DB_SSLMODE`, and `API_KEY` from environment
variables. Secrets Manager retrieval is not implemented in the current code.
For production, use an approved secure secret-injection approach, a dedicated
read-only database user, private VPC connectivity, and suitable authentication
for API consumers. Do not bake credentials into the image.

## Container image

From this directory, build for the architecture selected for the Lambda
function:

```bash
docker buildx build --platform linux/amd64 -t invest-iq-api .
```

Use `--platform linux/arm64` when the Lambda function uses ARM64. The image
architecture must match the function architecture.
