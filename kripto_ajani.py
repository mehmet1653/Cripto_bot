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

# 🚀 BTC ve ETH Çıkartılmış Yüksek Hacimli Coinler Listesi
TAKIP_EDILENLER = [
    'SOL/USDT:USDT', 'AVAX/USDT:USDT', 'XRP/USDT:USDT', 'DOGE/USDT:USDT', 
    'SUI/USDT:USDT', 'HYPE/USDT:USDT', 'NEAR/USDT:USDT', 'PEPE/USDT:USDT', 
    'RENDER/USDT:USDT', 'INJ/USDT:USDT'
]

BOT_CALISIYOR_MU = True

# ==================== SUPABASE HAFIZA FONKSİYONLARI ====================
def hafizayi_yukle():
    try:
        response = supabase.table("bot_hafiza").select("*").eq("id", 1).execute()
        if response.data and len(response.data) > 0:
            veri = response.data[0]
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
        print(f"Hafıza yükleme hatası: {e}")
        
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
    supabase.table("bot_hafiza").upsert({"id": 1, **varsayilan}).execute()
    return varsayilan

def hafizayi_kaydet():
    try:
        supabase.table("bot_hafiza").upsert({
            "id": 1,
            "aktif_sistemler": AKTIF_GRID_SISTEMLERI,
            "analitik": ANALitik_HAFIZA
        }).execute()
    except Exception as e:
        print(f"Hafıza kaydetme hatası: {e}")

kalici_veri = hafizayi_yukle()
AKTIF_GRID_SISTEMLERI = kalici_veri.get("aktif_sistemler", {})
ANALitik_HAFIZA = kalici_veri.get("analitik", {
    "basarisiz_analizler": [],
    "basarili_islem_sayisi": 0,
    "basarisiz_islem_sayisi": 0,
    "gunluk_net_kar_usd": 0.0,
    "egitim_verileri": []
})

MIN_ADX_GUCU = 20.0

# ==================== YAPAY ZEKA MODELİ (ML) ====================
ai_model = RandomForestClassifier(n_estimators=50, random_state=42)
ai_model_egitildi = False

def yapay_zekayi_egit_ve_guncelle():
    global ai_model, ai_model_egitildi
    veriler = ANALitik_HAFIZA.get("egitim_verileri", [])
    if len(veriler) < 5:
        ai_model_egitildi = False
        return
    try:
        X = [item[:4] for item in veriler]
        y = [item[4] for item in veriler]
        if len(set(y)) < 2:
            ai_model_egitildi = False
            return
        ai_model.fit(np.array(X), np.array(y))
        ai_model_egitildi = True
    except Exception as e:
        print(f"Yapay zeka eğitim hatası: {e}")
        ai_model_egitildi = False

def yapay_zeka_islem_onayi(rsi, adx, ema_fark, yon_kod):
    if not ai_model_egitildi:
        return True 
    try:
        return bool(ai_model.predict(np.array([[rsi, adx, ema_fark, yon_kod]]))[0] == 1)
    except Exception:
        return True

def telegram_mesaj_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": mesaj, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"Telegram Gönderme Hatası: {e}")

@app.route('/')
def home():
    return f"Testnet Dinamik Sinyal Botu | Aktif Pozisyon: {len(AKTIF_GRID_SISTEMLERI)} | Takip Edilen Coin: {len(TAKIP_EDILENLER)}"

def set_isolated_leverage_safely(symbol, leverage):
    try:
        exchange.set_margin_mode('isolated', symbol, {'leverage': leverage})
    except Exception:
        pass
    try:
        exchange.set_leverage(leverage, symbol)
        return True
    except Exception:
        return False

def pozisyonu_garantili_kapat(symbol, yon, miktar, sebep_mesaji, rsi=0, adx=0, ema_fark=0, basarili=True):
    kapatma_yonu = 'sell' if yon == 'LONG' else 'buy'
    try:
        for ord_item in exchange.fetch_open_orders(symbol):
            exchange.cancel_order(ord_item['id'], symbol)
    except Exception:
        pass

    try:
        market_info = exchange.market(symbol)
        min_amount = float(market_info['limits']['amount']['min'] or 1.0)
        if miktar < min_amount:
            miktar = min_amount
        miktar = float(exchange.amount_to_precision(symbol, miktar))
        exchange.create_market_order(symbol, kapatma_yonu, miktar, {'reduce_only': True})
    except Exception as e:
        try:
            time.sleep(0.2)
            ticker = exchange.fetch_ticker(symbol)
            guvenli_fiyat = ticker['ask'] if kapatma_yonu == 'buy' else ticker['bid']
            exchange.create_order(symbol, 'limit', kapatma_yonu, miktar, guvenli_fiyat, {'timeInForce': 'IOC', 'reduce_only': True})
        except Exception as e2:
            print(f"Kapatma hatası: {e2}")

    ANALitik_HAFIZA["egitim_verileri"].append([rsi, adx, ema_fark, (1 if yon == 'LONG' else -1), (1 if basarili else 0)])
    if len(ANALitik_HAFIZA["egitim_verileri"]) > 100:
        ANALitik_HAFIZA["egitim_verileri"].pop(0)
    yapay_zekayi_egit_ve_guncelle()

    if symbol in AKTIF_GRID_SISTEMLERI:
        del AKTIF_GRID_SISTEMLERI[symbol]
        hafizayi_kaydet()

    if sebep_mesaji:
        telegram_mesaj_gonder(sebep_mesaji)

# ==================== TELEGRAM KOMUTLARI ====================
async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        balance = exchange.fetch_balance()
        total = float(balance['total'].get('USDT', 0))
        
        try:
            borsa_pozisyonlari = exchange.fetch_positions()
            aktif_poslar = [p for p in borsa_pozisyonlari if float(p.get('contracts', 0)) > 0]
        except Exception:
            aktif_poslar = []

        mesaj = f"📊 *DİNAMİK BOT DURUMU*\n\n💰 Toplam Kasa: `{total:.2f} USDT`\n📌 Aktif İşlem Sayısı: `{len(AKTIF_GRID_SISTEMLERI)}`\n\n"
        
        if aktif_poslar:
            mesaj += "📋 *Açık Pozisyonlar:*\n"
            for pos in aktif_poslar:
                sym = pos.get('symbol')
                yon = str(pos.get('side', '')).upper()
                kaldirac = pos.get('leverage', 1)
                pnl = float(pos.get('unrealizedPnl', 0))
                yuzde = float(pos.get('percentage', 0))
                mesaj += f"🔹 *{sym}* (`{yon}` {kaldirac}x)\n   Kâr/Zarar: `{pnl:.2f} USDT` (`%{yuzde:.2f}`)\n"
        else:
            mesaj += "ℹ️ Şu an borsada açık aktif pozisyon bulunmuyor."

        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = True
    await update.message.reply_text("🟢 *Bot taramaya başladı!*", parse_mode='Markdown')

async def durdur_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ *Bot durduruldu.*", parse_mode='Markdown')

async def kapat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 *Tüm pozisyonlar kapatılıyor...*", parse_mode='Markdown')
    try:
        for pos in exchange.fetch_positions():
            if float(pos.get('contracts', 0)) > 0:
                pozisyonu_garantili_kapat(pos['symbol'], str(pos.get('side', '')).upper(), float(pos['contracts']), f"🛑 *MANUEL KAPATMA* - `{pos['symbol']}`", basarili=False)
        AKTIF_GRID_SISTEMLERI.clear()
        hafizayi_kaydet()
        await update.message.reply_text("✅ Tüm pozisyonlar kapatıldı.", parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

# ==================== 10 SANİYELİK ARKA PLAN TARAYICI ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, ANALitik_HAFIZA
    print("🚀 Dinamik Sinyal Tarayıcı aktif (10 sn döngü).")
    try:
        exchange.load_markets()
        yapay_zekayi_egit_ve_guncelle()
    except Exception as e:
        print(f"Piyasalar yüklenemedi: {e}")
    
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(3)
                continue

            try:
                borsa_pozisyonlari = exchange.fetch_positions()
            except Exception:
                borsa_pozisyonlari = []

            aktif_borsa_map = {pos['symbol']: pos for pos in borsa_pozisyonlari if float(pos.get('contracts', 0)) > 0}

            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU:
                    break
                try:
                    guncel_fiyat = exchange.fetch_ticker(symbol)['last']
                except Exception:
                    continue

                # --- 1. AÇIK POZİSYON KONTROLÜ VE DİNAMİK HEDEF / STOP YÖNETİMİ ---
                if symbol in aktif_borsa_map:
                    pos = aktif_borsa_map[symbol]
                    yon = str(pos.get('side', '')).upper()
                    merkez = float(pos.get('entryPrice', 0))
                    kaldirac_kullanilan = int(pos.get('leverage', 10))
                    
                    fark = (guncel_fiyat - merkez) / merkez if yon == "LONG" else (merkez - guncel_fiyat) / merkez
                    roe = fark * 100 * kaldirac_kullanilan

                    kayitli = AKTIF_GRID_SISTEMLERI.get(symbol, {})
                    hedef_roe = kayitli.get("hedef_roe", 20.0)
                    stop_roe = kayitli.get("stop_roe", 10.0)

                    if roe >= hedef_roe or float(pos.get('percentage', 0)) >= hedef_roe:
                        ANALitik_HAFIZA["gunluk_net_kar_usd"] += max(float(pos.get('unrealizedPnl', 0)), 1.0)
                        ANALitik_HAFIZA["basarili_islem_sayisi"] += 1
                        hafizayi_kaydet()
                        pozisyonu_garantili_kapat(symbol, yon, float(pos['contracts']), f"🚀 *DİNAMİK HEDEF ALINDI* - `{symbol}` (`+{roe:.2f}%`)", rsi=kayitli.get("giris_rsi", 50), adx=kayitli.get("giris_adx", 25), ema_fark=kayitli.get("ema_fark", 0), basarili=True)
                    elif roe <= -stop_roe or float(pos.get('percentage', 0)) <= -stop_roe:
                        ANALitik_HAFIZA["gunluk_net_kar_usd"] -= abs(float(pos.get('unrealizedPnl', 0)))
                        ANALitik_HAFIZA["basarisiz_islem_sayisi"] += 1
                        hafizayi_kaydet()
                        pozisyonu_garantili_kapat(symbol, yon, float(pos['contracts']), f"🛑 *DİNAMİK STOP KESİLDİ* - `{symbol}` (`{roe:.2f}%`)", rsi=kayitli.get("giris_rsi", 50), adx=kayitli.get("giris_adx", 25), ema_fark=kayitli.get("ema_fark", 0), basarili=False)
                    continue

                if symbol in aktif_borsa_map:
                    continue

                # --- 2. YENİ SİNYAL TESPİTİ VE DİNAMİK PUANLAMA (KASA / KALDIRAÇ / STOP) ---
                try:
                    balance = exchange.fetch_balance()
                    toplam_bakiye = float(balance['total'].get('USDT', 0))
                except Exception:
                    continue

                try:
                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    ema7 = ta.trend.ema_indicator(df['close'], window=7).iloc[-1]
                    ema21 = ta.trend.ema_indicator(df['close'], window=21).iloc[-1]
                    rsi = ta.momentum.rsi(df['close'], window=14).iloc[-1]
                    adx_val = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14).adx().iloc[-1]
                except Exception:
                    continue

                if adx_val < MIN_ADX_GUCU:
                    continue

                grid_yonu = "LONG" if ema7 > ema21 else "SHORT"
                if grid_yonu == "SHORT" and rsi < 42: continue 
                if grid_yonu == "LONG" and rsi > 58: continue 

                # 🧠 **DİNAMİK PUANLAMA SİSTEMİ**
                sinyal_puani = 50  # Baz puan
                if adx_val > 30: sinyal_puani += 20
                if abs(ema7 - ema21) / guncel_fiyat > 0.002: sinyal_puani += 15

                # Sinyal gücüne göre bütçe (kasa yönetimi), kaldıraç ve hedef/stop esnekliği
                if sinyal_puani >= 80:
                    dinamik_kaldirac = 20
                    kasa_orani = 0.35  # Güçlü sinyale kasanın %35'i
                    hedef_roe = 25.0
                    stop_roe = 8.0
                elif sinyal_puani >= 65:
                    dinamik_kaldirac = 10
                    kasa_orani = 0.20  # Normal sinyale kasanın %20'si
                    hedef_roe = 18.0
                    stop_roe = 10.0
                else:
                    dinamik_kaldirac = 5
                    kasa_orani = 0.10  # Zayıf sinyale kasanın %10'u
                    hedef_roe = 12.0
                    stop_roe = 12.0

                ema_fark_val = float(ema7 - ema21)
                if not yapay_zeka_islem_onayi(rsi, adx_val, ema_fark_val, 1 if grid_yonu == 'LONG' else -1):
                    continue

                set_isolated_leverage_safely(symbol, dinamik_kaldirac)
                
                hedef_marjin = toplam_bakiye * kasa_orani
                hedef_pozisyon_usdt = hedef_marjin * dinamik_kaldirac
                ham_miktar = hedef_pozisyon_usdt / guncel_fiyat

                try:
                    market_info = exchange.market(symbol)
                    contract_size = float(market_info.get('contractSize', 1.0))
                    min_amount = float(market_info['limits']['amount']['min'] or 1.0)
                    gercek_ham_miktar = max(ham_miktar / contract_size, min_amount)
                    miktar = float(exchange.amount_to_precision(symbol, gercek_ham_miktar))

                    exchange.create_market_order(symbol, 'buy' if grid_yonu == 'LONG' else 'sell', miktar)

                    AKTIF_GRID_SISTEMLERI[symbol] = {
                        "giris_fiyati": guncel_fiyat,
                        "giris_rsi": rsi,
                        "giris_adx": adx_val,
                        "ema_fark": ema_fark_val,
                        "hedef_roe": hedef_roe,
                        "stop_roe": stop_roe
                    }
                    hafizayi_kaydet()
                    telegram_mesaj_gonder(f"🚨 *DİNAMİK İŞLEM AÇILDI!*\n\n📌 *Coin:* `{symbol}`\n📊 *Yön:* `{grid_yonu}`\n⭐ *Puan:* `{sinyal_puani}`\n⚙️ *Kaldıraç:* `{dinamik_kaldirac}x`\n🎯 *Hedef TP:* `+{hedef_roe}%` | 🛑 *Stop SL:* `-{stop_roe}%`")
                except Exception as e:
                    print(f"Emir hatası ({symbol}): {e}")

        except Exception as e:
            print(f"Tarayıcı döngü hatası: {e}")
            
        time.sleep(10)

def flask_web_server():
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))

if __name__ == '__main__':
    # 1. Arka plan tarayıcısını thread olarak başlat
    threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True).start()
    
    # 2. Flask web sunucusunu ayrı bir thread'e al (Railway port dinlemesi için)
    threading.Thread(target=flask_web_server, daemon=True).start()
    
    # 3. Telegram Bot'u ANA THREAD (Main Thread) üzerinde çalıştır
    app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app_tg.add_handler(CommandHandler("durum", durum_komutu))
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))
    
    app_tg.run_polling()
