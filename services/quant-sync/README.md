# Quant Sync Service

Python data plane for DSH Quant. Owns upstream Tushare ingestion (rds / promax / official),
dictionary normalization, and raw SQLite persistence (market / finance / minute).

Run:

```powershell
python -m uvicorn quant_sync.main:app --app-dir services/quant-sync/src --host 127.0.0.1 --port 9101
```

See `contracts/openapi/quant-sync.yaml` for the API contract.

Set `QUANT_SYNC_API_KEY` in production; all mutating endpoints then require the
same value in the `X-API-Key` header. Data-source tokens are write-only through the API.
