import time
import threading
import sys
import requests
import ccxt
import pandas as pd
import ta
import os
import numpy as np
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

# BTC ve ETH hariç tutulmuş optimize altcoin sepeti
TAKIP_EDILENLER = [
    'SOL/USDT:USDT', 'AVAX/USDT:USDT', 'XRP/USDT:USDT', 
    'DOGE/USDT:USDT', 'SUI/USDT:USDT', 'LINK/USDT:USDT', 'ADA/USDT:USDT'
]

COIN_ID_MAP = {symbol: idx + 1 for idx, symbol in enumerate(TAKIP_EDILENLER)}

BOT_CALISIYOR_MU = True
KALDIRAC = 10
MARJIN_ORANI = 0.20  # Kasanın %20'si
MAKSIMUM_TOPLAM_POZISYON = 3
COOLDOWN_SURESI_SANIYE = 10 * 60

# ==================== SUPABASE HAFIZA FONKSİYONLARI ====================
def hafizayi_yukle():
    varsayilan = {
        "aktif_sistemler": {},
        "analitik": {
            "basarili_islem_sayisi": 0,
            "basarisiz_islem_sayisi": 0,
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
            "aktif_sistemler": AKTIF_SISTEMLER,
            "analitik": ANALITIK_HAFIZA,
            "cooldownlar": COIN_COOLDOWNLAR
        }).execute()
    except Exception:
        pass

kalici_veri = hafizayi_yukle()
AKTIF_SISTEMLER = kalici_veri.get("aktif_sistemler", {})
ANALITIK_HAFIZA = kalici_veri.get("analitik", {
    "basarili_islem_sayisi": 0,
    "basarisiz_islem_sayisi": 0,
    "egitim_verileri": []
})
COIN_COOLDOWNLAR = kalici_veri.get("cooldownlar", {})

# ==================== YAPAY ZEKA MODELİ ====================
ai_model = RandomForestClassifier(n_estimators=100, max_depth=6, random_state=42)
ai_model_egitildi = False

def yapay_zekayi_egit_ve_guncelle():
    global ai_model, ai_model_egitildi
    veriler = ANALITIK_HAFIZA.get("egitim_verileri", [])
    if len(veriler) < 15:
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

def yapay_zeka_islem_onayi(features):
    if not ai_model_egitildi:
        return True
    try:
        tahmin = ai_model.predict(np.array([features]))[0]
        return int(tahmin) == 1
    except Exception:
        return True

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
        if bid_orani > 0.58:
            return "ALICI_BASKIN"
        elif bid_orani < 0.42:
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
    return f"10x Trend & AI Bot Aktif | Aktif Pozisyon: {len(AKTIF_SISTEMLER)}"

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
        try:
            raw_positions = exchange.fetch_positions()
            borsa_poslari = [p for p in raw_positions if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
        except Exception:
            borsa_poslari = []

        toplam_pnl = sum(float(p.get('unrealizedPnl', 0)) for p in borsa_poslari)
        pnl_ikon = "🟢" if toplam_pnl >= 0 else "🔴"
        
        basarili_sayisi = ANALITIK_HAFIZA.get("basarili_islem_sayisi", 0)
        basarisiz_sayisi = ANALITIK_HAFIZA.get("basarisiz_islem_sayisi", 0)
        toplam_islem = basarili_sayisi + basarisiz_sayisi
        basari_orani = (basarili_sayisi / toplam_islem * 100) if toplam_islem > 0 else 0.0

        mesaj = (
            f"🚀 *10X TREND & AI BOT DURUMU*\n\n"
            f"💰 Toplam Kasa: `{total:.2f} USDT`\n"
            f"{pnl_ikon} Anlık Kâr/Zarar: `{toplam_pnl:+.2f} USDT`\n"
            f"📌 Açık Pozisyon: `{len(borsa_poslari)} / {MAKSIMUM_TOPLAM_POZISYON}`\n"
            f"✅ Başarılı: `{basarili_sayisi}` | ❌ Başarısız: `{basarisiz_sayisi}`\n"
            f"📈 Başarı Oranı: `%{basari_orani:.1f}`\n"
        )
        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = True
    await update.message.reply_text("🚀 *10x Bot Aktif Edildi!*", parse_mode='Markdown')

async def durdur_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ *Bot Durduruldu.*", parse_mode='Markdown')

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
        AKTIF_SISTEMLER.clear()
        hafizayi_kaydet()
        await update.message.reply_text("✅ Tüm pozisyonlar kapatıldı ve hafıza sıfırlandı.", parse_mode='Markdown')
    except Exception:
        AKTIF_SISTEMLER.clear()
        hafizayi_kaydet()
        await update.message.reply_text("✅ Hafıza temizlendi.", parse_mode='Markdown')

# ==================== ARKA PLAN TARAYICI ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, ANALITIK_HAFIZA
    print("🚀 [10X TREND MİMARİSİ] Başlatıldı. BTC/ETH Hariç Sepet Taranıyor.", flush=True)
    try:
        exchange.load_markets()
    except Exception:
        pass
    
    yapay_zekayi_egit_ve_guncelle()
    
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
                    if sym not in AKTIF_SISTEMLER:
                        try:
                            kontrat = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
                            yon = str(pos.get('side', '')).upper()
                            kapatma_yonu = 'sell' if yon == 'LONG' else 'buy'
                            tum_emirleri_iptal_et(sym)
                            exchange.create_order(sym, 'market', kapatma_yonu, kontrat, None, {'reduce_only': True})
                        except Exception:
                            pass

            # --- KAPATILANLARI İŞLE VE ÖĞREN ---
            for sym in list(AKTIF_SISTEMLER.keys()):
                if sym not in aktif_borsa_map:
                    kayitli_veri = AKTIF_SISTEMLER.pop(sym)
                    basarili_islem = False
                    realized_pnl = 0.0
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
                        ANALITIK_HAFIZA["basarili_islem_sayisi"] = ANALITIK_HAFIZA.get("basarili_islem_sayisi", 0) + 1
                        telegram_mesaj_gonder(f"🎉 *Hedefe Ulaşıldı (10x)* -> `{sym}` 🟢 (PnL: `{realized_pnl:+.2f}$`)")
                    else:
                        ANALITIK_HAFIZA["basarisiz_islem_sayisi"] = ANALITIK_HAFIZA.get("basarisiz_islem_sayisi", 0) + 1
                        telegram_mesaj_gonder(f"❌ *Stop Oldu (10x)* -> `{sym}` 🔴 (PnL: `{realized_pnl:+.2f}$`)")

                    if "parametreler" in kayitli_veri:
                        egitim_kaydi = kayitli_veri["parametreler"] + [1 if basarili_islem else 0]
                        if "egitim_verileri" not in ANALITIK_HAFIZA:
                            ANALITIK_HAFIZA["egitim_verileri"] = []
                        ANALITIK_HAFIZA["egitim_verileri"].append(egitim_kaydi)
                        if len(ANALITIK_HAFIZA["egitim_verileri"]) > 300:
                            ANALITIK_HAFIZA["egitim_verileri"] = ANALITIK_HAFIZA["egitim_verileri"][-300:]
                        yapay_zekayi_egit_ve_guncelle()

                    COIN_COOLDOWNLAR[sym] = time.time() + COOLDOWN_SURESI_SANIYE
                    hafizayi_kaydet()

            # --- TARAMA VE ANALİZ ---
            taranan_sinyaller = []
            su_anki_zaman = time.time()

            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU or symbol in aktif_borsa_map or len(aktif_borsa_map) >= MAKSIMUM_TOPLAM_POZISYON or su_anki_zaman < COIN_COOLDOWNLAR.get(symbol, 0):
                    continue

                try:
                    guncel_fiyat = exchange.fetch_ticker(symbol)['last']
                    df_15m = pd.DataFrame(exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50), columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    df_1h = pd.DataFrame(exchange.fetch_ohlcv(symbol, timeframe='1h', limit=50), columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

                    # Çoklu zaman dilimi trend filtresi (1h EMA 50)
                    ema50_1h = ta.trend.ema_indicator(df_1h['close'], window=50).iloc[-1]
                    trend_boga = df_1h['close'].iloc[-1] > ema50_1h
                    ema_fark = float(((df_1h['close'].iloc[-1] - ema50_1h) / guncel_fiyat) * 100)

                    rsi = float(ta.momentum.rsi(df_15m['close'], window=14).iloc[-1])
                    adx_val = float(ta.trend.ADXIndicator(df_15m['high'], df_15m['low'], df_15m['close'], window=14).adx().iloc[-1])
                    atr_yuzdesi = atr_ve_volatilite_hesapla(df_15m)
                    derinlik = emir_defteri_derinlik_analizi(symbol)
                    derinlik_kod = 1 if derinlik == "ALICI_BASKIN" else (-1 if derinlik == "SATICI_BASKIN" else 0)

                    # Hacim Patlaması Kontrolü (Volume Spike)
                    hacim_ort = df_15m['volume'].rolling(window=10).mean().iloc[-1]
                    anlik_hacim = df_15m['volume'].iloc[-1]
                    hacim_orani = float(anlik_hacim / hacim_ort) if hacim_ort > 0 else 1.0

                    fiyat_degisim = float(((df_15m['close'].iloc[-1] - df_15m['open'].iloc[-1]) / df_15m['open'].iloc[-1]) * 100)

                    puan = 50
                    if trend_boga:
                        puan += 25
                        grid_yonu = "LONG"
                        yon_kod = 1
                    else:
                        puan += 25
                        grid_yonu = "SHORT"
                        yon_kod = -1

                    # RSI ve Hacim Şartları
                    if grid_yonu == "LONG" and 45 <= rsi <= 70 and hacim_orani >= 1.5:
                        puan += 15
                    elif grid_yonu == "SHORT" and 30 <= rsi <= 55 and hacim_orani >= 1.5:
                        puan += 15

                    if adx_val > 22:
                        puan += 10

                    if derinlik == "ALICI_BASKIN" and grid_yonu == "LONG":
                        puan += 10
                    elif derinlik == "SATICI_BASKIN" and grid_yonu == "SHORT":
                        puan += 10

                    coin_id = COIN_ID_MAP.get(symbol, 1)
                    features = [rsi, adx_val, ema_fark, yon_kod, atr_yuzdesi, coin_id, derinlik_kod, hacim_orani, fiyat_degisim]

                    # Skor >= 75 ve AI Onayı Şartı
                    if puan >= 75 and yapay_zeka_islem_onayi(features):
                        taranan_sinyaller.append({
                            "symbol": symbol, "puan": puan, "yon": grid_yonu, "fiyat": guncel_fiyat, "atr": atr_yuzdesi,
                            "parametreler": features
                        })

                except Exception:
                    continue

            taranan_sinyaller.sort(key=lambda x: x["puan"], reverse=True)

            # --- İŞLEM AÇMA MİMARİSİ (10X & %20 MARJİN) ---
            for sinyal in taranan_sinyaller:
                if not BOT_CALISIYOR_MU or len(aktif_borsa_map) >= MAKSIMUM_TOPLAM_POZISYON:
                    break

                symbol = sinyal["symbol"]
                grid_yonu = sinyal["yon"]
                guncel_fiyat = sinyal["fiyat"]
                atr_yuzdesi = sinyal["atr"]

                try:
                    balance = exchange.fetch_balance()
                    toplam_bakiye = float(balance['total'].get('USDT', 0))
                except Exception:
                    continue

                if not set_leverage_and_margin_safely(symbol, KALDIRAC):
                    continue

                hedef_marjin = toplam_bakiye * MARJIN_ORANI
                ham_mask = (hedef_marjin * KALDIRAC) / guncel_fiyat
                
                try:
                    market_info = exchange.market(symbol)
                    miktar = float(exchange.amount_to_precision(symbol, max(round(ham_mask / float(market_info.get('contractSize', 1.0))), 1)))
                    
                    tum_emirleri_iptal_et(symbol)
                    exchange.create_order(symbol, 'market', 'buy' if grid_yonu == 'LONG' else 'sell', miktar)

                    giris_fiyati = guncel_fiyat
                    time.sleep(0.3)

                    hedef_oran_fiyat = 0.03  # %3 Kâr Hedefi
                    stop_oran_fiyat = (atr_yuzdesi * 1.5) / 100.0  # ATR Bazlı Dinamik Stop

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

                    AKTIF_SISTEMLER[symbol] = {
                        "yon": grid_yonu, "giris_fiyati": giris_fiyati, "hedef_fiyat": hedef_fiyat, 
                        "parametreler": sinyal["parametreler"]
                    }
                    hafizayi_kaydet()

                    print(f"🚀 10X İŞLEM AÇILDI: {symbol} | Yön: {grid_yonu} | Skor: {sinyal['puan']} | TP: {hedef_fiyat}", flush=True)
                    telegram_mesaj_gonder(f"🚀 *10X İŞLEM AÇILDI*\n📌 Coin: `{symbol}` | Yön: `{grid_yonu}` | Skor: `{sinyal['puan']}` | Hedef: `{hedef_fiyat}`")
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
