import time
import threading
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

import sys
import io

# Logların tamponda beklemeden anında ekrana ve konsola akması için zorunlu flushing
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, line_buffering=True)

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

TAKIP_EDILENLER_YEDEK = [
    'SOL/USDT:USDT', 'AVAX/USDT:USDT', 'XRP/USDT:USDT', 'DOGE/USDT:USDT', 'SUI/USDT:USDT',
    'BTC/USDT:USDT', 'ETH/USDT:USDT', 'ADA/USDT:USDT', 'LINK/USDT:USDT', 'NEAR/USDT:USDT'
]

BOT_CALISIYOR_MU = True
ADAY_SINYALLER = {} 

def hafizayi_yukle():
    try:
        response = supabase.table("bot_hafiza").select("*").eq("id", 1).execute()
        if response.data and len(response.data) > 0:
            veri = response.data[0]
            print("💾 Hafıza Supabase'den başarıyla yüklendi.")
            return {
                "aktif_sistemler": veri.get("aktif_sistemler", {}),
                "analitik": veri.get("analitik", {
                    "basarisiz_analizler": [],
                    "basarili_islem_sayisi": 0,
                    "basarisiz_islem_sayisi": 0,
                    "gunluk_net_kar_usd": 0.0,
                    "egitim_verileri": []
                })
            }
    except Exception as e:
        print(f"⚠️ Hafıza yükleme hatası: {e}")
        
    varsayilan = {
        "aktif_sistemler": {},
        "analitik": {
            "basarisiz_analizler": [],
            "basarili_islem_sayisi": 0,
            "basarisiz_islem_sayisi": 0,
            "gunluk_net_kar_usd": 0.0,
            "egitim_verileri": []
        }
    }
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
            "analitik": ANALitik_HAFIZA
        }).execute()
    except Exception as e:
        print(f"⚠️ Hafıza kaydetme hatası: {e}")

kalici_veri = hafizayi_yukle()
AKTIF_GRID_SISTEMLERI = kalici_veri.get("aktif_sistemler", {})
ANALitik_HAFIZA = kalici_veri.get("analitik", {
    "basarisiz_analizler": [],
    "basarili_islem_sayisi": 0,
    "basarisiz_islem_sayisi": 0,
    "gunluk_net_kar_usd": 0.0,
    "egitim_verileri": []
})
COIN_COOLDOWNLAR = {}

MAKSIMUM_NORMAL_POZISYON = 3
MAKSIMUM_ALTIN_ATIS_POZISYON = 2
COOLDOWN_SURESI_SANIYE = 15 * 60

ai_model = RandomForestClassifier(n_estimators=100, max_depth=6, random_state=42)
ai_model_egitildi = False

def yapay_zekayi_egit_ve_guncelle():
    global ai_model, ai_model_egitildi
    veriler = ANALitik_HAFIZA.get("egitim_verileri", [])
    if len(veriler) < 50:
        ai_model_egitildi = False
        return
    try:
        X = [item[:5] for item in veriler]
        y = [item[5] for item in veriler]
        if len(set(y)) < 2:
            ai_model_egitildi = False
            return
        ai_model.fit(np.array(X), np.array(y))
        ai_model_egitildi = True
    except Exception as e:
        print(f"⚠️ Yapay zeka eğitim hatası: {e}")
        ai_model_egitildi = False

def yapay_zeka_islem_onayi(rsi, adx, ema_fark, yon_kod, atr_yuzde):
    if not ai_model_egitildi:
        return True
    try:
        olasiliklar = ai_model.predict_proba(np.array([[rsi, adx, ema_fark, yon_kod, atr_yuzde]]))[0]
        classes = list(ai_model.classes_)
        basari_ihtimali = olasiliklar[classes.index(1)] if 1 in classes else 1.0
        return basari_ihtimali >= 0.55
    except Exception:
        return True

def atr_ve_volatilite_hesapla(df, period=14):
    try:
        atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=period).average_true_range().iloc[-1]
        fiyat = df['close'].iloc[-1]
        return float((atr / fiyat) * 100)
    except Exception:
        return float(1.5)

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

def hacim_ve_likidite_kontrolu(df):
    try:
        ortalama_hacim = df['volume'].rolling(window=20).mean().iloc[-1]
        son_hacim = df['volume'].iloc[-1]
        return son_hacim >= (ortalama_hacim * 0.15)
    except Exception:
        return True

def dinamik_coin_havuzu_getir():
    try:
        tickers = exchange.fetch_tickers()
        usdt_swap_tickers = {sym: data for sym, data in tickers.items() if '/USDT:USDT' in sym or sym.endswith(':USDT')}
        sorted_tickers = sorted(usdt_swap_tickers.items(), key=lambda x: x[1].get('quoteVolume', 0) or 0, reverse=True)
        top_coins = [item[0] for item in sorted_tickers[:40]]
        if not top_coins:
            return TAKIP_EDILENLER_YEDEK
        return top_coins
    except Exception as e:
        print(f"⚠️ Dinamik havuz çekilemedi: {e}")
        return TAKIP_EDILENLER_YEDEK

def telegram_mesaj_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": mesaj, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"⚠️ Telegram Gönderme Hatası: {e}")

@app.route('/')
def home():
    return f"Efsanevi Hibrit Bot Aktif | Aktif Pozisyon: {len(AKTIF_GRID_SISTEMLERI)}"

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
        basarili_sayisi = ANALitik_HAFIZA.get("basarili_islem_sayisi", 0)
        basarisiz_sayisi = ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0)
        toplam_islem = basarili_sayisi + basarisiz_sayisi
        basari_orani = (basarili_sayisi / toplam_islem * 100) if toplam_islem > 0 else 0.0

        pozisyon_detaylari = ""
        if borsa_poslari:
            pozisyon_detaylari = "\n🔍 *Açık Pozisyonlar:*\n"
            for p in borsa_poslari:
                sym = p.get('symbol', 'Bilinmeyen')
                yon = str(p.get('side', '')).upper()
                pnl_val = float(p.get('unrealizedPnl', 0))
                roe_val = float(p.get('percentage', 0))
                kaldirac_degeri = int(p.get('leverage', 10))
                pozisyon_detaylari += f"• `{sym}` | {yon} ({kaldirac_degeri}x) | PnL: `{pnl_val:+.2f} USDT` (`%{roe_val:.2f}`)\n"
        else:
            pozisyon_detaylari = "\n🔍 *Açık Pozisyon:* `Yok`\n"

        mesaj = (
            f"📊 *BOT DURUMU*\n\n"
            f"💰 Toplam Kasa: `{total:.2f} USDT`\n"
            f"{pnl_ikon} Anlık PnL: `{toplam_pnl:+.2f} USDT`\n"
            f"{pozisyon_detaylari}\n"
            f"✅ TP: `{basarili_sayisi}` | ❌ SL: `{basarisiz_sayisi}` | Başarı: `%{basari_orani:.1f}`"
        )
        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = True
    await update.message.reply_text("🟢 *Bot Aktif Edildi!*", parse_mode='Markdown')

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
                exchange.create_order(pos['symbol'], 'market', 'sell' if pos.get('side')=='long' else 'buy', kontrat, None, {'reduce_only': True})
        AKTIF_GRID_SISTEMLERI.clear()
        hafizayi_kaydet()
        await update.message.reply_text("✅ Temizlendi.", parse_mode='Markdown')
    except Exception as e:
        AKTIF_GRID_SISTEMLERI.clear()
        hafizayi_kaydet()
        await update.message.reply_text(f"✅ Hafıza temizlendi. ({e})")

def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, ANALitik_HAFIZA, ADAY_SINYALLER
    print("🚀 Tarayıcı Döngüsü Başlatıldı.")
    try:
        exchange.load_markets()
        yapay_zekayi_egit_ve_guncelle()
    except Exception as e:
        print(f"⚠️ Piyasalar yüklenemedi: {e}")
    
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(3)
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

            for sym in list(AKTIF_GRID_SISTEMLERI.keys()):
                if sym not in aktif_borsa_map:
                    kayitli_veri = AKTIF_GRID_SISTEMLERI[sym]
                    rsi_val = kayitli_veri.get("giris_rsi", 50)
                    adx_val = kayitli_veri.get("giris_adx", 25)
                    ema_fark_val = kayitli_veri.get("ema_fark", 0.0)
                    atr_val = kayitli_veri.get("atr_yuzde", 1.5)
                    yon_val = kayitli_veri.get("yon", "LONG")

                    tp_gerceklesti = False
                    try:
                        islem_gecmisi = exchange.fetch_closed_orders(sym, limit=2)
                        if islem_gecmisi:
                            son_emir = islem_gecmisi[-1]
                            if 'limit' in str(son_emir.get('type', '')).lower():
                                tp_gerceklesti = True
                    except Exception:
                        pass

                    if tp_gerceklesti:
                        ANALitik_HAFIZA["basarili_islem_sayisi"] += 1
                        print(f"🎯 [{sym}] TP Hedefine Ulaşıldı!")
                        telegram_mesaj_gonder(f"🎯 *KÂR ALINDI (TP)*\n📌 `{sym}`")
                    else:
                        ANALitik_HAFIZA["basarisiz_islem_sayisi"] += 1
                        print(f"🛑 [{sym}] SL Tetiklendi!")
                        telegram_mesaj_gonder(f"🛑 *ZARAR KESİLDİ (SL)*\n📌 `{sym}`")

                    del AKTIF_GRID_SISTEMLERI[sym]
                    hafizayi_kaydet()

            guncel_takip_listesi = dinamik_coin_havuzu_getir()
            taranan_sinyaller = []
            su_anki_zaman = time.time()
            yeni_aday_sinyalleri = {}

            for symbol in guncel_takip_listesi:
                if not BOT_CALISIYOR_MU:
                    break
                if symbol in aktif_borsa_map or su_anki_zaman < COIN_COOLDOWNLAR.get(symbol, 0):
                    continue

                try:
                    guncel_fiyat = exchange.fetch_ticker(symbol)['last']
                    ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
                    df_15m = pd.DataFrame(ohlcv_15m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    
                    ohlcv_1h = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=25)
                    df_1h = pd.DataFrame(ohlcv_1h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    
                    if not hacim_ve_likidite_kontrolu(df_15m):
                        continue

                    ema7_1h = ta.trend.ema_indicator(df_1h['close'], window=7).iloc[-1]
                    ema21_1h = ta.trend.ema_indicator(df_1h['close'], window=21).iloc[-1]
                    boga_trend_1h = ema7_1h > ema21_1h

                    ema7 = ta.trend.ema_indicator(df_15m['close'], window=7).iloc[-1]
                    ema21 = ta.trend.ema_indicator(df_15m['close'], window=21).iloc[-1]
                    rsi = ta.momentum.rsi(df_15m['close'], window=14).iloc[-1]
                    
                    adx_indicator = ta.trend.ADXIndicator(df_15m['high'], df_15m['low'], df_15m['close'], window=14)
                    adx_val = adx_indicator.adx().iloc[-1]
                    plus_di = adx_indicator.adx_pos().iloc[-1]
                    minus_di = adx_indicator.adx_neg().iloc[-1]
                    atr_yuzdesi = atr_ve_volatilite_hesapla(df_15m)
                    
                    derinlik_durumu = emir_defteri_derinlik_analizi(symbol)
                except Exception:
                    continue

                sinyal_puani = 75
                grid_yonu = "LONG" if ema7 > ema21 else "SHORT"
                is_altin_atis = (adx_val >= 35 and rsi < 30) or (adx_val >= 35 and rsi > 70)

                if ai_model_egitildi:
                    if not yapay_zeka_islem_onayi(rsi, adx_val, float(ema7 - ema21), 1 if grid_yonu=='LONG' else -1, atr_yuzdesi):
                        continue

                taranan_sinyaller.append({
                    "symbol": symbol, "puan": sinyal_puani, "yon": grid_yonu,
                    "rsi": rsi, "adx": adx_val, "ema_fark": float(ema7 - ema21),
                    "fiyat": guncel_fiyat, "atr": atr_yuzdesi, "altin_atis": is_altin_atis
                })

            taranan_sinyaller.sort(key=lambda x: x["puan"], reverse=True)

            for sinyal in taranan_sinyaller:
                if not BOT_CALISIYOR_MU:
                    break
                symbol = sinyal["symbol"]
                grid_yonu = sinyal["yon"]
                guncel_fiyat = sinyal["fiyat"]
                is_altin_atis = sinyal["altin_atis"]

                dinamik_kaldirac = 20 if is_altin_atis else 10
                kasa_orani = 0.25 if is_altin_atis else 0.20

                try:
                    balance = exchange.fetch_balance()
                    toplam_bakiye = float(balance['total'].get('USDT', 0))
                    exchange.set_leverage(dinamik_kaldirac, symbol)
                    
                    market_info = exchange.market(symbol)
                    contract_size = float(market_info.get('contractSize', 1.0))
                    min_amount = float(market_info['limits']['amount']['min'] or 1.0)
                    
                    # Bakiye ve kaldıraç oranına göre hassas miktar hesaplama (1 SOL sabitlemesi kaldırıldı)
                    hedef_marjin = toplam_bakiye * kasa_orani
                    hedef_pozisyon_usdt = hedef_marjin * dinamik_kaldirac
                    ham_miktar = (hedef_pozisyon_usdt / guncel_fiyat) / contract_size
                    
                    # Borsa min limit kontrolü
                    miktar = max(ham_miktar, min_amount)
                    miktar = float(exchange.amount_to_precision(symbol, miktar))
                    
                    emir_yonu = 'buy' if grid_yonu == 'LONG' else 'sell'
                    exchange.create_order(symbol, 'market', emir_yonu, miktar)

                    print(f"🛡️ [{symbol}] İşlem Açıldı -> Miktar: {miktar} | Kaldıraç: {dinamik_kaldirac}x")
                    telegram_mesaj_gonder(f"🚀 *İŞLEM AÇILDI*\n📌 `{symbol}` | Yön: `{grid_yonu}` | Miktar: `{miktar}`")
                    
                    AKTIF_GRID_SISTEMLERI[symbol] = {"yon": grid_yonu, "altin_atis": is_altin_atis}
                    hafizayi_kaydet()
                    break
                except Exception as e:
                    print(f"❌ Emir açma hatası ({symbol}): {e}")

        except Exception as e:
            print(f"⚠️ Döngü genel hatası: {e}")
            
        time.sleep(5)

if __name__ == '__main__':
    threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True).start()
    threading.Thread(target=flask_web_server, daemon=True).start()
    
    app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app_tg.add_handler(CommandHandler("durum", durum_komutu))
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))
    
    app_tg.run_polling()
