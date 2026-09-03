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
    'SOL/USDT:USDT', 'AVAX/USDT:USDT', 'XRP/USDT:USDT', 'DOGE/USDT:USDT', 'SUI/USDT:USDT'
]

COIN_ID_MAP = {
    'SOL/USDT:USDT': 1,
    'AVAX/USDT:USDT': 2,
    'XRP/USDT:USDT': 3,
    'DOGE/USDT:USDT': 4,
    'SUI/USDT:USDT': 5
}

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

# ==================== YAPAY ZEKA MODELİ ====================
ai_model = RandomForestClassifier(n_estimators=100, max_depth=6, random_state=42)
ai_model_egitildi = False

def yapay_zekayi_egit_ve_guncelle():
    global ai_model, ai_model_egitildi
    veriler = ANALitik_HAFIZA.get("egitim_verileri", [])
    
    if len(veriler) < 3:
        ai_model_egitildi = False
        print(f"🧠 Yapay Zeka gözlem modunda: {len(veriler)}/3 veri toplandı.")
        return

    try:
        X = [item[:6] for item in veriler]
        y = [item[6] for item in veriler]
        if len(set(y)) < 2:
            ai_model_egitildi = False
            return
            
        ai_model.fit(np.array(X), np.array(y))
        ai_model_egitildi = True
        print("🧠 Yapay Zeka (Esnek Piyasa Modlu) optimize edildi!")
    except Exception as e:
        print(f"Yapay zeka eğitim hatası: {e}")
        ai_model_egitildi = False

def yapay_zeka_islem_onayi(rsi, adx, ema_fark, yon_kod, atr_yuzde, coin_id, symbol):
    if not ai_model_egitildi:
        return True
    try:
        tahmin = ai_model.predict(np.array([[rsi, adx, ema_fark, yon_kod, atr_yuzde, coin_id]]))[0]
        sonuc = bool(tahmin == 1)
        if sonuc:
            print(f"[{symbol}] 🧠 Yapay Zeka Süzgeci: ONAYLANDI ✅")
        else:
            print(f"[{symbol}] 🧠 Yapay Zeka Süzgeci: REDDEDİLDİ ❌")
        return sonuc
    except Exception as e:
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
    return f"Profesyonel Altın Atış Botu Aktif | Aktif Pozisyon: {len(AKTIF_GRID_SISTEMLERI)}"

def set_leverage_safely(symbol, leverage):
    try:
        exchange.set_leverage(leverage, symbol)
        return True
    except Exception as e:
        print(f"Kaldıraç ayarlama hatası ({symbol} - {leverage}x): {e}")
        return False

def pozisyonu_garantili_kapat(symbol, yon, miktar, sebep_mesaji, rsi=50, adx=25, ema_fark=0.0, atr_yuzde=1.5, basarili=True):
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
    coin_id = COIN_ID_MAP.get(symbol, 0)
    
    ANALitik_HAFIZA["egitim_verileri"].append([rsi, adx, ema_fark, yon_kod, atr_yuzde, coin_id, sonuc_kod])
    if len(ANALitik_HAFIZA["egitim_verileri"]) > 150:
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
            borsa_poslari = []
            for p in raw_positions:
                kontrat = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                if kontrat > 0:
                    borsa_poslari.append(p)
        except Exception as e:
            print(f"Pozisyon çekme hatası: {e}")
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
                pozisyon_detaylari += f"• `{sym}` | {yon} | PnL: `{pnl_val:+.2f} USDT` (`%{roe_val:.2f}`)\n"
        else:
            pozisyon_detaylari = "\n🔍 *Açık Pozisyon:* `Yok`\n"

        mesaj = (
            f"📊 *PROFESYONEL BOT DURUMU*\n\n"
            f"💰 Toplam Kasa: `{total:.2f} USDT`\n"
            f"{pnl_ikon} Anlık Kâr/Zarar: `{toplam_pnl:+.2f} USDT`\n"
            f"📌 Açık Pozisyon Sayısı: `{len(borsa_poslari)} / {MAKSIMUM_TOPLAM_POZISYON}`\n"
            f"{pozisyon_detaylari}\n"
            f"🎯 *İstatistikler:*\n"
            f"✅ Başarılı (TP): `{basarili_sayisi}` | ❌ Başarısız (SL): `{basarisiz_sayisi}`\n"
            f"📈 Başarı Oranı: `%{basari_orani:.1f}`\n"
            f"🧠 Eğitim Verisi: `{len(ANALitik_HAFIZA.get('egitim_verileri', []))}/3`\n"
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
                pozisyonu_garantili_kapat(pos['symbol'], str(pos.get('side', '')).upper(), kontrat, f"🛑 *MANUEL KAPATMA* - `{pos['symbol']}`", basarili=False)
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
    print("🚀 Profesyonel Tarayıcı Devrede.")
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
                aktif_borsa_map = {}
                for p in raw_positions:
                    kontrat = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                    if kontrat > 0:
                        aktif_borsa_map[p['symbol']] = p
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
                kontrat_miktari = float(pos.get('contracts', 0) or pos.get('size', 0) or 1.0)

                kayitli = AKTIF_GRID_SISTEMLERI.get(symbol, {})
                hedef_roe = kayitli.get("hedef_roe", 15.0)
                stop_roe = kayitli.get("stop_roe", 10.0)
                rsi_val = kayitli.get("giris_rsi", 50)
                adx_val = kayitli.get("giris_adx", 25)
                ema_fark_val = kayitli.get("ema_fark", 0.0)
                atr_val = kayitli.get("atr_yuzde", 1.5)

                if roe >= hedef_roe or float(pos.get('percentage', 0)) >= hedef_roe:
                    ANALitik_HAFIZA["basarili_islem_sayisi"] += 1
                    hafizayi_kaydet()
                    pozisyonu_garantili_kapat(
                        symbol, yon, kontrat_miktari, 
                        f"🎯 *KÂR ALINDI (TP)*\n\n📌 *Coin:* `{symbol}`\n📊 *Yön:* `{yon}`\n💰 *Kâr:* `+{pnl:.2f} USDT` (`%{roe:.2f}`)", 
                        rsi=rsi_val, adx=adx_val, ema_fark=ema_fark_val, atr_yuzde=atr_val, basarili=True
                    )
                elif roe <= -stop_roe or float(pos.get('percentage', 0)) <= -stop_roe:
                    ANALitik_HAFIZA["basarisiz_islem_sayisi"] += 1
                    hafizayi_kaydet()
                    pozisyonu_garantili_kapat(
                        symbol, yon, kontrat_miktari, 
                        f"🛑 *ZARAR KESİLDİ (SL)*\n\n📌 *Coin:* `{symbol}`\n📊 *Yön:* `{yon}`\n📉 *Zarar:* `{pnl:.2f} USDT` (`%{roe:.2f}`)", 
                        rsi=rsi_val, adx=adx_val, ema_fark=ema_fark_val, atr_yuzde=atr_val, basarili=False
                    )

            # --- DİNAMİK TARAMA VE ÇOKLU ZAMAN DİLİMİ (4H) ANALİZİ ---
            taranan_sinyaller = []
            print("\n--- Yeni Profesyonel Tarama Döngüsü Başladı ---")

            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU:
                    break
                if symbol in aktif_borsa_map:
                    continue

                try:
                    guncel_fiyat = exchange.fetch_ticker(symbol)['last']
                    
                    ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
                    df_15m = pd.DataFrame(ohlcv_15m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    
                    if not hacim_ve_likidite_kontrolu(df_15m):
                        continue

                    ohlcv_4h = exchange.fetch_ohlcv(symbol, timeframe='4h', limit=20)
                    df_4h = pd.DataFrame(ohlcv_4h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    rsi_4h = ta.momentum.rsi(df_4h['close'], window=14).iloc[-1]
                    
                    ema7 = ta.trend.ema_indicator(df_15m['close'], window=7).iloc[-1]
                    ema21 = ta.trend.ema_indicator(df_15m['close'], window=21).iloc[-1]
                    rsi = ta.momentum.rsi(df_15m['close'], window=14).iloc[-1]
                    adx_val = ta.trend.ADXIndicator(df_15m['high'], df_15m['low'], df_15m['close'], window=14).adx().iloc[-1]
                    atr_yuzdesi = atr_ve_volatilite_hesapla(df_15m)
                except Exception as e:
                    print(f"[{symbol}] Veri çekme hatası: {e}")
                    continue

                if adx_val < 20.0 and not (rsi < 30 or rsi > 70):
                    continue

                sinyal_puani = 50
                grid_yonu = "LONG" if ema7 > ema21 else "SHORT"

                if grid_yonu == "LONG" and rsi_4h > 75:
                    sinyal_puani -= 20
                    print(f"[{symbol}] ⚠️ 4h RSI aşırı yüksek ({rsi_4h:.1f}), Long için puan kırpıldı (-20).")
                elif grid_yonu == "SHORT" and rsi_4h < 25:
                    sinyal_puani -= 20
                    print(f"[{symbol}] ⚠️ 4h RSI aşırı düşük ({rsi_4h:.1f}), Short için puan kırpıldı (-20).")

                if adx_val >= 20:
                    sinyal_puani += 15
                if adx_val >= 30:
                    sinyal_puani += 10

                if grid_yonu == "LONG":
                    if 40 <= rsi <= 58:
                        sinyal_puani += 15
                    elif rsi < 30:
                        sinyal_puani += 25
                else:
                    if 42 <= rsi <= 60:
                        sinyal_puani += 15
                    elif rsi > 70:
                        sinyal_puani += 25

                ema_fark_orani = abs(ema7 - ema21) / guncel_fiyat
                if ema_fark_orani > 0.002:
                    sinyal_puani += 10

                is_altin_atis = sinyal_puani >= 90
                ema_fark_val = float(ema7 - ema21)
                yon_kod = 1 if grid_yonu == 'LONG' else -1
                coin_id = COIN_ID_MAP.get(symbol, 0)
                
                ai_onay = yapay_zeka_islem_onayi(rsi, adx_val, ema_fark_val, yon_kod, atr_yuzdesi, coin_id, symbol)
                if not ai_onay:
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
                    "altin_atis": is_altin_atis,
                    "rsi_4h": rsi_4h
                })

            taranan_sinyaller.sort(key=lambda x: x["puan"], reverse=True)

            # --- İŞLEM AÇMA MANTIĞI ---
            for sinyal in taranan_sinyaller:
                if not BOT_CALISIYOR_MU:
                    break

                symbol = sinyal["symbol"]
                grid_yonu = sinyal["yon"]
                sinyal_puani = sinyal["puan"]
                rsi = sinyal["rsi"]
                adx_val = sinyal["adx"]
                ema_fark_val = sinyal["ema_fark"]
                guncel_fiyat = sinyal["fiyat"]
                atr_yuzdesi = sinyal["atr"]
                is_altin_atis = sinyal["altin_atis"]
                rsi_4h = sinyal["rsi_4h"]

                if sinyal_puani < 70 and not is_altin_atis:
                    print(f"[{symbol}] ❌ Esnek süzgeç/puan kırpılması sonrası puan yetersiz ({sinyal_puani}), atlanıyor.")
                    continue

                # Puan 100 (mutlak zirve) ise maksimum pozisyon ve aynı yön sınırlarını es geç/baypas et
                if sinyal_puani < 100:
                    if len(aktif_borsa_map) >= MAKSIMUM_TOPLAM_POZISYON:
                        continue

                    ayni_yon_sayisi = sum(1 for p in aktif_borsa_map.values() if str(p.get('side', '')).upper() == grid_yonu)
                    if ayni_yon_sayisi >= MAKSIMUM_AYNI_YON_SAYISI:
                        continue 

                if (grid_yonu == "LONG" and rsi_4h > 75) or (grid_yonu == "SHORT" and rsi_4h < 25):
                    dinamik_kaldirac = 5
                    kasa_orani = 0.05
                    hedef_roe = 12.0
                    stop_roe = 7.0
                    print(f"[{symbol}] 🛡️ Riskli bölge tespiti: Kaldıraç 5x ve düşük bakiye oranı (%5) ile temkinli giriliyor.")
                elif is_altin_atis and sinyal_puani == 100:
                    dinamik_kaldirac = 20
                    kasa_orani = 0.20
                    hedef_roe = 25.0
                    stop_roe = 10.0
                elif is_altin_atis:
                    dinamik_kaldirac = 20
                    kasa_orani = 0.15
                    hedef_roe = 20.0
                    stop_roe = 10.0
                else:
                    dinamik_kaldirac = 10
                    kasa_orani = 0.10
                    hedef_roe = 15.0
                    stop_roe = 10.0

                try:
                    balance = exchange.fetch_balance()
                    toplam_bakiye = float(balance['total'].get('USDT', 0))
                except Exception as e:
                    continue

                if not set_leverage_safely(symbol, dinamik_kaldirac):
                    continue
                
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
                        "atr_yuzde": atr_yuzdesi,
                        "hedef_roe": hedef_roe,
                        "stop_roe": stop_roe
                    }
                    hafizayi_kaydet()
                    
                    telegram_mesaj_gonder(
                        f"🛡️ *ESNEK DÖNGÜ İŞLEMİ AÇILDI*\n\n"
                        f"📌 *Coin:* `{symbol}`\n"
                        f"📊 *Yön:* `{grid_yonu}` | ⭐ *Puan:* `{sinyal_puani}/100`\n"
                        f"⚙️ *Kaldıraç:* `{dinamik_kaldirac}x` | 💰 *Kasa Oranı:* `%{kasa_orani*100:.0f}`\n"
                        f"📈 *15m RSI:* `{rsi:.1f}` | *4h RSI:* `{rsi_4h:.1f}`\n"
                        f"🎯 *Hedef TP ROE:* `+{hedef_roe:.1f}%`"
                    )
                    print(f"🎯 İŞLEM BAŞARIYLA AÇILDI: {symbol} - {grid_yonu}")
                    aktif_borsa_map[symbol] = {'symbol': symbol, 'side': grid_yonu, 'contracts': miktar}
                except Exception as e:
                    print(f"❌ Emir açma hatası ({symbol}): {e}")

                break

        except Exception as e:
            print(f"Tarayıcı döngü genel hatası: {e}")
            
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
