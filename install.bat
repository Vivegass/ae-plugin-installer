@echo off
rem Console version of AE Plugin Installer.
rem Drop archives onto this file, or run it to process every archive in this folder.
rem Requires Python 3.10+ (py launcher) and 7-Zip for rar/7z archives.
cd /d "%~dp0"
py -3 -m ae_plugin_installer.cli --pause %*
