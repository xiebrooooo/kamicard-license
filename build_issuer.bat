@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo   卡密签发工具 - 打包
echo ============================================================
echo.
echo   ⚠ 打出来的 exe 含签发密钥，只能自己留着，别外发。
echo.

REM ---- 找 Python ----
set PY=
where py >nul 2>nul && set PY=py -3
if "%PY%"=="" (
  where python >nul 2>nul && set PY=python
)
if "%PY%"=="" (
  echo [错误] 找不到 Python，请先安装 Python 3.11~3.13
  pause
  exit /b 1
)
echo [1/3] Python: %PY%
%PY% -c "import sys;print('      版本:', sys.version.split()[0])"

REM ---- 依赖 ----
echo.
echo [2/3] 检查依赖 ...
%PY% -c "import webview, PyInstaller" 2>nul
if errorlevel 1 (
  echo       缺少 pywebview 或 PyInstaller，正在安装 ...
  %PY% -m pip install pywebview pyinstaller -q ^
      -i https://mirrors.ustc.edu.cn/pypi/simple/
  if errorlevel 1 (
    echo [错误] 安装失败
    pause
    exit /b 1
  )
)
echo       依赖就绪

REM ---- 打包 ----
echo.
echo [3/3] 打包 ...
if exist build_issuer rmdir /s /q build_issuer
if exist dist_issuer rmdir /s /q dist_issuer

%PY% -m PyInstaller --noconfirm --windowed --onefile ^
  --name 卡密签发工具 ^
  --distpath dist_issuer ^
  --workpath build_issuer ^
  --add-data "issuer/ui;ui" ^
  --paths "." ^
  --collect-all webview --collect-submodules clr_loader ^
  issuer/card_tool.py

if not exist "dist_issuer\卡密签发工具.exe" (
  echo.
  echo [错误] 打包失败。去掉命令里的 --noconfirm 之外的重定向，重跑看报错。
  echo        常见原因：webview 缺少 clr_loader / pythonnet
  pause
  exit /b 1
)

echo.
if exist build_issuer rmdir /s /q build_issuer
if exist "卡密签发工具.spec" del /q "卡密签发工具.spec"

echo ============================================================
echo   完成！
echo.
echo   产物：dist_issuer\卡密签发工具.exe
echo.
echo   别忘了：签发记录会写在 exe 同目录的 cards\history.json
echo ============================================================
pause
