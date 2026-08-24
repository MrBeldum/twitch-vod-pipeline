@echo off
REM No arguments starts the desktop app; otherwise pass through to the CLI.
setlocal
set ROOT=%~dp0
set HOST=%ROOT%VODPipeline.exe
set PY=C:\Python314\python.exe
if not exist "%PY%" set PY=python

pushd "%ROOT%"
if "%~1"=="" (
  if exist "%HOST%" (
    start "" "%HOST%"
  ) else (
    "%PY%" -m vodpipe app
  )
) else (
  "%PY%" -m vodpipe %*
)
popd
endlocal
