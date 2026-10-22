@echo off
rem Console version of AE Plugin Installer.
rem Drop archives onto this file, or run it to process every archive in this folder.
rem Requires Python 3.10+ (py launcher). rar/7z need 7-Zip or WinRAR
rem (plain rar and zip also work through tar.exe built into Windows 10/11).
cd /d "%~dp0"
py -3 -m ae_plugin_installer.cli --pause %*
