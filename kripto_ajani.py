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

TAKIP_EDILENLER = [
    'SOL/USDT:USDT', 'AVAX/USDT:USDT', 'XRP/USDT:USDT', 
    'DOGE/USDT:USDT', 'SUI/USDT:USDT', 'LINK/USDT:USDT', 'ADA/USDT:USDT'
]

COIN_ID_MAP = {symbol: idx + 1 for idx, symbol in enumerate(TAKIP_EDILENLER)}

BOT_CALISIYOR_MU = True
KALDIRAC = 10
MARJIN_ORANI = 0.20
MAKSIMUM_TOPLAM_POZISYON = 3
COOLDOWN_SURESI_SANIYE = 10 * 60

# Komisyon ve Grid Aralık Optimizasyonu (%0.6 adım, maker/taker maliyetini ekarte eder)
GRID_ADIM_YUZDESI = 0.006 
STOP_SAPMA_YUZDESI = 2.5 # %2.5 kırılımda stop/reset

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
    except Exception as e:
        print(f"⚠️ [SUPABASE] Hafıza yükleme hatası: {e}", flush=True)
    
    try:
        supabase.table("bot_hafiza").upsert({"id": 1, **varsayilan}).execute()
    except Exception as e:
        print(f"⚠️ [SUPABASE] Varsayılan hafıza kayıt hatası: {e}", flush=True)
        
    return varsayilan

def hafizayi_kaydet():
    try:
        supabase.table("bot_hafiza").upsert({
            "id": 1,
            "aktif_sistemler": AKTIF_SISTEMLER,
            "analitik": ANALITIK_HAFIZA,
            "cooldownlar": COIN_COOLDOWNLAR
        }).execute()
    except Exception as e:
        print(f"⚠️ [SUPABASE] Hafıza kayıt hatası: {e}", flush=True)

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
    except Exception as e:
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
        if len(df) < period:
            return 1.5
        atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=period).average_true_range().iloc[-1]
        fiyat = df['close'].iloc[-1]
        return float((atr / fiyat) * 100)
    except Exception:
        return 1.5

def bollinger_bandwidth_hesapla(df, window=20):
    try:
        if len(df) < window:
            return 0.05
        indicator = ta.volatility.BollingerBands(df['close'], window=window, window_dev=2)
        upper = indicator.bollinger_hband().iloc[-1]
        lower = indicator.bollinger_lband().iloc[-1]
        mavg = indicator.bollinger_mavg().iloc[-1]
        if mavg == 0:
            return 0.0
        return float((upper - lower) / mavg)
    except Exception:
        return 0.05

def tum_emirleri_iptal_et(symbol):
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
    return f"Gelişmiş Dinamik Hibrit Grid Bot Aktif | Aktif Sistemler: {len(AKTIF_SISTEMLER)}"

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
            borsa_poslari = {p['symbol']: p for p in raw_positions if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0}
        except Exception:
            borsa_poslari = {}

        toplam_pnl = sum(float(p.get('unrealizedPnl', 0)) for p in borsa_poslari.values())
        pnl_ikon = "🟢" if toplam_pnl >= 0 else "🔴"
        
        mesaj = (
            f"🚀 *GELİŞMİŞ DİNAMİK RİSK YÖNETİMLİ GRID BOT*\n\n"
            f"💰 Kasa: `{total:.2f} USDT`\n"
            f"{pnl_ikon} Anlık PnL: `{toplam_pnl:+.2f} USDT`\n"
            f"📌 Aktif Sistemler: `{len(AKTIF_SISTEMLER)} / {MAKSIMUM_TOPLAM_POZISYON}`\n"
            f"🧠 Yapay Zeka Durumu: `{'Aktif ve Eğitimli' if ai_model_egitildi else 'Veri Toplanıyor'}`\n"
        )
        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = True
    await update.message.reply_text("🚀 *Bot Aktif Edildi!*", parse_mode='Markdown')

async def durdur_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ *Bot Durduruldu.*", parse_mode='Markdown')

async def kapat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        for pos in exchange.fetch_positions():
            kontrat = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
            if kontrat > 0:
                symbol = pos['symbol']
                yon = str(pos.get('side', '')).upper()
                tum_emirleri_iptal_et(symbol)
                exchange.create_order(symbol, 'market', 'sell' if yon == 'LONG' else 'buy', kontrat, None, {'reduce_only': True})
        for sym in TAKIP_EDILENLER:
            tum_emirleri_iptal_et(sym)
        AKTIF_SISTEMLER.clear()
        hafizayi_kaydet()
        await update.message.reply_text("✅ Tüm sistem temizlendi.", parse_mode='Markdown')
    except Exception as e:
        AKTIF_SISTEMLER.clear()
        hafizayi_kaydet()
        await update.message.reply_text(f"✅ Hafıza temizlendi ({e}).", parse_mode='Markdown')

# ==================== ARKA PLAN TARAYICI VE DÖNGÜ ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, ANALITIK_HAFIZA
    print("🚀 [GELİŞMİŞ HİBRİT GRID] Arka plan tarayıcısı ve yapay zeka yöneticisi başlatıldı.", flush=True)
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

            # --- 1. AKTİF GRID KONTROLÜ VE DİNAMİK YENİLEME ---
            for sym in list(AKTIF_SISTEMLER.keys()):
                veri = AKTIF_SISTEMLER[sym]
                try:
                    guncel_fiyat = exchange.fetch_ticker(sym)['last']
                    merkez = veri.get("merkez_fiyat", guncel_fiyat)
                    
                    # Çok katmanlı risk, ADX ve %2.5 sapma kontrolü
                    ohlcv_data = exchange.fetch_ohlcv(sym, timeframe='1h', limit=30)
                    df_1h = pd.DataFrame(ohlcv_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    adx_val = float(ta.trend.ADXIndicator(df_1h['high'], df_1h['low'], df_1h['close'], window=14).adx().iloc[-1])
                    
                    fiyat_sapma = abs((guncel_fiyat - merkez) / merkez) * 100

                    if adx_val > 30 or fiyat_sapma > STOP_SAPMA_YUZDESI:
                        print(f"⚠️ [STOP/RESET] {sym} sınır dışına çıktı! Kırılım tespit edildi. Stop Olunuyor.", flush=True)
                        tum_emirleri_iptal_et(sym)
                        if sym in aktif_borsa_map:
                            pos = aktif_borsa_map[sym]
                            kontrat = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
                            yon = str(pos.get('side', '')).upper()
                            exchange.create_order(sym, 'market', 'sell' if yon == 'LONG' else 'buy', kontrat, None, {'reduce_only': True})
                        
                        # Başarısız işlem analitiği kaydı
                        ANALITIK_HAFIZA["basarisiz_islem_sayisi"] += 1
                        AKTIF_SISTEMLER.pop(sym)
                        COIN_COOLDOWNLAR[sym] = time.time() + COOLDOWN_SURESI_SANIYE
                        hafizayi_kaydet()
                        
                        telegram_mesaj_gonder(f"🛑 *Grid Stop / Reset* -> `{sym}` (Sapma: `%{fiyat_sapma:.2f}` | ADX: `{adx_val:.1f}`)")
                        continue

                    # DİNAMİK DÖNGÜ KONTROLÜ: Eksilen limit emirleri tazele
                    acik_emirler = exchange.fetch_open_orders(sym)
                    if len(acik_emirler) < 4:
                        print(f"🔄 [DİNAMİK GRID] {sym} için dolan emirler fark edildi, ağ tazeleniyor...", flush=True)
                        tum_emirleri_iptal_et(sym)
                        
                        kademe_sayisi = 3
                        balance = exchange.fetch_balance()
                        toplam_bakiye = float(balance['total'].get('USDT', 0))
                        kademe_butce = (toplam_bakiye * 0.15) / (kademe_sayisi * 2)
                        market_info = exchange.market(sym)

                        for i in range(1, kademe_sayisi + 1):
                            alis_fiyat = float(exchange.price_to_precision(sym, guncel_fiyat * (1.0 - (i * GRID_ADIM_YUZDESI))))
                            ham_mask_alis = (kademe_butce * KALDIRAC) / alis_fiyat
                            mik_alis = float(exchange.amount_to_precision(sym, max(round(ham_mask_alis / float(market_info.get('contractSize', 1.0))), 1)))
                            exchange.create_order(sym, 'limit', 'buy', mik_alis, alis_fiyat)

                            satis_fiyat = float(exchange.price_to_precision(sym, guncel_fiyat * (1.0 + (i * GRID_ADIM_YUZDESI))))
                            ham_mask_satis = (kademe_butce * KALDIRAC) / satis_fiyat
                            mik_satis = float(exchange.amount_to_precision(sym, max(round(ham_mask_satis / float(market_info.get('contractSize', 1.0))), 1)))
                            exchange.create_order(sym, 'limit', 'sell', mik_satis, satis_fiyat)

                        veri["merkez_fiyat"] = guncel_fiyat
                        hafizayi_kaydet()

                except Exception as e:
                    print(f"⚠️ [GRID YÖNETİMİ] Hata ({sym}): {e}", flush=True)

            # --- 2. YENİ PARİTE TARAMA VE YAPAY ZEKA DESTEKLİ KURULUM ---
            su_anki_zaman = time.time()
            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU or symbol in AKTIF_SISTEMLER or len(AKTIF_SISTEMLER) >= MAKSIMUM_TOPLAM_POZISYON or su_anki_zaman < COIN_COOLDOWNLAR.get(symbol, 0):
                    continue

                try:
                    ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=30)
                    if len(ohlcv_15m) < 20:
                        continue
                    df_15m = pd.DataFrame(ohlcv_15m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    
                    rsi = float(ta.momentum.rsi(df_15m['close'], window=14).iloc[-1])
                    bb_bandwidth = bollinger_bandwidth_hesapla(df_15m)
                    atr_val = atr_ve_volatilite_hesapla(df_15m)
                    guncel_fiyat = df_15m['close'].iloc[-1]

                    # Yapay zeka ve teknik filtreler
                    features = [rsi, bb_bandwidth, atr_val, guncel_fiyat, 0, 0, 0, 0, COIN_ID_MAP.get(symbol, 1)]
                    ai_onay = yapay_zeka_islem_onayi(features)

                    if bb_bandwidth < 0.045 and 40 <= rsi <= 60 and ai_onay:
                        if not set_leverage_and_margin_safely(symbol, KALDIRAC):
                            continue

                        balance = exchange.fetch_balance()
                        toplam_bakiye = float(balance['total'].get('USDT', 0))
                        kademe_sayisi = 3
                        kademe_butce = (toplam_bakiye * 0.15) / (kademe_sayisi * 2)
                        market_info = exchange.market(symbol)
                        
                        tum_emirleri_iptal_et(symbol)

                        for i in range(1, kademe_sayisi + 1):
                            alis_fiyat = float(exchange.price_to_precision(symbol, guncel_fiyat * (1.0 - (i * GRID_ADIM_YUZDESI))))
                            ham_mask_alis = (kademe_butce * KALDIRAC) / alis_fiyat
                            mik_alis = float(exchange.amount_to_precision(symbol, max(round(ham_mask_alis / float(market_info.get('contractSize', 1.0))), 1)))
                            exchange.create_order(symbol, 'limit', 'buy', mik_alis, alis_fiyat)

                            satis_fiyat = float(exchange.price_to_precision(symbol, guncel_fiyat * (1.0 + (i * GRID_ADIM_YUZDESI))))
                            ham_mask_satis = (kademe_butce * KALDIRAC) / satis_fiyat
                            mik_satis = float(exchange.amount_to_precision(symbol, max(round(ham_mask_satis / float(market_info.get('contractSize', 1.0))), 1)))
                            exchange.create_order(symbol, 'limit', 'sell', mik_satis, satis_fiyat)

                        AKTIF_SISTEMLER[symbol] = {
                            "mod": "DİNAMİK_GRID", "merkez_fiyat": guncel_fiyat
                        }
                        hafizayi_kaydet()

                        print(f"✨ [YENİ GRID] {symbol} üzerinde yapay zeka onaylı dinamik grid kuruldu.", flush=True)
                        telegram_mesaj_gonder(f"⚡ *Dinamik Grid Kuruldu* -> `{symbol}` (Merkez: `{guncel_fiyat}`)")
                        break

                except Exception as e:
                    print(f"⚠️ [TARAMA] Hata ({symbol}): {e}", flush=True)

        except Exception as e:
            print(f"⚠️ [DÖNGÜ] Genel hata: {e}", flush=True)
        time.sleep(10)

def flask_web_server():
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

if __name__ == '__main__':
    threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True).start()
    threading.Thread(target=flask_web_server, daemon=True).start()
    
    app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app_tg.add_handler(CommandHandler("durum", durum_komutu))
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))
    
    app_tg.run_polling()
