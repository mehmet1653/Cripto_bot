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

TAKIP_EDILENLER = [
    'SOL/USDT:USDT', 'AVAX/USDT:USDT', 'XRP/USDT:USDT', 'DOGE/USDT:USDT', 
    'SUI/USDT:USDT', 'HYPE/USDT:USDT', 'NEAR/USDT:USDT', 'RENDER/USDT:USDT', 'INJ/USDT:USDT'
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

MAKSIMUM_AYNI_YON_SAYISI = 2
MAKSIMUM_TOPLAM_POZISYON = 3

# ==================== YAPAY ZEKA MODELİ (ML) ====================
ai_model = RandomForestClassifier(n_estimators=50, random_state=42)
ai_model_egitildi = False

def yapay_zekayi_egit_ve_guncelle():
    global ai_model, ai_model_egitildi
    veriler = ANALitik_HAFIZA.get("egitim_verileri", [])
    if len(veriler) < 3:
        ai_model_egitildi = False
        return
    try:
        X = [item[:4] for item in veriler]
        y = [item[4] for item in veriler]
        if len(set(y)) < 2:
            X.extend([[50, 25, 0.001, 1], [50, 25, -0.001, -1]])
            y.extend([1, 0])
        ai_model.fit(np.array(X), np.array(y))
        ai_model_egitildi = True
    except Exception as e:
        print(f"Yapay zeka eğitim hatası: {e}")
        ai_model_egitildi = False

def yapay_zeka_islem_onayi(rsi, adx, ema_fark, yon_kod):
    if not ai_model_egitildi:
        return True
    try:
        tahmin = ai_model.predict(np.array([[rsi, adx, ema_fark, yon_kod]]))[0]
        return bool(tahmin == 1)
    except Exception:
        return True

def atr_ve_volatilite_hesapla(df, period=14):
    try:
        atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=period).average_true_range().iloc[-1]
        fiyat = df['close'].iloc[-1]
        return float((atr / fiyat) * 100)
    except Exception:
        return 1.5

def hacim_ve_likidite_kontrolu(df):
    try:
        ortalama_hacim = df['volume'].rolling(window=20).mean().iloc[-1]
        son_hacim = df['volume'].iloc[-1]
        if son_hacim < (ortalama_hacim * 0.15):
            return False
        return True
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
    return f"Güvenli Seçici Bot (Katı ADX Filtreli) | Aktif Pozisyon: {len(AKTIF_GRID_SISTEMLERI)}"

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

def pozisyonu_garantili_kapat(symbol, yon, miktar, sebep_mesaji, rsi=50, adx=25, ema_fark=0.0, basarili=True):
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
        
        exchange.create_order(symbol, 'market', kapatma_yonu, miktar, None, {'reduce_only': True})
    except Exception as e:
        print(f"Kapatma API hatası: {e}")

    yon_kod = 1 if yon == 'LONG' else -1
    sonuc_kod = 1 if basarili else 0
    ANALitik_HAFIZA["egitim_verileri"].append([rsi, adx, ema_fark, yon_kod, sonuc_kod])
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
            raw_positions = exchange.fetch_positions()
            borsa_poslari = [p for p in raw_positions if float(p.get('contracts', 0)) > 0]
        except Exception:
            borsa_poslari = []

        toplam_pnl = sum(float(p.get('unrealizedPnl', 0)) for p in borsa_poslari)
        pnl_ikon = "🟢" if toplam_pnl >= 0 else "🔴"

        basarili_sayisi = ANALitik_HAFIZA.get("basarili_islem_sayisi", 0)
        basarisiz_sayisi = ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0)
        toplam_islem = basarili_sayisi + basarisiz_sayisi
        basari_orani = (basarili_sayisi / toplam_islem * 100) if toplam_islem > 0 else 0.0

        mesaj = (
            f"📊 *ALTIN ATIŞ BOT DURUMU*\n\n"
            f"💰 Toplam Kasa: `{total:.2f} USDT`\n"
            f"{pnl_ikon} Anlık Kâr/Zarar: `{toplam_pnl:+.2f} USDT`\n"
            f"📌 Açık Pozisyon: `{len(borsa_poslari)} / {MAKSIMUM_TOPLAM_POZISYON}`\n\n"
            f"🎯 *İstatistikler:*\n"
            f"✅ Başarılı (TP): `{basarili_sayisi}` | ❌ Başarısız (SL): `{basarisiz_sayisi}`\n"
            f"📈 Başarı Oranı: `%{basari_orani:.1f}`\n\n"
        )
        
        if borsa_poslari:
            mesaj += "📋 *Aktif Pozisyonlar:*\n"
            for pos in borsa_poslari:
                sym = pos.get('symbol')
                yon = str(pos.get('side', '')).upper()
                kaldirac = pos.get('leverage', 1)
                pnl = float(pos.get('unrealizedPnl', 0))
                yuzde = float(pos.get('percentage', 0))
                isaret = "🟢" if pnl >= 0 else "🔴"
                mesaj += f"🔹 *{sym}* (`{yon}` {kaldirac}x)\n   {isaret} `{pnl:+.2f} USDT` (`%{yuzde:+.2f}`)\n"
        else:
            mesaj += "ℹ️ Açık pozisyon bulunmuyor."
            AKTIF_GRID_SISTEMLERI.clear()
            hafizayi_kaydet()

        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = True
    await update.message.reply_text("🟢 *Altın Atış Bot Aktif Edildi!*", parse_mode='Markdown')

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
        AKTIF_GRID_SISTEMLERI.clear()
        hafizayi_kaydet()
        await update.message.reply_text(f"✅ Hafıza temizlendi. (Not: {e})", parse_mode='Markdown')

# ==================== ARKA PLAN TARAYICI ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, ANALitik_HAFIZA
    print("🚀 Altın Atış Tarayıcı Devrede.")
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
                raw_positions = exchange.fetch_positions()
                aktif_borsa_map = {p['symbol']: p for p in raw_positions if float(p.get('contracts', 0)) > 0}
            except Exception:
                aktif_borsa_map = {}

            for sym in list(AKTIF_GRID_SISTEMLERI.keys()):
                if sym not in aktif_borsa_map:
                    del AKTIF_GRID_SISTEMLERI[sym]
                    hafizayi_kaydet()

            # --- TP / SL KONTROLÜ ---
            for symbol, pos in aktif_borsa_map.items():
                try:
                    guncel_fiyat = exchange.fetch_ticker(symbol)['last']
                except Exception:
                    continue

                yon = str(pos.get('side', '')).upper()
                merkez = float(pos.get('entryPrice', 0))
                kaldirac_kullanilan = int(pos.get('leverage', 5))
                
                fark = (guncel_fiyat - merkez) / merkez if yon == "LONG" else (merkez - guncel_fiyat) / merkez
                roe = fark * 100 * kaldirac_kullanilan
                pnl = float(pos.get('unrealizedPnl', 0))

                kayitli = AKTIF_GRID_SISTEMLERI.get(symbol, {})
                hedef_roe = kayitli.get("hedef_roe", 5.0)
                stop_roe = kayitli.get("stop_roe", 12.0)
                rsi_val = kayitli.get("giris_rsi", 50)
                adx_val = kayitli.get("giris_adx", 25)
                ema_fark_val = kayitli.get("ema_fark", 0.0)

                if roe >= hedef_roe or float(pos.get('percentage', 0)) >= hedef_roe:
                    ANALitik_HAFIZA["basarili_islem_sayisi"] += 1
                    hafizayi_kaydet()
                    pozisyonu_garantili_kapat(
                        symbol, yon, float(pos['contracts']), 
                        f"🎯 *KÂR ALINDI (TP)*\n\n📌 *Coin:* `{symbol}`\n📊 *Yön:* `{yon}`\n💰 *Kâr:* `+{pnl:.2f} USDT` (`%{roe:.2f}`)", 
                        rsi=rsi_val, adx=adx_val, ema_fark=ema_fark_val, basarili=True
                    )
                elif roe <= -stop_roe or float(pos.get('percentage', 0)) <= -stop_roe:
                    ANALitik_HAFIZA["basarisiz_islem_sayisi"] += 1
                    hafizayi_kaydet()
                    pozisyonu_garantili_kapat(
                        symbol, yon, float(pos['contracts']), 
                        f"🛑 *ZARAR KESİLDİ (SL)*\n\n📌 *Coin:* `{symbol}`\n📊 *Yön:* `{yon}`\n📉 *Zarar:* `{pnl:.2f} USDT` (`%{roe:.2f}`)", 
                        rsi=rsi_val, adx=adx_val, ema_fark=ema_fark_val, basarili=False
                    )

            # --- TÜM LİSTEYİ DİNAMİK TARA VE PUANLA ---
            taranan_sinyaller = []

            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU:
                    break
                if symbol in aktif_borsa_map:
                    continue

                try:
                    guncel_fiyat = exchange.fetch_ticker(symbol)['last']
                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    
                    if not hacim_ve_likidite_kontrolu(df):
                        continue

                    ema7 = ta.trend.ema_indicator(df['close'], window=7).iloc[-1]
                    ema21 = ta.trend.ema_indicator(df['close'], window=21).iloc[-1]
                    rsi = ta.momentum.rsi(df['close'], window=14).iloc[-1]
                    adx_val = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14).adx().iloc[-1]
                    atr_yuzdesi = atr_ve_volatilite_hesapla(df)
                except Exception:
                    continue

                # KATI ADX FİLTRESİ: ADX 20'nin altındaysa ve piyasa aşırı alım/satımda (Altın Atış) değilse işlemi baştan ele!
                if adx_val < 20.0 and not (rsi < 30 or rsi > 70):
                    continue

                # Dinamik Puanlama ve Sinyal Üretimi
                sinyal_puani = 50
                grid_yonu = "LONG" if ema7 > ema21 else "SHORT"

                # Trend ve Momentum Kriterleri
                if adx_val >= 20:
                    sinyal_puani += 15
                if adx_val >= 30:
                    sinyal_puani += 10

                # RSI Filtreleri ve Puan Ekleme
                if grid_yonu == "LONG":
                    if 40 <= rsi <= 58:
                        sinyal_puani += 15
                    elif rsi < 30:       # Aşırı satış (Altın Atış Adayı)
                        sinyal_puani += 25
                        grid_yonu = "LONG"
                else:  # SHORT
                    if 42 <= rsi <= 60:
                        sinyal_puani += 15
                    elif rsi > 70:       # Aşırı alım (Altın Atış Adayı)
                        sinyal_puani += 25
                        grid_yonu = "SHORT"

                # EMA Farkı Güç Puanı
                ema_fark_orani = abs(ema7 - ema21) / guncel_fiyat
                if ema_fark_orani > 0.002:
                    sinyal_puani += 10

                # ALTIN ATIŞ KONTROLÜ (90 ve üzeri puanlar)
                is_altin_atis = sinyal_puani >= 90

                ema_fark_val = float(ema7 - ema21)
                yon_kod = 1 if grid_yonu == 'LONG' else -1
                
                if not yapay_zeka_islem_onayi(rsi, adx_val, ema_fark_val, yon_kod):
                    continue

                taranan_sinyaller.append({
                    "symbol": symbol,
                    "puan": sinyal_puani,
                    "yon": grid_yonu,
                    "rsi": rsi,
                    "adx": adx_val,
                    "ema_fark": ema_fark_val,
                    "fiyat": guncel_fiyat,
                    "atr": atr_yuzdesi,
                    "altin_atis": is_altin_atis
                })

            taranan_sinyaller.sort(key=lambda x: x["puan"], reverse=True)

            # --- EN YÜKSEK POTANSİYELLİYE İŞLEM AÇ ---
            for sinyal in taranan_sinyaller:
                if not BOT_CALISIYOR_MU:
                    break

                if len(aktif_borsa_map) >= MAKSIMUM_TOPLAM_POZISYON:
                    break

                symbol = sinyal["symbol"]
                grid_yonu = sinyal["yon"]
                sinyal_puani = sinyal["puan"]
                rsi = sinyal["rsi"]
                adx_val = sinyal["adx"]
                ema_fark_val = sinyal["ema_fark"]
                guncel_fiyat = sinyal["fiyat"]
                is_altin_atis = sinyal["altin_atis"]

                ayni_yon_sayisi = sum(1 for p in aktif_borsa_map.values() if str(p.get('side', '')).upper() == grid_yonu)
                if ayni_yon_sayisi >= MAKSIMUM_AYNI_YON_SAYISI:
                    continue 

                # Altın Atış Özel Parametreleri (x20 kaldıraç, %20 TP, %10 SL)
                if is_altin_atis:
                    dinamik_kaldirac = 20
                    kasa_orani = 0.15
                    hedef_roe = 20.0
                    stop_roe = 10.0
                else:
                    dinamik_kaldirac = 7
                    kasa_orani = 0.10
                    hedef_roe = 6.0
                    stop_roe = 12.0

                try:
                    balance = exchange.fetch_balance()
                    toplam_bakiye = float(balance['total'].get('USDT', 0))
                except Exception:
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
                    
                    if miktar < min_amount:
                        miktar = min_amount

                    emir_yonu = 'buy' if grid_yonu == 'LONG' else 'sell'
                    exchange.create_order(symbol, 'market', emir_yonu, miktar)

                    AKTIF_GRID_SISTEMLERI[symbol] = {
                        "giris_rsi": rsi,
                        "giris_adx": adx_val,
                        "ema_fark": ema_fark_val,
                        "hedef_roe": hedef_roe,
                        "stop_roe": stop_roe
                    }
                    hafizayi_kaydet()
                    
                    baslik = "🌟 *ALTIN ATIŞ İŞLEMİ AÇILDI!*" if is_altin_atis else "🛡️ *STANDART İŞLEM AÇILDI*"
                    telegram_mesaj_gonder(
                        f"{baslik}\n\n"
                        f"📌 *Coin:* `{symbol}`\n"
                        f"📊 *Yön:* `{grid_yonu}` | ⭐ *Puan:* `{sinyal_puani}/100`\n"
                        f"⚙️ *Kaldıraç:* `{dinamik_kaldirac}x` | 💰 *Kasa Oranı:* `%{kasa_orani*100:.0f}`\n"
                        f"📈 *RSI:* `{rsi:.1f}` | *ADX:* `{adx_val:.1f}`\n"
                        f"🎯 *Hedef TP ROE:* `+{hedef_roe:.1f}%`\n"
                        f"🛑 *Stop SL ROE:* `-{stop_roe:.1f}%`"
                    )
                    
                    aktif_borsa_map[symbol] = {'symbol': symbol, 'side': grid_yonu, 'contracts': miktar}
                except Exception as e:
                    print(f"Market emir hatası ({symbol}): {e}")

                break

        except Exception as e:
            print(f"Tarayıcı döngü hatası: {e}")
            
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
