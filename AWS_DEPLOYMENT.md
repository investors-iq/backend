# AWS Lambda and Aurora Serverless Deployment Guide

This guide deploys the Invest IQ read-only FastAPI service as an AWS Lambda
container image and connects it to an Aurora PostgreSQL Serverless database.
The intended request path is:

```text
Client -> API Gateway -> Lambda (private subnets) -> optional RDS Proxy -> Aurora PostgreSQL
```

The service uses a Lambda container image, FastAPI, and Mangum. It reads
`DATABASE_URL`, `DB_SSLMODE`, and `API_KEY` from its environment. It does not
create database tables or load spreadsheet data. Replace placeholders such as
`YOUR_AWS_REGION` and `YOUR_DATABASE` with your own values. Keep ECR, Lambda,
API Gateway, and Aurora in the same AWS Region.

## Prerequisites

- An AWS account with permissions to configure VPC, RDS, ECR, Lambda, IAM, and
  API Gateway.
- AWS CLI configured for the correct account and Region.
- Docker with Buildx.
- An Aurora PostgreSQL Serverless cluster (Serverless v2 recommended for new
  deployments) and its database credentials.
- The `instruments` and `transactions` tables populated with the data the API
  should expose.

Check the AWS CLI identity and Region before creating resources:

```bash
aws sts get-caller-identity
aws configure get region
```

## 1. Prepare Aurora PostgreSQL

1. In the RDS console, open the Aurora PostgreSQL DB cluster and note its VPC,
   database name, port (normally `5432`), and endpoint. Use the reader endpoint
   for routine reads when the cluster has readers; otherwise use the cluster
   endpoint.
2. Keep the cluster private. Do not enable public access just to connect the
   Lambda. The Lambda will access Aurora within the VPC.
3. Ensure `public.instruments` and `public.transactions` exist and contain the
   desired data. The importer creates those tables when run by a database user
   with write privileges. The API is read-only and neither creates schema nor
   imports data. If Aurora is private, run the importer from a host with private
   VPC access, such as an approved VPN-connected workstation or controlled EC2
   instance.
4. Create a dedicated PostgreSQL login for the API instead of using the
   database administrator. Grant only database connection, schema usage, and
   table read privileges. Run the following as a database administrator, after
   replacing the role, password, and database placeholders:

   ```sql
   CREATE ROLE invest_iq_api LOGIN PASSWORD 'REPLACE_WITH_A_STRONG_PASSWORD';
   GRANT CONNECT ON DATABASE YOUR_DATABASE TO invest_iq_api;
   GRANT USAGE ON SCHEMA public TO invest_iq_api;
   GRANT SELECT ON TABLE public.instruments, public.transactions TO invest_iq_api;
   ```

   Use a generated password; do not use the example or commit real credentials.

## 2. Configure VPC networking and security groups

1. Identify the VPC and private subnets used by Aurora. In the EC2 console,
   create a security group in that VPC for Lambda (for example,
   `invest-iq-api-lambda`).
2. In Aurora's security group, add an inbound PostgreSQL rule for TCP port
   `5432`, with the Lambda security group as the source. Do not expose the
   database port to `0.0.0.0/0`.
3. When configuring Lambda, select the same VPC and private subnets with routes
   to the database subnets, then attach the Lambda security group. Ensure its
   outbound rules allow database traffic.
4. Optionally configure RDS Proxy for the Aurora cluster to help manage
   connections when Lambda scales out. Allow the Lambda security group to
   connect to the proxy security group on TCP `5432`, and use the proxy
   endpoint in `DATABASE_URL`.

Lambda's execution role needs VPC networking permissions when the function is
attached to a VPC. Use the basic Lambda execution role and include the
permissions in the AWS-managed `AWSLambdaVPCAccessExecutionRole` policy. A
Lambda in private subnets does not need internet access to connect to Aurora in
the same VPC. If the function must call public services, provide a suitable NAT
route or a VPC endpoint and appropriate egress controls.

## 3. Create an ECR repository and push the image

Set shell variables for the target account and Region:

```bash
export AWS_REGION='YOUR_AWS_REGION'
export AWS_ACCOUNT_ID='YOUR_12_DIGIT_ACCOUNT_ID'
export ECR_REPOSITORY='invest-iq-api'
```

Create the ECR repository if needed, then authenticate Docker:

```bash
aws ecr describe-repositories \
  --repository-names "$ECR_REPOSITORY" \
  --region "$AWS_REGION" >/dev/null 2>&1 || \
aws ecr create-repository \
  --repository-name "$ECR_REPOSITORY" \
  --image-scanning-configuration scanOnPush=true \
  --region "$AWS_REGION"

aws ecr get-login-password --region "$AWS_REGION" | \
  docker login --username AWS --password-stdin \
    "$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"
```

Choose the Lambda architecture (`x86_64` or `arm64`) and build a matching image
from this directory. For `x86_64`:

```bash
docker buildx build \
  --platform linux/amd64 \
  --provenance=false \
  --push \
  --tag "$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$ECR_REPOSITORY:latest" \
  ./backend
```

For an `arm64` Lambda, use `--platform linux/arm64`. The Lambda architecture and
container image architecture must match. The repository's
[`Dockerfile`](./Dockerfile) uses the AWS Lambda Python 3.12
base image and starts the FastAPI application through Mangum.

## 4. Create and configure the Lambda function

1. In the Lambda console, choose **Create function** → **Container image**.
   Enter a function name, select the image pushed to ECR, and choose its
   matching architecture.
2. Select or create an execution role with basic Lambda logging permissions
   and the VPC permissions described above.
3. Under **Configuration → VPC**, select the Aurora VPC, private subnets that
   can reach Aurora, and the Lambda security group.
4. Under **Configuration → General configuration**, set an appropriate timeout
   (for example, 30 seconds) and memory size (for example, 512 MB), then save.
5. Under **Configuration → Environment variables**, set:

   - `DATABASE_URL` to
     `postgresql://API_ROLE:URL_ENCODED_PASSWORD@DB_ENDPOINT:5432/YOUR_DATABASE`
   - `DB_SSLMODE` to `require`
   - `API_KEY` to a long, randomly generated key

   Use the RDS Proxy endpoint if using a proxy; otherwise use the Aurora
   endpoint. URL-encode special characters in the database password.

   **Secrets note:** The current code reads these values directly from Lambda
   environment variables; it does not fetch secrets from AWS Secrets Manager.
   Environment variables are convenient for initial testing but are not a
   secret-rotation integration. Before production, add Secrets Manager retrieval
   or another approved secret-injection mechanism. Never bake credentials into
   the container image, source code, or a checked-in `.env` file.
6. Save the settings. Use an HTTP API invocation in the next section to test
   the service. A generic Lambda console test event is not itself an HTTP
   request and may not be a valid event for Mangum.

## 5. Expose Lambda through API Gateway

1. In the API Gateway console, create an **HTTP API** and add an integration
   for this Lambda function in the same Region.
2. Add a `$default` route to the Lambda integration so FastAPI paths and HTTP
   methods (including `/docs` and `/api/v1/...`) are forwarded to the app.
   Enable automatic deployment or deploy the stage after adding the route.
   Allow API Gateway to add Lambda invoke permission when prompted.
3. Copy the API invoke URL. The app itself validates the `X-API-Key` header.
   API Gateway's optional API-key/usage-plan feature is separate and does not
   replace the app's header check.
4. Restrict access appropriately. The app API key is a shared secret, not
   user identity or authorization. Do not embed it in public browser or mobile
   clients. Add an identity layer such as Amazon Cognito or an API Gateway
   authorizer before exposing private portfolio data to end users. For browser
   access, configure CORS to allow only the required frontend origins and the
   `X-API-Key` header.

## 6. Test the deployed service

Set the API URL and the API key configured for Lambda:

```bash
export API_URL='https://YOUR_API_ID.execute-api.YOUR_AWS_REGION.amazonaws.com'
export API_KEY='YOUR_CONFIGURED_API_KEY'
```

Call the health endpoint and protected API routes:

```bash
curl -i "$API_URL/health"
curl -i -H "X-API-Key: $API_KEY" "$API_URL/api/v1/instruments?limit=10"
curl -i -H "X-API-Key: $API_KEY" \
  "$API_URL/api/v1/transactions?type=BUY&limit=10"
curl -i -H "X-API-Key: $API_KEY" "$API_URL/api/v1/portfolio/holdings"
curl -i -H "X-API-Key: $API_KEY" "$API_URL/api/v1/portfolio/summary"
```

Expect `200` for health and valid protected requests, and `401` for a protected
request without a valid API key. Swagger UI should be available at
`$API_URL/docs` when API Gateway forwards that route. The health endpoint only
checks that the function responds; it does not contact Aurora.

A `503` from a data endpoint indicates a database configuration, connection,
or query problem. Check the Lambda CloudWatch logs, VPC/subnets, security-group
rules, endpoint, credentials, and table grants.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Lambda times out on data requests | VPC/subnet selection, routes, Aurora/proxy security-group inbound TCP `5432`, Lambda egress, and endpoint |
| `503` with database connection error | `DATABASE_URL`, password URL encoding, `DB_SSLMODE=require`, database name, and read-only user grants |
| `401` on protected route | `X-API-Key` header matches the Lambda `API_KEY` value |
| Health is `200` but database routes fail | Health is process liveness only; inspect the Lambda logs and database network/configuration |
| `502` from API Gateway | Lambda invocation permission, deployed `$default` route, function errors, and CloudWatch logs |
| Tables not found | Importer has been run against the intended database/schema; API expects `public.instruments` and `public.transactions` |
