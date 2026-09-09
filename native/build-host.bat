@echo off
rem dlss5-feed-host64.exe -- the 64-bit NGX host (desktop-nr live build).
cd /d "%~dp0"
setlocal
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul
cl /nologo /O2 /EHsc /W3 /MD /std:c++17 /Iinclude /Isrc dlss5-feed-host64.cpp spout_bridge.cpp ^
   /Fe:nvngx.dll ^
   /link lib\Windows_x86_64\x64\nvsdk_ngx_d.lib SpoutDX.lib version.lib kernel32.lib user32.lib gdi32.lib advapi32.lib ole32.lib d3d11.lib d3d12.lib dxgi.lib d3dcompiler.lib WindowsApp.lib dwmapi.lib
if errorlevel 1 exit /b 1
endlocal
echo host built.
