' Quidian Media Downloader — Silent Background Launcher
' Resilient auto-locating launcher
Option Explicit
Dim WshShell, fso, q, appDir, py, logPath, i, candidates, cand
q = Chr(34)
Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

appDir = fso.GetParentFolderName(WScript.ScriptFullName)
If Not fso.FileExists(appDir & "\app.py") Then
    candidates = Array( _
        "C:\Users\chkam\OneDrive\Desktop\Media_Downloader_Project", _
        "C:\Users\chkam\OneDrive\Desktop\02_Projects & Development\Media_Downloader_Project", _
        "C:\Users\chkam\Desktop\Media_Downloader_Project", _
        "C:\Users\chkam\Desktop\02_Projects & Development\Media_Downloader_Project" _
    )
    For Each cand In candidates
        If fso.FileExists(cand & "\app.py") Then
            appDir = cand
            Exit For
        End If
    Next
End If

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

Dim userProf, localApp, pyw
userProf = WshShell.ExpandEnvironmentStrings("%USERPROFILE%")
localApp = WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%")
pyw = ""

Dim pyCandidates(4)
pyCandidates(0) = localApp & "\Programs\Python\Python314\pythonw.exe"
pyCandidates(1) = localApp & "\Programs\Python\Python311\pythonw.exe"
pyCandidates(2) = userProf & "\miniconda3\envs\quidian\pythonw.exe"
pyCandidates(3) = userProf & "\anaconda3\envs\quidian\pythonw.exe"
pyCandidates(4) = "C:\Users\chkam\AppData\Local\Programs\Python\Python314\pythonw.exe"

For Each cand In pyCandidates
  If fso.FileExists(cand) Then
    pyw = cand
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
  ' Fallback: open anyway
  WshShell.Run "http://127.0.0.1:5050/", 1, False
End If
