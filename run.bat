@echo off
cd /d %~dp0
call venv\Scripts\activate
waitress-serve --host=0.0.0.0 --port=8000 app:app
