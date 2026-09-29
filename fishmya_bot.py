#!/usr/bin/env python3
"""
FishMya Game - Adaptive Rate Calibration Bot
Author: GHOST
Version: 21.0 - Re-calibrates periodically
"""

import asyncio
import aiohttp
import json
import time
import sys
import os
from datetime import datetime
from typing import Dict, List, Optional, Any
import logging
import msgpack
import ssl
import websocket
import threading

# ==================== CONFIGURATION ====================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GAME_ACCESS_TOKEN = os.environ.get("GAME_ACCESS_TOKEN", "")
WS_URL = "wss://api-fishmcloud.ugame.vn:2083"

WS_HEADERS = [
    "User-Agent: Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Origin: https://fishmya.ugame.vn",
    "Accept-Language: my-MM,my;q=0.9,en-US;q=0.8,en;q=0.7",
    "X-Requested-With: com.mytel.myid"
]

# ==================== RATE CONTROL ====================
CALIBRATION_REQUESTS = 4000     # စမ်းသပ်မယ့် request
MIN_ACCEPTED = 50               # အနည်းဆုံး လက်ခံရမယ့်အရေအတွက်
INITIAL_RATE = 150              # Default rate
RECALIBRATE_EVERY = 300         # 5 မိနစ်တစ်ခါ re-calibrate
SLEEP_BETWEEN_CYCLES = 0.5      # Cycle ကြားစောင့်
PING_INTERVAL = 5
RECV_TIMEOUT = 0.05
RECV_WINDOW = 0.3

# ==================== TARGET ROUTE ====================
TARGET_ROUTE = {"route": "claimItemOnline", "data": {"package": 5}, "desc": "Pkg 5", "coins": 1500}

# ==================== LOGGING ====================
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# ==================== TELEGRAM API ====================
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
last_update_id = 0
owner_chat_id = None

# ==================== STATE ====================
bot_state = {
    'scanning': False,
    'exploiting': False,
    'is_running': False,
    'found_routes': [],
    'total_claimed': 0,
    'current_balance': 0,
    'start_balance': 0,
    'claims_done': 0,
    'total_claims': 0,
    'errors': 0,
    'last_error': 'None',
    'login_ok': False,
    'connected': False,
    'route_stats': {},
    'last_coin_time': None,
    'auto_restart_count': 0,
    'start_time': None,
    'best_route': None,
    'best_route_coins': 0,
    'coins_per_second': 0,
    'current_requests_per_second': 0,
    'dynamic_rate': INITIAL_RATE,
    'calibration_history': [],
    'last_calibration': {},
}

state_lock = threading.Lock()

# ==================== UTILS ====================
def extract_coins(decoded: Dict) -> int:
    if not decoded:
        return 0
    coin_keys = ['cash', 'coin', 'coins', 'gold', 'reward', 'amount',
                 'changeCash', 'newCash', 'balance', 'bonus', 'gift',
                 'point', 'points', 'money', 'diamond', 'gem', 'totalCash']
    def search(obj, depth=0):
        if depth > 10:
            return 0
        if isinstance(obj, dict):
            for key, value in obj.items():
                key_lower = key.lower()
                if any(k in key_lower for k in coin_keys):
                    if isinstance(value, (int, float)) and value > 0:
                        return int(value)
                    elif isinstance(value, str) and value.isdigit() and int(value) > 0:
                        return int(value)
                result = search(value, depth + 1)
                if result > 0:
                    return result
        elif isinstance(obj, list):
            for item in obj:
                result = search(item, depth + 1)
                if result > 0:
                    return result
        return 0
    return search(decoded)

async def send_telegram(chat_id: str, text: str, keyboard=None):
    url = f"{TELEGRAM_API}/sendMessage"
    payload = {'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'}
    if keyboard:
        payload['reply_markup'] = keyboard
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=15) as response:
                if response.status == 200:
                    return True
                else:
                    logger.error(f"Telegram failed: {response.status}")
                    return False
    except Exception as e:
        logger.error(f"Telegram error: {e}")
        return False

async def get_updates(offset: int = 0) -> List[Dict]:
    url = f"{TELEGRAM_API}/getUpdates"
    params = {'timeout': 30, 'offset': offset}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=35) as response:
                data = await response.json()
                if data.get('ok'):
                    return data.get('result', [])
    except:
        pass
    return []

def get_main_keyboard():
    return json.dumps({
        "inline_keyboard": [
            [{"text": "🛑 Stop", "callback_data": "stop"},
             {"text": "📊 Status", "callback_data": "status"}],
            [{"text": "💰 Balance", "callback_data": "balance"},
             {"text": "📈 Stats", "callback_data": "stats"}]
        ]
    })

# ==================== CONNECT & LOGIN ====================
def connect_and_login():
    try:
        ws = websocket.create_connection(
            WS_URL,
            header=WS_HEADERS,
            sslopt={"cert_reqs": ssl.CERT_NONE},
            timeout=30
        )
        ws.send(msgpack.packb({
            "route": "mytelLogin",
            "data": {"accessToken": GAME_ACCESS_TOKEN, "language": "my"},
            "msgId": 1
        }, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)

        ws.settimeout(15)
        for _ in range(30):
            try:
                m = ws.recv()
                d = msgpack.unpackb(m, raw=False)
                inner = d.get("data", {})
                if not isinstance(inner, dict):
                    inner = {}
                if d.get("msgId") == 1 or d.get("route") == "mytelLogin":
                    if inner.get("ok"):
                        return ws, inner
                    else:
                        logger.error(f"Login rejected: {inner}")
                        try:
                            ws.close()
                        except:
                            pass
                        return None, None
            except websocket.WebSocketTimeoutException:
                continue
            except Exception as e:
                logger.error(f"Login recv error: {e}")
                break
        try:
            ws.close()
        except:
            pass
        return None, None
    except Exception as e:
        logger.error(f"Connection error: {e}")
        return None, None

# ==================== VERIFY PKG 5 ====================
def verify_pkg5():
    global bot_state
    bot_state['scanning'] = True
    bot_state['found_routes'] = []

    ws, login_data = connect_and_login()
    if not ws or not login_data:
        bot_state['scanning'] = False
        return False

    bot_state['login_ok'] = True
    bot_state['current_balance'] = login_data.get("cash", 0)
    bot_state['start_balance'] = login_data.get("cash", 0)

    ws.send(msgpack.packb({
        "route": "play",
        "data": {"roomId": 1},
        "msgId": 2
    }, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
    time.sleep(1)

    logger.info("🔍 Verifying Pkg 5...")
    msg_id = 1000
    coins_found = 0

    ws.send(msgpack.packb({
        "route": TARGET_ROUTE['route'],
        "data": TARGET_ROUTE['data'],
        "msgId": msg_id
    }, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)

    ws.settimeout(1.0)
    try:
        while True:
            m = ws.recv()
            d = msgpack.unpackb(m, raw=False)
            if d.get("route") == "reloadCash":
                change = d.get("data", {}).get("changeCash", 0)
                if change > 0:
                    coins_found = change
                    break
            if d.get("msgId") == msg_id:
                coins_found = extract_coins(d)
                if coins_found > 0:
                    break
    except websocket.WebSocketTimeoutException:
        pass
    except:
        pass
    msg_id += 1

    repeat_coins = 0
    if coins_found > 0:
        ws.send(msgpack.packb({
            "route": TARGET_ROUTE['route'],
            "data": TARGET_ROUTE['data'],
            "msgId": msg_id
        }, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
        ws.settimeout(1.0)
        try:
            while True:
                m = ws.recv()
                d = msgpack.unpackb(m, raw=False)
                if d.get("route") == "reloadCash":
                    change = d.get("data", {}).get("changeCash", 0)
                    if change > 0:
                        repeat_coins = change
                        break
                if d.get("msgId") == msg_id:
                    repeat_coins = extract_coins(d)
                    if repeat_coins > 0:
                        break
        except websocket.WebSocketTimeoutException:
            pass
        except:
            pass

    try:
        ws.close()
    except:
        pass

    bot_state['scanning'] = False

    if coins_found > 0 and repeat_coins > 0:
        bot_state['best_route'] = TARGET_ROUTE
        bot_state['best_route_coins'] = coins_found
        bot_state['found_routes'] = [TARGET_ROUTE]
        logger.info(f"✅ Pkg 5 verified: {coins_found} coins/claim, repeatable")
        return True
    else:
        logger.warning(f"❌ Pkg 5 failed")
        return False

# ==================== CALIBRATION ====================
def calibrate_rate(ws):
    """
    Send 4000 requests, count accepted, return (accepted, new_rate)
    - accepted < 50 → retry with 4000
    - accepted >= 50 → use accepted as new rate
    """
    logger.info(f"🧪 CALIBRATION: Sending {CALIBRATION_REQUESTS} requests...")

    accepted = 0
    rejected = 0
    timeouts = 0
    coins_from_test = 0
    msg_id = 20000

    for i in range(CALIBRATION_REQUESTS):
        try:
            ws.send(msgpack.packb({
                "route": TARGET_ROUTE['route'],
                "data": TARGET_ROUTE['data'],
                "msgId": msg_id
            }, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
            msg_id += 1
        except Exception as e:
            logger.error(f"Send error: {e}")
            break
        time.sleep(0.002)

    logger.info(f"✅ Sent {CALIBRATION_REQUESTS}. Waiting 5s...")

    ws.settimeout(0.2)
    response_end = time.time() + 5
    while time.time() < response_end:
        try:
            m = ws.recv()
            if not m:
                continue
            d = msgpack.unpackb(m, raw=False)
            route = d.get("route", "")
            inner = d.get("data", {})
            if route == "reloadCash":
                change = inner.get("changeCash", 0)
                if change > 0:
                    accepted += 1
                    coins_from_test += change
            elif inner.get("ok") is False:
                rejected += 1
        except websocket.WebSocketTimeoutException:
            timeouts += 1
            continue
        except ssl.SSLError as e:
            logger.error(f"SSL error: {e}")
            break
        except Exception as e:
            logger.error(f"Recv error: {e}")
            break

    logger.info(
        f"📊 CALIBRATION RESULT:\n"
        f"   ✅ Accepted: {accepted}\n"
        f"   ❌ Rejected: {rejected}\n"
        f"   ⏰ Timeouts: {timeouts}\n"
        f"   💰 Coins: {coins_from_test:,}"
    )

    # Save history
    result = {
        'sent': CALIBRATION_REQUESTS,
        'accepted': accepted,
        'rejected': rejected,
        'timeouts': timeouts,
        'coins': coins_from_test,
        'time': datetime.now().strftime('%H:%M:%S'),
    }
    bot_state['last_calibration'] = result
    bot_state['calibration_history'].append(result)
    if len(bot_state['calibration_history']) > 20:
        bot_state['calibration_history'].pop(0)

    # ---- Decide new rate ----
    if accepted < MIN_ACCEPTED:
        # လက်ခံမှု နည်း → 4000 နဲ့ ပြန်စမ်း
        logger.warning(f"⚠️ Accepted {accepted} < {MIN_ACCEPTED} → retry with {CALIBRATION_REQUESTS}")
        return accepted, CALIBRATION_REQUESTS
    else:
        # လက်ခံမှု ကောင်း → accepted ကို rate အဖြစ်သုံး
        logger.info(f"✅ Accepted {accepted} >= {MIN_ACCEPTED} → new rate = {accepted}")
        return accepted, accepted


# ==================== EXPLOIT ====================
def exploit_loop():
    global bot_state
    if not bot_state['found_routes']:
        return

    bot_state['exploiting'] = True
    bot_state['is_running'] = True
    bot_state['total_claimed'] = 0
    bot_state['claims_done'] = 0
    bot_state['errors'] = 0
    bot_state['auto_restart_count'] = 0
    bot_state['start_time'] = datetime.now()
    bot_state['route_stats'] = {'Pkg 5': {'sent': 0, 'received': 0, 'coins': 0}}

    retry_delay = 5
    while bot_state['is_running']:
        ws, login_data = connect_and_login()
        if not ws or not login_data:
            bot_state['errors'] += 1
            bot_state['last_error'] = "Login failed"
            bot_state['connected'] = False
            logger.error(f"Login failed, retry in {retry_delay}s...")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay + 5, 30)
            continue
        else:
            retry_delay = 5

        bot_state['login_ok'] = True
        bot_state['connected'] = True
        bot_state['current_balance'] = login_data.get("cash", bot_state['current_balance'])

        try:
            ws.send(msgpack.packb({
                "route": "play",
                "data": {"roomId": 1},
                "msgId": 2
            }, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
        except:
            pass
        time.sleep(1)

        # ===== INITIAL CALIBRATION LOOP =====
        dynamic_rate = INITIAL_RATE
        calibration_ok = False

        if owner_chat_id:
            asyncio.run(send_telegram(
                owner_chat_id,
                f"🧪 *Calibration Starting*\n\n"
                f"📤 Sending {CALIBRATION_REQUESTS} test requests..."
            ))

        while bot_state['is_running'] and not calibration_ok:
            accepted, dynamic_rate = calibrate_rate(ws)

            if accepted >= MIN_ACCEPTED:
                calibration_ok = True
                bot_state['dynamic_rate'] = dynamic_rate
                logger.info(f"🎯 Using rate: {dynamic_rate} req/cycle")

                if owner_chat_id:
                    asyncio.run(send_telegram(
                        owner_chat_id,
                        f"🎯 *Calibration Complete!*\n\n"
                        f"✅ Accepted: {accepted}/{CALIBRATION_REQUESTS}\n"
                        f"📊 New Rate: *{dynamic_rate} req/cycle*\n"
                        f"💰 Test coins: {bot_state['last_calibration'].get('coins', 0):,}\n\n"
                        f"⚡ Starting exploit..."
                    ))
            else:
                logger.warning(f"❌ Calibration failed ({accepted}/{CALIBRATION_REQUESTS}). Retry...")
                if owner_chat_id:
                    asyncio.run(send_telegram(
                        owner_chat_id,
                        f"⚠️ *Calibration Failed*\n\n"
                        f"Accepted only {accepted}/{CALIBRATION_REQUESTS}\n"
                        f"🔄 Retrying with {CALIBRATION_REQUESTS} again in 5s..."
                    ))
                time.sleep(5)

                try:
                    ws.close()
                except:
                    pass
                ws, login_data = connect_and_login()
                if not ws or not login_data:
                    logger.error("Reconnect failed")
                    break

                try:
                    ws.send(msgpack.packb({
                        "route": "play",
                        "data": {"roomId": 1},
                        "msgId": 2
                    }, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
                except:
                    pass
                time.sleep(1)

        if not bot_state['is_running'] or not calibration_ok:
            try:
                ws.close()
            except:
                pass
            continue

        # ===== EXPLOIT LOOP with dynamic_rate + re-calibration =====
        msg_id = 5000
        last_coin_time = time.time()
        coins_in_interval = 0
        interval_start = time.time()
        request_count = 0
        connection_broken = False
        last_ping_time = time.time()
        last_calibration_time = time.time()

        if owner_chat_id:
            asyncio.run(send_telegram(
                owner_chat_id,
                f"⚡ *Exploit Started!*\n\n"
                f"🎯 Target: Pkg 5 (1500 coins/claim)\n"
                f"📊 Rate: {dynamic_rate} req/cycle\n"
                f"🔄 Re-calibrate every {RECALIBRATE_EVERY}s\n\n"
                f"💡 Use *Status* button."
            ))

        try:
            while bot_state['is_running'] and not connection_broken:
                # ---- Periodic re-calibration ----
                if time.time() - last_calibration_time >= RECALIBRATE_EVERY:
                    logger.info(f"🔄 Re-calibrating (every {RECALIBRATE_EVERY}s)...")

                    # Pause exploit briefly
                    accepted, new_rate = calibrate_rate(ws)

                    if accepted >= MIN_ACCEPTED:
                        dynamic_rate = new_rate
                        bot_state['dynamic_rate'] = new_rate
                        logger.info(f"🎯 New rate: {new_rate}")

                        if owner_chat_id:
                            asyncio.run(send_telegram(
                                owner_chat_id,
                                f"🔄 *Re-Calibrated*\n\n"
                                f"✅ Accepted: {accepted}/{CALIBRATION_REQUESTS}\n"
                                f"📊 New Rate: *{new_rate}*"
                            ))
                    else:
                        logger.warning(f"⚠️ Re-calib failed ({accepted}/{CALIBRATION_REQUESTS}). Keep old rate.")
                        if owner_chat_id:
                            asyncio.run(send_telegram(
                                owner_chat_id,
                                f"⚠️ *Re-Calib Failed*\n\n"
                                f"Accepted only {accepted}/{CALIBRATION_REQUESTS}\n"
                                f"Keeping rate: {dynamic_rate}"
                            ))

                    last_calibration_time = time.time()

                # ---- Ping ----
                if time.time() - last_ping_time >= PING_INTERVAL:
                    try:
                        ws.send(msgpack.packb({
                            "route": "ping",
                            "data": {},
                            "msgId": 0
                        }, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
                        last_ping_time = time.time()
                    except:
                        pass

                # ---- Send dynamic_rate requests ----
                send_ok = True
                for _ in range(dynamic_rate):
                    if not bot_state['is_running']:
                        break
                    try:
                        ws.send(msgpack.packb({
                            "route": TARGET_ROUTE['route'],
                            "data": TARGET_ROUTE['data'],
                            "msgId": msg_id
                        }, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
                    except Exception as e:
                        logger.error(f"Send error: {e}")
                        connection_broken = True
                        send_ok = False
                        break
                    request_count += 1
                    msg_id += 1

                if not send_ok or connection_broken:
                    break

                with state_lock:
                    bot_state['route_stats']['Pkg 5']['sent'] += dynamic_rate
                    bot_state['claims_done'] += dynamic_rate

                # ---- Receive window ----
                ws.settimeout(RECV_TIMEOUT)
                recv_end = time.time() + RECV_WINDOW
                while time.time() < recv_end:
                    try:
                        m = ws.recv()
                        if not m:
                            continue
                        d = msgpack.unpackb(m, raw=False)
                        route = d.get("route", "")
                        inner = d.get("data", {})
                        if route == "reloadCash":
                            change = inner.get("changeCash", 0)
                            if change > 0:
                                with state_lock:
                                    bot_state['total_claimed'] += change
                                    bot_state['current_balance'] = inner.get("newCash", bot_state['current_balance'])
                                    coins_in_interval += change
                                    last_coin_time = time.time()
                                    bot_state['route_stats']['Pkg 5']['received'] += 1
                                    bot_state['route_stats']['Pkg 5']['coins'] += change
                    except websocket.WebSocketTimeoutException:
                        break
                    except ssl.SSLError as e:
                        logger.error(f"SSL error: {e}")
                        connection_broken = True
                        break
                    except Exception as e:
                        logger.error(f"Recv error: {e}")
                        connection_broken = True
                        break

                if connection_broken:
                    break

                # ---- CPS every 1s ----
                if time.time() - interval_start >= 1.0:
                    cps = coins_in_interval / (time.time() - interval_start)
                    with state_lock:
                        bot_state['coins_per_second'] = cps
                        bot_state['current_requests_per_second'] = request_count / (time.time() - interval_start)
                    coins_in_interval = 0
                    interval_start = time.time()
                    request_count = 0

                # ---- Coins stopped 60s ----
                if time.time() - last_coin_time > 60:
                    logger.warning("⚠️ Coins stopped 60s! Reconnecting...")
                    bot_state['auto_restart_count'] += 1
                    connection_broken = True
                    break

                time.sleep(SLEEP_BETWEEN_CYCLES)

        except Exception as e:
            logger.error(f"Exploit error: {e}")
            bot_state['errors'] += 1
            bot_state['last_error'] = str(e)
            bot_state['auto_restart_count'] += 1
        finally:
            try:
                ws.close()
            except Exception:
                pass
            bot_state['connected'] = False

        if bot_state['is_running']:
            logger.info(f"🔄 Auto restart #{bot_state['auto_restart_count']}...")
            time.sleep(8)

    bot_state['exploiting'] = False

# ==================== AUTO MAIN LOOP ====================
def auto_main_loop():
    while True:
        try:
            logger.info("🔄 Verifying Pkg 5...")
            success = verify_pkg5()
            if success:
                logger.info("✅ Pkg 5 confirmed. Cooling down 5s...")
                time.sleep(5)
                logger.info("⚡ Starting exploit with calibration...")
                exploit_loop()
            else:
                logger.warning("❌ Pkg 5 not working, retry in 10s...")
                time.sleep(10)
        except Exception as e:
            logger.error(f"Auto loop error: {e}")
            time.sleep(5)

# ==================== TELEGRAM HANDLERS ====================
async def process_command(chat_id: str, text: str):
    global owner_chat_id
    text = text.strip()
    if text.startswith('/start'):
        if owner_chat_id is None:
            owner_chat_id = chat_id
        lc = bot_state.get('last_calibration', {}) or {}
        status_text = (
            "🤖 *FishMya Pkg 5 Adaptive Bot*\n\n"
            f"🎯 Target: Pkg 5 ({TARGET_ROUTE['coins']:,}/claim)\n"
            f"📊 Dynamic Rate: *{bot_state.get('dynamic_rate', 0)}* req\n"
            f"🧪 Last Calib: {lc.get('accepted', 0)}/{lc.get('sent', 0)}\n"
            f"💰 Balance: {bot_state.get('current_balance', 0):,}\n"
            f"📈 Gained: +{bot_state.get('total_claimed', 0):,}\n"
            f"📊 CPS: {int(bot_state.get('coins_per_second', 0)):,}\n"
            f"🚀 RPS: {int(bot_state.get('current_requests_per_second', 0))}\n\n"
            "Use buttons to control."
        )
        await send_telegram(chat_id, status_text, get_main_keyboard())
    elif text in ['/stop']:
        bot_state['is_running'] = False
        await send_telegram(chat_id, "🛑 *Stopped by user.*")
    elif text in ['/status']:
        status = "🟢 Running" if bot_state['is_running'] else "🔴 Stopped"
        elapsed = (datetime.now() - bot_state['start_time']).seconds if bot_state['start_time'] else 0
        lc = bot_state.get('last_calibration', {}) or {}
        text_msg = (
            f"📊 *Status*\n\n"
            f"State: {status}\n"
            f"🎯 Target: Pkg 5\n"
            f"📊 Rate: *{bot_state.get('dynamic_rate', 0)}* req\n"
            f"🧪 Calib: {lc.get('accepted', 0)}/{lc.get('sent', 0)}\n"
            f"Claims: {bot_state['claims_done']:,}\n"
            f"💰 Balance: {bot_state['current_balance']:,}\n"
            f"📈 Gained: +{bot_state['total_claimed']:,}\n"
            f"📈 CPS: {int(bot_state.get('coins_per_second', 0)):,}\n"
            f"🚀 RPS: {int(bot_state.get('current_requests_per_second', 0))}\n"
            f"🔄 Restarts: {bot_state['auto_restart_count']}\n"
            f"⏱️ Elapsed: {elapsed}s"
        )
        await send_telegram(chat_id, text_msg, get_main_keyboard())
    elif text in ['/balance']:
        await send_telegram(chat_id, f"💰 Balance: {bot_state['current_balance']:,}\n📈 Gained: +{bot_state['total_claimed']:,}")
    elif text in ['/stats']:
        lc = bot_state.get('last_calibration', {}) or {}
        hist = bot_state.get('calibration_history', [])
        stats_text = "📊 *Detailed Stats*\n\n"
        stats_text += f"🎯 Target: Pkg 5 ({TARGET_ROUTE['coins']:,}/claim)\n"
        stats_text += f"📊 Dynamic Rate: *{bot_state.get('dynamic_rate', 0)}* req\n"
        stats_text += f"📈 Current CPS: {int(bot_state.get('coins_per_second', 0)):,}\n"
        stats_text += f"🚀 RPS: {int(bot_state.get('current_requests_per_second', 0))}\n"
        if lc:
            stats_text += "\n🧪 *Last Calibration:*\n"
            stats_text += f"  • Sent: {lc.get('sent', 0)}\n"
            stats_text += f"  • ✅ Accepted: {lc.get('accepted', 0)}\n"
            stats_text += f"  • ❌ Rejected: {lc.get('rejected', 0)}\n"
            stats_text += f"  • ⏰ Timeouts: {lc.get('timeouts', 0)}\n"
            stats_text += f"  • 💰 Coins: {lc.get('coins', 0):,}\n"
        if hist:
            stats_text += "\n📜 *Recent Calibrations:*\n"
            for h in hist[-5:]:
                stats_text += f"  • {h.get('time', '')}: {h.get('accepted', 0)}/{h.get('sent', 0)}\n"
        stats_text += "\n📊 Route Stats:\n"
        for desc, s in (bot_state.get('route_stats', {}) or {}).items():
            stats_text += f"  • {desc}: {s.get('coins', 0):,} coins ({s.get('sent', 0)} sent / {s.get('received', 0)} recv)\n"
        await send_telegram(chat_id, stats_text, get_main_keyboard())

async def handle_callback(chat_id: str, data: str):
    if data == "stop":
        bot_state['is_running'] = False
        await send_telegram(chat_id, "🛑 *Stopped.*")
    elif data == "status":
        await process_command(chat_id, "/status")
    elif data == "balance":
        await process_command(chat_id, "/balance")
    elif data == "stats":
        await process_command(chat_id, "/stats")

# ==================== MAIN ====================
async def main():
    global last_update_id, owner_chat_id
    print("Starting FishMya Pkg 5 Adaptive Bot...")
    threading.Thread(target=auto_main_loop, daemon=True).start()
    while True:
        try:
            updates = await get_updates(last_update_id + 1)
            for update in updates:
                if not isinstance(update, dict):
                    continue
                update_id = update.get('update_id', 0)
                if update_id > last_update_id:
                    last_update_id = update_id

                cb = update.get('callback_query')
                if isinstance(cb, dict):
                    msg = cb.get('message') or {}
                    chat = msg.get('chat') or {}
                    chat_id = str(chat.get('id', ''))
                    data = cb.get('data', '')
                    if chat_id and data:
                        await handle_callback(chat_id, data)
                    continue

                msg = update.get('message')
                if isinstance(msg, dict):
                    chat = msg.get('chat') or {}
                    chat_id = str(chat.get('id', ''))
                    text = msg.get('text', '')
                    if chat_id and text:
                        if owner_chat_id is None:
                            owner_chat_id = chat_id
                        await process_command(chat_id, text)
            await asyncio.sleep(2)
        except KeyboardInterrupt:
            bot_state['is_running'] = False
            break
        except Exception as e:
            logger.error(f"Main loop error: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
