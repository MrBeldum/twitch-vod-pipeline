@echo off
REM No arguments starts the dashboard; otherwise pass through to the CLI.
setlocal
set PY=C:\Python314\python.exe
if not exist "%PY%" set PY=python

pushd "%~dp0"
if "%~1"=="" (
  "%PY%" -m vodpipe dashboard
) else (
  "%PY%" -m vodpipe %*
)
popd
endlocal
