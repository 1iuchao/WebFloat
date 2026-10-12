' WebFloat launcher (silent, no console window)
Option Explicit
Dim fso, sh, cur, pyw
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

cur = fso.GetParentFolderName(WScript.ScriptFullName)
pyw = cur & "\.venv\Scripts\pythonw.exe"

If Not fso.FileExists(pyw) Then
    MsgBox "Virtual environment not found." & vbCrLf & vbCrLf & _
           "Please run install.bat first, then try again." & vbCrLf & vbCrLf & _
           "Expected: " & pyw, 16, "WebFloat"
    WScript.Quit 1
End If

sh.CurrentDirectory = cur
sh.Run """" & pyw & """ """ & cur & "\ds_popup.py""", 0, False
