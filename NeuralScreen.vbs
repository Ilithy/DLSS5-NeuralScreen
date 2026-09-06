' NeuralScreen.vbs — скрытый лаунчер (без консольных окон)
' Запускает main.py через pythonw.exe (runtime/pythonw.exe или из PATH),
' весь вывод уходит в NeuralScreen.log рядом с программой.
Option Explicit

Dim fso, shell, dir, py, nvruntime, devpython
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

dir = fso.GetParentFolderName(WScript.ScriptFullName)

' --- Поиск Python: NEURALSCREEN_PYTHON -> runtime\pythonw.exe -> dev path -> PATH ---
py = shell.ExpandEnvironmentStrings("%NEURALSCREEN_PYTHON%")
If py = "%NEURALSCREEN_PYTHON%" Then py = ""
If py <> "" And Not fso.FileExists(py) Then py = ""

If py = "" Then
    nvruntime = dir & "\runtime\pythonw.exe"
    If fso.FileExists(nvruntime) Then py = nvruntime
End If

If py = "" Then
    devpython = "D:\YouTube-DTF\dlss5\video-tools\merserk-0.1\bin\python-3.13.15-embed-amd64\pythonw.exe"
    If fso.FileExists(devpython) Then py = devpython
End If

If py = "" Then
    ' fallback: pythonw из PATH (если пользователь поставил Python)
    py = "pythonw"
End If

' --- Проверка NGX runtime (165 МБ, не хранится в git) ---
If Not fso.FileExists(dir & "\native\nvngx_dlssnr.dll") Then
    MsgBox "NeuralScreen: native\nvngx_dlssnr.dll not found." & vbCrLf & _
           "Copy the NVIDIA DLSS 5 Neural Rendering runtime there." & _
           "See README.md, section Requirements.", 16, "NeuralScreen"
    WScript.Quit 1
End If

' --- Проверка воркера (артефакт сборки) ---
If Not fso.FileExists(dir & "\native\nvngx.dll") Then
    MsgBox "NeuralScreen: native\nvngx.dll not found." & vbCrLf & _
           "Build it with native\build-host.bat or re-download the release archive.", _
           16, "NeuralScreen"
    WScript.Quit 1
End If

' --- Запуск без окна (window style 0), без ожидания ---
shell.Run """" & py & """ -u """ & dir & "\main.py""", 0, False
