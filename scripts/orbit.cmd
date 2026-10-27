@echo off
rem Orbit CLI launcher (Windows).
rem
rem Why this exists: `[project.scripts]` only produces `orbit.exe` after the
rem package is INSTALLED (uv sync / pip install -e .). This machine has no
rem network and no setuptools, so the package cannot be installed - but the CLI
rem must still work. So we run the source directly via `-m app.cli`, which is
rem exactly equivalent (same entry function `app.cli.main:main`).
rem
rem Keep this file ASCII-only: cmd.exe reads .cmd in the OEM codepage, and
rem UTF-8 comments get mangled into bogus commands.
rem
rem Usage: add E:\bbbooottt\scripts to PATH, then run `orbit status`.
setlocal
set "ORBIT_ROOT=%~dp0.."
set "PYTHONPATH=%ORBIT_ROOT%\backend;%PYTHONPATH%"
set "PYTHONIOENCODING=utf-8"
"%ORBIT_ROOT%\backend\.venv\Scripts\python.exe" -m app.cli %*
exit /b %ERRORLEVEL%
