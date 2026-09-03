@echo off
rem  Download the binary dependencies this project needs: bin\ and cslol-tools\.
rem
rem  Sources:
rem    - ritobin:     https://github.com/moonshadow565/ritobin/releases
rem                   (pinned tag 2025-10-05-e686d9e; ritobin.zip -> extract bin\)
rem    - cslol-tools: https://github.com/LeagueToolkit/cslol-manager/releases
rem                   (pinned tag 2026-04-15-23f2308;
rem                    cslol-manager-windows.exe is a 7z SFX,
rem                    extracts cslol-manager\cslol-tools -> cslol-tools\)
rem
rem  File-system footprint (all paths are inside this project):
rem    .\.setup-tmp\    temp downloads (deleted when this script finishes)
rem    .\bin\           ritobin binaries
rem    .\cslol-tools\   cslol tools
rem  All files managed by this setup script stay inside the project directory.
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
set "CERTUTIL=%SystemRoot%\System32\certutil.exe"
set "FINDSTR=%SystemRoot%\System32\findstr.exe"
if not exist "%CURL%" (echo [setup] missing %CURL% -- Windows 10 1803+ required & exit /b 1)
if not exist "%TAR%" (echo [setup] missing %TAR% -- Windows 10 1803+ required & exit /b 1)
if not exist "%CERTUTIL%" (echo [setup] missing %CERTUTIL% & exit /b 1)
if not exist "%FINDSTR%" (echo [setup] missing %FINDSTR% & exit /b 1)

set "RITOBIN_URL=https://github.com/moonshadow565/ritobin/releases/download/2025-10-05-e686d9e/ritobin.zip"
set "RITOBIN_SHA256=5834dc9a699b67176f09b5ec9c2fdcae19314ffbc583c4196ec8fce0bf85fc35"
set "CSLOL_URL=https://github.com/LeagueToolkit/cslol-manager/releases/download/2026-04-15-23f2308/cslol-manager-windows.exe"
set "CSLOL_SHA256=f528db8cf63ebd580886c747bff7ca2de69644307724738eea3de22ce8ea04ac"

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
call :verify_sha256 "%TMP_DIR%\ritobin.zip" "%RITOBIN_SHA256%"
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
if %FORCE%==0 if exist "%CSLOL_DIR%\wad-make.exe" if exist "%CSLOL_DIR%\wad-extract.exe" if exist "%CSLOL_DIR%\hashes.game.txt" (
    echo [setup] skip cslol-tools ^(already present at .\%CSLOL_DIR%^); use --force to re-download
    exit /b 0
)
echo [setup] === cslol-tools ===
echo [setup] GET %CSLOL_URL%
"%CURL%" -fL --progress-bar -o "%TMP_DIR%\cslol-manager-windows.exe" "%CSLOL_URL%"
if errorlevel 1 exit /b 1
call :verify_sha256 "%TMP_DIR%\cslol-manager-windows.exe" "%CSLOL_SHA256%"
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


rem ---------------------------------------------------------------------------
:verify_sha256
"%CERTUTIL%" -hashfile "%~1" SHA256 | "%FINDSTR%" /I /X /C:"%~2" >nul
if errorlevel 1 (
    echo [setup] SHA-256 mismatch for %~1
    echo [setup] expected %~2
    "%CERTUTIL%" -hashfile "%~1" SHA256
    exit /b 1
)
echo [setup] SHA-256 verified for %~1
exit /b 0
