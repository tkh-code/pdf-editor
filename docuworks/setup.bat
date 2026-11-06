@echo off
rem DocuWorks to PDF : create shortcuts (Desktop and SendTo)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0dw2pdf.ps1" -Setup
