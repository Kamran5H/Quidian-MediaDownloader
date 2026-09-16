' Quidian Media Downloader — Silent Background Launcher
Option Explicit
Dim WshShell, fso, q, appDir, py, logPath, i
q = Chr(34)
Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
appDir = fso.GetParentFolderName(WScript.ScriptFullName)
logPath = appDir & "\media_downloader_launch.log"
WshShell.CurrentDirectory = appDir

Function ServerUp()
  Dim h
  ServerUp = False
  On Error Resume Next
  Set h = CreateObject("MSXML2.ServerXMLHTTP.6.0")
  h.setTimeouts 1500, 1500, 1500, 1500
  h.Open "GET", "http://127.0.0.1:5050/api/ping", False
  h.Send
  If Err.Number = 0 And h.Status = 200 Then ServerUp = True
  On Error GoTo 0
End Function

If ServerUp() Then
  WshShell.Run "http://127.0.0.1:5050/", 1, False
  WScript.Quit
End If

Dim userProf, localApp, pyw, candidate
userProf = WshShell.ExpandEnvironmentStrings("%USERPROFILE%")
localApp = WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%")
pyw = ""

Dim candidates(4)
candidates(0) = localApp & "\Programs\Python\Python314\pythonw.exe"
candidates(1) = localApp & "\Programs\Python\Python311\pythonw.exe"
candidates(2) = userProf & "\miniconda3\envs\quidian\pythonw.exe"
candidates(3) = userProf & "\anaconda3\envs\quidian\pythonw.exe"
candidates(4) = "C:\Users\chkam\AppData\Local\Programs\Python\Python314\pythonw.exe"

For Each candidate In candidates
  If fso.FileExists(candidate) Then
    pyw = candidate
    Exit For
  End If
Next

If pyw <> "" Then
  WshShell.Run q & pyw & q & " app.py", 0, False
Else
  WshShell.Run "cmd /c python app.py", 0, False
End If

For i = 1 To 40        ' up to 20s
  WScript.Sleep 500
  If ServerUp() Then Exit For
Next

If ServerUp() Then
  WshShell.Run "http://127.0.0.1:5050/", 1, False
Else
  ' Fallback check: try opening browser anyway
  WshShell.Run "http://127.0.0.1:5050/", 1, False
End If
