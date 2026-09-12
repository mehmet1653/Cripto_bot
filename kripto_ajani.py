import os
import time
import threading
import requests
import ccxt
import pandas as pd
import ta
import numpy as np
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler
from sklearn.ensemble import RandomForestClassifier
from supabase import create_client, Client

# ==================== AYARLAR VE ANAHTARLAR ====================
TELEGRAM_TOKEN = "8870934003:AAGOzmO_VwYnj0Wz2hehI176rKiOkEaV0b0"
CHAT_ID = "6929517567"

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://rllpcylzhptqwzmzehnv.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "Sb_secret_ln9y67Ep_zCtOQ9Q2NE8KQ_nf0gKkmO")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

exchange = ccxt.gate({
    'apiKey': '82cca880898a88d1a31e86d8eb474c57',
    'secret': '1ac479b9df5e6f2e89560b0d238a250694719b6fcae20da00ebc54ad6aeb8898',
    'enableRateLimit': True,
    'options': {
        'defaultType': 'swap'
    }
})

exchange.set_sandbox_mode(True)

TAKIP_EDILENLER = [
    'SOL/USDT:USDT', 
    'XRP/USDT:USDT', 
    'DOGE/USDT:USDT', 
    'LTC/USDT:USDT', 
    'LINK/USDT:USDT', 
    'ADA/USDT:USDT', 
    'UNI/USDT:USDT'
]

COIN_ID_MAP = {
    'SOL/USDT:USDT': 1,
    'XRP/USDT:USDT': 2,
    'DOGE/USDT:USDT': 3,
    'LTC/USDT:USDT': 4,
    'LINK/USDT:USDT': 5,
    'ADA/USDT:USDT': 6,
    'UNI/USDT:USDT': 7
}

BOT_CALISIYOR_MU = True
state_lock = threading.Lock()

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
    try:
        supabase.table("bot_hafiza").upsert({"id": 1, **varsayilan}).execute()
    except Exception as e:
        print(f"⚠️ Hafıza tablo oluşturma/ilk kayıt hatası: {e}", flush=True)
    return varsayilan

def hafizayi_kaydet():
    with state_lock:
        try:
            clean_egitim = []
            for item in ANALitik_HAFIZA.get("egitim_verileri", []):
                clean_egitim.append([
                    float(item[0]), float(item[1]), float(item[2]), 
                    int(item[3]), float(item[4]), int(item[5]), int(item[6])
                ])
            
            payload_analitik = {
                "basarisiz_analizler": ANALitik_HAFIZA.get("basarisiz_analizler", []),
                "basarili_islem_sayisi": int(ANALitik_HAFIZA.get("basarili_islem_sayisi", 0)),
                "basarisiz_islem_sayisi": int(ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0)),
                "gunluk_net_kar_usd": float(ANALitik_HAFIZA.get("gunluk_net_kar_usd", 0.0)),
                "egitim_verileri": clean_egitim
            }

            clean_cooldowns = {k: float(v) for k, v in COIN_COOLDOWNLAR.items()}

            supabase.table("bot_hafiza").upsert({
                "id": 1,
                "aktif_sistemler": AKTIF_GRID_SISTEMLERI,
                "analitik": payload_analitik,
                "cooldownlar": clean_cooldowns
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
    with state_lock:
        veriler = list(ANALitik_HAFIZA.get("egitim_verileri", []))
    
    if len(veriler) < 20:
        ai_model_egitildi = False
        return
    try:
        X = [item[:6] for item in veriler]
        y = [item[6] for item in veriler]
        if len(set(y)) < 2:
            ai_model_egitildi = False
            return
        ai_model.fit(np.array(X), np.array(y))
        ai_model_egitildi = True
    except Exception as e:
        print(f"⚠️ Yapay zeka eğitim hatası: {e}", flush=True)
        ai_model_egitildi = False

def yapay_zeka_islem_onayi(rsi, adx, ema_fark, yon_kod, atr_yuzde, coin_id, symbol):
    if not ai_model_egitildi:
        return True
    try:
        olasiliklar = ai_model.predict_proba(np.array([[float(rsi), float(adx), float(ema_fark), int(yon_kod), float(atr_yuzde), int(coin_id)]]))[0]
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
        return 1.5

def emir_defteri_derinlik_analizi(symbol):
    try:
        order_book = exchange.fetch_order_book(symbol, limit=20)
        bids, asks = order_book.get('bids', []), order_book.get('asks', [])
        toplam_bid = sum(b[1] for b in bids)
        toplam_ask = sum(a[1] for a in asks)
        if toplam_bid + toplam_ask == 0: return "DENGELI"
        bid_orani = toplam_bid / (toplam_bid + toplam_ask)
        return "ALICI_BASKIN" if bid_orani > 0.55 else ("SATICI_BASKIN" if bid_orani < 0.45 else "DENGELI")
    except Exception:
        return "DENGELI"

def hacim_ve_likidite_kontrolu(df):
    try:
        return df['volume'].iloc[-1] >= (df['volume'].rolling(window=20).mean().iloc[-1] * 0.10)
    except Exception:
        return True

def sinyal_hala_gecerli_mi(symbol, yon):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=30)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        ema7 = ta.trend.ema_indicator(df['close'], window=7).iloc[-1]
        ema21 = ta.trend.ema_indicator(df['close'], window=21).iloc[-1]
        rsi = ta.momentum.rsi(df['close'], window=14).iloc[-1]

        if yon == 'LONG' and ema7 < ema21 and rsi < 45: return False
        elif yon == 'SHORT' and ema7 > ema21 and rsi > 55: return False
    except Exception:
        pass
    return True

def telegram_mesaj_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": mesaj, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"⚠️ Telegram Gönderme Hatası: {e}", flush=True)

def set_leverage_safely(symbol, leverage):
    try:
        exchange.set_leverage(leverage, symbol)
        return True
    except Exception as e:
        print(f"⚠️ Kaldıraç hatası ({symbol}): {e}", flush=True)
        return False

def pozisyonu_garantili_kapat(symbol, yon, miktar, sebep_mesaji, rsi=50, adx=25, ema_fark=0.0, atr_yuzde=1.5, basarili=True):
    kapatma_yonu = 'sell' if yon == 'LONG' else 'buy'
    try:
        for ord_item in exchange.fetch_open_orders(symbol):
            exchange.cancel_order(ord_item['id'], symbol)
    except Exception: pass

    try:
        market_info = exchange.market(symbol)
        min_amount = float(market_info['limits']['amount']['min'] or 1.0)
        if miktar < min_amount: miktar = min_amount
        miktar = float(exchange.amount_to_precision(symbol, miktar))
        exchange.create_order(symbol, 'market', kapatma_yonu, miktar, None, {'reduceOnly': True})
    except Exception as e:
        print(f"⚠️ Kapatma API hatası: {e}", flush=True)

    with state_lock:
        COIN_COOLDOWNLAR[symbol] = float(time.time() + COOLDOWN_SURESI_SANIYE)
        ANALitik_HAFIZA["egitim_verileri"].append([
            float(rsi), float(adx), float(ema_fark), 
            int(1 if yon == 'LONG' else -1), float(atr_yuzde), 
            int(COIN_ID_MAP.get(symbol, 0)), int(1 if basarili else 0)
        ])
        if len(ANALitik_HAFIZA["egitim_verileri"]) > 150: 
            ANALitik_HAFIZA["egitim_verileri"].pop(0)

        if symbol in AKTIF_GRID_SISTEMLERI:
            del AKTIF_GRID_SISTEMLERI[symbol]

    yapay_zekayi_egit_ve_guncelle()
    hafizayi_kaydet()

    if sebep_mesaji: telegram_mesaj_gonder(sebep_mesaji)

# ==================== TELEGRAM KOMUTLARI ====================
async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("📲 Telegram /durum komutu alındı.", flush=True)
    try:
        balance = exchange.fetch_balance()
        total = float(balance['total'].get('USDT', 0))
        borsa_poslari = [p for p in exchange.fetch_positions() if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
        toplam_pnl = sum(float(p.get('unrealizedPnl', 0)) for p in borsa_poslari)
        
        with state_lock:
            basarili_s = int(ANALitik_HAFIZA.get("basarili_islem_sayisi", 0))
            basarisiz_s = int(ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0))
            egitim_uzunluk = len(ANALitik_HAFIZA.get("egitim_verileri", []))

        toplam_i = basarili_s + basarisiz_s
        basari_o = (basarili_s / toplam_i * 100) if toplam_i > 0 else 0.0

        pnl_ikon = "🟢" if toplam_pnl >= 0 else "🔴"
        mesaj = (
            f"📊 *HİBRİT BOT DURUMU*\n\n"
            f"💰 Toplam Kasa: `{total:.2f} USDT`\n"
            f"{pnl_ikon} Anlık PnL: `{toplam_pnl:+.2f} USDT`\n"
            f"📌 Açık Pozisyon: `{len(borsa_poslari)} / {MAKSIMUM_TOPLAM_POZISYON}`\n\n"
        )

        if borsa_poslari:
            mesaj += "📋 *Açık Pozisyonlar Detay:*\n"
            for p in borsa_poslari:
                sym = p.get('symbol')
                yon = str(p.get('side', '')).upper()
                merkez = float(p.get('entryPrice', 0))
                kaldirac = int(p.get('leverage', 10))
                pnl_val = float(p.get('unrealizedPnl', 0))
                
                try:
                    guncel_fiyat = exchange.fetch_ticker(sym)['last']
                    fark = (guncel_fiyat - merkez) / merkez if yon == "LONG" else (merkez - guncel_fiyat) / merkez
                    roe = fark * 100 * kaldirac
                except Exception:
                    roe = 0.0

                pos_ikon = "🟢" if pnl_val >= 0 else "🔴"
                mesaj += f"{pos_ikon} `{sym}` | {yon} ({kaldirac}x)\n   └ PnL: `{pnl_val:+.2f} USDT` (`%{roe:+.2f}`)\n"
            mesaj += "\n"

        mesaj += (
            f"✅ Başarılı TP: `{basarili_s}` | ❌ Başarısız SL: `{basarisiz_s}`\n"
            f"📈 Başarı Oranı: `%{basari_o:.1f}`\n"
            f"🧠 AI Verisi: `{egitim_uzunluk}/20`"
        )
        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        print(f"⚠️ Durum komutu hatası: {e}", flush=True)
        await update.message.reply_text(f"⚠️ Durum hatası: {e}")

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = True
    print("📲 Telegram /baslat komutu alındı.", flush=True)
    await update.message.reply_text("🟢 *Bot Aktif Edildi!*", parse_mode='Markdown')

async def durdur_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    print("📲 Telegram /durdur komutu alındı.", flush=True)
    await update.message.reply_text("⏸️ *Bot durduruldu.*", parse_mode='Markdown')

async def kapat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("📲 Telegram /kapat komutu alındı.", flush=True)
    await update.message.reply_text("🔄 Tüm pozisyonlar kapatılıyor...", parse_mode='Markdown')
    try:
        for pos in exchange.fetch_positions():
            kontrat = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
            if kontrat > 0:
                pozisyonu_garantili_kapat(pos['symbol'], str(pos.get('side', '')).upper(), kontrat, f"🛑 *MANUEL KAPATMA* - `{pos['symbol']}`", basarili=False)
        
        with state_lock:
            AKTIF_GRID_SISTEMLERI.clear()
        hafizayi_kaydet()
        await update.message.reply_text("✅ Tüm pozisyonlar kapatıldı.", parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"⚠️ Kapatma hatası: {e}")

# ==================== ARKA PLAN TARAYICI ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, ANALitik_HAFIZA
    print("🚀 Gelişmiş Hibrit Tarayıcı Arka Plan Döngüsü Başlatıldı.", flush=True)
    try:
        exchange.load_markets()
        yapay_zekayi_egit_ve_guncelle()
    except Exception as e:
        print(f"⚠️ Piyasalar yüklenirken hata: {e}", flush=True)
    
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5)
                continue

            print("🔄 Döngü taraması yapılıyor...", flush=True)

            try:
                raw_positions = exchange.fetch_positions()
                aktif_borsa_map = {p['symbol']: p for p in raw_positions if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0}
            except Exception as e:
                print(f"⚠️ Pozisyonlar çekilemedi: {e}", flush=True)
                aktif_borsa_map = {}

            with state_lock:
                for sym in list(AKTIF_GRID_SISTEMLERI.keys()):
                    if sym not in aktif_borsa_map:
                        del AKTIF_GRID_SISTEMLERI[sym]
                        hafizayi_kaydet()

            for symbol, pos in aktif_borsa_map.items():
                try:
                    ticker = exchange.fetch_ticker(symbol)
                    guncel_fiyat = ticker.get('last')
                    if not guncel_fiyat:
                        continue
                except Exception: continue

                yon = str(pos.get('side', '')).upper()
                merkez = float(pos.get('entryPrice', 0))
                kaldirac = int(pos.get('leverage', 10))
                
                fark = (guncel_fiyat - merkez) / merkez if yon == "LONG" else (merkez - guncel_fiyat) / merkez
                roe = fark * 100 * kaldirac
                pnl = float(pos.get('unrealizedPnl', 0))
                kontrat = float(pos.get('contracts', 0) or pos.get('size', 0) or 1.0)

                with state_lock:
                    kayitli = AKTIF_GRID_SISTEMLERI.get(symbol, {})
                hedef_roe = kayitli.get("hedef_roe", 20.0)
                stop_roe = kayitli.get("stop_roe", 10.0)

                print(f"🔍 [POZİSYON] {symbol} | Yön: {yon} | RoE: %{roe:+.2f} | PnL: {pnl:+.2f} USDT", flush=True)

                if not sinyal_hala_gecerli_mi(symbol, yon):
                    basarili_mi = pnl > 0
                    with state_lock:
                        if basarili_mi:
                            ANALitik_HAFIZA["basarili_islem_sayisi"] = int(ANALitik_HAFIZA.get("basarili_islem_sayisi", 0)) + 1
                        else:
                            ANALitik_HAFIZA["basarisiz_islem_sayisi"] = int(ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0)) + 1
                    hafizayi_kaydet()
                    pozisyonu_garantili_kapat(symbol, yon, kontrat, f"🧠 *AKILLI ERKEN ÇIKIŞ*\n📌 `{symbol}` | Sinyal bozuldu. PnL: `{pnl:+.2f} USDT` (`%{roe:+.2f}`)", basarili=basarili_mi)
                    continue

                if roe >= hedef_roe:
                    with state_lock:
                        ANALitik_HAFIZA["basarili_islem_sayisi"] = int(ANALitik_HAFIZA.get("basarili_islem_sayisi", 0)) + 1
                    hafizayi_kaydet()
                    pozisyonu_garantili_kapat(symbol, yon, kontrat, f"🎯 *KÂR ALINDI (TP)*\n📌 `{symbol}` | Kâr: `+{pnl:.2f} USDT` (`%{roe:.2f}`)", basarili=True)
                elif roe <= -stop_roe:
                    with state_lock:
                        ANALitik_HAFIZA["basarisiz_islem_sayisi"] = int(ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0)) + 1
                    hafizayi_kaydet()
                    pozisyonu_garantili_kapat(symbol, yon, kontrat, f"🛑 *ZARAR KESİLDİ (SL)*\n📌 `{symbol}` | Zarar: `{pnl:.2f} USDT` (`%{roe:.2f}`)", basarili=False)

            taranan_sinyaller = []

            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU: break
                if symbol in aktif_borsa_map:
                    continue
                
                with state_lock:
                    cooldown_sure = COIN_COOLDOWNLAR.get(symbol, 0)
                if time.time() < cooldown_sure:
                    continue

                try:
                    ticker = exchange.fetch_ticker(symbol)
                    guncel_fiyat = ticker.get('last')
                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
                    if not guncel_fiyat or not ohlcv:
                        continue
                        
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    if not hacim_ve_likidite_kontrolu(df):
                        continue

                    ema7 = ta.trend.ema_indicator(df['close'], window=7).iloc[-1]
                    ema21 = ta.trend.ema_indicator(df['close'], window=21).iloc[-1]
                    rsi = ta.momentum.rsi(df['close'], window=14).iloc[-1]
                    adx_ind = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14)
                    adx_val = adx_ind.adx().iloc[-1]
                    
                    degisim = ((guncel_fiyat - df['close'].iloc[-10]) / df['close'].iloc[-10]) * 100
                    derinlik = emir_defteri_derinlik_analizi(symbol)
                    atr = atr_ve_volatilite_hesapla(df)
                except Exception as e:
                    print(f"⚠️ Veri çekme hatası ({symbol}): {e}", flush=True)
                    continue

                sinyal_puani, grid_yonu = 50, "LONG"
                
                if adx_val < 22 and degisim >= 2.5 and rsi > 65 and derinlik in ["SATICI_BASKIN", "DENGELI"]:
                    grid_yonu, sinyal_puani = "SHORT", 85
                elif adx_val < 22 and degisim <= -2.5 and rsi < 35 and derinlik in ["ALICI_BASKIN", "DENGELI"]:
                    grid_yonu, sinyal_puani = "LONG", 85
                elif adx_val >= 25:
                    grid_yonu = "LONG" if adx_ind.adx_pos().iloc[-1] > adx_ind.adx_neg().iloc[-1] else "SHORT"
                    sinyal_puani = 75
                else:
                    grid_yonu = "LONG" if ema7 > ema21 else "SHORT"
                    sinyal_puani = 60

                print(f"📊 [ANALİZ] {symbol} | Yön: {grid_yonu} | Puan: {sinyal_puani} | RSI: {rsi:.1f} | ADX: {adx_val:.1f}", flush=True)

                if not yapay_zeka_islem_onayi(rsi, adx_val, float(ema7 - ema21), (1 if grid_yonu == 'LONG' else -1), atr, COIN_ID_MAP.get(symbol, 0), symbol):
                    continue

                taranan_sinyaller.append({"symbol": symbol, "puan": sinyal_puani, "yon": grid_yonu, "rsi": rsi, "adx": adx_val, "ema_fark": float(ema7 - ema21), "fiyat": guncel_fiyat, "atr": atr, "altin_atis": sinyal_puani >= 85})

            taranan_sinyaller.sort(key=lambda x: x["puan"], reverse=True)

            for sinyal in taranan_sinyaller:
                if not BOT_CALISIYOR_MU: break
                if len(aktif_borsa_map) >= MAKSIMUM_TOPLAM_POZISYON: break
                if sinyal["puan"] < 70: continue
                if sum(1 for p in aktif_borsa_map.values() if str(p.get('side', '')).upper() == sinyal["yon"]) >= MAKSIMUM_AYNI_YON_SAYISI: continue

                kaldirac = 20 if sinyal["altin_atis"] else 10
                kasa_orani = 0.25 if sinyal["altin_atis"] else 0.20

                try:
                    balance = exchange.fetch_balance()
                    toplam_bakiye = float(balance['total'].get('USDT', 0))
                    if not set_leverage_safely(sinyal["symbol"], kaldirac): continue
                    
                    market = exchange.market(sinyal["symbol"])
                    miktar = float(exchange.amount_to_precision(sinyal["symbol"], max((toplam_bakiye * kasa_orani * kaldirac) / sinyal["fiyat"] / float(market.get('contractSize', 1.0)), float(market['limits']['amount']['min'] or 1.0))))
                    
                    giris_fiyati = sinyal["fiyat"]
                    islem_yonu = sinyal["yon"]
                    
                    if islem_yonu == 'LONG':
                        tp_fiyat = giris_fiyati * (1 + (0.20 / kaldirac))
                        sl_fiyat = giris_fiyati * (1 - (0.10 / kaldirac))
                    else:
                        tp_fiyat = giris_fiyati * (1 - (0.20 / kaldirac))
                        sl_fiyat = giris_fiyati * (1 + (0.10 / kaldirac))

                    emir_parametreleri = {
                        'take_profit': float(exchange.price_to_precision(sinyal["symbol"], tp_fiyat)),
                        'stop_loss': float(exchange.price_to_precision(sinyal["symbol"], sl_fiyat))
                    }

                    exchange.create_order(
                        sinyal["symbol"], 
                        'market', 
                        'buy' if islem_yonu == 'LONG' else 'sell', 
                        miktar, 
                        None, 
                        emir_parametreleri
                    )
                    
                    with state_lock:
                        AKTIF_GRID_SISTEMLERI[sinyal["symbol"]] = {
                            "giris_rsi": float(sinyal["rsi"]), 
                            "giris_adx": float(sinyal["adx"]), 
                            "ema_fark": float(sinyal["ema_fark"]), 
                            "atr_yuzde": float(sinyal["atr"]), 
                            "hedef_roe": 20.0, 
                            "stop_roe": 10.0
                        }
                    hafizayi_kaydet()
                    
                    print(f"⚡ [İŞLEM AÇILDI] {sinyal['symbol']} | Yön: {islem_yonu} | Puan: {sinyal['puan']} | TP: {tp_fiyat:.4f} | SL: {sl_fiyat:.4f}", flush=True)
                    telegram_mesaj_gonder(f"⚡ *İŞLEM AÇILDI*\n📌 `{sinyal['symbol']}` | Yön: `{islem_yonu}` | Puan: `{sinyal['puan']}` | Kaldıraç: `{kaldirac}x`\n🎯 TP: `{tp_fiyat:.4f}` | 🛑 SL: `{sl_fiyat:.4f}`")
                    break
                except Exception as e:
                    print(f"❌ İşlem açma hatası: {e}", flush=True)

        except Exception as e:
            print(f"⚠️ Tarayıcı döngü hatası: {e}", flush=True)
        time.sleep(10)

if __name__ == '__main__':
    t = threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True)
    t.start()
    
    app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app_tg.add_handler(CommandHandler("durum", durum_komutu))
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))
    
    print("🤖 Telegram Bot (Polling Modu) Başlatılıyor...", flush=True)
    app_tg.run_polling()
