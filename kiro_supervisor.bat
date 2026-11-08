@echo off
rem Kiro gateway supervisor - restart if crash
set PYTHONIOENCODING=utf-8
set GW=%~dp0kiro_gateway.py
:loop
python "%GW%" > "%TEMP%\kiro_gateway.log" 2>&1
echo [%date% %time%] gateway exited, restarting in 3s >> "%TEMP%\kiro_gateway_supervisor.log"
timeout /t 3 /nobreak >nul
goto loop
