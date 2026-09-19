import os
import time
import threading
import sys
import requests
import ccxt
import pandas as pd
import ta
import numpy as np
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler
from sklearn.ensemble import RandomForestClassifier
from supabase import create_client, Client

os.environ['PYTHONUNBUFFERED'] = '1'
sys.stdout.reconfigure(line_buffering=True)

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
    'LINK/USDT:USDT'
]

COIN_ID_MAP = {
    'SOL/USDT:USDT': 1,
    'XRP/USDT:USDT': 2,
    'DOGE/USDT:USDT': 3,
    'LTC/USDT:USDT': 4,
    'LINK/USDT:USDT': 5
}

BOT_CALISIYOR_MU = True
state_lock = threading.Lock()
KALDIRAC = 5

GLOBAL_COOLDOWN_BITIS = 0.0
SON_ZARAR_ZAMANLARI = []
COIN_OI_TAKIP = {} 

# ==================== SUPABASE HAFIZA ====================
def hafizayi_yukle():
    try:
        response = supabase.table("bot_hafiza").select("*").eq("id", 1).execute()
        if response.data and len(response.data) > 0:
            veri = response.data[0]
            print("💾 Hafıza Supabase'den başarıyla yüklendi.", flush=True)
            return {
                "aktif_sistemler": veri.get("aktif_sistemler", {}),
                "analitik": veri.get("analitik", {
                    "basarili_islem_sayisi": 0,
                    "basarisiz_islem_sayisi": 0,
                    "egitim_verileri": []
                }),
                "cooldownlar": veri.get("cooldownlar", {})
            }
    except Exception as e:
        print(f"⚠️ Hafıza yükleme hatası: {e}", flush=True)
        
    varsayilan = {
        "aktif_sistemler": {},
        "analitik": {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0, "egitim_verileri": []},
        "cooldownlar": {}
    }
    try:
        supabase.table("bot_hafiza").upsert({"id": 1, **varsayilan}).execute()
    except Exception as e:
        print(f"⚠️ Hafıza tablo hatası: {e}", flush=True)
    return varsayilan

def hafizayi_kaydet():
    with state_lock:
        try:
            payload_analitik = {
                "basarili_islem_sayisi": int(ANALitik_HAFIZA.get("basarili_islem_sayisi", 0)),
                "basarisiz_islem_sayisi": int(ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0)),
                "egitim_verileri": ANALitik_HAFIZA.get("egitim_verileri", [])
            }
            
            clean_cooldowns = {}
            for k, v in COIN_COOLDOWNLAR.items():
                if isinstance(v, dict):
                    clean_cooldowns[k] = {
                        "zaman": float(v.get("zaman", 0)),
                        "son_yon": str(v.get("son_yon", ""))
                    }
                else:
                    clean_cooldowns[k] = {"zaman": float(v), "son_yon": ""}

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
ANALitik_HAFIZA = kalici_veri.get("analitik", {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0, "egitim_verileri": []})
COIN_COOLDOWNLAR = kalici_veri.get("cooldownlar", {})

MAKSIMUM_TOPLAM_POZISYON = 3
COOLDOWN_SURESI_SANIYE = 30 * 60

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
        if len(set(y)) < 2: return
        ai_model.fit(np.array(X), np.array(y))
        ai_model_egitildi = True
    except Exception:
        ai_model_egitildi = False

def yapay_zeka_islem_onayi(rsi, adx, ema_fark, yon_kod, atr_yuzde, coin_id):
    if not ai_model_egitildi: return True
    try:
        olasiliklar = ai_model.predict_proba(np.array([[float(rsi), float(adx), float(ema_fark), int(yon_kod), float(atr_yuzde), int(coin_id)]]))[0]
        classes = list(ai_model.classes_)
        return (olasiliklar[classes.index(1)] if 1 in classes else 1.0) >= 0.55
    except Exception:
        return True

def atr_ve_volatilite_hesapla(df):
    try:
        atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range().iloc[-1]
        return float((atr / df['close'].iloc[-1]) * 100)
    except Exception:
        return 1.5

def fonlama_orani_analizi(symbol, yon):
    try:
        fr_data = exchange.fetch_funding_rate(symbol)
        fr = float(fr_data.get('fundingRate', 0.0) or 0.0)
        if yon == 'LONG' and fr > 0.0005:
            return 0, f"Fonlama aşırı pozitif ({fr:.5f})"
        elif yon == 'SHORT' and fr < -0.0005:
            return 0, f"Fonlama aşırı negatif ({fr:.5f})"
        return 5, f"Fonlama dengeli ({fr:.5f})"
    except Exception:
        return 2, "Fonlama okunamadı"

def coklu_zaman_dilimi_trend_kontrolu(symbol, yon):
    try:
        ohlcv_1h = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=20)
        df_1h = pd.DataFrame(ohlcv_1h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        ema5_1h = ta.trend.ema_indicator(df_1h['close'], window=5).iloc[-1]
        ema13_1h = ta.trend.ema_indicator(df_1h['close'], window=13).iloc[-1]
        
        if yon == 'LONG' and ema5_1h < ema13_1h:
            return False, "1h ana trend SHORT"
        elif yon == 'SHORT' and ema5_1h > ema13_1h:
            return False, "1h ana trend LONG"
            
        return True, "1h onay"
    except Exception:
        return True, "1h hata"

def btc_trend_kontrolu():
    try:
        ohlcv_btc = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='15m', limit=30)
        df_btc = pd.DataFrame(ohlcv_btc, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        ema5_btc = ta.trend.ema_indicator(df_btc['close'], window=5).iloc[-1]
        ema13_btc = ta.trend.ema_indicator(df_btc['close'], window=13).iloc[-1]
        adx_btc = ta.trend.ADXIndicator(df_btc['high'], df_btc['low'], df_btc['close'], window=14).adx().iloc[-1]
        
        if adx_btc >= 25:
            if ema5_btc > ema13_btc: return "LONG"
            elif ema5_btc < ema13_btc: return "SHORT"
        return "NOTR"
    except Exception:
        return "NOTR"

def dinamik_tp_sl_hesapla(df, giris_fiyati, yon):
    try:
        df['body'] = abs(df['close'] - df['open'])
        ortalama_mum_boyu_yuzde = (df['body'].rolling(window=10).mean().iloc[-1] / giris_fiyati) * 100
        atr_yuzde = atr_ve_volatilite_hesapla(df)
        
        son_hacim = df['volume'].iloc[-1]
        ortalama_hacim = df['volume'].rolling(window=10).mean().iloc[-1]
        hacim_carpani = 1.3 if son_hacim > (ortalama_hacim * 1.5) else 1.0

        faktor = max(1.0, min(3.5, (ortalama_mum_boyu_yuzde + atr_yuzde) / 1.5))
        tp_yuzde = max(0.025, 0.030 * faktor * hacim_carpani)
        sl_yuzde = max(0.015, 0.018 * faktor)
        
        if yon == 'LONG':
            tp_fiyat = giris_fiyati * (1 + tp_yuzde)
            sl_fiyat = giris_fiyati * (1 - sl_yuzde)
            kapat_yon = 'sell'
        else:
            tp_fiyat = giris_fiyati * (1 - tp_yuzde)
            sl_fiyat = giris_fiyati * (1 + sl_yuzde)
            kapat_yon = 'buy'
            
        return tp_fiyat, sl_fiyat, kapat_yon, tp_yuzde * 100 * KALDIRAC
    except Exception:
        if yon == 'LONG':
            return giris_fiyati * 1.035, giris_fiyati * 0.98, 'sell', 17.5
        else:
            return giris_fiyati * 0.965, giris_fiyati * 1.02, 'buy', 17.5

def zamana_entegre_hacim_ve_egilim_kontrolu(df):
    try:
        if len(df) < 5: return True
        hacimler = df['volume'].iloc[-5:]
        genel_hacim_ort = hacimler.mean()
        if (hacimler.iloc[-1] > (genel_hacim_ort * 3.5)) and (hacimler.iloc[-2] < genel_hacim_ort):
            return False
        return True
    except Exception:
        return True

def acik_pozisyon_oi_kontrolu(symbol):
    try:
        oi_data = exchange.fetch_open_interest(symbol)
        current_oi = float(oi_data.get('openInterestAmount', 0) or oi_data.get('openInterest', 0) or 0)
        simdiki_zaman = time.time()
        if symbol in COIN_OI_TAKIP:
            onceki_veri = COIN_OI_TAKIP[symbol]
            eski_oi = onceki_veri["oi"]
            if eski_oi > 0:
                oi_degisim_yuzdesi = ((current_oi - eski_oi) / eski_oi) * 100
                COIN_OI_TAKIP[symbol] = {"oi": current_oi, "zaman": simdiki_zaman}
                return current_oi, oi_degisim_yuzdesi
        COIN_OI_TAKIP[symbol] = {"oi": current_oi, "zaman": simdiki_zaman}
        return current_oi, 0.0
    except Exception:
        return 0.0, 0.0

def telegram_mesaj_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": mesaj, "parse_mode": "Markdown"}, timeout=5)
    except Exception: pass

def pozisyonu_kapat(symbol, yon, miktar, sebep_mesaji, basarili=True, cezali_mi=False):
    global GLOBAL_COOLDOWN_BITIS, SON_ZARAR_ZAMANLARI
    kapatma_yonu = 'sell' if yon == 'LONG' else 'buy'
    try:
        try: exchange.cancel_all_orders(symbol)
        except Exception: pass
        exchange.create_order(symbol, 'market', kapatma_yonu, miktar, None, {'reduceOnly': True})
    except Exception as e:
        print(f"⚠️ Kapatma hatası: {e}", flush=True)

    with state_lock:
        bas_sayi = int(ANALitik_HAFIZA.get("basarili_islem_sayisi", 0))
        basarisiz_sayi = int(ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0))
        if basarili: bas_sayi += 1
        else:
            basarisiz_sayi += 1
            simdiki_zaman = time.time()
            SON_ZARAR_ZAMANLARI = [t for t in SON_ZARAR_ZAMANLARI if simdiki_zaman - t < 1800]
            SON_ZARAR_ZAMANLARI.append(simdiki_zaman)
            if len(SON_ZARAR_ZAMANLARI) >= 2:
                GLOBAL_COOLDOWN_BITIS = simdiki_zaman + (2 * 3600)
                telegram_mesaj_gonder("🚨 *GENEL SİGORTA ATTI (KÜRESEL KORUMA)*\n⚠️ Aşırı tasfiye dalgası tespit edildi. Bot **2 saat boyunca** yeni işlem durdurdu!")

        ANALitik_HAFIZA["basarili_islem_sayisi"] = bas_sayi
        ANALitik_HAFIZA["basarisiz_islem_sayisi"] = basarisiz_sayi

        if cezali_mi:
            COIN_COOLDOWNLAR[symbol] = {"zaman": float(time.time() + COOLDOWN_SURESI_SANIYE), "son_yon": yon}
        else:
            if symbol in COIN_COOLDOWNLAR: del COIN_COOLDOWNLAR[symbol]

        if symbol in AKTIF_GRID_SISTEMLERI: del AKTIF_GRID_SISTEMLERI[symbol]
            
    hafizayi_kaydet()
    print(f"📌 Pozisyon Kapatıldı: {symbol} - Sebep: {sebep_mesaji}", flush=True)
    if sebep_mesaji: telegram_mesaj_gonder(sebep_mesaji)

def dikey_bariyer_kontrol(pozisyon, mevcut_mumlar):
    try:
        islem_yonu = pozisyon.get("yon", "LONG")
        giris_fiyati = pozisyon.get("giris_fiyati", 0)
        anlik_fiyat = pozisyon.get("anlik_fiyat", giris_fiyati)
        kaldirac = pozisyon.get("kaldirac", KALDIRAC)

        fiyat_degisim_yuzdesi = (anlik_fiyat - giris_fiyati) / giris_fiyati if islem_yonu == "LONG" else (giris_fiyati - anlik_fiyat) / giris_fiyati
        kaldiracli_roe = fiyat_degisim_yuzdesi * kaldirac * 100

        if kaldiracli_roe >= 32.0:
            return {"kapat_ilsi": True, "neden": f"Ana TP Patlatıldı! (ROE: %{kaldiracli_roe:.2f})"}

        if len(mevcut_mumlar) < 5: return {"kapat_ilsi": False, "neden": "Yetersiz mum."}

        son_mumlar = mevcut_mumlar[-5:]
        hacimler = [m[5] for m in son_mumlar]
        govdeler = [abs(m[4] - m[1]) for m in son_mumlar]

        if (hacimler[4] < hacimler[3] < hacimler[2]) and (govdeler[4] < govdeler[3] < govdeler[2]) and kaldiracli_roe >= 8.0:
            return {"kapat_ilsi": True, "neden": f"Hacim Söndü, Erken Kâr Alındı (ROE: %{kaldiracli_roe:.2f})"}

        return {"kapat_ilsi": False, "neden": "Devam."}
    except Exception:
        return {"kapat_ilsi": False, "neden": "Hata."}

# ==================== TELEGRAM KOMUTLARI ====================
async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        balance = exchange.fetch_balance()
        total = float(balance['total'].get('USDT', 0))
        borsa_poslari = [p for p in exchange.fetch_positions() if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
        toplam_pnl = sum(float(p.get('unrealizedPnl', 0)) for p in borsa_poslari)
        
        basarili = int(ANALitik_HAFIZA.get("basarili_islem_sayisi", 0))
        basarisiz = int(ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0))
        toplam_islem = basarili + basarisiz
        basari_orani = (basarili / toplam_islem * 100) if toplam_islem > 0 else 0.0

        global GLOBAL_COOLDOWN_BITIS
        kalan_global = int((GLOBAL_COOLDOWN_BITIS - time.time()) / 60)
        global_durum = f"🔴 Aktif (Kalan: {kalan_global} dk)" if kalan_global > 0 else "🟢 Pasif (Normal)"

        pos_detaylari = ""
        for p in borsa_poslari:
            sym = p['symbol']
            yon = str(p.get('side', '')).upper()
            pnl_val = float(p.get('unrealizedPnl', 0))
            giris = float(p.get('entryPrice', 0))
            kaldirac_val = int(p.get('leverage', KALDIRAC))
            guncel_fiyat = exchange.fetch_ticker(sym)['last']
            fark = (guncel_fiyat - giris) / giris if yon == "LONG" else (giris - guncel_fiyat) / giris
            roe = fark * 100 * kaldirac_val
            pos_detaylari += f"\n• `{sym}` | {yon} | Giriş: `{giris}`\n  PnL: `{pnl_val:+.2f} USDT` (`%{roe:+.2f}`)"

        mesaj = (
            f"📊 **BOT DURUM RAPORU**\n\n"
            f"👑 BTC Yönü: `{btc_trend_kontrolu()}`\n"
            f"💰 Kasa: `{total:.2f} USDT` | PnL: `{toplam_pnl:+.2f} USDT`\n"
            f"🛡️ Sigorta: `{global_durum}`\n"
            f"📌 Pozisyon: `{len(borsa_poslari)} / {MAKSIMUM_TOPLAM_POZISYON}`"
            f"{pos_detaylari}\n\n"
            f"✅ Başarılı TP: `{basarili}` | ❌ Başarısız SL: `{basarisiz}`\n"
            f"📈 Başarı Oranı: `%{basari_orani:.1f}`"
        )
        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU, GLOBAL_COOLDOWN_BITIS
    BOT_CALISIYOR_MU = True
    GLOBAL_COOLDOWN_BITIS = 0.0
    await update.message.reply_text("🟢 Bot Aktif!")

async def durdur_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ Bot durduruldu.")

async def kapat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        for pos in exchange.fetch_positions():
            kontrat = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
            if kontrat > 0:
                pozisyonu_kapat(pos['symbol'], str(pos.get('side', '')).upper(), kontrat, f"🛑 Manuel Kapatma - `{pos['symbol']}`", basarili=False, cezali_mi=False)
        await update.message.reply_text("✅ Tüm pozisyonlar kapatıldı.")
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

# ==================== ARKA PLAN TARAYICI ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, GLOBAL_COOLDOWN_BITIS
    print("🚀 Bot Arka Plan Döngüsü Aktif ve Çalışıyor...", flush=True)
    try:
        exchange.load_markets()
        yapay_zekayi_egit_ve_guncelle()
    except Exception as e:
        print(f"⚠️ İlk yükleme hatası: {e}", flush=True)
    
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                print("⏸️ Bot durduruldu konumunda, bekleniyor...", flush=True)
                time.sleep(5)
                continue

            if time.time() < GLOBAL_COOLDOWN_BITIS:
                kalan_dk = int((GLOBAL_COOLDOWN_BITIS - time.time()) / 60)
                print(f"🛡️ Genel sigorta aktif, kalan süre: {kalan_dk} dakika.", flush=True)
                time.sleep(15)
                continue

            btc_yonu = btc_trend_kontrolu()
            print(f"🔍 [Döngü Başladı] BTC Trend Yönü: {btc_yonu}", flush=True)

            try:
                raw_positions = exchange.fetch_positions()
                aktif_borsa_map = {p['symbol']: p for p in raw_positions if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0}
                print(f"📊 Aktif Borsa Pozisyon Sayısı: {len(aktif_borsa_map)}", flush=True)
            except Exception as e:
                aktif_borsa_map = {}
                print(f"⚠️ Pozisyonlar çekilemedi: {e}", flush=True)

            # Mevcut pozisyonları denetle
            for symbol, pos in aktif_borsa_map.items():
                try:
                    guncel_fiyat = exchange.fetch_ticker(symbol)['last']
                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=20)
                except Exception: continue

                yon = str(pos.get('side', '')).upper()
                merkez = float(pos.get('entryPrice', 0))
                kaldirac_val = int(pos.get('leverage', KALDIRAC))
                fark = (guncel_fiyat - merkez) / merkez if yon == "LONG" else (merkez - guncel_fiyat) / merkez
                roe = fark * 100 * kaldirac_val
                pnl = float(pos.get('unrealizedPnl', 0))
                kontrat = float(pos.get('contracts', 0) or pos.get('size', 0) or 1.0)

                print(f"   -> İzlenen Poz: {symbol} | {yon} | Giriş: {merkez} | Güncel: {guncel_fiyat} | ROE: %{roe:+.2f} | PnL: {pnl:+.2f}", flush=True)

                if roe <= -20.0:
                    pozisyonu_kapat(symbol, yon, kontrat, f"🛑 *ZARAR KESİLDİ (SL)*\n📌 `{symbol}` | Zarar: `{pnl:.2f} USDT`", basarili=False, cezali_mi=True)
                    continue

                veri_paketi = {"symbol": symbol, "yon": yon, "giris_fiyati": merkez, "anlik_fiyat": guncel_fiyat, "kaldirac": kaldirac_val}
                bariyer_durum = dikey_bariyer_kontrol(veri_paketi, ohlcv)
                if bariyer_durum["kapat_ilsi"]:
                    pozisyonu_kapat(symbol, yon, kontrat, f"🎯 *HACİM / ESNEK ÇIKIŞ*\n📌 `{symbol}`\nSebep: `{bariyer_durum['neden']}`", basarili=True, cezali_mi=True)

            taranan_sinyaller = []

            # Coinleri tara
            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU: break
                
                with state_lock:
                    cooldown_veri = COIN_COOLDOWNLAR.get(symbol)
                    if cooldown_veri:
                        zaman_kontrol = cooldown_veri.get("zaman", 0) if isinstance(cooldown_veri, dict) else float(cooldown_veri)
                        if (zaman_kontrol - time.time()) > 0:
                            print(f"⏳ Cooldown'da: {symbol}", flush=True)
                            continue

                try:
                    oi_degeri, oi_degisim = acik_pozisyon_oi_kontrolu(symbol)
                    if oi_degisim > 20.0:
                        print(f"⚠️ OI Değişimi çok yüksek ({symbol}: %{oi_degisim:.1f}), atlandı.", flush=True)
                        continue

                    ticker = exchange.fetch_ticker(symbol)
                    anlik_fiyat = float(ticker['last'])

                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

                    if not zamana_entegre_hacim_ve_egilim_kontrolu(df):
                        print(f"⚠️ Hacim anomalisi var ({symbol}), atlandı.", flush=True)
                        continue

                    ema5 = ta.trend.ema_indicator(df['close'], window=5).iloc[-1]
                    ema13 = ta.trend.ema_indicator(df['close'], window=13).iloc[-1]
                    rsi = ta.momentum.rsi(df['close'], window=14).iloc[-1]
                    adx_val = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14).adx().iloc[-1]
                    atr = atr_ve_volatilite_hesapla(df)

                    son_kapanan_close = df['close'].iloc[-2]
                    son_kapanan_open = df['open'].iloc[-1]
                    onceki_kapanan_close = df['close'].iloc[-3]
                    onceki_kapanan_open = df['open'].iloc[-3]

                    short_formasyon_onayi = (onceki_kapanan_close > onceki_kapanan_open) and (son_kapanan_close < son_kapanan_open)
                    long_formasyon_onayi = (onceki_kapanan_close < onceki_kapanan_open) and (son_kapanan_close > son_kapanan_open)

                except Exception as e:
                    print(f"⚠️ Veri çekme hatası ({symbol}): {e}", flush=True)
                    continue

                if ema5 > ema13 and (long_formasyon_onayi or anlik_fiyat > ema5):
                    grid_yonu = "LONG"
                elif ema5 < ema13 and (short_formasyon_onayi or anlik_fiyat < ema5):
                    grid_yonu = "SHORT"
                else:
                    grid_yonu = "LONG" if ema5 > ema13 else "SHORT"

                # BTC Trend Filtresi Kontrolü
                if btc_yonu == "LONG" and grid_yonu == "SHORT": 
                    print(f"🚫 BTC Filtresi: {symbol} için LONG istendi ama BTC LONG (Reddedildi)", flush=True)
                    continue
                elif btc_yonu == "SHORT" and grid_yonu == "LONG": 
                    print(f"🚫 BTC Filtresi: {symbol} için SHORT istendi ama BTC SHORT (Reddedildi)", flush=True)
                    continue

                # 1 Saatlik MTF Kontrolü
                mtf_onay, mtf_mesaj = coklu_zaman_dilimi_trend_kontrolu(symbol, grid_yonu)
                if not mtf_onay:
                    print(f"🚫 MTF Filtresi: {symbol} reddedildi ({mtf_mesaj})", flush=True)
                    continue

                fonlama_puani, fonlama_mesaji = fonlama_orani_analizi(symbol, grid_yonu)
                if fonlama_puani == 0:
                    print(f"🚫 Fonlama Filtresi: {symbol} reddedildi ({fonlama_mesaji})", flush=True)
                    continue

                temel_puan = 70 if adx_val >= 28 else 40
                anlik_momentum_bonus = 5 if anlik_fiyat > son_kapanan_close else 0
                formasyon_bonus = 10

                sinyal_puani = temel_puan + formasyon_bonus + anlik_momentum_bonus + fonlama_puani
                print(f"💡 Sinyal Analizi -> {symbol} | Yön: {grid_yonu} | Puan: {sinyal_puani} (Eşik: 75)", flush=True)

                if symbol in aktif_borsa_map:
                    mevcut_pos = aktif_borsa_map[symbol]
                    mevcut_yon = str(mevcut_pos.get('side', '')).upper()
                    mevcut_kontrat = float(mevcut_pos.get('contracts', 0) or mevcut_pos.get('size', 0) or 1.0)
                    
                    son_iki_mum_ters_mi = (df['close'].iloc[-2] < df['open'].iloc[-2]) and (df['close'].iloc[-3] < df['open'].iloc[-3]) if mevcut_yon == "LONG" else (df['close'].iloc[-2] > df['open'].iloc[-2]) and (df['close'].iloc[-3] > df['open'].iloc[-3])
                    ema_makas_kesin_tersi = (ema5 < ema13) if mevcut_yon == "LONG" else (ema5 > ema13)
                    
                    if sinyal_puani >= 90 and mevcut_yon != grid_yonu and ema_makas_kesin_tersi and son_iki_mum_ters_mi:
                        pozisyonu_kapat(symbol, mevcut_yon, mevcut_kontrat, f"🔄 *RÜZGAR KESİN TERSİNE DÖNDÜ*\n📌 `{symbol}` | Pozisyon kapatıldı.", basarili=False, cezali_mi=True)
                        aktif_borsa_map.pop(symbol, None)
                        continue

                if symbol not in aktif_borsa_map:
                    ai_onay = yapay_zeka_islem_onayi(rsi, adx_val, float(ema5 - ema13), (1 if grid_yonu == 'LONG' else -1), atr, COIN_ID_MAP.get(symbol, 0))
                    if not ai_onay:
                        print(f"🤖 Yapay Zeka Onay Vermedi: {symbol}", flush=True)
                        continue

                    taranan_sinyaller.append({
                        "symbol": symbol, "puan": sinyal_puani, "yon": grid_yonu, 
                        "rsi": rsi, "adx": adx_val, "ema_fark": float(ema5 - ema13), 
                        "fiyat": anlik_fiyat, "atr": atr, "df": df
                    })

            taranan_sinyaller.sort(key=lambda x: x["puan"], reverse=True)

            for sinyal in taranan_sinyaller:
                if not BOT_CALISIYOR_MU: break
                if len(aktif_borsa_map) >= MAKSIMUM_TOPLAM_POZISYON: 
                    print(f"🛑 Maksimum pozisyon sınırına ulaşıldı ({MAKSIMUM_TOPLAM_POZISYON}).", flush=True)
                    break
                
                if sinyal["puan"] < 75: 
                    print(f"⚠️ Puan yetersiz ({sinyal['symbol']} -> {sinyal['puan']} < 75)", flush=True)
                    continue

                try:
                    toplam_bakiye = float(exchange.fetch_balance()['total'].get('USDT', 0))
                    exchange.set_leverage(KALDIRAC, sinyal["symbol"])
                    market = exchange.market(sinyal["symbol"])
                    miktar = float(exchange.amount_to_precision(sinyal["symbol"], max((toplam_bakiye * 0.20 * KALDIRAC) / sinyal["fiyat"] / float(market.get('contractSize', 1.0)), float(market['limits']['amount']['min'] or 1.0))))
                    
                    islem_yonu = 'buy' if sinyal["yon"] == 'LONG' else 'sell'
                    giris_fiyati = sinyal["fiyat"]
                    
                    exchange.create_order(sinyal["symbol"], 'market', islem_yonu, miktar)
                    tp_fiyat, sl_fiyat, kapat_yon, hedef_roe = dinamik_tp_sl_hesapla(sinyal["df"], giris_fiyati, sinyal["yon"])

                    try:
                        exchange.create_order(sinyal["symbol"], 'limit', kapat_yon, miktar, tp_fiyat, {'reduceOnly': True})
                        exchange.create_order(sinyal["symbol"], 'stop', kapat_yon, miktar, sl_fiyat, {'stopPrice': sl_fiyat, 'reduceOnly': True})
                    except Exception: pass

                    with state_lock:
                        AKTIF_GRID_SISTEMLERI[sinyal["symbol"]] = {"giris_rsi": float(sinyal["rsi"]), "giris_zamani": time.time()}
                    hafizayi_kaydet()
                    
                    print(f"⚡ BAŞARILI: İşlem Açıldı -> {sinyal['symbol']} | Yön: {sinyal['yon']} | Puan: {sinyal['puan']}", flush=True)
                    telegram_mesaj_gonder(f"⚡ *İŞLEM AÇILDI (5X, BTC + MTF + 75+ ONAYLI)*\n📌 `{sinyal['symbol']}` | Yön: `{sinyal['yon']}` | Puan: `{sinyal['puan']}`")
                    break
                except Exception as e:
                    print(f"❌ İşlem açma hatası ({sinyal['symbol']}): {e}", flush=True)

        except Exception as e:
            print(f"⚠️ Döngü genel hata: {e}", flush=True)
        
        print("💤 Döngü tamamlandı, 10 saniye bekleniyor...\n", flush=True)
        time.sleep(10)

if __name__ == '__main__':
    t = threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True)
    t.start()
    
    app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app_tg.add_handler(CommandHandler("durum", durum_komutu))
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))
    
    print("🤖 Telegram Bot Başlatılıyor...", flush=True)
    app_tg.run_polling()
