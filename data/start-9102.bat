@echo off
setlocal
cd /d "D:\ProdProject\AI\quantification-harness"
for /f "usebackq tokens=1,* delims==" %%A in (".quant.env") do set "%%A=%%B"
set "QUANT_ENGINE_PROJECT_ROOT=D:\ProdProject\AI\quantification-harness"
set "PYTHONPATH=D:\ProdProject\AI\quantification-harness\services\quant-engine\src"
"D:\ProdApp\Python\Python313\python.exe" -m uvicorn quant_engine.main:app --app-dir "D:\ProdProject\AI\quantification-harness\services\quant-engine\src" --host 127.0.0.1 --port 9102 >> "D:\ProdProject\AI\quantification-harness\data\quant-engine-9102.log" 2>&1
