@echo off
REM run.bat - one-command startup for the Agentic Commerce demo (Windows version of run.sh)
REM
REM Safe to re-run: it will NOT wipe existing demo data. db\seed.py only
REM inserts seed rows if the products table is empty (pass --force to
REM db\seed.py yourself if you deliberately want to reset).
REM
REM This script starts the merchant FastAPI service and then prints the two
REM follow-up commands (buyer agent CLI, Streamlit dashboard) rather than
REM starting them itself, since they are interactive/foreground processes
REM best run in their own terminal windows - see the printed instructions below.
REM
REM NOTE: this version deliberately avoids IF (...) ELSE (...) parenthesized
REM blocks with literal "(" or ")" characters inside them, since cmd.exe's
REM parser can break on that combination ("... was unexpected at this time.").
REM goto-based flow control is used instead, which is more robust.

setlocal

cd /d "%~dp0"

echo === Agentic Commerce Demo - startup ===
echo.

REM --- 1. venv setup ---
if exist ".venv\" goto venv_exists

echo [1/4] Creating virtual environment .venv ...
python -m venv .venv
if errorlevel 1 goto venv_create_failed
goto venv_done

:venv_create_failed
echo Failed to create virtual environment. Is Python installed and on PATH?
exit /b 1

:venv_exists
echo [1/4] Virtual environment already exists - reusing .venv

:venv_done
call .venv\Scripts\activate.bat

REM --- 2. install requirements ---
echo [2/4] Installing requirements.txt...
python -m pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

REM --- 3. .env check ---
if exist ".env" goto env_done

echo.
echo   NOTE: .env not found. Copying .env.example to .env
echo   Edit .env and add your real Razorpay TEST-MODE keys before
echo   running a real, non-simulated purchase.
copy /y ".env.example" ".env" >nul

:env_done

REM --- 4. seed DB if missing ---
echo [3/4] Seeding database - skipped automatically if data already exists...
python db\seed.py

REM --- 5. start the merchant FastAPI service ---
echo [4/4] Starting merchant service on http://127.0.0.1:8000 ...
echo.
echo ======================================================================
echo  Merchant service starting now - this window will stay attached to it.
echo.
echo  In TWO SEPARATE Command Prompt windows, run:
echo.
echo    1. Buyer agent CLI, example:
echo       cd /d "%~dp0"
echo       .venv\Scripts\activate.bat
echo       python buyer_agent\agent.py --product keyboard --budget 10000 --agent-id agent_high --agent-secret YOUR_SECRET_HERE
echo.
echo    2. Streamlit dashboard:
echo       cd /d "%~dp0"
echo       .venv\Scripts\activate.bat
echo       streamlit run dashboard\app.py
echo.
echo  These are separate manual commands because they are interactive
echo  foreground processes best kept in their own terminal windows.
echo ======================================================================
echo.

python -m uvicorn merchant_service.main:app --host 127.0.0.1 --port 8000

endlocal