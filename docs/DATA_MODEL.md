# Data model

## Persistence domains

PostgreSQL stores four categories of state:

| Domain | Main records | Operational meaning |
| --- | --- | --- |
| Routing | provider credentials, custom upstreams, disabled routes | Determines which outbound paths can serve a request |
| Client access | managed router keys and policy fields | Determines who can call, what they can request, and how much they can consume |
| Operations | request logs and server configuration | Supports monitoring, diagnosis, and usage accounting |
| Conversation systems | playground sessions, messages, and Brain records | Stores user content and derived memory |

## Core tables

| Table | Purpose |
| --- | --- |
| `api_keys` | Upstream credentials and availability state |
| `router_api_keys` | Managed client credentials, quota, expiry, allowlists, aliases, and prompts |
| `custom_providers` | Database-defined compatible upstream configuration |
| `disabled_providers` | Runtime route suppression |
| `request_logs` | Request status, timing, routing, and usage evidence |
| `server_config` | Mutable service configuration |
| `chat_sessions` | Playground conversation containers |
| `chat_messages` | Playground messages |
| `brain_conversations` | Persisted conversation memory |
| `brain_decisions` | Structured decisions and outcomes |
| `brain_facts` | Structured facts and provenance fields |
| `brain_profiles` | Per-client derived profile state |

## Identity boundaries

Managed client-key records are the authorization source for compatible inference. Brain records use a hash of the supplied credential as their scope identifier. This avoids storing the raw credential as a foreign identifier, but a leaked credential still grants access according to the route authentication behavior.

Admin sessions are separate from client keys. Provider credentials are separate from both and authorize outbound requests.

## Usage accounting

Request logs capture evidence used by dashboard totals and diagnosis. Managed keys maintain token consumption for quota enforcement. A provider may return usage only at the end of a stream, so interrupted streams can have incomplete accounting evidence.

Do not edit quota counters without an audit record. When totals disagree, compare the normalized response, terminal stream event, request log, and managed-key record.

## Brain records

Brain stores source text, derived vectors or searchable representation, session relationships, timestamps, and structured decision or fact fields. Profiles summarize client-scoped history. Retention and deletion must cover both direct records and derived information.

## Schema lifecycle

Application startup creates missing compatible schema objects. This supports simple deployments but does not provide a complete versioned rollback path. Before a schema-affecting release:

1. capture a database dump;
2. test startup against a restored copy;
3. inspect added or changed indexes;
4. validate both old and new record paths;
5. document whether rollback requires restoring the database.

## Backup classification

A database export can contain credentials, prompts, responses, client policy, usage, and operational history. Treat every dump as a secret. Encrypt it, restrict access, record retention, and test restoration on an isolated database.
