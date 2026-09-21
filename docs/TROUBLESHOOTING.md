# Troubleshooting

## Diagnostic order

Use this order to avoid confusing one failure layer with another:

1. Confirm the application process and container health.
2. Confirm PostgreSQL connectivity.
3. Validate client authentication and policy with the inventory endpoint.
4. Test route resolution with one known enabled identifier.
5. Test one upstream credential with a bounded request.

## Inventory is empty

Check client expiry, quota, allowlist, disabled routes, custom upstream configuration, and credential availability. The public inventory is dynamic; do not compare it with a list copied from documentation.

## Authentication is rejected

Confirm the header format and that the value belongs to the intended trust domain. A dashboard login, managed router key, legacy router password, and upstream credential are different secrets.


## All requests fail before dispatch

Inspect database health, policy rejection details, alias resolution, and route prefixes. Confirm that the resolved identifier remains allowed for the client.

## One upstream path fails

Check its base URL, DNS, TLS, credential state, disabled status, identifier availability, response status, and response format. Test one request only. Repeated retries can consume quota or trigger wider limiting.

## Streaming stops early

Separate client disconnect, reverse-proxy timeout, upstream timeout, malformed server event, and translation error. Confirm that the proxy disables buffering and allows long-lived responses. Inspect the final complete event and usage event when present.

## Tool calls are malformed

Compare the incoming tool schema, translated upstream payload, streamed argument fragments, normalized identifier, and final client payload. JSON arguments can arrive over several stream events and must be assembled in order.

## Credential rotation loops

Review the slow-response threshold, limited timestamps, cooldown, and upstream health. A threshold below normal time-to-first-token can rotate healthy credentials. Pause the affected route before clearing credential state repeatedly.

## Usage or quota looks wrong

Compare provider-reported usage, normalized usage, request logs, and the managed-key counter. Interrupted streams may lack a terminal usage event. Preserve evidence before correcting counters.

## Dashboard live activity is stale

Check admin session validity, `/api/sse`, reverse-proxy buffering, connection timeout, and browser network errors. Historical API requests can still work while the SSE channel is blocked.



## Compose cannot build

The repository references an optional sibling context at `../copilot-api`. Restore that directory or remove the optional service through a Compose override. Do not change the main application build context to hide the missing dependency.

## Evidence to collect

Record the deployed commit, request timestamp, route, non-secret client identifier, resolved route, response status, latency, container state, and relevant redacted logs. Never collect authorization headers or raw provider credentials.
