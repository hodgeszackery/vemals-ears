@echo off
setlocal
cd /d "%~dp0.."
rem A TCL_LIBRARY or PYTHONHOME left behind by another Python points the
rem bundled interpreter at a foreign standard library, and the caption
rem window dies on import instead of opening.
set "TCL_LIBRARY="
set "TK_LIBRARY="
set "PYTHONHOME="
set "PYTHONPATH="
set "DOWNTIME_PYW=%~dp0..\runtime\pythonw.exe"
if not exist "%DOWNTIME_PYW%" set "DOWNTIME_PYW=pythonw"
start "Vemal's Ears Preview" "%DOWNTIME_PYW%" "%~dp0voice_captions.py" --demo
