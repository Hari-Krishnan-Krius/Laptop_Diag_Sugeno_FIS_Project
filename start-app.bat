@echo off

call venv\Scripts\activate.bat
copy .env.example .env
pip install wmi pywin32
python run.py

pause