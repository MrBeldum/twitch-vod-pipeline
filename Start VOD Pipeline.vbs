' Double-click launcher: opens the compiled Windows host when present,
' otherwise pythonw -m vodpipe app. Logs go to logs\vodpipe-app.log.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
host = root & "\VODPipeline.exe"
sh.CurrentDirectory = root
If fso.FileExists(host) Then
  sh.Run """" & host & """", 1, False
Else
  py = "C:\Python314\pythonw.exe"
  If Not fso.FileExists(py) Then py = "pythonw.exe"
  sh.Run """" & py & """ -m vodpipe app", 0, False
End If
