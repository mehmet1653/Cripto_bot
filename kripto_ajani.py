import time
import threading
import sys
import requests
import ccxt
import pandas as pd
import ta
import os
import numpy as np
from datetime import datetime
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler
from sklearn.ensemble import RandomForestClassifier
from supabase import create_client, Client

sys.stdout.reconfigure(line_buffering=True)

app = Flask(__name__)

# ==================== AYARLAR VE ANAHTARLAR ====================
TELEGRAM_TOKEN = "8870934003:AAGIpiwdgpnQVW7nbJIRcR0dOLOzj-MOZsA"
CHAT_ID = "6929517567"

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://rllpcylzhptqwzmzehnv.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "Sb_secret_ln9y67Ep_zCtOQ9Q2NE8KQ_nf0gKkmO")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

exchange = ccxt.gate({
    'apiKey': '82cca880898a88d1a31e86d8eb474c57',
    'secret': '1ac479b9df5e6f2e89560b0d238a250694719b6fcae20da00ebc54ad6aeb8898',
    'enableRateLimit': True,
    'timeout': 15000,
    'options': {
        'defaultType': 'swap'
    }
})

exchange.set_sandbox_mode(True)

TAKIP_EDILENLER = [
    'SOL/USDT:USDT', 'AVAX/USDT:USDT', 'XRP/USDT:USDT', 
    'DOGE/USDT:USDT', 'SUI/USDT:USDT', 'LINK/USDT:USDT', 'ADA/USDT:USDT'
]

COIN_ID_MAP = {
    'SOL/USDT:USDT': 1,
    'AVAX/USDT:USDT': 2,
    'XRP/USDT:USDT': 3,
    'DOGE/USDT:USDT': 4,
    'SUI/USDT:USDT': 5,
    'LINK/USDT:USDT': 6,
    'ADA/USDT:USDT': 7
}

BOT_CALISIYOR_MU = True

# ==================== SUPABASE HAFIZA FONKSİYONLARI ====================
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
        response = supabase.table("bot_hafiza").select("*").eq("id", 1).execute()
        if response.data and len(response.data) > 0:
            veri = response.data[0]
            return {
                "aktif_sistemler": veri.get("aktif_sistemler", {}),
                "analitik": veri.get("analitik", varsayilan["analitik"]),
                "cooldownlar": veri.get("cooldownlar", {})
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
            "analitik": ANALitik_HAFIZA,
            "cooldownlar": COIN_COOLDOWNLAR
        }).execute()
    except Exception:
        pass

kalici_veri = hafizayi_yukle()
AKTIF_GRID_SISTEMLERI = kalici_veri.get("aktif_sistemler", {})
ANALitik_HAFIZA = kalici_veri.get("analitik", {
    "basarisiz_analizler": [],
    "basarili_islem_sayisi": 0,
    "basarisiz_islem_sayisi": 0,
    "gunluk_net_kar_usd": 0.0,
    "egitim_verileri": []
})
COIN_COOLDOWNLAR = kalici_veri.get("cooldownlar", {})

MAKSIMUM_TOPLAM_POZISYON = 3
COOLDOWN_SURESI_SANIYE = 10 * 60

# ==================== YAPAY ZEKA MODELİ ====================
ai_model = RandomForestClassifier(n_estimators=100, max_depth=6, random_state=42)
ai_model_egitildi = False

def yapay_zekayi_egit_ve_guncelle():
    global ai_model, ai_model_egitildi
    veriler = ANALitik_HAFIZA.get("egitim_verileri", [])
    if len(veriler) < 20:
        ai_model_egitildi = False
        return
    try:
        X = [item[:9] for item in veriler]
        y = [item[9] for item in veriler]
        if len(set(y)) < 2:
            ai_model_egitildi = False
            return
        ai_model.fit(np.array(X), np.array(y))
        ai_model_egitildi = True
    except Exception:
        ai_model_egitildi = False

def atr_ve_volatilite_hesapla(df, period=14):
    try:
        atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=period).average_true_range().iloc[-1]
        fiyat = df['close'].iloc[-1]
        return float((atr / fiyat) * 100)
    except Exception:
        return 1.5

def emir_defteri_derinlik_analizi(symbol):
    try:
        order_book = exchange.fetch_order_book(symbol, limit=20)
        bids = order_book.get('bids', [])
        asks = order_book.get('asks', [])
        toplam_bid_hacim = sum(bid[1] for bid in bids)
        toplam_ask_hacim = sum(ask[1] for ask in asks)
        if toplam_bid_hacim + toplam_ask_hacim == 0:
            return "DENGELI"
        bid_orani = toplam_bid_hacim / (toplam_bid_hacim + toplam_ask_hacim)
        if bid_orani > 0.55:
            return "ALICI_BASKIN"
        elif bid_orani < 0.45:
            return "SATICI_BASKIN"
        return "DENGELI"
    except Exception:
        return "DENGELI"

def tum_emirleri_iptal_et(symbol):
    try:
        acik_emirler = exchange.fetch_open_orders(symbol)
        for emir in acik_emirler:
            try:
                exchange.cancel_order(emir['id'], symbol)
            except Exception:
                pass
    except Exception:
        pass
    try:
        exchange.cancel_all_orders(symbol)
    except Exception:
        pass

def telegram_mesaj_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": mesaj, "parse_mode": "Markdown"}, timeout=10)
    except Exception:
        pass

@app.route('/')
def home():
    return f"15x Puanlı Bot Aktif | Aktif Pozisyon: {len(AKTIF_GRID_SISTEMLERI)}"

def set_leverage_and_margin_safely(symbol, leverage):
    try:
        exchange.set_leverage(leverage, symbol)
        try:
            exchange.set_margin_mode('isolated', symbol)
        except Exception:
            pass
        return True
    except Exception:
        return False

# ==================== TELEGRAM KOMUTLARI ====================
async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        balance = exchange.fetch_balance()
        total = float(balance['total'].get('USDT', 0))
        free = float(balance['free'].get('USDT', 0))
        try:
            raw_positions = exchange.fetch_positions()
            borsa_poslari = [p for p in raw_positions if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
        except Exception:
            borsa_poslari = []

        toplam_pnl = sum(float(p.get('unrealizedPnl', 0)) for p in borsa_poslari)
        pnl_ikon = "🟢" if toplam_pnl >= 0 else "🔴"
        
        basarili_sayisi = ANALitik_HAFIZA.get("basarili_islem_sayisi", 0)
        basarisiz_sayisi = ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0)
        toplam_islem = basarili_sayisi + basarisiz_sayisi
        basari_orani = (basarili_sayisi / toplam_islem * 100) if toplam_islem > 0 else 0.0

        mesaj = (
            f"🎯 *15X PUANLI İŞLEM BOTU*\n\n"
            f"💰 Toplam Kasa: `{total:.2f} USDT` (Serbest: `{free:.2f} USDT`)\n"
            f"{pnl_ikon} Anlık Kâr/Zarar: `{toplam_pnl:+.2f} USDT`\n"
            f"📌 Açık Pozisyon Sayısı: `{len(borsa_poslari)} / {MAKSIMUM_TOPLAM_POZISYON}`\n"
            f"✅ Kâr (`TP`): `{basarili_sayisi}` | ❌ Zarar (`Stop`): `{basarisiz_sayisi}`\n"
            f"📈 Başarı Oranı: `%{basari_orani:.1f}`\n"
        )
        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = True
    await update.message.reply_text("🎯 *15x Puanlı Bot Aktif!*", parse_mode='Markdown')

async def durdur_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ *Bot durduruldu.*", parse_mode='Markdown')

async def kapat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 *Tüm pozisyonlar kapatılıyor...*", parse_mode='Markdown')
    try:
        for pos in exchange.fetch_positions():
            kontrat = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
            if kontrat > 0:
                symbol = pos['symbol']
                yon = str(pos.get('side', '')).upper()
                kapatma_yonu = 'sell' if yon == 'LONG' else 'buy'
                tum_emirleri_iptal_et(symbol)
                exchange.create_order(symbol, 'market', kapatma_yonu, kontrat, None, {'reduce_only': True})
        AKTIF_GRID_SISTEMLERI.clear()
        hafizayi_kaydet()
        await update.message.reply_text("✅ Tüm pozisyonlar kapatıldı.", parse_mode='Markdown')
    except Exception:
        AKTIF_GRID_SISTEMLERI.clear()
        hafizayi_kaydet()
        await update.message.reply_text("✅ Hafıza temizlendi.", parse_mode='Markdown')

# ==================== ARKA PLAN TARAYICI ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, ANALitik_HAFIZA
    print("🎯 [PUANLI MOD] 15x ve %20 Marjin Stratejisi Devrede.", flush=True)
    try:
        exchange.load_markets()
    except Exception:
        pass
    
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5)
                continue

            try:
                raw_positions = exchange.fetch_positions()
                aktif_borsa_map = {p['symbol']: p for p in raw_positions if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0}
            except Exception:
                aktif_borsa_map = {}

            # --- LİMİT KONTROLÜ ---
            if len(aktif_borsa_map) > MAKSIMUM_TOPLAM_POZISYON:
                for sym, pos in list(aktif_borsa_map.items()):
                    if sym not in AKTIF_GRID_SISTEMLERI:
                        try:
                            kontrat = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
                            yon = str(pos.get('side', '')).upper()
                            kapatma_yonu = 'sell' if yon == 'LONG' else 'buy'
                            tum_emirleri_iptal_et(sym)
                            exchange.create_order(sym, 'market', kapatma_yonu, kontrat, None, {'reduce_only': True})
                        except Exception:
                            pass

            # --- KAPATILANLARI İŞLE ---
            for sym in list(AKTIF_GRID_SISTEMLERI.keys()):
                if sym not in aktif_borsa_map:
                    AKTIF_GRID_SISTEMLERI.pop(sym)
                    basarili_islem = False
                    try:
                        tum_emirleri_iptal_et(sym)
                        my_trades = exchange.fetch_my_trades(sym, limit=3)
                        if my_trades:
                            son_trade = my_trades[-1]
                            realized_pnl = float(son_trade.get('info', {}).get('pnl', 0) or 0)
                            basarili_islem = realized_pnl > 0
                    except Exception:
                        pass

                    if basarili_islem:
                        ANALitik_HAFIZA["basarili_islem_sayisi"] = ANALitik_HAFIZA.get("basarili_islem_sayisi", 0) + 1
                        telegram_mesaj_gonder(f"🎉 *Kâr Alındı (15x)* -> `{sym}` 🟢")
                    else:
                        ANALitik_HAFIZA["basarisiz_islem_sayisi"] = ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0) + 1
                        telegram_mesaj_gonder(f"❌ *Stop Oldu (15x)* -> `{sym}` 🔴")

                    COIN_COOLDOWNLAR[sym] = time.time() + COOLDOWN_SURESI_SANIYE
                    hafizayi_kaydet()

            # --- AÇIK POZİSYONLAR İÇİN ANLIK TRAILING / OTOMATİK KÂR AL (KİLİTLEME) ---
            for sym, pos in aktif_borsa_map.items():
                try:
                    unrealized_pnl = float(pos.get('unrealizedPnl', 0) or 0)
                    initial_margin = float(pos.get('initialMargin', 0) or pos.get('margin', 0) or 1.0)
                    
                    if unrealized_pnl >= 1.2:
                        kontrat = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
                        yon = str(pos.get('side', '')).upper()
                        kapatma_yonu = 'sell' if yon == 'LONG' else 'buy'
                        
                        tum_emirleri_iptal_et(sym)
                        exchange.create_order(sym, 'market', kapatma_yonu, kontrat, None, {'reduce_only': True})
                        
                        if sym in AKTIF_GRID_SISTEMLERI:
                            AKTIF_GRID_SISTEMLERI.pop(sym)
                        
                        ANALitik_HAFIZA["basarili_islem_sayisi"] = ANALitik_HAFIZA.get("basarili_islem_sayisi", 0) + 1
                        hafizayi_kaydet()
                        
                        print(f"💰 KÂR KORUMA / ERKEN TP: {sym} | Gerçekleşen Kâr: {unrealized_pnl:.2f} USDT", flush=True)
                        telegram_mesaj_gonder(f"💰 *Kâr Koruma Devrede (Kapatıldı)*\n📌 Coin: `{sym}` | Kâr: `+{unrealized_pnl:.2f} USDT` 🟢")
                except Exception as e:
                    pass

            # --- PUANLI SİNYAL ANALİZİ ---
            taranan_sinyaller = []
            su_anki_zaman = time.time()

            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU or symbol in aktif_borsa_map or len(aktif_borsa_map) >= MAKSIMUM_TOPLAM_POZISYON or su_anki_zaman < COIN_COOLDOWNLAR.get(symbol, 0):
                    continue

                try:
                    guncel_fiyat = exchange.fetch_ticker(symbol)['last']
                    df_15m = pd.DataFrame(exchange.fetch_ohlcv(symbol, timeframe='15m', limit=40), columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    df_1h = pd.DataFrame(exchange.fetch_ohlcv(symbol, timeframe='1h', limit=30), columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

                    ema7_1h = ta.trend.ema_indicator(df_1h['close'], window=7).iloc[-1]
                    ema21_1h = ta.trend.ema_indicator(df_1h['close'], window=21).iloc[-1]
                    trend_1h_boga = ema7_1h > ema21_1h

                    rsi = ta.momentum.rsi(df_15m['close'], window=14).iloc[-1]
                    adx_val = ta.trend.ADXIndicator(df_15m['high'], df_15m['low'], df_15m['close'], window=14).adx().iloc[-1]
                    atr_yuzdesi = atr_ve_volatilite_hesapla(df_15m)
                    derinlik = emir_defteri_derinlik_analizi(symbol)

                    puan = 50
                    grid_yonu = "LONG"

                    if trend_1h_boga:
                        puan += 20
                        grid_yonu = "LONG"
                    else:
                        puan += 20
                        grid_yonu = "SHORT"

                    if grid_yonu == "LONG" and 40 <= rsi <= 60:
                        puan += 15
                    elif grid_yonu == "SHORT" and 40 <= rsi <= 60:
                        puan += 15

                    if adx_val > 25:
                        puan += 15

                    if derinlik == "ALICI_BASKIN" and grid_yonu == "LONG":
                        puan += 10
                    elif derinlik == "SATICI_BASKIN" and grid_yonu == "SHORT":
                        puan += 10

                    if puan >= 75:
                        taranan_sinyaller.append({
                            "symbol": symbol, "puan": puan, "yon": grid_yonu, "fiyat": guncel_fiyat, "atr": atr_yuzdesi
                        })

                except Exception:
                    continue

            taranan_sinyaller.sort(key=lambda x: x["puan"], reverse=True)

            # --- 15X VE %20 KASA MARJİNİ İLE İŞLEM AÇMA ---
            for sinyal in taranan_sinyaller:
                if not BOT_CALISIYOR_MU or len(aktif_borsa_map) >= MAKSIMUM_TOPLAM_POZISYON:
                    break

                symbol = sinyal["symbol"]
                grid_yonu = sinyal["yon"]
                guncel_fiyat = sinyal["fiyat"]
                atr_yuzdesi = sinyal["atr"]

                dinamik_kaldirac = 15
                try:
                    balance = exchange.fetch_balance()
                    toplam_bakiye = float(balance['total'].get('USDT', 0))
                except Exception:
                    continue

                if not set_leverage_and_margin_safely(symbol, dinamik_kaldirac):
                    continue

                hedef_marjin = toplam_bakiye * 0.20
                ham_mask = (hedef_marjin * dinamik_kaldirac) / guncel_fiyat
                
                try:
                    market_info = exchange.market(symbol)
                    miktar = float(exchange.amount_to_precision(symbol, max(round(ham_mask / float(market_info.get('contractSize', 1.0))), 1)))
                    
                    tum_emirleri_iptal_et(symbol)
                    exchange.create_order(symbol, 'market', 'buy' if grid_yonu == 'LONG' else 'sell', miktar)

                    giris_fiyati = guncel_fiyat
                    time.sleep(0.3)

                    hedef_oran_fiyat = 0.018 
                    stop_oran_fiyat = (atr_yuzdesi * 1.2) / 100.0

                    if grid_yonu == 'LONG':
                        stop_fiyat = giris_fiyati * (1.0 - stop_oran_fiyat)
                        hedef_fiyat = giris_fiyati * (1.0 + hedef_oran_fiyat)
                        kapatma_yonu = 'sell'
                    else:
                        stop_fiyat = giris_fiyati * (1.0 + stop_oran_fiyat)
                        hedef_fiyat = giris_fiyati * (1.0 - hedef_oran_fiyat)
                        kapatma_yonu = 'buy'

                    stop_fiyat = float(exchange.price_to_precision(symbol, stop_fiyat))
                    hedef_fiyat = float(exchange.price_to_precision(symbol, hedef_fiyat))

                    try:
                        exchange.create_order(symbol, 'stop', kapatma_yonu, miktar, stop_fiyat, {'stopPrice': stop_fiyat, 'triggerPrice': stop_fiyat, 'reduceOnly': True})
                    except Exception:
                        exchange.create_order(symbol, 'stop_market', kapatma_yonu, miktar, stop_fiyat, {'stopPrice': stop_fiyat, 'triggerPrice': stop_fiyat, 'reduceOnly': True})

                    exchange.create_order(symbol, 'limit', kapatma_yonu, miktar, hedef_fiyat, {'reduceOnly': True})

                    AKTIF_GRID_SISTEMLERI[symbol] = {
                        "yon": grid_yonu, "giris_fiyati": giris_fiyati, "hedef_fiyat": hedef_fiyat, "trailing_aktif": False
                    }
                    hafizayi_kaydet()

                    print(f"🎯 15X PUANLI İŞLEM: {symbol} | Yön: {grid_yonu} | Skor: {sinyal['puan']} | TP: {hedef_fiyat}", flush=True)
                    telegram_mesaj_gonder(f"🎯 *15X PUANLI İŞLEM AÇILDI*\n📌 Coin: `{symbol}` | Yön: `{grid_yonu}` | Skor: `{sinyal['puan']}` | Hedef: `{hedef_fiyat}`")
                    break
                except Exception as e:
                    print(f"❌ İşlem açma hatası: {e}", flush=True)

        except Exception as e:
            print(f"⚠️ Döngü hatası: {e}", flush=True)
        time.sleep(10)

def flask_web_server():
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))

if __name__ == '__main__':
    threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True).start()
    threading.Thread(target=flask_web_server, daemon=True).start()
    
    app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app_tg.add_handler(CommandHandler("durum", durum_komutu))
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))
    
    app_tg.run_polling()
