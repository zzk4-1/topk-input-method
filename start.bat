@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 Python，请先安装 Python 3.10+：https://www.python.org/downloads/
  pause
  exit /b 1
)

if not exist key.txt (
  echo [提示] 未找到 key.txt：请先在项目根目录创建 key.txt，写入你的 DeepSeek API Key。
  echo        获取地址：https://platform.deepseek.com/ （API Keys 页面创建并复制）
  pause
  exit /b 1
)

echo 正在启动 Top-K 输入法（网页版）...
python server.py --open
pause
