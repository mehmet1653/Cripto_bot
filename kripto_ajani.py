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
            print("💾 Hafıza Supabase'den başarıyla yüklendi.", flush=True)
            return {
                "aktif_sistemler": veri.get("aktif_sistemler", {}),
                "analitik": veri.get("analitik", {
                    "basarisiz_analizler": [],
                    "basarili_islem_sayisi": 0,
                    "basarisiz_islem_sayisi": 0,
                    "gunluk_net_kar_usd": 0.0,
                    "egitim_verileri": []
                }),
                "cooldownlar": veri.get("cooldownlar", {})
            }
    except Exception as e:
        print(f"⚠️ Hafıza yükleme hatası: {e}", flush=True)
        
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
    supabase.table("bot_hafiza").upsert({"id": 1, **varsayilan}).execute()
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
COOLDOWN_SURESI_SANIYE = 15 * 60  # 15 Dakika Soğuma Süresi

# ==================== YAPAY ZEKA MODELİ ====================
ai_model = RandomForestClassifier(n_estimators=100, max_depth=6, random_state=42)
ai_model_egitildi = False

def yapay_zekayi_egit_ve_guncelle():
    global ai_model, ai_model_egitildi
    veriler = ANALitik_HAFIZA.get("egitim_verileri", [])
    
    if len(veriler) < 20:
        ai_model_egitildi = False
        print(f"🧠 Yapay Zeka öğrenme aşamasında: {len(veriler)}/20 veri toplandı.", flush=True)
        return

    try:
        X = [item[:6] for item in veriler]
        y = [item[6] for item in veriler]
        if len(set(y)) < 2:
            ai_model_egitildi = False
            return
            
        ai_model.fit(np.array(X), np.array(y))
        ai_model_egitildi = True
        print("🧠 Yapay Zeka dengeli veri setiyle yeniden eğitildi!", flush=True)
    except Exception as e:
        print(f"⚠️ Yapay zeka eğitim hatası: {e}", flush=True)
        ai_model_egitildi = False

def yapay_zeka_islem_onayi(rsi, adx, ema_fark, yon_kod, atr_yuzde, coin_id, symbol):
    if not ai_model_egitildi:
        return True
    try:
        olasiliklar = ai_model.predict_proba(np.array([[rsi, adx, ema_fark, yon_kod, atr_yuzde, coin_id]]))[0]
        classes = list(ai_model.classes_)
        if 1 in classes:
            basari_ihtimali = olasiliklar[classes.index(1)]
        else:
            basari_ihtimali = 1.0

        if basari_ihtimali >= 0.35:
            print(f"[{symbol}] 🧠 Yapay Zeka Süzgeci: ONAYLANDI ✅ (İhtimal: %{basari_ihtimali*100:.1f})", flush=True)
            return True
        else:
            print(f"[{symbol}] 🧠 Yapay Zeka Süzgeci: REDDEDİLDİ ❌ (İhtimal düşük: %{basari_ihtimali*100:.1f})", flush=True)
            return False
    except Exception as e:
        print(f"⚠️ AI onay hatası: {e}", flush=True)
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

def hacim_ve_likidite_kontrolu(df):
    try:
        ortalama_hacim = df['volume'].rolling(window=20).mean().iloc[-1]
        son_hacim = df['volume'].iloc[-1]
        return son_hacim >= (ortalama_hacim * 0.10)
    except Exception:
        return True

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
    return f"Hibrit Bot Aktif | Aktif Pozisyon: {len(AKTIF_GRID_SISTEMLERI)}"

def set_leverage_safely(symbol, leverage):
    try:
        exchange.set_leverage(leverage, symbol)
        return True
    except Exception as e:
        print(f"⚠️ Kaldıraç ayarlama hatası ({symbol} - {leverage}x): {e}", flush=True)
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
        print(f"⚠️ Kapatma API hatası: {e}", flush=True)

    COIN_COOLDOWNLAR[symbol] = time.time() + COOLDOWN_SURESI_SANIYE
    print(f"⏳ [{symbol}] için 15 dakikalık cooldown (soğuma) başlatıldı.", flush=True)

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

# ==================== TELEGRAM KOMUTLARI (DETAYLI RAPOR) ====================
async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("📲 Kullanıcı /durum komutunu çalıştırdı.", flush=True)
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
                pozisyon_detaylari += f"• `{sym}` | {yon} | PnL: `{pnl_val:+.2f} USDT` (`%{roe_val:.2f}`)\n"
        else:
            pozisyon_detaylari = "\n🔍 *Açık Pozisyon:* `Yok`\n"

        mesaj = (
            f"📊 *HİBRİT BOT DURUMU*\n\n"
            f"💰 Toplam Kasa: `{total:.2f} USDT`\n"
            f"{pnl_ikon} Anlık Kâr/Zarar: `{toplam_pnl:+.2f} USDT`\n"
            f"📌 Açık Pozisyon Sayısı: `{len(borsa_poslari)} / {MAKSIMUM_TOPLAM_POZISYON}`\n"
            f"{pozisyon_detaylari}\n"
            f"🎯 *İstatistikler:*\n"
            f"✅ Başarılı (TP): `{basarili_sayisi}` | ❌ Başarısız (SL): `{basarisiz_sayisi}`\n"
            f"📈 Başarı Oranı: `%{basari_orani:.1f}`\n"
            f"🧠 Eğitim Verisi: `{len(ANALitik_HAFIZA.get('egitim_verileri', []))}/20`\n"
        )
        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        print(f"⚠️ Durum komutu hatası: {e}", flush=True)
        await update.message.reply_text(f"Hata: {e}")

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = True
    print("🟢 Bot komutla AKTİF edildi.", flush=True)
    await update.message.reply_text("🟢 *Bot Aktif Edildi!*", parse_mode='Markdown')

async def durdur_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    print("⏸️ Bot komutla DURDURULDU.", flush=True)
    await update.message.reply_text("⏸️ *Bot durduruldu.*", parse_mode='Markdown')

async def kapat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("🔄 Tüm pozisyonlar manuel kapatılıyor...", flush=True)
    await update.message.reply_text("🔄 *Tüm pozisyonlar kapatılıyor...*", parse_mode='Markdown')
    try:
        for pos in exchange.fetch_positions():
            kontrat = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
            if kontrat > 0:
                pozisyonu_garantili_kapat(pos['symbol'], str(pos.get('side', '')).upper(), kontrat, f"🛑 *MANUEL KAPATMA* - `{pos['symbol']}`", basarili=False)
        AKTIF_GRID_SISTEMLERI.clear()
        hafizayi_kaydet()
        await update.message.reply_text("✅ Tüm pozisyonlar ve hafıza temizlendi.", parse_mode='Markdown')
    except Exception as e:
        AKTIF_GRID_SISTEMLERI.clear()
        hafizayi_kaydet()
        await update.message.reply_text(f"✅ Hafıza temizlendi. (Not: {e})", parse_mode='Markdown')

# ==================== ARKA PLAN TARAYICI (TERMİNAL LOGLARI İLE) ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, ANALitik_HAFIZA
    print("🚀 Gelişmiş Hibrit Tarayıcı ve Log Sistemi Devrede.", flush=True)
    try:
        exchange.load_markets()
        yapay_zekayi_egit_ve_guncelle()
    except Exception as e:
        print(f"⚠️ Piyasalar yüklenemedi: {e}", flush=True)
    
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

            # --- AÇIK POZİSYONLARIN TP / SL VE BREAKEVEN KONTROLÜ ---
            for symbol, pos in aktif_borsa_map.items():
                try:
                    guncel_fiyat = exchange.fetch_ticker(symbol)['last']
                except Exception:
                    continue

                yon = str(pos.get('side', '')).upper()
                merkez = float(pos.get('entryPrice', 0))
                kaldirac_kullanilan = int(pos.get('leverage', 10))
                
                fark = (guncel_fiyat - merkez) / merkez if yon == "LONG" else (merkez - guncel_fiyat) / merkez
                roe = fark * 100 * kaldirac_kullanilan
                pnl = float(pos.get('unrealizedPnl', 0))
                kontrat_miktari = float(pos.get('contracts', 0) or pos.get('size', 0) or 1.0)

                kayitli = AKTIF_GRID_SISTEMLERI.get(symbol, {})
                hedef_roe = kayitli.get("hedef_roe", 20.0)
                stop_roe = kayitli.get("stop_roe", 10.0)
                breakeven_yapildi = kayitli.get("breakeven_yapildi", False)
                
                rsi_val = kayitli.get("giris_rsi", 50)
                adx_val = kayitli.get("giris_adx", 25)
                ema_fark_val = kayitli.get("ema_fark", 0.0)
                atr_val = kayitli.get("atr_yuzde", 1.5)

                # Breakeven Mantığı: Kâr %10'a ulaştığında stopu giriş fiyatına (0 noktası) sabitle
                if not breakeven_yapildi and roe >= 10.0:
                    kayitli["breakeven_yapildi"] = True
                    kayitli["stop_roe"] = 0.0  
                    hafizayi_kaydet()
                    print(f"🛡️ [{symbol}] Kâr %10'a ulaştı. Stop giriş fiyatına (0 noktasına) sabitlendi!", flush=True)
                    telegram_mesaj_gonder(f"🛡️ *Breakeven Devrede (0 Risk)*\n📌 `{symbol}` pozisyonunun stopu giriş fiyatına sabitlendi!")

                current_stop = kayitli.get("stop_roe", stop_roe)

                if roe >= hedef_roe or float(pos.get('percentage', 0)) >= hedef_roe:
                    ANALitik_HAFIZA["basarili_islem_sayisi"] += 1
                    hafizayi_kaydet()
                    print(f"🎯 KÂR ALINDI (TP): {symbol} | Yön: {yon} | PnL: +{pnl:.2f} USDT (%{roe:.2f})", flush=True)
                    pozisyonu_garantili_kapat(
                        symbol, yon, kontrat_miktari, 
                        f"🎯 *KÂR ALINDI (TP)*\n\n📌 *Coin:* `{symbol}`\n📊 *Yön:* `{yon}`\n💰 *Kâr:* `+{pnl:.2f} USDT` (`%{roe:.2f}`)", 
                        rsi=rsi_val, adx=adx_val, ema_fark=ema_fark_val, atr_yuzde=atr_val, basarili=True
                    )
                elif roe <= -current_stop and current_stop > 0:
                    ANALitik_HAFIZA["basarisiz_islem_sayisi"] += 1
                    hafizayi_kaydet()
                    print(f"🛑 ZARAR KESİLDİ (SL): {symbol} | Yön: {yon} | Sonuç: {pnl:.2f} USDT (%{roe:.2f})", flush=True)
                    pozisyonu_garantili_kapat(
                        symbol, yon, kontrat_miktari, 
                        f"🛑 *ZARAR KESİLDİ / BREAKEVEN (SL)*\n\n📌 *Coin:* `{symbol}`\n📊 *Yön:* `{yon}`\n📉 *Sonuç:* `{pnl:.2f} USDT` (`%{roe:.2f}`)", 
                        rsi=rsi_val, adx=adx_val, ema_fark=ema_fark_val, atr_yuzde=atr_val, basarili=False
                    )

            # --- TÜM COİNLERİ TARAYIP EN İYİ SİNYALİ SEÇME ---
            taranan_sinyaller = []
            su_anki_zaman = time.time()
            print("\n--- 🔍 Yeni Hibrit Piyasa Taraması Başladı ---", flush=True)

            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU:
                    break
                if symbol in aktif_borsa_map:
                    print(f"[{symbol}] Zaten açık pozisyon var, taranmıyor.", flush=True)
                    continue

                # Cooldown (Soğuma) kontrolü
                bitis_zamani = COIN_COOLDOWNLAR.get(symbol, 0)
                if su_anki_zaman < bitis_zamani:
                    kalan_dakika = int((bitis_zamani - su_anki_zaman) / 60)
                    print(f"[{symbol}] ⏳ Cooldown aktif. Kalan süre: {kalan_dakika} dk.", flush=True)
                    continue

                try:
                    guncel_fiyat = exchange.fetch_ticker(symbol)['last']
                    ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
                    df_15m = pd.DataFrame(ohlcv_15m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    
                    if not hacim_ve_likidite_kontrolu(df_15m):
                        print(f"[{symbol}] ⚠️ Hacim yetersiz, atlanıyor.", flush=True)
                        continue

                    ema7 = ta.trend.ema_indicator(df_15m['close'], window=7).iloc[-1]
                    ema21 = ta.trend.ema_indicator(df_15m['close'], window=21).iloc[-1]
                    rsi = ta.momentum.rsi(df_15m['close'], window=14).iloc[-1]
                    
                    adx_indicator = ta.trend.ADXIndicator(df_15m['high'], df_15m['low'], df_15m['close'], window=14)
                    adx_val = adx_indicator.adx().iloc[-1]
                    plus_di = adx_indicator.adx_pos().iloc[-1]
                    minus_di = adx_indicator.adx_neg().iloc[-1]
                    
                    atr_yuzdesi = atr_ve_volatilite_hesapla(df_15m)
                    fiyat_10_mum_once = df_15m['close'].iloc[-10]
                    degisim_yuzdesi = ((guncel_fiyat - fiyat_10_mum_once) / fiyat_10_mum_once) * 100
                    derinlik_durumu = emir_defteri_derinlik_analizi(symbol)
                except Exception as e:
                    print(f"[{symbol}] ⚠️ Veri çekme hatası: {e}", flush=True)
                    continue

                sinyal_puani = 50
                grid_yonu = "LONG"

                guclu_trend_var = adx_val >= 25
                trend_yonu_boga = plus_di > minus_di

                tepe_kosulu = (degisim_yuzdesi >= 2.5 and rsi > 62 and derinlik_durumu in ["SATICI_BASKIN", "DENGELI"])
                dip_kosulu = (degisim_yuzdesi <= -2.5 and rsi < 38 and derinlik_durumu in ["ALICI_BASKIN", "DENGELI"])

                if tepe_kosulu:
                    grid_yonu = "SHORT"
                    sinyal_puani = 88
                    print(f"[{symbol}] 🏔️ Tepe Tespiti (Short) | Puan: 88 | Değişim: %{degisim_yuzdesi:.1f} | RSI: {rsi:.1f}", flush=True)
                elif dip_kosulu:
                    grid_yonu = "LONG"
                    sinyal_puani = 88
                    print(f"[{symbol}] 🎯 Dip Tespiti (Long) | Puan: 88 | Değişim: %{degisim_yuzdesi:.1f} | RSI: {rsi:.1f}", flush=True)
                elif guclu_trend_var:
                    grid_yonu = "LONG" if trend_yonu_boga else "SHORT"
                    sinyal_puani = 75
                    print(f"[{symbol}] 📈 Güçlü Trend ({grid_yonu}) | ADX: {adx_val:.1f} | Puan: 75", flush=True)
                else:
                    grid_yonu = "LONG" if ema7 > ema21 else "SHORT"
                    if adx_val >= 18:
                        sinyal_puani += 15
                    if grid_yonu == "LONG" and rsi < 50:
                        sinyal_puani += 20
                    elif grid_yonu == "SHORT" and rsi > 50:
                        sinyal_puani += 20
                    print(f"[{symbol}] 📊 Yatay/Zayıf Trend ({grid_yonu}) | Puan: {sinyal_puani} | RSI: {rsi:.1f}", flush=True)

                is_altin_atis = sinyal_puani >= 85
                ema_fark_val = float(ema7 - ema21)
                yon_kod = 1 if grid_yonu == 'LONG' else -1
                coin_id = COIN_ID_MAP.get(symbol, 0)
                
                if not yapay_zeka_islem_onayi(rsi, adx_val, ema_fark_val, yon_kod, atr_yuzdesi, coin_id, symbol):
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

            # Puanı en yüksek olan en iyi sinyali en üste al
            taranan_sinyaller.sort(key=lambda x: x["puan"], reverse=True)

            # --- EN İYİ SİNYAL İLE İŞLEM AÇMA ---
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

                if sinyal_puani < 65 and not is_altin_atis:
                    print(f"[{symbol}] Sinyal puanı ({sinyal_puani}) eşik değerin altında, işlem açılmadı.", flush=True)
                    continue

                if len(aktif_borsa_map) >= MAKSIMUM_TOPLAM_POZISYON:
                    print("⚠️ Maksimum toplam pozisyon sınırına ulaşıldı.", flush=True)
                    break

                ayni_yon_sayisi = sum(1 for p in aktif_borsa_map.values() if str(p.get('side', '')).upper() == grid_yonu)
                if ayni_yon_sayisi >= MAKSIMUM_AYNI_YON_SAYISI:
                    print(f"⚠️ Aynı yönde ({grid_yonu}) maksimum pozisyon sınırına ulaşıldı.", flush=True)
                    continue 

                # Kurallar: Normal işlem (%20 kâr, %10 zarar, 10x, %20 kasa payı) / Altın atış (%25 kâr, %12.5 zarar, 20x, %25 kasa payı)
                if is_altin_atis:
                    dinamik_kaldirac = 20
                    kasa_orani = 0.25
                    hedef_roe = 25.0
                    stop_roe = 12.5
                else:
                    dinamik_kaldirac = 10
                    kasa_orani = 0.20
                    hedef_roe = 20.0
                    stop_roe = 10.0

                try:
                    balance = exchange.fetch_balance()
                    toplam_bakiye = float(balance['total'].get('USDT', 0))
                except Exception:
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
                    
                    emir_yonu = 'buy' if grid_yonu == 'LONG' else 'sell'
                    exchange.create_order(symbol, 'market', emir_yonu, miktar)

                    AKTIF_GRID_SISTEMLERI[symbol] = {
                        "giris_rsi": rsi,
                        "giris_adx": adx_val,
                        "ema_fark": ema_fark_val,
                        "atr_yuzde": atr_yuzdesi,
                        "hedef_roe": hedef_roe,
                        "stop_roe": stop_roe,
                        "breakeven_yapildi": False
                    }
                    hafizayi_kaydet()
                    
                    print(f"🚀 İŞLEM AÇILDI: {symbol} | Yön: {grid_yonu} | Puan: {sinyal_puani} | Kaldıraç: {dinamik_kaldirac}x | Kasa Oranı: %{kasa_orani*100}", flush=True)
                    telegram_mesaj_gonder(
                        f"⚡ *EN İYİ SİNYAL İŞLEMİ AÇILDI*\n\n"
                        f"📌 *Coin:* `{symbol}`\n"
                        f"📊 *Yön:* `{grid_yonu}` | ⭐ *Puan:* `{sinyal_puani}/100`\n"
                        f"⚙️ *Kaldıraç:* `{dinamik_kaldirac}x` | 💰 *Kasa Oranı:* `%{kasa_orani*100:.0f}`\n"
                        f"🎯 *Hedef TP:* `+{hedef_roe}%` | *Stop SL:* `-{stop_roe}%`"
                    )
                    break 
                except Exception as e:
                    print(f"❌ Emir açma hatası ({symbol}): {e}", flush=True)

        except Exception as e:
            print(f"⚠️ Tarayıcı döngü genel hatası: {e}", flush=True)
            
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
