' Starts the bot without a console window.
' Run run.bat once first to save the token into token.txt.
Option Explicit

Dim fso, shell, folder, command
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

folder = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = folder

command = "pythonw.exe """ & folder & "\bot.py"""
shell.Run command, 0, False
