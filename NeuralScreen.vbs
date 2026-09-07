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
    ' fallback: pythonw из PATH (если пользователь поставил Python)
    py = "pythonw"
End If

' --- Проверка Python: без него запуск вслепую = тихий отказ ---
If py = "pythonw" Then
    Dim found
    found = False
    Dim pathVar, parts, part
    pathVar = shell.ExpandEnvironmentStrings("%PATH%")
    parts = Split(pathVar, ";")
    For Each part In parts
        If part <> "" And fso.FileExists(part & "\pythonw.exe") Then
            found = True
            Exit For
        End If
    Next
    If Not found Then
        MsgBox "NeuralScreen: pythonw.exe not found." & vbCrLf & _
               "Install Python or unpack the release archive (it bundles a portable runtime).", _
               16, "NeuralScreen"
        WScript.Quit 1
    End If
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
