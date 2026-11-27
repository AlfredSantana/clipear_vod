@echo off
chcp 65001 >nul
title Clipear - Servidor local
cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
    echo [ERROR] Falta la instalacion. Ejecuta primero Instalar.bat
    pause
    exit /b 1
)

call venv\Scripts\activate.bat

:: abre el navegador a los pocos segundos, cuando el servidor ya arranco
start "" /min cmd /c "timeout /t 6 >nul & start http://127.0.0.1:5000"

python app.py
pause
