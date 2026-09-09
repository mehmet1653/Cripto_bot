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
            print("💾 Hafıza Supabase'den başarıyla yüklendi.", flush=True)
            return {
                "aktif_sistemler": veri.get("aktif_sistemler", {}),
                "analitik": veri.get("analitik", varsayilan["analitik"]),
                "cooldownlar": veri.get("cooldownlar", {})
            }
    except Exception as e:
        print(f"⚠️ Hafıza yükleme uyarısı: {e}", flush=True)
        
    try:
        supabase.table("bot_hafiza").upsert({"id": 1, **varsayilan}).execute()
        print("💾 Varsayılan hafıza Supabase'e kaydedildi.", flush=True)
    except Exception as ex:
        print(f"⚠️ Varsayılan kayıt oluşturulamadı: {ex}", flush=True)
        
    return varsayilan

def hafizayi_kaydet():
    try:
        supabase.table("bot_hafiza").upsert({
            "id": 1,
            "aktif_sistemler": AKTIF_GRID_SISTEMLERI,
            "analitik": ANALitik_HAFIZA,
            "cooldownlar": COIN_COOLDOWNLAR
        }).execute()
    except Exception as e:
        print(f"⚠️ Hafıza kaydetme hatası: {e}", flush=True)

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

MAKSIMUM_AYNI_YON_SAYISI = 2
MAKSIMUM_TOPLAM_POZISYON = 3
COOLDOWN_SURESI_SANIYE = 15 * 60

# ==================== YAPAY ZEKA MODELİ ====================
ai_model = RandomForestClassifier(n_estimators=100, max_depth=6, random_state=42)
ai_model_egitildi = False

def yapay_zekayi_egit_ve_guncelle():
    global ai_model, ai_model_egitildi
    veriler = ANALitik_HAFIZA.get("egitim_verileri", [])
    
    if len(veriler) < 30:
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
        print(f"🤖 Yapay zeka modeli tersine çevrilmiş stratejiyle güncellendi!", flush=True)
    except Exception as e:
        print(f"⚠️ Yapay zeka eğitim hatası: {e}", flush=True)
        ai_model_egitildi = False

def yapay_zeka_islem_onayi(rsi, adx, ema_fark, yon_kod, atr_yuzde, coin_id, derinlik_kod, hacim_orani, fiyat_degisim, sinyal_puani):
    if not ai_model_egitildi:
        return True
    try:
        features = np.array([[rsi, adx, ema_fark, yon_kod, atr_yuzde, coin_id, derinlik_kod, hacim_orani, fiyat_degisim]])
        olasiliklar = ai_model.predict_proba(features)[0]
        classes = list(ai_model.classes_)
        basari_ihtimali = olasiliklar[classes.index(1)] if 1 in classes else 1.0
        return basari_ihtimali >= 0.40
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
    except Exception as e:
        print(f"⚠️ Telegram Gönderme Hatası: {e}", flush=True)

@app.route('/')
def home():
    return f"Tersine Çevrilmiş Hibrit Bot Aktif | Aktif Pozisyon: {len(AKTIF_GRID_SISTEMLERI)}"

def set_leverage_and_margin_safely(symbol, leverage):
    try:
        exchange.set_leverage(leverage, symbol)
        try:
            exchange.set_margin_mode('isolated', symbol)
        except Exception:
            pass
        return True
    except Exception as e:
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
        
        mesaj = (
            f"🔄 *İNVERSED (TERS) STRATEJİ BOTU*\n\n"
            f"💰 Toplam Kasa: `{total:.2f} USDT`\n"
            f"{pnl_ikon} Anlık Kâr/Zarar: `{toplam_pnl:+.2f} USDT`\n"
            f"📌 Açık Pozisyon Sayısı: `{len(borsa_poslari)} / {MAKSIMUM_TOPLAM_POZISYON}`\n"
        )
        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = True
    await update.message.reply_text("🔄 *Tersine Çevrilmiş Strateji Botu Aktif!*", parse_mode='Markdown')

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
        await update.message.reply_text("✅ Hafıza sıfırlandı.", parse_mode='Markdown')
    except Exception as e:
        AKTIF_GRID_SISTEMLERI.clear()
        hafizayi_kaydet()
        await update.message.reply_text(f"✅ Hafıza temizlendi.", parse_mode='Markdown')

# ==================== ARKA PLAN TARAYICI ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, ANALitik_HAFIZA
    print("🔄 [TERSİNE ÇEVRİLDİ] Momentum Kırılımı ve Ters Mantık Stratejisi Devrede.", flush=True)
    try:
        exchange.load_markets()
        yapay_zekayi_egit_ve_guncelle()
    except Exception as e:
        print(f"⚠️ Piyasalar yüklenemedi: {e}", flush=True)
    
    derinlik_kodlar = {"ALICI_BASKIN": 1, "DENGELI": 0, "SATICI_BASKIN": -1}
    tur_sayaci = 0
    
    while True:
        try:
            tur_sayaci += 1
            zaman_str = datetime.now().strftime('%H:%M:%S')
            print(f"\n[{zaman_str}] 🔄 TERS STRATEJİ TARAMA TURU (Tur #{tur_sayaci})", flush=True)

            if not BOT_CALISIYOR_MU:
                time.sleep(5)
                continue

            try:
                raw_positions = exchange.fetch_positions()
                aktif_borsa_map = {}
                for p in raw_positions:
                    kontrat = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                    if kontrat > 0:
                        aktif_borsa_map[p['symbol']] = p
            except Exception:
                aktif_borsa_map = {}

            # --- TRAILING VE POZISYON KONTROLÜ ---
            for sym, kayitli_veri in list(AKTIF_GRID_SISTEMLERI.items()):
                if sym in aktif_borsa_map:
                    poz = aktif_borsa_map[sym]
                    try:
                        guncel_fiyat = exchange.fetch_ticker(sym)['last']
                        giris_fiyati = float(poz.get('entryPrice', kayitli_veri.get('giris_fiyati', guncel_fiyat)))
                        yon = kayitli_veri.get("yon", "LONG")
                        
                        fiyat_farki_yuzde = ((guncel_fiyat - giris_fiyati) / giris_fiyati) * 100 if yon == "LONG" else ((giris_fiyati - guncel_fiyat) / giris_fiyati) * 100

                        if fiyat_farki_yuzde >= 0.8 and not kayitli_veri.get("trailing_aktif", False):
                            kayitli_veri["trailing_aktif"] = True
                            tum_emirleri_iptal_et(sym)
                            kapatma_yonu = 'sell' if yon == 'LONG' else 'buy'
                            miktar = float(poz.get('contracts', 0) or poz.get('size', 0))
                            breakeven_fiyat = float(exchange.price_to_precision(sym, giris_fiyati))
                            stop_params = {'stopPrice': breakeven_fiyat, 'triggerPrice': breakeven_fiyat, 'reduceOnly': True, 'price_type': 'mark_price'}
                            try:
                                exchange.create_order(sym, 'stop', kapatma_yonu, miktar, breakeven_fiyat, stop_params)
                            except Exception:
                                exchange.create_order(sym, 'stop_market', kapatma_yonu, miktar, breakeven_fiyat, stop_params)
                            hafizayi_kaydet()
                    except Exception:
                        pass

            # --- KAPATILANLARI İŞLE ---
            for sym in list(AKTIF_GRID_SISTEMLERI.keys()):
                if sym not in aktif_borsa_map:
                    kayitli_veri = AKTIF_GRID_SISTEMLERI.pop(sym)
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
                        telegram_mesaj_gonder(f"🎉 *Kâr Alındı (Ters Strateji)* -> `{sym}` 🟢")
                    else:
                        ANALitik_HAFIZA["basarisiz_islem_sayisi"] = ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0) + 1
                        telegram_mesaj_gonder(f"❌ *Stop Oldu (Ters Strateji)* -> `{sym}` 🔴")

                    COIN_COOLDOWNLAR[sym] = time.time() + COOLDOWN_SURESI_SANIYE
                    hafizayi_kaydet()

            # --- TERSİNE ÇEVRİLMİŞ SİNYAL ÜRETİMİ ---
            taranan_sinyaller = []
            su_anki_zaman = time.time()

            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU or symbol in aktif_borsa_map or su_anki_zaman < COIN_COOLDOWNLAR.get(symbol, 0):
                    continue

                try:
                    guncel_fiyat = exchange.fetch_ticker(symbol)['last']
                    df_15m = pd.DataFrame(exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50), columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    df_1h = pd.DataFrame(exchange.fetch_ohlcv(symbol, timeframe='1h', limit=30), columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

                    ema7_1h = ta.trend.ema_indicator(df_1h['close'], window=7).iloc[-1]
                    ema21_1h = ta.trend.ema_indicator(df_1h['close'], window=21).iloc[-1]
                    trend_1h_boga = ema7_1h > ema21_1h

                    rsi = ta.momentum.rsi(df_15m['close'], window=14).iloc[-1]
                    adx_val = ta.trend.ADXIndicator(df_15m['high'], df_15m['low'], df_15m['close'], window=14).adx().iloc[-1]
                    atr_yuzdesi = atr_ve_volatilite_hesapla(df_15m)
                    
                    fiyat_10_mum_once = df_15m['close'].iloc[-10]
                    degisim_yuzdesi = ((guncel_fiyat - fiyat_10_mum_once) / fiyat_10_mum_once) * 100
                    derinlik_durumu = emir_defteri_derinlik_analizi(symbol)

                    # TERS MANTIK: Eskiden düşene long açıyorduk, şimdi yükselen trendin ve güçlü RSI'ın peşinden gidiyoruz (Momentum Breakout)
                    long_onayli = (rsi > 50) and (degisim_yuzdesi > 0.5)
                    short_onayli = (rsi < 50) and (degisim_yuzdesi < -0.5)

                except Exception:
                    continue

                if long_onayli:
                    grid_yonu = "LONG"
                    sinyal_puani = 85
                elif short_onayli:
                    grid_yonu = "SHORT"
                    sinyal_puani = 85
                else:
                    continue

                taranan_sinyaller.append({
                    "symbol": symbol, "puan": sinyal_puani, "yon": grid_yonu, "rsi": rsi, "adx": adx_val,
                    "ema_fark": 0.0, "fiyat": guncel_fiyat, "atr": atr_yuzdesi, "altin_atis": True,
                    "derinlik_durumu": derinlik_durumu, "hacim_orani": 1.0, "fiyat_degisim": degisim_yuzdesi
                })

            taranan_sinyaller.sort(key=lambda x: x["puan"], reverse=True)

            # --- İŞLEM AÇMA ---
            for sinyal in taranan_sinyaller:
                if not BOT_CALISIYOR_MU or len(aktif_borsa_map) >= MAKSIMUM_TOPLAM_POZISYON:
                    break

                symbol = sinyal["symbol"]
                grid_yonu = sinyal["yon"]
                guncel_fiyat = sinyal["fiyat"]
                atr_yuzdesi = sinyal["atr"]
                degisim_yuzdesi = sinyal["fiyat_degisim"]

                dinamik_kaldirac = 10
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

                    hedef_oran_fiyat = 0.02  # Net %2 Hedef
                    stop_oran_fiyat = (atr_yuzdesi * 1.5) / 100.0

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

                    print(f"🚀 TERS STRATEJİ İŞLEM: {symbol} | Yön: {grid_yonu} | Hedef: {hedef_fiyat}", flush=True)
                    telegram_mesaj_gonder(f"🔄 *İNVERSED (TERS) İŞLEM AÇILDI*\n📌 Coin: `{symbol}` | Yön: `{grid_yonu}` | Hedef: `{hedef_fiyat}`")
                    break
                except Exception as e:
                    print(f"❌ Hata: {e}", flush=True)

        except Exception as e:
            print(f"⚠️ Hata: {e}", flush=True)
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
