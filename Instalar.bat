@echo off
chcp 65001 >nul
title Clipear - Instalacion
cd /d "%~dp0"
echo.
echo ===== Instalacion de Clipear =====
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] No encuentro Python.
    echo Instala Python 3.11 o 3.12 desde https://www.python.org/downloads/
    echo y marca la casilla "Add python.exe to PATH" durante la instalacion.
    echo Luego cierra esta ventana y vuelve a ejecutar Instalar.bat
    pause
    exit /b 1
)

if not exist "venv\Scripts\activate.bat" (
    echo Creando el entorno de Python...
    python -m venv venv
    if errorlevel 1 (
        echo [ERROR] No pude crear el entorno de Python.
        pause
        exit /b 1
    )
)

call venv\Scripts\activate.bat
echo Instalando las librerias, puede tardar varios minutos...
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Fallo la instalacion de las librerias. Copia el mensaje de arriba y buscalo o pidele ayuda a alguien.
    pause
    exit /b 1
)

set NEEDRESTART=0

where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo.
    echo ffmpeg no esta instalado. Lo instalo con winget...
    winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
    set NEEDRESTART=1
)

where deno >nul 2>&1
if errorlevel 1 (
    echo.
    echo Deno no esta instalado. Es necesario para YouTube. Lo instalo con winget...
    winget install --id DenoLand.Deno -e --accept-source-agreements --accept-package-agreements
    set NEEDRESTART=1
)

echo.
echo ===== Listo =====
if "%NEEDRESTART%"=="1" (
    echo Se instalaron programas nuevos: CIERRA esta ventana antes de continuar.
)
echo Para usar Clipear, abre Iniciar.bat
echo.
pause
