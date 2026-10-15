@echo off
chcp 65001 >nul
setlocal

rem ============================================================
rem  歌词翻译成片工具 — 双击启动
rem  会自动找到可用的 Python 并启动本地网页服务
rem ============================================================

cd /d "%~dp0"

set "PY="
rem 依次尝试:DSH 自带运行时 -> 系统 python -> py 启动器
if exist "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" (
  set "PY=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
)
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  where py >nul 2>nul && set "PY=py"
)

if not defined PY (
  echo.
  echo   [错误] 没有找到 Python。
  echo   请先安装 Python 3.9 以上版本,或把 python.exe 放到 PATH 里。
  echo.
  pause
  exit /b 1
)

echo.
echo   正在启动… 浏览器会自动打开
echo   关闭这个窗口即可停止服务
echo.

rem 延迟 2 秒打开浏览器(等端口起来)
start "" cmd /c "timeout /t 2 >nul & start http://127.0.0.1:8770/"

"%PY%" "%~dp0serve.py" --port 8770

echo.
echo   服务已停止。
pause
