' Launch scheduled-sync.ps1 with no console window at all.
'
' An "Interactive" scheduled task opens its console on the desktop. The first trial
' run was killed 13 seconds in with STATUS_CONTROL_C_EXIT (0xC000013A) -- the
' signal a console process gets when its window is closed. wscript.exe is a windowed
' host with no console of its own, and Run(..., 0, True) starts PowerShell hidden
' from the first instant: nothing appears, nothing can be closed by accident.
'
' The child's exit code is passed through, so Task Scheduler's "last run result"
' still reports the real outcome.

Option Explicit
Dim shell, fso, here, profile, command

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)

profile = "cloud-security"
If WScript.Arguments.Count > 0 Then profile = WScript.Arguments(0)

command = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File """ _
        & here & "\scheduled-sync.ps1"" -TargetProfile """ & profile & """"

WScript.Quit shell.Run(command, 0, True)
