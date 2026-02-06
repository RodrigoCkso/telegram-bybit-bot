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

# --- Carregar .env sempre a partir da pasta do ficheiro ---
try:
    from dotenv import load_dotenv
    env_path = Path(__file__).resolve().parent / ".env"
    load_dotenv(dotenv_path=env_path)
except Exception:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # string

BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")

BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "false").lower() == "true"
BYBIT_BASE_URL = "https://api-testnet.bybit.com" if BYBIT_TESTNET else "https://api.bybit.com"

ADMIN_TELEGRAM_USER_ID = os.getenv("ADMIN_TELEGRAM_USER_ID")
ADMIN_TELEGRAM_USER_ID = int(ADMIN_TELEGRAM_USER_ID) if ADMIN_TELEGRAM_USER_ID else None

last_notification_time = None
telegram_update_offset = None

# Guarda o contexto do último /saldo por utilizador (para deletar depois)
# { user_id: {"cmd_msg_id": int, "menu_msg_id": int, "chat_id": str, "ts": float} }
saldo_context = {}


# -------------------- Helpers --------------------

def require_env():
    missing = []
    for k, v in [
        ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
        ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
        ("BYBIT_API_KEY", BYBIT_API_KEY),
        ("BYBIT_API_SECRET", BYBIT_API_SECRET),
    ]:
        if not v:
            missing.append(k)
    if missing:
        raise RuntimeError(f"Faltam env vars: {', '.join(missing)}")

def is_admin(user_id: int) -> bool:
    return (ADMIN_TELEGRAM_USER_ID is None) or (user_id == ADMIN_TELEGRAM_USER_ID)

def fmt_usd(x) -> str:
    try:
        return f"${float(x):,.2f}"
    except Exception:
        return "$-"


# -------------------- Telegram --------------------

def tg_get(method: str, params: dict | None = None, timeout=20):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    r = requests.get(url, params=params, timeout=timeout)
    try:
        return r.json()
    except Exception:
        return {"ok": False, "description": f"Non-JSON: {r.text[:200]}", "status": r.status_code}

def tg_post(method: str, payload: dict, timeout=20):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    r = requests.post(url, json=payload, timeout=timeout)
    try:
        return r.json()
    except Exception:
        return {"ok": False, "description": f"Non-JSON: {r.text[:200]}", "status": r.status_code}

def telegram_send(text: str, reply_markup: dict | None = None) -> tuple[bool, str, int | None]:
    """
    Retorna: (ok, erro, message_id_do_bot)
    """
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    data = tg_post("sendMessage", payload)
    if not data.get("ok"):
        err = data.get("description", str(data))
        logger.error(f"sendMessage falhou: {data}")
        return False, err, None
    msg_id = (data.get("result") or {}).get("message_id")
    return True, "", msg_id

def telegram_delete_message(chat_id: str, message_id: int) -> bool:
    """
    Deleta mensagem no chat. Para deletar mensagens de outros users no grupo,
    o bot precisa ser admin + permissão "Delete messages".
    """
    data = tg_post("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
    if not data.get("ok"):
        # não spammar logs demais; mas guarda para debug
        logger.warning(f"deleteMessage falhou (chat={chat_id} msg={message_id}): {data}")
        return False
    return True

def telegram_answer_callback(callback_query_id: str, text: str = "", show_alert: bool = False):
    data = tg_post("answerCallbackQuery", {
        "callback_query_id": callback_query_id,
        "text": text,
        "show_alert": show_alert
    })
    if not data.get("ok"):
        logger.error(f"answerCallbackQuery falhou: {data}")

def telegram_delete_webhook_drop_pending():
    data = tg_get("deleteWebhook", {"drop_pending_updates": "true"})
    logger.info(f"deleteWebhook: {data}")

def telegram_get_webhook_info():
    data = tg_get("getWebhookInfo")
    logger.info(f"getWebhookInfo: {data}")

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
    try:
        data = r.json()
    except Exception:
        logger.error(f"getUpdates non-json (status={r.status_code}): {r.text[:300]}")
        return []

    if not data.get("ok"):
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
             {"text": "🟠 Capital em trade (1x)", "callback_data": "cap:trade"}],
        ]
    }


def schedule_cleanup(chat_id: str, msg_ids: list[int], delay_seconds: int = 5):
    """
    Agenda apagar mensagens após X segundos. Tenta apagar todas,
    mesmo se falhar alguma.
    """
    def _do():
        for mid in msg_ids:
            try:
                telegram_delete_message(chat_id, mid)
            except Exception as e:
                logger.warning(f"Erro ao deletar msg {mid}: {e}")

    t = threading.Timer(delay_seconds, _do)
    t.daemon = True
    t.start()


# -------------------- Bybit --------------------

def bybit_sign_get(path: str, query_params: dict, recv_window="5000"):
    timestamp = str(int(time.time() * 1000))
    query_string = urlencode(query_params, doseq=True)
    sign_str = f"{timestamp}{BYBIT_API_KEY}{recv_window}{query_string}"

    signature = hmac.new(
        BYBIT_API_SECRET.encode("utf-8"),
        sign_str.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    headers = {
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": recv_window,
        "X-BAPI-SIGN": signature,
    }

    url = f"{BYBIT_BASE_URL}{path}"
    r = requests.get(url, params=query_params, headers=headers, timeout=20)
    try:
        return r.json()
    except Exception:
        return {"retCode": -1, "retMsg": f"Non-JSON: {r.text[:200]}"}

def get_wallet_balance_any(coins=("USDT", "USDC", "BTC", "ETH")):
    last_err = None
    for acct in ["UNIFIED", "CONTRACT", "SPOT"]:
        data = bybit_sign_get("/v5/account/wallet-balance", {
            "accountType": acct,
            "coin": ",".join(coins)
        })
        if data.get("retCode") == 0:
            lst = (data.get("result") or {}).get("list") or []
            if lst:
                return lst[0], None, acct
            return None, "Resposta vazia", acct
        last_err = f"{data.get('retMsg')} (retCode={data.get('retCode')}, accountType={acct})"
    return None, last_err or "Erro desconhecido", None

def get_open_positions():
    positions = []
    last_err = None

    attempts = [
        ("linear", "USDT"),
        ("linear", "USDC"),
        ("inverse", "BTC"),
        ("inverse", "USDT"),
    ]

    for category, settle in attempts:
        data = bybit_sign_get("/v5/position/list", {"category": category, "settleCoin": settle})
        if data.get("retCode") != 0:
            last_err = f"{data.get('retMsg')} (retCode={data.get('retCode')}, category={category}, settleCoin={settle})"
            continue

        lst = (data.get("result") or {}).get("list") or []
        for p in lst:
            try:
                size = float(p.get("size", 0) or 0)
            except Exception:
                size = 0

            if size > 0:
                positions.append({
                    "symbol": p.get("symbol"),
                    "side": p.get("side"),
                    "size": p.get("size"),
                    "upl": p.get("unrealisedPnl"),
                })

    if not positions and last_err:
        return None, last_err
    return positions, None


# -------------------- Funções --------------------

def fn_capital_livre(user_id: int) -> str:
    if not is_admin(user_id):
        return "⛔️ Sem permissão."
    acc, err, acct = get_wallet_balance_any()
    if err:
        return f"❌ Bybit: {err}"
    avail = acc.get("totalAvailableBalance")
    equity = acc.get("totalEquity")
    return f"🟢 <b>Capital livre</b> <i>{acct}</i>\nDisponível: <b>{fmt_usd(avail)}</b>\nEquity: {fmt_usd(equity)}"

def fn_capital_em_trade_1x(user_id: int) -> str:
    if not is_admin(user_id):
        return "⛔️ Sem permissão."
    acc, err, acct = get_wallet_balance_any()
    if err:
        return f"❌ Bybit: {err}"
    equity = float(acc.get("totalEquity") or 0)
    avail = float(acc.get("totalAvailableBalance") or 0)
    in_trade = max(0.0, equity - avail)
    return f"🟠 <b>Capital em trade (1x)</b> <i>{acct}</i>\nEm trade: <b>{fmt_usd(in_trade)}</b>\nDisponível: {fmt_usd(avail)}"

def fn_posicoes_abertas(user_id: int) -> str:
    if not is_admin(user_id):
        return "⛔️ Sem permissão."
    positions, err = get_open_positions()
    if err:
        return f"❌ Bybit: {err}"
    if not positions:
        return "📌 <b>Posições abertas</b>\n— nenhuma —"
    lines = []
    for p in positions[:12]:
        lines.append(f"• <b>{p['symbol']}</b> {p['side']} | size: {p['size']} | UPL: {p['upl']}")
    extra = f"\n<i>+{len(positions)-12} ocultas</i>" if len(positions) > 12 else ""
    return "📌 <b>Posições abertas</b>\n" + "\n".join(lines) + extra


# -------------------- BTC alerta (opcional) --------------------

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

def send_btc_update():
    global last_notification_time
    now = datetime.now()
    if last_notification_time and (now - last_notification_time).total_seconds() < 2700:
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
    ok, err, _ = telegram_send(f"{emoji} <b>BTC</b> {fmt_usd(price)} ({change:+.2f}%)")
    if ok:
        last_notification_time = now
    else:
        logger.error(f"BTC update falhou: {err}")


# -------------------- Main --------------------

def main():
    require_env()
    logger.info("🤖 Bot local iniciado (VSCode). Testnet=%s", BYBIT_TESTNET)

    telegram_get_webhook_info()
    telegram_delete_webhook_drop_pending()
    telegram_get_webhook_info()

    while True:
        try:
            updates = telegram_get_updates(timeout=30)

            for upd in updates:
                if "message" in upd:
                    msg = upd["message"] or {}
                    chat = msg.get("chat") or {}
                    chat_id = str(chat.get("id", ""))

                    if chat_id != str(TELEGRAM_CHAT_ID):
                        continue

                    text = (msg.get("text") or "").strip().lower()
                    user = msg.get("from") or {}
                    user_id = int(user.get("id", 0))
                    user_msg_id = int(msg.get("message_id", 0))

                    if text.startswith("/saldo"):
                        if not is_admin(user_id):
                            telegram_send("⛔️ Sem permissão.")
                        else:
                            ok, err, menu_msg_id = telegram_send("📍 <b>Saldo</b>\nEscolhe:", reply_markup=kb_saldo_menu())
                            if ok and menu_msg_id:
                                saldo_context[user_id] = {
                                    "cmd_msg_id": user_msg_id,
                                    "menu_msg_id": menu_msg_id,
                                    "chat_id": chat_id,
                                    "ts": time.time(),
                                }

                if "callback_query" in upd:
                    cq = upd["callback_query"] or {}
                    cq_id = cq.get("id")
                    data = cq.get("data") or ""
                    user_id = int((cq.get("from") or {}).get("id", 0))

                    logger.info(f"CALLBACK recebido: data={data} user_id={user_id}")
                    telegram_answer_callback(cq_id)  # ACK

                    msg = cq.get("message") or {}
                    chat_id = str((msg.get("chat") or {}).get("id", ""))

                    if chat_id != str(TELEGRAM_CHAT_ID):
                        telegram_answer_callback(cq_id, "Chat inválido.", show_alert=True)
                        continue

                    if data == "pos:open":
                        out = fn_posicoes_abertas(user_id)
                    elif data == "cap:free":
                        out = fn_capital_livre(user_id)
                    elif data == "cap:trade":
                        out = fn_capital_em_trade_1x(user_id)
                    else:
                        out = "Opção desconhecida."

                    ok, err, reply_msg_id = telegram_send(out)
                    if not ok:
                        telegram_answer_callback(cq_id, f"Erro: {err}", show_alert=True)
                        continue

                    # Agenda apagar: /saldo (user), menu (bot), resposta (bot)
                    ctx = saldo_context.get(user_id)
                    if ctx and reply_msg_id:
                        ids_to_delete = [ctx["menu_msg_id"], reply_msg_id]

                        # tenta apagar o /saldo do user também (só funciona se bot for admin com delete)
                        if ctx.get("cmd_msg_id"):
                            ids_to_delete.insert(0, ctx["cmd_msg_id"])

                        schedule_cleanup(chat_id=ctx["chat_id"], msg_ids=ids_to_delete, delay_seconds=5)

                        # limpa o contexto
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
