' Startet die Fenster-Oberflaeche (Agent-Fenster.py) OHNE schwarzes
' Konsolenfenster. Einfach doppelklicken.
'
' Nutzt "pythonw" (Python ohne Konsole). Falls das nicht gefunden wird,
' wird als Rueckfall "python" verwendet (dann erscheint kurz eine Konsole).

Dim fso, sh, scriptDir
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = scriptDir

Dim target
target = """" & scriptDir & "\Agent-Fenster.py"""

On Error Resume Next
sh.Run "pythonw " & target, 0, False        ' 0 = kein sichtbares Fenster
If Err.Number <> 0 Then
    Err.Clear
    sh.Run "python " & target, 1, False     ' Rueckfall: mit Konsole
    If Err.Number <> 0 Then
        MsgBox "Python wurde nicht gefunden. Bitte von python.org installieren" & vbCrLf & _
               "und beim Setup 'Add Python to PATH' anhaken.", vbExclamation, "Recherche-Agent"
    End If
End If
On Error Goto 0
