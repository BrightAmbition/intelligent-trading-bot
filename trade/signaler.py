import os
import sys
import argparse
import math, time
from datetime import datetime
from decimal import *

import pandas as pd
import asyncio

from apscheduler.schedulers.background import BackgroundScheduler

from binance.client import Client
from binance.exceptions import *
from binance.helpers import date_to_milliseconds, interval_to_milliseconds
from binance.enums import *

from common.utils import *
from trade.App import *
from trade.Database import *

import logging
log = logging.getLogger('signaler')
logging.basicConfig(
    filename="signaler.log",  # parameter in App
    level=logging.DEBUG,
    #format = "%(asctime)s.%(msecs)03d %(levelname)s %(module)s - %(funcName)s: %(message)s",
    format = "%(asctime)s %(levelname)s %(message)s",
    #datefmt = '%Y-%m-%d %H:%M:%S',
)

async def sync_signaler_task():
    """
    It is a highest level task which is added to the event loop and executed normally every 1 minute and then it calls other tasks.
    """
    symbol = App.config["trader"]["symbol"]
    startTime, endTime = get_interval("1m")
    now_ts = now_timestamp()

    log.info(f"===> Start signaler task. Timestamp {now_ts}. Interval [{startTime},{endTime}].")

    #
    # 0. Check server state (if necessary)
    #
    if data_provider_problems_exist():
        await data_provider_health_check()
        if data_provider_problems_exist():
            log.error(f"Problems with the data provider server found. Skip. Will try later.")
            return

    #
    # 1. Ensure that we are up-to-date with klines
    #
    res = await sync_data_collector_task()

    if res > 0:
        return

    # Now the local database is up-to-date with latest (klines) data from the market and hence can use for analysis

    #
    # 2. Derive features by using latest (up-to-date) daa from local db
    #

    # Generate features, generate predictions, generate signals
    # We use latest trained models (they are supposed to be periodically re-trained)
    App.database.analyze(symbol)

    # Now we have a list of signals and can make trade decisions using trading logic and trade
    #

    #
    # 5.
    # Notify
    #
    signal = App.config["signaler"]["signal"]
    if signal.get("side") == "BUY":
        print(f"=====>>> BUY: {signal}")
    elif signal.get("side") == "SELL":
        print(f"<<<===== SELL: {signal}")
    else:
        print(f"----- : {signal}")
        pass

    # TODO: Validation
    #last_kline_ts = App.database.get_last_kline_ts(symbol)
    #if last_kline_ts + 60_000 != startTime:
    #    log.error(f"Problem during analysis. Last kline end ts {last_kline_ts + 60_000} not equal to start of current interval {startTime}.")

    # TODO: Trading logic
    # If signal BUY:
    #   if bought, then do nothing (already done)
    #   if buying, then do nothing (in future, we could adjust limit price)
    #   if sold, then create buy order and switch to buying state (another procedure will sync state)
    #   if selling, then cancel sell order, and then create buy order and switch to buying
    # If no signal:
    #   If buying or selling, then either do nothing or cancel order
    #   If bought or sold, then do nothing

    # Four states: buying, bought, selling, sold
    # Function for determining state from my account (sync_state): if there is order then selling or buying, if BTC then bought, if USDT then sold

    # Scheduled function for checking existng order state (if any), and if executed (or cancele, failed etc.) then switch to corresponding state


    log.info(f"<=== End signaler task.")

#
# Request/update market data
#

# Load order book (order book could be requested along with klines)
# order_book = App.client.get_order_book(symbol="BTCUSDT", limit=100)  # 100-1_000
# order_book_ticker = App.client.get_orderbook_ticker(symbol="BTCUSDT")  # dict: "bidPrice", "bidQty", "askPrice", "askQty",
# print(order_book_ticker)

async def sync_data_collector_task():
    """
    Collect latest data.
    After executing this task our local (in-memory) data state is up-to-date.
    Hence, we can do something useful like data analysis and trading.

    Limitations and notes:
    - Currently, we can work only with one symbol
    - We update only local state by loading latest data. If it is necessary to initialize the db then another function should be used.
    """

    symbol = App.config["trader"]["symbol"]
    symbols = [symbol]  # In future, we might want to collect other data, say, from other cryptocurrencies

    # Request newest data
    # We do this in any case in order to update our state (data, orders etc.)
    missing_klines_count = App.database.get_missing_klines_count(symbol)

    #coros = [request_klines(sym, "1m", 5) for sym in symbols]
    tasks = [asyncio.create_task(request_klines(sym, "1m", missing_klines_count+1)) for sym in symbols]

    results = {}
    timeout = 5  # Seconds to wait for the result

    # Process responses in the order of arrival
    for fut in asyncio.as_completed(tasks, timeout=timeout):
        # Get the results
        res = None
        try:
            res = await fut
        except TimeoutError as te:
            log.warning(f"Timeout {timeout} seconds when requesting kline data.")
            return 1
        except Exception as e:
            log.warning(f"Exception when requesting kline data.")
            return 1

        # Add to the database (will overwrite existing klines if any)
        if res and res.keys():
            results.update(res)
            try:
                added_count = App.database.store_klines(res)
            except Exception as e:
                log.error(f"Error storing kline result in the database. Exception: {e}")
                return 1
        else:
            log.error("Received empty or wrong result from klines request.")
            return 1

    return 0

async def request_klines(symbol, freq, limit):
    """
    Request klines data from the service for one symbol. Maximum the specified number of klines will be returned.

    :return: Dict with the symbol as a key and a list of klines as a value. One kline is also a list.
    """
    now_ts = now_timestamp()

    startTime, endTime = get_interval(freq)

    klines = []
    try:
        # INFO:
        # - startTime: include all intervals (ids) with same or greater id: if within interval then excluding this interval; if is equal to open time then include this interval
        # - endTime: include all intervals (ids) with same or smaller id: if equal to left border then return this interval, if within interval then return this interval
        # - It will return also incomplete current interval (in particular, we could collect approximate klines for higher frequencies by requesting incomplete intervals)
        klines = App.client.get_klines(symbol=symbol, interval=freq, limit=limit, endTime=now_ts)
        # Return: list of lists, that is, one kline is a list (not dict) with items ordered: timestamp, open, high, low, close etc.
    except BinanceRequestException as bre:
        # {"code": 1103, "msg": "An unknown parameter was sent"}
        log.error(f"BinanceRequestException while requesting klines: {bre}")
        return {}
    except BinanceAPIException as bae:
        # {"code": 1002, "msg": "Invalid API call"}
        log.error(f"BinanceAPIException while requesting klines: {bae}")
        return {}
    except Exception as e:
        log.error(f"Exception while requesting klines: {e}")
        return {}

    #
    # Post-process
    #

    # Find latest *full* (completed) interval in the result list.
    # The problem is that the result also contains the current (still running) interval which we want to exclude
    klines_full = [kl for kl in klines if kl[0] < startTime]

    last_full_kline = klines_full[-1]
    last_full_kline_ts = last_full_kline[0]

    if last_full_kline_ts != startTime - 60_000:
        log.error(f"UNEXPECTED RESULT: Last full kline timestamp {last_full_kline_ts} is not equal to previous full interval start {startTime - 60_000}. Maybe some results are missing and there are gaps.")

    # Return all received klines with the symbol as a key
    return {symbol: klines_full}

#
# Server and account info
#

async def data_provider_health_check():
    """
    Request information about the data provider server state.
    """
    symbol = App.config["trader"]["symbol"]

    # Get server state (ping) and trade status (e.g., trade can be suspended on some symbol)
    system_status = App.client.get_system_status()
    #{
    #    "status": 0,  # 0: normal，1：system maintenance
    #    "msg": "normal"  # normal or System maintenance.
    #}
    if not system_status or system_status.get("status") != 0:
        App.config["trader"]["state"]["server_status"] = 1
        return 1
    App.config["trader"]["state"]["server_status"] = 0

    # Ping the server

    # Check time synchronization
    #server_time = App.client.get_server_time()
    #time_diff = int(time.time() * 1000) - server_time['serverTime']
    # TODO: Log large time differences (or better trigger time synchronization procedure)

    return 0
