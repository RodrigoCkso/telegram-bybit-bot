# bot.py — ENGLISH COMMANDS (attractive) + auto-delete + /pnl shows open assets + live price
# Commands:
#   /wallet   -> menu (Free Balance / In-Trade / Open Positions)
#   /pnl      -> Month-to-date PnL (includes open trades) + shows your open assets + current price
#   /ids      -> shows user_id + chat_id (auto-delete 5s)
#   /commands -> list commands (auto-delete 8s)
#
# Aliases (old PT commands still work):
#   /saldo -> /wallet
#   /mensal -> /pnl
#   /id -> /ids
#   /comandos -> /commands

import os
import time
import hmac
import hashlib
import logging
import json
import threading
from datetime import datetime
from urllib.parse import urlencode
from pathlib import Path

import requests

# --- Load .env ---
try:
    from dotenv import load_dotenv
    env_path = Path(__file__).resolve().parent / ".env"
    load_dotenv(dotenv_path=env_path)
except Exception:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# -------------------- ENV --------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # if set, limit commands to this chat

BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "false").lower() == "true"
BYBIT_BASE_URL = "https://api-testnet.bybit.com" if BYBIT_TESTNET else "https://api.bybit.com"

BTC_ALERT_SECONDS = int(os.getenv("BTC_ALERT_SECONDS", "2700"))  # 45 min
AUTO_DELETE_SECONDS = int(os.getenv("AUTO_DELETE_SECONDS", "5"))

# Auto-delete timers
COMMANDS_DELETE_SECONDS = 8
PNL_DELETE_SECONDS = 7
IDS_DELETE_SECONDS = 5  # ✅ requested

# Monthly snapshot file (MTD baseline)
MONTHLY_FILE = Path(__file__).resolve().parent / "monthly_snapshot.json"

# BTC anti-spam persistence
BTC_LAST_FILE = Path(__file__).resolve().parent / "btc_last_sent.txt"


# -------------------- STATE --------------------
telegram_update_offset = None
btc_last_sent_ts = 0

# For /wallet inline menu cleanup
# { user_id: {"cmd_msg_id": int, "menu_msg_id": int, "chat_id": str} }
wallet_context: dict[int, dict] = {}


# -------------------- HELPERS --------------------
def require_env():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")

def fmt_usd(x) -> str:
    try:
        return f"${float(x):,.2f}"
    except Exception:
        return "$-"

def schedule_cleanup(chat_id: str, msg_ids: list[int], delay_seconds: int):
    def _do():
        for mid in msg_ids:
            telegram_delete_message(chat_id, mid)
    t = threading.Timer(delay_seconds, _do)
    t.daemon = True
    t.start()

def load_btc_last_sent():
    global btc_last_sent_ts
    try:
        if BTC_LAST_FILE.exists():
            btc_last_sent_ts = int(BTC_LAST_FILE.read_text().strip())
    except Exception:
        btc_last_sent_ts = 0

def save_btc_last_sent(ts: int):
    try:
        BTC_LAST_FILE.write_text(str(ts))
    except Exception:
        pass


# -------------------- MONTHLY SNAPSHOT --------------------
def load_monthly_data() -> dict:
    if MONTHLY_FILE.exists():
        try:
            return json.loads(MONTHLY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_monthly_data(data: dict):
    try:
        MONTHLY_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

def current_month_key() -> str:
    return datetime.now().strftime("%Y-%m")

def month_label(month_key: str) -> str:
    try:
        return datetime.strptime(month_key, "%Y-%m").strftime("%B %Y")
    except Exception:
        return month_key


# -------------------- TELEGRAM (RAW API) --------------------
def tg_get(method: str, params: dict | None = None, timeout=20):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    r = requests.get(url, params=params, timeout=timeout)
    return r.json()

def tg_post(method: str, payload: dict, timeout=20):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    r = requests.post(url, json=payload, timeout=timeout)
    return r.json()

def telegram_send(chat_id: str, text: str, reply_markup: dict | None = None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    data = tg_post("sendMessage", payload)
    if not data.get("ok"):
        logger.error(f"sendMessage failed: {data}")
        return False, data.get("description", str(data)), None
    return True, "", (data.get("result") or {}).get("message_id")

def telegram_delete_message(chat_id: str, message_id: int):
    data = tg_post("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
    if not data.get("ok"):
        # deleting can fail if too old / missing admin perms, etc.
        logger.warning(f"deleteMessage failed chat={chat_id} msg={message_id}: {data}")
        return False
    return True

def telegram_answer_callback(callback_query_id: str, text: str = "", show_alert: bool = False):
    tg_post("answerCallbackQuery", {
        "callback_query_id": callback_query_id,
        "text": text,
        "show_alert": show_alert
    })

def telegram_delete_webhook_drop_pending():
    # Avoid "webhook vs polling" conflicts
    logger.info(f"deleteWebhook: {tg_get('deleteWebhook', {'drop_pending_updates': 'true'})}")

def telegram_get_updates(timeout=30):
    global telegram_update_offset
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {
        "timeout": timeout,
        "allowed_updates": json.dumps(["message", "callback_query"]),
    }
    if telegram_update_offset is not None:
        params["offset"] = telegram_update_offset

    r = requests.get(url, params=params, timeout=timeout + 20)
    data = r.json()

    if not data.get("ok"):
        if data.get("error_code") == 409:
            logger.error("409 CONFLICT: another instance is polling getUpdates.")
            time.sleep(5)
            return []
        logger.error(f"getUpdates failed: {data}")
        return []

    updates = data.get("result", [])
    if updates:
        telegram_update_offset = updates[-1]["update_id"] + 1
    return updates


# -------------------- UI (KEYBOARDS) --------------------
def kb_wallet_menu():
    return {
        "inline_keyboard": [
            [{"text": "📌 Open Positions", "callback_data": "pos:open"}],
            [{"text": "🟢 Free Balance", "callback_data": "cap:free"},
             {"text": "🟠 In-Trade (Cost)", "callback_data": "cap:trade"}],
        ]
    }


# -------------------- BYBIT CLIENT --------------------
class BybitClient:
    def __init__(self, api_key: str, api_secret: str, base_url: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url

    def sign_get(self, path: str, query_params: dict, recv_window="5000"):
        ts = str(int(time.time() * 1000))
        qs = urlencode(query_params, doseq=True)
        sign_str = f"{ts}{self.api_key}{recv_window}{qs}"
        sig = hmac.new(self.api_secret.encode(), sign_str.encode(), hashlib.sha256).hexdigest()

        headers = {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": recv_window,
            "X-BAPI-SIGN": sig,
        }
        r = requests.get(f"{self.base_url}{path}", params=query_params, headers=headers, timeout=20)
        return r.json()

    def wallet_best(self):
        # Try common account types
        last = None
        for acct in ["UNIFIED", "CONTRACT", "SPOT"]:
            data = self.sign_get("/v5/account/wallet-balance", {"accountType": acct, "coin": "USDT,USDC,BTC,ETH"})
            if data.get("retCode") == 0:
                lst = (data.get("result") or {}).get("list") or []
                if lst:
                    return lst[0], None, acct
                return None, "Empty response", acct
            last = f"{data.get('retMsg')} (retCode={data.get('retCode')}, acct={acct})"
        return None, last, None

    def open_positions_all(self):
        # Bybit v5 needs settleCoin or symbol; we try common settleCoins
        attempts = [("linear", "USDT"), ("linear", "USDC"), ("inverse", "BTC"), ("inverse", "USDT")]
        positions = []
        last_err = None

        for category, settle in attempts:
            data = self.sign_get("/v5/position/list", {"category": category, "settleCoin": settle})
            if data.get("retCode") != 0:
                last_err = f"{data.get('retMsg')} (retCode={data.get('retCode')}, {category}/{settle})"
                continue

            lst = (data.get("result") or {}).get("list") or []
            for p in lst:
                try:
                    if float(p.get("size") or 0) > 0:
                        positions.append({
                            "symbol": p.get("symbol"),
                            "side": p.get("side"),
                            "size": p.get("size"),
                            "upl": p.get("unrealisedPnl"),
                        })
                except Exception:
                    pass

        if not positions and last_err:
            return None, last_err
        return positions, None


def load_users(max_users=10):
    users = {}
    for i in range(1, max_users + 1):
        uid = os.getenv(f"BYBIT_USER_{i}_ID")
        key = os.getenv(f"BYBIT_USER_{i}_KEY")
        sec = os.getenv(f"BYBIT_USER_{i}_SECRET")
        if uid and key and sec:
            users[int(uid)] = BybitClient(key, sec, BYBIT_BASE_URL)
    return users

USERS = load_users()

def get_client_for_user(user_id: int) -> BybitClient | None:
    return USERS.get(user_id)


# -------------------- MARKET PRICE (FOR /pnl POSITIONS) --------------------
def bybit_get_ticker_price(symbol: str):
    """
    Fetch current price for symbol (prefer markPrice; fallback lastPrice).
    Tries linear first (USDT/USDC perps), then inverse.
    Returns: (price_float, "mark"/"last") or (None, None)
    """
    for category in ["linear", "inverse"]:
        try:
            url = f"{BYBIT_BASE_URL}/v5/market/tickers"
            params = {"category": category, "symbol": symbol}
            r = requests.get(url, params=params, timeout=10)
            data = r.json()
            if data.get("retCode") != 0:
                continue

            lst = (data.get("result") or {}).get("list") or []
            if not lst:
                continue

            t = lst[0]
            mark = t.get("markPrice")
            last = t.get("lastPrice")

            if mark and float(mark) > 0:
                return float(mark), "mark"
            if last and float(last) > 0:
                return float(last), "last"
        except Exception:
            continue

    return None, None


# -------------------- METRICS --------------------
def compute_trade_metrics(client: BybitClient):
    acc, err, acct = client.wallet_best()
    if err:
        return None, err

    def f(key):
        try:
            return float(acc.get(key) or 0)
        except Exception:
            return 0.0

    wallet_balance = f("totalWalletBalance")
    equity = f("totalEquity")
    margin_balance = f("totalMarginBalance")
    available_margin = f("totalAvailableBalance")

    position_im = f("totalPositionIM")
    initial_margin = f("totalInitialMargin")
    order_im = f("totalOrderIM")
    maintenance = f("totalMaintenanceMargin")

    if position_im <= 0:
        position_im = initial_margin

    # Bybit-reported asset figure
    assets_now = margin_balance if margin_balance > 0 else equity

    positions, perr = client.open_positions_all()
    pnl_open = 0.0
    if positions and not perr:
        for p in positions:
            try:
                pnl_open += float(p.get("upl") or 0)
            except Exception:
                pass

    used = max(0.0, assets_now - available_margin)
    capital_cost = assets_now - pnl_open

    capital_free_real = wallet_balance - position_im - order_im - maintenance
    capital_free_real = max(0.0, capital_free_real)

    # Reliable equity mark-to-market that includes open PnL
    equity_mtm = wallet_balance + pnl_open

    return {
        "acct": acct,
        "wallet_balance": wallet_balance,
        "assets_now": assets_now,
        "available_margin": available_margin,
        "used": used,
        "pnl_open": pnl_open,
        "capital_cost": capital_cost,
        "capital_free_real": capital_free_real,
        "equity_mtm": equity_mtm,
        "positions": positions or [],
    }, None


# -------------------- MTD PNL (MONTH-TO-DATE) --------------------
def compute_mtd_pnl_for_user(user_id: int):
    """
    Month-to-date profit% for the requesting user.
    Baseline: start_wallet stored when month starts (first time bot sees new month).
    Current: equity_mtm = wallet_balance + open_pnl (includes open trades).
    """
    client = get_client_for_user(user_id)
    if not client:
        return None, "No API configured for you."

    m, err = compute_trade_metrics(client)
    if err:
        return None, f"Bybit: {err}"

    month = current_month_key()
    key = str(user_id)

    data = load_monthly_data()
    if key not in data:
        data[key] = {}

    # Migrate old key if needed
    if data[key].get("month") == month and "start_wallet" not in data[key] and "start_equity" in data[key]:
        data[key]["start_wallet"] = float(data[key]["start_equity"])
        save_monthly_data(data)

    # New month => snapshot baseline wallet
    if data[key].get("month") != month:
        data[key] = {"month": month, "start_wallet": float(m["wallet_balance"])}
        save_monthly_data(data)

    start_wallet = float(data[key].get("start_wallet", float(m["wallet_balance"])))
    start_equity = start_wallet  # baseline (simple and matches your expectation)
    now_equity = float(m["equity_mtm"])

    pct = ((now_equity - start_equity) / start_equity * 100) if start_equity > 0 else 0.0

    return {
        "month": month,
        "acct": m["acct"],
        "start_equity": start_equity,
        "now_wallet": float(m["wallet_balance"]),
        "now_equity": now_equity,
        "pnl_open": float(m["pnl_open"]),
        "pct": pct,
    }, None


def fn_pnl(user_id: int) -> str:
    r, err = compute_mtd_pnl_for_user(user_id)
    if err:
        return f"❌ {err}"

    # ✅ Add open assets + live price
    pos_lines = []
    client = get_client_for_user(user_id)
    if client:
        m, merr = compute_trade_metrics(client)
        if not merr:
            positions = [p for p in (m["positions"] or []) if float(p.get("size") or 0) > 0]
            for p in positions[:3]:  # keep it clean
                sym = p.get("symbol") or "?"
                price, src = bybit_get_ticker_price(sym)
                if price is None:
                    pos_lines.append(f"• <b>{sym}</b> — price: n/a")
                else:
                    pos_lines.append(f"• <b>{sym}</b> — price: <b>{price:,.4f}</b> <i>({src})</i>")
            if len(positions) > 3:
                pos_lines.append(f"<i>+{len(positions)-3} more…</i>")

    title = month_label(r["month"])
    emoji = "📈" if r["pct"] >= 0 else "📉"

    positions_block = ""
    if pos_lines:
        positions_block = "\n\n<b>Open Assets</b>\n" + "\n".join(pos_lines)

    return (
        f"📊 <b>MTD PnL — {title}</b>\n\n"
        f"👤 <b>{user_id}</b> <i>{r['acct']}</i>\n"
        f"Start (baseline): {fmt_usd(r['start_equity'])}\n"
        f"Open PnL: {fmt_usd(r['pnl_open'])}\n"
        f"Now (equity): <b>{fmt_usd(r['now_equity'])}</b>\n"
        f"Result: {emoji} <b>{r['pct']:+.2f}%</b>"
        f"{positions_block}"
    )


# -------------------- BOT TEXT --------------------
def fn_commands() -> str:
    return (
        "✨ <b>Quick Commands</b>\n\n"
        "• <b>/wallet</b> — balances & positions menu\n"
        "• <b>/pnl</b> — month-to-date PnL (includes open trades)\n"
        "• <b>/ids</b> — show your user_id & chat_id\n"
        "• <b>/commands</b> — show this list"
    )

def fn_ids(message: dict) -> str:
    chat = message.get("chat") or {}
    user = message.get("from") or {}
    username = user.get("username")
    name = " ".join([x for x in [user.get("first_name"), user.get("last_name")] if x]).strip() or "-"
    u = f"@{username}" if username else "-"
    return (
        "🆔 <b>IDs</b>\n"
        f"👤 user_id: <b>{user.get('id')}</b>\n"
        f"👤 name: <b>{name}</b> ({u})\n"
        f"💬 chat_id: <b>{chat.get('id')}</b>\n"
        f"💬 chat_type: <b>{chat.get('type')}</b>"
    )


# -------------------- WALLET MENU FUNCTIONS --------------------
def fn_free_balance(user_id: int) -> str:
    client = get_client_for_user(user_id)
    if not client:
        return "⛔️ No API configured for you."

    m, err = compute_trade_metrics(client)
    if err:
        return f"❌ {err}"

    free_to_use = max(0.0, float(m["available_margin"]))
    buffer_margin = max(0.0, float(m["capital_free_real"]))

    return (
        f"🟢 <b>Free Balance</b> <i>{m['acct']}</i>\n"
        f"Available (to use): <b>{fmt_usd(free_to_use)}</b>\n"
        f"Buffer (extra margin): {fmt_usd(buffer_margin)}\n\n"
        f"ℹ️ Wallet: {fmt_usd(m['wallet_balance'])}\n"
        f"ℹ️ Assets (Bybit): {fmt_usd(m['assets_now'])}"
    )

def fn_in_trade_cost(user_id: int) -> str:
    client = get_client_for_user(user_id)
    if not client:
        return "⛔️ No API configured for you."

    m, err = compute_trade_metrics(client)
    if err:
        return f"❌ {err}"

    return (
        f"🟠 <b>In-Trade (Cost)</b> <i>{m['acct']}</i>\n"
        f"Cost basis: <b>{fmt_usd(m['capital_cost'])}</b>\n"
        f"Now (equity mtm): {fmt_usd(m['equity_mtm'])}\n"
        f"Available margin: {fmt_usd(m['available_margin'])}\n\n"
        f"ℹ️ Used: {fmt_usd(m['used'])} | Open PnL: {fmt_usd(m['pnl_open'])}"
    )

def fn_open_positions(user_id: int) -> str:
    client = get_client_for_user(user_id)
    if not client:
        return "⛔️ No API configured for you."

    m, err = compute_trade_metrics(client)
    if err:
        return f"❌ {err}"

    positions = [p for p in m["positions"] if float(p.get("size") or 0) > 0]
    if not positions:
        return "📌 <b>Open Positions</b>\n— none —"

    lines = []
    for p in positions[:12]:
        lines.append(f"• <b>{p['symbol']}</b> {p['side']} | size: {p['size']} | UPL: {p['upl']}")
    extra = f"\n<i>+{len(positions)-12} hidden</i>" if len(positions) > 12 else ""
    return "📌 <b>Open Positions</b>\n" + "\n".join(lines) + extra


# -------------------- BTC ALERTS --------------------
def get_btc_price():
    url = f"{BYBIT_BASE_URL}/v5/market/tickers"
    params = {"category": "linear"}
    r = requests.get(url, params=params, timeout=10)
    data = r.json()
    if data.get("retCode") != 0:
        return None
    for t in (data.get("result") or {}).get("list") or []:
        if t.get("symbol") == "BTCUSDT":
            return float(t["lastPrice"]), float(t.get("price24hPcnt", 0)) * 100
    return None

def send_btc_update(force: bool = False):
    global btc_last_sent_ts
    if not TELEGRAM_CHAT_ID:
        return

    now_ts = int(time.time())
    if not force:
        if btc_last_sent_ts and (now_ts - btc_last_sent_ts) < BTC_ALERT_SECONDS:
            return

    got = get_btc_price()
    if not got:
        return

    price, change = got
    emoji = "📊"
    if change > 5:
        emoji = "🚀"
    elif change > 1:
        emoji = "📈"
    elif change < -5:
        emoji = "📉"
    elif change < 0:
        emoji = "🔻"

    ok, err, _ = telegram_send(str(TELEGRAM_CHAT_ID), f"{emoji} <b>BTC</b> {fmt_usd(price)} ({change:+.2f}%)")
    if ok:
        btc_last_sent_ts = now_ts
        save_btc_last_sent(now_ts)
    else:
        logger.error(f"BTC update failed: {err}")


# -------------------- MAIN LOOP --------------------
def main():
    require_env()

    global USERS
    USERS = load_users()

    logger.info("🤖 Bot started. Testnet=%s | Users=%s | CHAT_LIMIT=%s | BTC_ALERT_SECONDS=%s",
                BYBIT_TESTNET, list(USERS.keys()), TELEGRAM_CHAT_ID or "(no limit)", BTC_ALERT_SECONDS)

    telegram_delete_webhook_drop_pending()
    load_btc_last_sent()

    # Send 1 BTC alert on startup (if TELEGRAM_CHAT_ID is set)
    try:
        send_btc_update(force=True)
    except Exception as e:
        logger.error(f"BTC startup error: {e}")

    while True:
        try:
            updates = telegram_get_updates(timeout=30)

            for upd in updates:
                # ---------------- MESSAGE ----------------
                if "message" in upd:
                    msg = upd["message"] or {}
                    chat = msg.get("chat") or {}
                    chat_id = str(chat.get("id", ""))

                    text = (msg.get("text") or "").strip()
                    text_l = text.lower()

                    user = msg.get("from") or {}
                    user_id = int(user.get("id", 0))
                    user_msg_id = int(msg.get("message_id", 0))

                    # Chat limit (if set)
                    if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
                        continue

                    # /commands (and /comandos)
                    if text_l.startswith("/commands") or text_l.startswith("/comandos"):
                        ok, _, reply_id = telegram_send(chat_id, fn_commands())
                        if ok and reply_id:
                            schedule_cleanup(chat_id, [user_msg_id, reply_id], delay_seconds=COMMANDS_DELETE_SECONDS)
                        continue

                    # /ids (and /id) ✅ auto-delete 5s
                    if text_l.startswith("/ids") or text_l.startswith("/id"):
                        ok, _, reply_id = telegram_send(chat_id, fn_ids(msg))
                        if ok and reply_id:
                            schedule_cleanup(chat_id, [user_msg_id, reply_id], delay_seconds=IDS_DELETE_SECONDS)
                        continue

                    # /pnl (and /mensal) ✅ auto-delete 7s
                    if text_l.startswith("/pnl") or text_l.startswith("/mensal"):
                        ok, _, reply_id = telegram_send(chat_id, fn_pnl(user_id))
                        if ok and reply_id:
                            schedule_cleanup(chat_id, [user_msg_id, reply_id], delay_seconds=PNL_DELETE_SECONDS)
                        continue

                    # /wallet (and /saldo) => inline menu
                    if text_l.startswith("/wallet") or text_l.startswith("/saldo"):
                        if not get_client_for_user(user_id):
                            telegram_send(chat_id, "⛔️ No API configured for you.")
                            continue

                        ok, _, menu_msg_id = telegram_send(chat_id, "💼 <b>Wallet</b>\nPick an option:", reply_markup=kb_wallet_menu())
                        if ok and menu_msg_id:
                            wallet_context[user_id] = {
                                "cmd_msg_id": user_msg_id,
                                "menu_msg_id": menu_msg_id,
                                "chat_id": chat_id
                            }
                        continue

                # ---------------- CALLBACK ----------------
                if "callback_query" in upd:
                    cq = upd["callback_query"] or {}
                    cq_id = cq.get("id")
                    data = cq.get("data") or ""
                    user_id = int((cq.get("from") or {}).get("id", 0))

                    telegram_answer_callback(cq_id)

                    msg = cq.get("message") or {}
                    chat_id = str((msg.get("chat") or {}).get("id", ""))

                    # Chat limit (if set)
                    if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
                        telegram_answer_callback(cq_id, "Invalid chat.", show_alert=True)
                        continue

                    if not get_client_for_user(user_id):
                        telegram_send(chat_id, "⛔️ No API configured for you.")
                        continue

                    if data == "pos:open":
                        out = fn_open_positions(user_id)
                    elif data == "cap:free":
                        out = fn_free_balance(user_id)
                    elif data == "cap:trade":
                        out = fn_in_trade_cost(user_id)
                    else:
                        out = "Unknown option."

                    ok, _, reply_msg_id = telegram_send(chat_id, out)
                    ctx = wallet_context.get(user_id)
                    if ok and reply_msg_id and ctx:
                        ids_to_delete = [ctx["cmd_msg_id"], ctx["menu_msg_id"], reply_msg_id]
                        schedule_cleanup(ctx["chat_id"], ids_to_delete, delay_seconds=AUTO_DELETE_SECONDS)
                        wallet_context.pop(user_id, None)

        except Exception as e:
            logger.error(f"Loop error: {e}")

        # BTC periodic alert
        try:
            send_btc_update(force=False)
        except Exception as e:
            logger.error(f"BTC error: {e}")

        time.sleep(1)


if __name__ == "__main__":
    main()
