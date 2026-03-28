@echo off
cd /d %~dp0
echo ダッシュボードを起動しています...
pip install -r requirements.txt -q
set FLASK_ENV=production
python app.py
pause
