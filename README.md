How to setup:

## 1. Create a Python Virtual Environment

Open a terminal inside the project folder and run:

```
python -m venv venv
```

Activate the environment.

**Windows**

```
venv\Scripts\activate
```

**Mac/Linux**

```
source venv/bin/activate
```

Once activated, your terminal should display `(venv)` at the beginning.

---

## 2. Install Required Dependencies

Install all required packages listed in `requirements.txt`.

```
pip install -r requirements.txt
```

Wait until installation finishes before moving to the next step.

---

## 3. Install and Configure Interactive Brokers TWS

Download **Trader Workstation (TWS)** from the Interactive Brokers website and install it.

Open TWS and log in using your **Paper Trading account**.

Then configure API access:

1. Go to **File → Global Configuration**
2. Navigate to **API → Settings**
3. Enable the following options:

   * Enable ActiveX and Socket Clients → **ON**
   * Socket Port → **7497** (Paper Trading)
   * Read-Only API → **OFF**
   * Bypass Order Precautions for API Orders → **ON**

Click **Apply** and then **OK**.

---

## 4. Verify Configuration File

Open the file:

```
config.py
```

Ensure the settings match paper trading:

```
port = 7497
mode = paper
```

You can modify other strategy parameters here if needed.

---

## 5. Start the Trading System

Before running the program, make sure **TWS is open and logged in**.

Open a terminal inside the project folder.

### Run in Read-Only Mode (signals only)

```
python runner.py --mode readonly
```

This connects to IBKR and prints trading signals without placing orders.

### Run in Paper Trading Mode

```
python runner.py --mode paper
```

This mode executes trades inside your **IBKR paper trading account**.

---

## 6. Run a Backtest

If historical data exists in the `data` folder, you can run a backtest.

```
python backtest.py --csv data/spy_1min_hist.csv --start 2024-01-01
```

For detailed trade-by-trade output:

```
python backtest.py --csv data/spy_1min_hist.csv --verbose
```

---

## 7. Stop the Program

To stop the system safely, press:

```
Ctrl + C
```

The program will close any open position before exiting.

---

## 8. Output Files

After running the system, logs and results will be saved in these locations:

```
logs/trades.csv
logs/daily_summary.csv
logs/errors.log
logs/backtest_results.csv
data/spy_1min_hist.csv
```

These files contain trade records, daily performance summaries, system logs, and historical market data.
