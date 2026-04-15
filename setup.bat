@echo off
rem  Download the binary dependencies this project needs: bin\ and cslol-tools\.
rem
rem  Sources:
rem    - ritobin:     https://github.com/moonshadow565/ritobin/releases
rem                   (pinned tag 2025-10-05-e686d9e; ritobin.zip -> extract bin\)
rem    - cslol-tools: https://github.com/LeagueToolkit/cslol-manager/releases
rem                   (latest; cslol-manager-windows.exe is a 7z SFX,
rem                    extracts cslol-manager\cslol-tools -> cslol-tools\)
rem
rem  File-system footprint (all paths are inside this project):
rem    .\.setup-tmp\    temp downloads (deleted when this script finishes)
rem    .\bin\           ritobin binaries
rem    .\cslol-tools\   cslol tools
rem  No files are created outside the project directory.
rem
rem  Requires Windows 10 1803+ (for built-in curl.exe and tar.exe).
rem
rem  Usage:
rem    setup.bat                  download anything missing
rem    setup.bat --force          re-download and overwrite
rem    setup.bat --only ritobin   just ritobin (or --only cslol)

setlocal EnableExtensions
cd /d "%~dp0"

set "TMP_DIR=.setup-tmp"
set "BIN_DIR=bin"
set "CSLOL_DIR=cslol-tools"
set "ROOT=%CD%"

rem  Pin to the Windows-shipped curl and tar so a user's PATH cannot shadow
rem  them with GNU/MSYS builds (GNU tar doesn't read zip archives).
set "CURL=%SystemRoot%\System32\curl.exe"
set "TAR=%SystemRoot%\System32\tar.exe"
if not exist "%CURL%" (echo [setup] missing %CURL% -- Windows 10 1803+ required & exit /b 1)
if not exist "%TAR%" (echo [setup] missing %TAR% -- Windows 10 1803+ required & exit /b 1)

set "RITOBIN_URL=https://github.com/moonshadow565/ritobin/releases/download/2025-10-05-e686d9e/ritobin.zip"
set "CSLOL_URL=https://github.com/LeagueToolkit/cslol-manager/releases/latest/download/cslol-manager-windows.exe"

set "FORCE=0"
set "ONLY="

:parse
if "%~1"=="" goto after_parse
if /I "%~1"=="--force" (set "FORCE=1" & shift & goto parse)
if /I "%~1"=="--only" (
    if "%~2"=="" (echo [setup] --only requires a value: ritobin^|cslol & exit /b 2)
    set "ONLY=%~2"
    shift & shift & goto parse
)
echo [setup] unknown arg: %~1  (valid: --force, --only ritobin^|cslol)
exit /b 2
:after_parse

if not "%ONLY%"=="" if /I not "%ONLY%"=="ritobin" if /I not "%ONLY%"=="cslol" (
    echo [setup] --only must be "ritobin" or "cslol", got "%ONLY%"
    exit /b 2
)

echo [setup] project root = %ROOT%

if exist "%TMP_DIR%" rmdir /s /q "%TMP_DIR%"
mkdir "%TMP_DIR%" || goto fail

if /I "%ONLY%"=="cslol" goto after_ritobin
call :do_ritobin
if errorlevel 1 goto fail
:after_ritobin

if /I "%ONLY%"=="ritobin" goto after_cslol
call :do_cslol
if errorlevel 1 goto fail
:after_cslol

rmdir /s /q "%TMP_DIR%"
echo [setup] done.
endlocal & exit /b 0

:fail
echo [setup] ERROR -- aborting
if exist "%TMP_DIR%" rmdir /s /q "%TMP_DIR%"
endlocal & exit /b 1


rem ---------------------------------------------------------------------------
:do_ritobin
if %FORCE%==0 if exist "%BIN_DIR%\ritobin_cli.exe" (
    echo [setup] skip ritobin ^(already present at .\%BIN_DIR%^); use --force to re-download
    exit /b 0
)
echo [setup] === ritobin ===
echo [setup] GET %RITOBIN_URL%
"%CURL%" -fL --progress-bar -o "%TMP_DIR%\ritobin.zip" "%RITOBIN_URL%"
if errorlevel 1 exit /b 1

rem Windows tar (libarchive) handles zips; extract inside the temp dir so the
rem archive's top-level "bin\" lands at %TMP_DIR%\bin, then move it into place.
pushd "%TMP_DIR%"
"%TAR%" -xf ritobin.zip
set "TAR_ERR=%ERRORLEVEL%"
popd
if not "%TAR_ERR%"=="0" exit /b %TAR_ERR%

if exist "%BIN_DIR%" rmdir /s /q "%BIN_DIR%"
move /Y "%TMP_DIR%\bin" "%BIN_DIR%" >nul
if errorlevel 1 exit /b 1
echo [setup] bin\ ready
exit /b 0


rem ---------------------------------------------------------------------------
:do_cslol
if %FORCE%==0 if exist "%CSLOL_DIR%\wad-make.exe" (
    echo [setup] skip cslol-tools ^(already present at .\%CSLOL_DIR%^); use --force to re-download
    exit /b 0
)
echo [setup] === cslol-tools ===
echo [setup] GET %CSLOL_URL%
"%CURL%" -fL --progress-bar -o "%TMP_DIR%\cslol-manager-windows.exe" "%CSLOL_URL%"
if errorlevel 1 exit /b 1

rem The SFX is a 7z console self-extractor; -y accepts prompts, -o<dir> picks
rem the output directory. Inside it, files live under cslol-manager\..., so
rem we get %TMP_DIR%\cslol-out\cslol-manager\cslol-tools.
mkdir "%TMP_DIR%\cslol-out" || exit /b 1
"%TMP_DIR%\cslol-manager-windows.exe" -y -o"%ROOT%\%TMP_DIR%\cslol-out" >nul
if errorlevel 1 exit /b 1

if not exist "%TMP_DIR%\cslol-out\cslol-manager\cslol-tools" (
    echo [setup] cslol-tools\ not found after extraction; release layout may have changed
    exit /b 1
)

if exist "%CSLOL_DIR%" rmdir /s /q "%CSLOL_DIR%"
move /Y "%TMP_DIR%\cslol-out\cslol-manager\cslol-tools" "%CSLOL_DIR%" >nul
if errorlevel 1 exit /b 1
echo [setup] cslol-tools\ ready
exit /b 0
