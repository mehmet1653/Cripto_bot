import os
import time
import threading
import sys
import requests
import ccxt
import pandas as pd
import ta
import numpy as np
from datetime import datetime, timezone
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler
from supabase import create_client, Client
from dotenv import load_dotenv

from strategy import (
    hesapla_gostergeler, sinyal_uret, btc_trend_hesapla,
    RISK_REWARD, KOMISYON_ORANI, MIN_STOP_PCT, MAX_STOP_PCT
)

load_dotenv()
sys.stdout.reconfigure(line_buffering=True)
app = Flask(__name__)

# ==================== AYARLAR ====================
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

exchange = ccxt.gate({
    'apiKey': os.environ["GATE_API_KEY"],
    'secret': os.environ["GATE_SECRET"],
    'enableRateLimit': True,
    'timeout': 15000,
    'options': {'defaultType': 'swap'}
})
exchange.set_sandbox_mode(True)

TAKIP_EDILENLER = [
    'SOL/USDT:USDT', 'AVAX/USDT:USDT', 'XRP/USDT:USDT',
    'DOGE/USDT:USDT', 'SUI/USDT:USDT', 'LINK/USDT:USDT', 'ADA/USDT:USDT'
]

# ==================== RİSK PARAMETRELERİ ====================
ISLEM_BASI_RISK_PCT = 0.01        # Kasanın %1'i (önce %10'du, çok yüksekti)
KALDIRAC = 5
MAKSIMUM_TOPLAM_POZISYON = 3
GUNLUK_MAX_KAYIP_PCT = 0.03       # %3 günlük kayıp → bot durur
COOLDOWN_SURESI_SANIYE = 10 * 60
SEKTOR_LIMITI = 1                 # Aynı sektörden 1 pozisyon (basit: her coin tek)

SEKTOR_MAP = {
    'SOL/USDT:USDT': 'L1', 'AVAX/USDT:USDT': 'L1', 'SUI/USDT:USDT': 'L1',
    'ADA/USDT:USDT': 'L1', 'XRP/USDT:USDT': 'PAYMENT', 'LINK/USDT:USDT': 'ORACLE',
    'DOGE/USDT:USDT': 'MEME'
}

BOT_CALISIYOR_MU = True
GUN_BASI_KASA = None
GUN_BASI_TARIH = None

# ==================== SUPABASE ====================
def hafizayi_yukle():
    varsayilan = {
        "aktif_sistemler": {},
        "analitik": {
            "basarisiz_analizler": [],
            "basarili_islem_sayisi": 0,
            "basarisiz_islem_sayisi": 0,
            "gunluk_net_kar_usd": 0.0,
            "egitim_verileri": []
        },
        "cooldownlar": {}
    }
    try:
        r = supabase.table("bot_hafiza").select("*").eq("id", 1).execute()
        if r.data:
            v = r.data[0]
            return {
                "aktif_sistemler": v.get("aktif_sistemler", {}),
                "analitik": v.get("analitik", varsayilan["analitik"]),
                "cooldownlar": v.get("cooldownlar", {})
            }
    except Exception:
        pass
    try:
        supabase.table("bot_hafiza").upsert({"id": 1, **varsayilan}).execute()
    except Exception:
        pass
    return varsayilan

def hafizayi_kaydet():
    try:
        supabase.table("bot_hafiza").upsert({
            "id": 1,
            "aktif_sistemler": AKTIF_GRID_SISTEMLERI,
            "analitik": ANALITIK_HAFIZA,
            "cooldownlar": COIN_COOLDOWNLAR
        }).execute()
    except Exception:
        pass

kalici = hafizayi_yukle()
AKTIF_GRID_SISTEMLERI = kalici["aktif_sistemler"]
ANALITIK_HAFIZA = kalici["analitik"]
COIN_COOLDOWNLAR = kalici["cooldownlar"]

# ==================== YARDIMCI ====================
def telegram_mesaj_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": mesaj, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception:
        pass

def tum_emirleri_iptal_et(symbol):
    try:
        for e in exchange.fetch_open_orders(symbol):
            try: exchange.cancel_order(e['id'], symbol)
            except Exception: pass
    except Exception:
        pass
    try: exchange.cancel_all_orders(symbol)
    except Exception: pass

def set_leverage_and_margin_safely(symbol, leverage):
    try:
        exchange.set_leverage(leverage, symbol)
        try: exchange.set_margin_mode('isolated', symbol)
        except Exception: pass
        return True
    except Exception:
        return False

def aktif_sektorler():
    return set(SEKTOR_MAP.get(s, 'DIGER') for s in AKTIF_GRID_SISTEMLERI.keys())

def gunluk_kayip_kontrol():
    """Günlük kayıp limiti aşıldı mı?"""
    global GUN_BASI_KASA, GUN_BASI_TARIH
    bugun = datetime.now(timezone.utc).date()
    if GUN_BASI_TARIH != bugun:
        try:
            bal = exchange.fetch_balance()
            GUN_BASI_KASA = float(bal['total'].get('USDT', 0))
            GUN_BASI_TARIH = bugun
        except Exception:
            return False
    if not GUN_BASI_KASA or GUN_BASI_KASA <= 0:
        return False
    try:
        bal = exchange.fetch_balance()
        su_an = float(bal['total'].get('USDT', 0))
        kayip_pct = (GUN_BASI_KASA - su_an) / GUN_BASI_KASA
        return kayip_pct >= GUNLUK_MAX_KAYIP_PCT
    except Exception:
        return False

def btc_trend_al():
    try:
        ohlcv = exchange.fetch_ohlcv('BTC/USDT:USDT', '1h', limit=60)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        return btc_trend_hesapla(df)
    except Exception:
        return None

# ==================== FLASK ====================
@app.route('/')
def home():
    return f"Bot Aktif | Pozisyon: {len(AKTIF_GRID_SISTEMLERI)}"

# ==================== TELEGRAM ====================
async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        balance = exchange.fetch_balance()
        total = float(balance['total'].get('USDT', 0))
        free = float(balance['free'].get('USDT', 0))
        try:
            raw = exchange.fetch_positions()
            poslari = [p for p in raw if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
        except Exception:
            poslari = []
        pnl = sum(float(p.get('unrealizedPnl', 0)) for p in poslari)
        baslangic = total - pnl
        pnl_pct = (pnl / baslangic * 100) if baslangic > 0 else 0
        ikon = "🟢" if pnl >= 0 else "🔴"

        bs = ANALITIK_HAFIZA.get("basarili_islem_sayisi", 0)
        bz = ANALITIK_HAFIZA.get("basarisiz_islem_sayisi", 0)
        tot = bs + bz
        oran = (bs / tot * 100) if tot else 0

        detay = ""
        if poslari:
            detay = "\n📋 *Aktif Pozisyonlar:*\n"
            for p in poslari:
                detay += f"• `{p.get('symbol')}` | {str(p.get('side','')).upper()} | `{float(p.get('unrealizedPnl',0)):+.2f}` USDT\n"
        else:
            detay = "\n📋 *Aktif Pozisyon Yok*\n"

        mesaj = (
            f"🎯 *GÜVENLİ İŞLEM BOTU*\n\n"
            f"💰 Toplam: `{total:.2f}` USDT (Serbest: `{free:.2f}`)\n"
            f"{ikon} PnL: `{pnl:+.2f}` USDT (`%{pnl_pct:+.2f}`)\n"
            f"📌 Pozisyon: `{len(poslari)}/{MAKSIMUM_TOPLAM_POZISYON}`\n"
            f"{detay}\n"
            f"✅ TP: `{bs}` | ❌ Stop: `{bz}`\n"
            f"📈 Başarı: `%{oran:.1f}`\n"
        )
        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def baslat_komutu(update, context):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = True
    await update.message.reply_text("🎯 *Bot Aktif!*", parse_mode='Markdown')

async def durdur_komutu(update, context):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ *Durduruldu.*", parse_mode='Markdown')

async def kapat_komutu(update, context):
    await update.message.reply_text("🔄 *Pozisyonlar kapatılıyor...*", parse_mode='Markdown')
    try:
        for pos in exchange.fetch_positions():
            k = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
            if k > 0:
                sym = pos['symbol']
                yon = str(pos.get('side', '')).upper()
                kapat = 'sell' if yon == 'LONG' else 'buy'
                tum_emirleri_iptal_et(sym)
                exchange.create_order(sym, 'market', kapat, k, None, {'reduce_only': True})
        AKTIF_GRID_SISTEMLERI.clear()
        hafizayi_kaydet()
        await update.message.reply_text("✅ Hepsi kapatıldı.", parse_mode='Markdown')
    except Exception as e:
        AKTIF_GRID_SISTEMLERI.clear()
        hafizayi_kaydet()
        await update.message.reply_text(f"⚠️ Hafıza temizlendi: {e}", parse_mode='Markdown')

async def backtest_komutu(update, context):
    await update.message.reply_text("⏳ Backtest başlatıldı, ~2 dk sürer...")
    from backtest import backtest_coin
    satirlar = ["```", f"{'COIN':<18} {'İŞL':>5} {'WIN%':>6} {'PF':>6} {'EV%':>7} {'DD%':>7}"]
    for c in TAKIP_EDILENLER:
        try:
            r = backtest_coin(c, gun_sayisi=180)
            satirlar.append(f"{c[:16]:<18} {r['islem']:>5} {r['win_rate']:>6} "
                            f"{r['profit_factor']:>6} {r['beklenen_deger']:>7} {r['max_dd']:>7}")
        except Exception as e:
            satirlar.append(f"{c[:16]:<18} HATA: {e}")
    satirlar.append("```")
    await update.message.reply_text("\n".join(satirlar), parse_mode='Markdown')

# ==================== ANA DÖNGÜ ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, ANALITIK_HAFIZA
    print("🎯 [GÜVENLİ MOD] R/R 1:2, Komisyon dahil, BTC filtresi aktif.", flush=True)
    try: exchange.load_markets()
    except Exception: pass

    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5); continue

            # Günlük kayıp limiti
            if gunluk_kayip_kontrol():
                telegram_mesaj_gonder("🛑 *Günlük %3 kayıp limiti aşıldı!* Bot 24 saat durdu.")
                BOT_CALISIYOR_MU = False
                time.sleep(3600); continue

            # Açık pozisyonları çek
            try:
                raw = exchange.fetch_positions()
                aktif_borsa = {p['symbol']: p for p in raw
                               if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0}
            except Exception:
                aktif_borsa = {}

            # Kapananları işle — GERÇEK PnL ile
            for sym in list(AKTIF_GRID_SISTEMLERI.keys()):
                if sym not in aktif_borsa:
                    AKTIF_GRID_SISTEMLERI.pop(sym)
                    basarili = False
                    try:
                        tum_emirleri_iptal_et(sym)
                        # Son kapanan emirleri çek, gerçek PnL
                        closed = exchange.fetch_closed_orders(sym, limit=10)
                        gercek_pnl = 0.0
                        if closed:
                            # En son kapanan iki emir (TP+SL veya tam tersi)
                            son = sorted(closed, key=lambda x: x['timestamp'] or 0)[-2:]
                            for o in son:
                                gercek_pnl += float(o.get('info', {}).get('pnl', 0) or 0)
                        basarili = gercek_pnl > 0
                    except Exception:
                        pass

                    if basarili:
                        ANALITIK_HAFIZA["basarili_islem_sayisi"] = ANALITIK_HAFIZA.get("basarili_islem_sayisi", 0) + 1
                        telegram_mesaj_gonder(f"🎉 *Kâr Alındı* → `{sym}` 🟢")
                    else:
                        ANALITIK_HAFIZA["basarisiz_islem_sayisi"] = ANALITIK_HAFIZA.get("basarisiz_islem_sayisi", 0) + 1
                        # Sadece zarar sonrası cooldown
                        COIN_COOLDOWNLAR[sym] = time.time() + COOLDOWN_SURESI_SANIYE
                        telegram_mesaj_gonder(f"❌ *Stop* → `{sym}` 🔴")

                    hafizayi_kaydet()

            # BTC trendi
            btc_trend = btc_trend_al()

            # Sinyal tara
            su_an = time.time()
            sinyaller = []
            mevcut_sektorler = aktif_sektorler()

            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU: break
                if symbol in aktif_borsa: continue
                if len(aktif_borsa) >= MAKSIMUM_TOPLAM_POZISYON: break
                if su_an < COIN_COOLDOWNLAR.get(symbol, 0): continue
                # Sektör limiti
                if SEKTOR_MAP.get(symbol, 'DIGER') in mevcut_sektorler: continue

                try:
                    o15 = exchange.fetch_ohlcv(symbol, '15m', limit=120)
                    o1h = exchange.fetch_ohlcv(symbol, '1h', limit=60)
                    o4h = exchange.fetch_ohlcv(symbol, '4h', limit=210)
                    df15 = pd.DataFrame(o15, columns=['timestamp','open','high','low','close','volume'])
                    df1h = pd.DataFrame(o1h, columns=['timestamp','open','high','low','close','volume'])
                    df4h = pd.DataFrame(o4h, columns=['timestamp','open','high','low','close','volume'])

                    g = hesapla_gostergeler(df15, df1h, df4h)
                    sig = sinyal_uret(g, btc_trend)
                    if sig:
                        sinyaller.append({"symbol": symbol, **sig})
                except Exception:
                    continue

            # İşlem aç (kasanın %1 riski)
            for s in sinyaller:
                if not BOT_CALISIYOR_MU: break
                if len(aktif_borsa) >= MAKSIMUM_TOPLAM_POZISYON: break
                symbol = s["symbol"]
                yon = s["yon"]

                try:
                    bal = exchange.fetch_balance()
                    kasa = float(bal['total'].get('USDT', 0))
                except Exception:
                    continue
                if kasa <= 0: continue

                if not set_leverage_and_margin_safely(symbol, KALDIRAC):
                    continue

                # Risk = Kasa × %1  →  Bu, stop olduğunda kaybedeceğim miktar
                risk_usdt = kasa * ISLEM_BASI_RISK_PCT
                # Pozisyon büyüklüğü = Risk / Stop%  (kald
