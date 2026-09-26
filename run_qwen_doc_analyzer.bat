@echo off
rem Wrapper fuer den Windows-Taskplaner.
rem PYTHON_EXE per Umgebungsvariable ueberschreiben, z. B.:
rem   set PYTHON_EXE=C:\MeineDateienDesk\qwenDocAnalysis\venv\Scripts\python.exe
if "%PYTHON_EXE%"=="" set "PYTHON_EXE=python"
cd /d "%~dp0"
"%PYTHON_EXE%" qwen_doc_analyzer.py %*
exit /b %ERRORLEVEL%
