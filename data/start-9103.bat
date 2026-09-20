@echo off
rem Bind loopback only (the old manual start used --host 0.0.0.0, exposing writes to the LAN).
rem Load QUANT_*_API_KEY / QUANT_SYNC_CREDENTIAL_KEY from .quant.env.
cd /d "D:\ProdProject\AI\quantification-harness"
for /f "usebackq eol=# tokens=1,* delims==" %%a in (".quant.env") do set "%%a=%%b"
"D:\ProdApp\Python\Python313\python.exe" -m uvicorn data_query.main:app --app-dir "D:\ProdProject\AI\quantification-harness\services\data-query-service\src" --host 127.0.0.1 --port 9103 >> "D:\ProdProject\AI\quantification-harness\data\data-query-9103.log" 2>&1
