@echo off
title Observatoire Immobilier Grand Noumea
echo ============================================================
echo   DEMARRAGE DE L'OBSERVATOIRE IMMOBILIER GRAND NOUMEA
echo ============================================================
echo.
echo Verification du serveur local...
netstat -ano | findstr ":8080 " | findstr "LISTENING" >nul
if %errorlevel% neq 0 (
    echo Demarrage du serveur Python en arriere-plan...
    start /B "" ".venv\Scripts\python.exe" server.py
    timeout /t 3 /nobreak >nul
) else (
    echo Le serveur est deja actif sur http://localhost:8080
)

echo.
echo Ouverture de votre tableau de bord dans votre navigateur...
start "" "http://localhost:8080"
echo.
echo Le tableau de bord est accessible sur http://localhost:8080
echo.
pause
