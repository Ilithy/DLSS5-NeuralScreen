@echo off
rem NeuralScreen - DLSS 5 Neural Rendering overlay for the Windows desktop.
rem Consoles stays open: main.py logs FPS and pipeline timings into it.
setlocal
cd /d "%~dp0"

rem --- Python: env var -> bundled runtime -> known dev path -> PATH ---------
set "NS_PY=%NEURALSCREEN_PYTHON%"
if not defined NS_PY if exist "%~dp0runtime\python.exe" set "NS_PY=%~dp0runtime\python.exe"
if not defined NS_PY if exist "D:\YouTube-DTF\dlss5\video-tools\merserk-0.1\bin\python-3.13.15-embed-amd64\python.exe" set "NS_PY=D:\YouTube-DTF\dlss5\video-tools\merserk-0.1\bin\python-3.13.15-embed-amd64\python.exe"
if not defined NS_PY set "NS_PY=python"

rem --- NGX runtime: 165 MB redistributable, not stored in git ---------------
if not exist "%~dp0native\nvngx_dlssnr.dll" (
    echo [NeuralScreen] native\nvngx_dlssnr.dll not found.
    echo Copy the DLSS Ray Reconstruction / Neural Rendering runtime there.
    echo See README.md, section "Requirements".
    pause
    exit /b 1
)

rem --- Worker: build artefact, not stored in git ----------------------------
if not exist "%~dp0native\nvngx.dll" (
    echo [NeuralScreen] native\nvngx.dll not found - building it.
    call "%~dp0native\build-host.bat"
    if errorlevel 1 (
        echo [NeuralScreen] Worker build failed. See README.md, section "Build".
        pause
        exit /b 1
    )
)

"%NS_PY%" -u "%~dp0main.py" %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo [NeuralScreen] exit code %RC%
    pause
)
exit /b %RC%
