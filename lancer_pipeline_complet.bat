@echo off
title Pipeline Data, Import Multi-Sources & Quality Gate - Sentinel ImmoNC
echo ============================================================
echo   SYNCHRONISATION MULTI-PORTAILS + CONTROLE QUALITE AUTO
echo ============================================================
echo.
.venv\Scripts\python.exe main.py --sync-live --audit
echo.
echo ============================================================
echo   EXECUTION TERMINEE (Rapport: logs\quality_gate_last.json)
echo ============================================================
pause
