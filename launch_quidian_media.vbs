' Quidian Media Downloader — Silent Background Launcher
Option Explicit
Dim WshShell, fso, q, appDir, py, logPath, i
q = Chr(34)
appDir = "C:\Users\chkam\OneDrive\Desktop\Media_Downloader_Project"
logPath = appDir & "\media_downloader_launch.log"
Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
WshShell.CurrentDirectory = appDir

Function ServerUp()
  Dim h
  ServerUp = False
  On Error Resume Next
  Set h = CreateObject("MSXML2.ServerXMLHTTP.6.0")
  h.setTimeouts 1500, 1500, 1500, 1500
  h.Open "GET", "http://127.0.0.1:5050/api/status", False
  h.Send
  If Err.Number = 0 And (h.Status = 200 Or h.Status = 404) Then ServerUp = True
  On Error GoTo 0
End Function

If ServerUp() Then
  WshShell.Run "http://127.0.0.1:5050/", 1, False
  WScript.Quit
End If

py = "C:\Users\chkam\AppData\Local\Programs\Python\Python314\python.exe"
If Not fso.FileExists(py) Then py = "python.exe"

' Run Flask app completely hidden in background (window style 0)
WshShell.Run "cmd /c " & q & q & py & q & " app.py > " & q & logPath & q & " 2>&1" & q, 0, False

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
