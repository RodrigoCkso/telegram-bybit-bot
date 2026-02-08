# bot.py — Capital livre = capital fora de trade (wallet - margens usadas)
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

# --- Carregar .env ---
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
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # se definido, limita /saldo a este chat

BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "false").lower() == "true"
BYBIT_BASE_URL = "https://api-testnet.bybit.com" if BYBIT_TESTNET else "https://api.bybit.com"

BTC_ALERT_SECONDS = int(os.getenv("BTC_ALERT_SECONDS", "2700"))  # 45 min
AUTO_DELETE_SECONDS = int(os.getenv("AUTO_DELETE_SECONDS", "5"))

# -------------------- Estado --------------------
telegram_update_offset = None

# BTC: anti-spam + persistência
btc_last_sent_ts = 0
BTC_LAST_FILE = Path(__file__).resolve().parent / "btc_last_sent.txt"

# { user_id: {"cmd_msg_id": int, "menu_msg_id": int, "chat_id": str} }
saldo_context: dict[int, dict] = {}


# -------------------- Helpers --------------------
def require_env():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Falta TELEGRAM_BOT_TOKEN")

def fmt_usd(x) -> str:
    try:
        return f"${float(x):,.2f}"
    except Exception:
        return "$-"

def schedule_cleanup(chat_id: str, msg_ids: list[int], delay_seconds: int = AUTO_DELETE_SECONDS):
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


# -------------------- Telegram --------------------
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
        logger.error(f"sendMessage falhou: {data}")
        return False, data.get("description", str(data)), None
    return True, "", (data.get("result") or {}).get("message_id")

def telegram_delete_message(chat_id: str, message_id: int):
    data = tg_post("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
    if not data.get("ok"):
        logger.warning(f"deleteMessage falhou chat={chat_id} msg={message_id}: {data}")
        return False
    return True

def telegram_answer_callback(callback_query_id: str, text: str = "", show_alert: bool = False):
    tg_post("answerCallbackQuery", {
        "callback_query_id": callback_query_id,
        "text": text,
        "show_alert": show_alert
    })

def telegram_delete_webhook_drop_pending():
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
            logger.error("409 CONFLICT: outra instância do bot está a correr (getUpdates).")
            time.sleep(5)
            return []
        logger.error(f"getUpdates falhou: {data}")
        return []

    updates = data.get("result", [])
    if updates:
        telegram_update_offset = updates[-1]["update_id"] + 1
    return updates

def kb_saldo_menu():
    return {
        "inline_keyboard": [
            [{"text": "📌 Posições abertas", "callback_data": "pos:open"}],
            [{"text": "🟢 Capital livre", "callback_data": "cap:free"},
             {"text": "🟠 Capital em trade (custo)", "callback_data": "cap:trade"}],
        ]
    }


# -------------------- Bybit --------------------
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
        last = None
        for acct in ["UNIFIED", "CONTRACT", "SPOT"]:
            data = self.sign_get("/v5/account/wallet-balance", {"accountType": acct, "coin": "USDT,USDC,BTC,ETH"})
            if data.get("retCode") == 0:
                lst = (data.get("result") or {}).get("list") or []
                if lst:
                    return lst[0], None, acct
                return None, "Resposta vazia", acct
            last = f"{data.get('retMsg')} (retCode={data.get('retCode')}, acct={acct})"
        return None, last, None

    def open_positions_all(self):
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


# -------------------- Métricas (robustas) --------------------
def compute_trade_metrics(client: BybitClient):
    """
    Retorna métricas robustas para UTA/cross:
      - wallet_balance: dinheiro base
      - capital_free_real: wallet - (positionIM/initialMargin) - orderIM - maintenanceMargin
      - assets_now: totalMarginBalance (ou totalEquity)
      - available_margin: totalAvailableBalance (margem disponível)
      - used: assets_now - available_margin
      - pnl_open: soma UPL das posições
      - capital_cost: assets_now - pnl_open (valor base antes da PnL)
    """
    acc, err, acct = client.wallet_best()
    if err:
        return None, err

    def f(key):
        try:
            return float(acc.get(key) or 0)
        except Exception:
            return 0.0

    # base
    wallet_balance = f("totalWalletBalance")
    equity = f("totalEquity")
    margin_balance = f("totalMarginBalance")
    available_margin = f("totalAvailableBalance")

    # margens (nem todas as contas devolvem todos os campos)
    position_im = f("totalPositionIM")
    initial_margin = f("totalInitialMargin")
    order_im = f("totalOrderIM")
    maintenance = f("totalMaintenanceMargin")

    # fallback: se totalPositionIM vier 0, usa totalInitialMargin
    if position_im <= 0:
        position_im = initial_margin

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

    # ✅ Capital livre REAL (fora de trade)
    capital_free_real = wallet_balance - position_im - order_im - maintenance
    capital_free_real = max(0.0, capital_free_real)  # nunca negativo

    return {
        "acct": acct,
        "wallet_balance": wallet_balance,
        "assets_now": assets_now,
        "available_margin": available_margin,
        "used": used,
        "pnl_open": pnl_open,
        "capital_cost": capital_cost,
        "position_im": position_im,
        "order_im": order_im,
        "maintenance": maintenance,
        "capital_free_real": capital_free_real,
        "positions": positions or [],
    }, None


# -------------------- Bot funcs --------------------
def fn_id(message: dict) -> str:
    chat = message.get("chat") or {}
    user = message.get("from") or {}
    username = user.get("username")
    name = " ".join([x for x in [user.get("first_name"), user.get("last_name")] if x]).strip() or "-"
    u = f"@{username}" if username else "-"
    return (
        "🆔 <b>IDs</b>\n"
        f"👤 user_id: <b>{user.get('id')}</b>\n"
        f"👤 nome: <b>{name}</b> ({u})\n"
        f"💬 chat_id: <b>{chat.get('id')}</b>\n"
        f"💬 chat_type: <b>{chat.get('type')}</b>"
    )

# ✅ AGORA: capital livre = capital fora de trade
def fn_capital_livre(user_id: int) -> str:
    client = get_client_for_user(user_id)
    if not client:
        return "⛔️ Não tens API configurada neste bot."

    m, err = compute_trade_metrics(client)
    if err:
        return f"❌ Bybit: {err}"

    # ✅ o que tu queres: dinheiro “livre para usar” (margem disponível)
    free_to_use = max(0.0, float(m["available_margin"]))

    # info extra para explicar porque aparece “47”
    buffer_margin = max(0.0, float(m["capital_free_real"]))

    return (
        f"🟢 <b>Capital livre</b> <i>{m['acct']}</i>\n"
        f"Livre (para usar): <b>{fmt_usd(free_to_use)}</b>\n"
        f"Buffer (margem sobrante): {fmt_usd(buffer_margin)}\n\n"
        f"ℹ️ Wallet: {fmt_usd(m['wallet_balance'])}\n"
        f"ℹ️ Assets (agora): {fmt_usd(m['assets_now'])}"
    )

def fn_capital_em_trade_custo(user_id: int) -> str:
    client = get_client_for_user(user_id)
    if not client:
        return "⛔️ Não tens API configurada neste bot."

    m, err = compute_trade_metrics(client)
    if err:
        return f"❌ Bybit: {err}"

    return (
        f"🟠 <b>Capital em trade (custo)</b> <i>{m['acct']}</i>\n"
        f"Capital (custo): <b>{fmt_usd(m['capital_cost'])}</b>\n"
        f"Assets (agora): {fmt_usd(m['assets_now'])}\n"
        f"Margem disponível: {fmt_usd(m['available_margin'])}\n\n"
        f"ℹ️ Em uso: {fmt_usd(m['used'])} | PnL aberta: {fmt_usd(m['pnl_open'])}"
    )

def fn_posicoes_abertas(user_id: int) -> str:
    client = get_client_for_user(user_id)
    if not client:
        return "⛔️ Não tens API configurada neste bot."

    m, err = compute_trade_metrics(client)
    if err:
        return f"❌ Bybit: {err}"

    positions = [p for p in m["positions"] if float(p.get("size") or 0) > 0]
    if not positions:
        return "📌 <b>Posições abertas</b>\n— nenhuma —"

    lines = []
    for p in positions[:12]:
        lines.append(f"• <b>{p['symbol']}</b> {p['side']} | size: {p['size']} | UPL: {p['upl']}")
    extra = f"\n<i>+{len(positions)-12} ocultas</i>" if len(positions) > 12 else ""
    return "📌 <b>Posições abertas</b>\n" + "\n".join(lines) + extra


# -------------------- BTC alert --------------------
def get_btc_price():
    url = f"{BYBIT_BASE_URL}/v5/market/tickers"
    params = {"category": "linear"}
    r = requests.get(url, params=params, timeout=10)
    data = r.json()
    if data.get("retCode") != 0:
        return None
    for ticker in data["result"]["list"]:
        if ticker["symbol"] == "BTCUSDT":
            return float(ticker["lastPrice"]), float(ticker.get("price24hPcnt", 0)) * 100
    return None

def send_btc_update(force: bool = False):
    global btc_last_sent_ts
    if not TELEGRAM_CHAT_ID:
        return

    now_ts = int(time.time())

    # Só respeita o cooldown quando NÃO é force
    if not force:
        if btc_last_sent_ts and (now_ts - btc_last_sent_ts) < BTC_ALERT_SECONDS:
            return

    got = get_btc_price()
    if not got:
        return

    price, change = got
    emoji = "📊"
    if change > 5: emoji = "🚀"
    elif change > 1: emoji = "📈"
    elif change < -5: emoji = "📉"
    elif change < 0: emoji = "🔻"

    ok, err, _ = telegram_send(str(TELEGRAM_CHAT_ID), f"{emoji} <b>BTC</b> {fmt_usd(price)} ({change:+.2f}%)")
    if ok:
        btc_last_sent_ts = now_ts
        save_btc_last_sent(now_ts)
    else:
        logger.error(f"BTC update falhou: {err}")

# -------------------- Main --------------------
def main():
    require_env()

    global USERS
    USERS = load_users()

    logger.info("🤖 Bot iniciado. Testnet=%s | Users=%s | CHAT_LIMIT=%s",
                BYBIT_TESTNET, list(USERS.keys()), TELEGRAM_CHAT_ID or "(sem limite)")

    telegram_delete_webhook_drop_pending()
    load_btc_last_sent()

    # ✅ manda logo 1 ao iniciar (e começa o timer a partir daqui)
    send_btc_update(force=True)

    while True:
        try:
            updates = telegram_get_updates(timeout=30)

            for upd in updates:
                if "message" in upd:
                    msg = upd["message"] or {}
                    chat = msg.get("chat") or {}
                    chat_id = str(chat.get("id", ""))

                    text = (msg.get("text") or "").strip()
                    text_l = text.lower()

                    user = msg.get("from") or {}
                    user_id = int(user.get("id", 0))
                    user_msg_id = int(msg.get("message_id", 0))

                    if text_l.startswith("/id"):
                        telegram_send(chat_id, fn_id(msg))
                        continue

                    if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
                        continue

                    if text_l.startswith("/saldo"):
                        if not get_client_for_user(user_id):
                            telegram_send(chat_id, "⛔️ Não tens API configurada neste bot.")
                            continue

                        ok, _, menu_msg_id = telegram_send(chat_id, "📍 <b>Saldo</b>\nEscolhe:", reply_markup=kb_saldo_menu())
                        if ok and menu_msg_id:
                            saldo_context[user_id] = {"cmd_msg_id": user_msg_id, "menu_msg_id": menu_msg_id, "chat_id": chat_id}

                if "callback_query" in upd:
                    cq = upd["callback_query"] or {}
                    cq_id = cq.get("id")
                    data = cq.get("data") or ""
                    user_id = int((cq.get("from") or {}).get("id", 0))

                    telegram_answer_callback(cq_id)

                    msg = cq.get("message") or {}
                    chat_id = str((msg.get("chat") or {}).get("id", ""))

                    if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
                        telegram_answer_callback(cq_id, "Chat inválido.", show_alert=True)
                        continue

                    if not get_client_for_user(user_id):
                        telegram_send(chat_id, "⛔️ Não tens API configurada neste bot.")
                        continue

                    if data == "pos:open":
                        out = fn_posicoes_abertas(user_id)
                    elif data == "cap:free":
                        out = fn_capital_livre(user_id)
                    elif data == "cap:trade":
                        out = fn_capital_em_trade_custo(user_id)
                    else:
                        out = "Opção desconhecida."

                    ok, _, reply_msg_id = telegram_send(chat_id, out)
                    ctx = saldo_context.get(user_id)
                    if ok and reply_msg_id and ctx:
                        ids_to_delete = [ctx["cmd_msg_id"], ctx["menu_msg_id"], reply_msg_id]
                        schedule_cleanup(ctx["chat_id"], ids_to_delete, delay_seconds=AUTO_DELETE_SECONDS)
                        saldo_context.pop(user_id, None)

        except Exception as e:
            logger.error(f"Erro no loop: {e}")

        try:
            send_btc_update()
        except Exception as e:
            logger.error(f"Erro BTC: {e}")

        time.sleep(1)


if __name__ == "__main__":
    main()
