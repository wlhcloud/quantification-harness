# Data Query Service

Read-only API for market, minute, finance, factor, and quality metadata. It never owns synchronization jobs and opens source SQLite databases with `mode=ro` and `PRAGMA query_only=ON`.

Default endpoint: `http://127.0.0.1:9103/api/v1`.

Credential fields are always redacted; this service must never expose upstream tokens.
