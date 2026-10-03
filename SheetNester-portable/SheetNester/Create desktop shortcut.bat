@echo off
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ws=New-Object -ComObject WScript.Shell; $l=$ws.CreateShortcut((Join-Path $ws.SpecialFolders('Desktop') 'SheetNester.lnk')); $l.TargetPath='%~dp0SheetNester.bat'; $l.WorkingDirectory='%~dp0'; $l.IconLocation='%~dp0python\python.exe,0'; $l.Save()"
echo Shortcut 'SheetNester' created on the Desktop.
pause
