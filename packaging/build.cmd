@echo off
setlocal
set ROOT=%~dp0..
set PACK=%~dp0
set CSC=%SystemRoot%\Microsoft.NET\Framework64\v4.0.30319\csc.exe
if not exist "%CSC%" set CSC=%SystemRoot%\Microsoft.NET\Framework\v4.0.30319\csc.exe
if not exist "%CSC%" (
  echo csc.exe not found. .NET Framework 4 is required to compile the Windows host.
  exit /b 1
)
if not exist "%PACK%vodpipe.ico" (
  echo packaging\vodpipe.ico is missing.
  exit /b 1
)
"%CSC%" /nologo /optimize+ /target:winexe /platform:x64 ^
  /reference:System.Windows.Forms.dll /reference:System.Drawing.dll ^
  /win32icon:"%PACK%vodpipe.ico" /win32manifest:"%PACK%app.manifest" ^
  /out:"%ROOT%\VODPipeline.exe" "%PACK%host.cs"
if errorlevel 1 exit /b 1
copy /Y "%PACK%VODPipeline.VisualElementsManifest.xml" "%ROOT%\VODPipeline.VisualElementsManifest.xml" >nul
echo Built %ROOT%\VODPipeline.exe
endlocal
