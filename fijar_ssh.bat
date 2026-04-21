@echo off
echo Añadiendo C:\Windows\System32\OpenSSH al PATH...
setx PATH "%PATH%;C:\Windows\System32\OpenSSH"
echo.
echo Hecho. Por favor, cierra esta terminal y abre una nueva para aplicar los cambios.
pause
