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
                    request_count +
