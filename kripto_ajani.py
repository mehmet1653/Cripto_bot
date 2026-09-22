import os
import time
import threading
import sys
import asyncio
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

BOT_CALISIYOR_MU = True
state_lock = threading.Lock()
KALDIRAC = 5

GLOBAL_COOLDOWN_BITIS = 0.0
SON_ZARAR_ZAMANLARI = []
COIN_OI_TAKIP = {} 
ANLIK_PIYASA_MODU = "TESTERE" # Başlangıç modu

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
COOLDOWN_SURESI_SANIYE = 45 * 60  # Testere ve genel sirkülasyonu önlemek için 45 dk cooldown

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

def emir_defteri_derinlik_analizi(symbol, limit=30):
    try:
        order_book = exchange.fetch_order_book(symbol, limit=limit)
        bids = order_book.get('bids', [])
        asks = order_book.get('asks', [])

        if not bids or not asks:
            return "NEUTRAL", 50.0, "NORMAL", None, None

        toplam_alis_hacmi = sum([item[1] for item in bids])
        toplam_satis_hacmi = sum([item[1] for item in asks])
        toplam_hacim = toplam_alis_hacmi + toplam_satis_hacmi

        if toplam_hacim == 0:
            return "NEUTRAL", 50.0, "NORMAL", None, None

        alis_yuzdesi = (toplam_alis_hacmi / toplam_hacim) * 100
        ortalama_kademe_hacmi = toplam_hacim / (len(bids) + len(asks))
        duvar_esigi = ortalama_kademe_hacmi * 4
        
        en_yakin_satis_duvari = None
        for ask_fiyat, ask_hacim in asks:
            if ask_hacim >= duvar_esigi:
                en_yakin_satis_duvari = ask_fiyat
                break

        en_yakin_alis_duvari = None
        for bid_fiyat, bid_hacim in bids:
            if bid_hacim >= duvar_esigi:
                en_yakin_alis_duvari = bid_fiyat
                break

        duvar_durumu = "NORMAL"
        if en_yakin_satis_duvari:
            duvar_durumu = "SATIS_DUVARI_VAR"
        elif en_yakin_alis_duvari:
            duvar_durumu = "ALIS_DUVARI_VAR"

        if alis_yuzdesi > 58.0:
            book_durum = "BUY_PRESSURE"
        elif alis_yuzdesi < 42.0:
            book_durum = "SELL_PRESSURE"
        else:
            book_durum = "BALANCED"

        return book_durum, alis_yuzdesi, duvar_durumu, en_yakin_satis_duvari, en_yakin_alis_duvari
    except Exception:
        return "NEUTRAL", 50.0, "NORMAL", None, None

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
            return -10, f"Fonlama aşırı pozitif (Ceza)"
        elif yon == 'SHORT' and fr < -0.0005:
            return -10, f"Fonlama aşırı negatif (Ceza)"
        return 5, f"Fonlama dengeli"
    except Exception:
        return 0, "Fonlama okunamadı"

def btc_trend_kontrolu():
    global ANLIK_PIYASA_MODU
    try:
        ohlcv_btc_1h = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='1h', limit=30)
        df_btc_1h = pd.DataFrame(ohlcv_btc_1h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        ema9_1h = ta.trend.ema_indicator(df_btc_1h['close'], window=9).iloc[-1]
        ema21_1h = ta.trend.ema_indicator(df_btc_1h['close'], window=21).iloc[-1]
        adx_1h = ta.trend.ADXIndicator(df_btc_1h['high'], df_btc_1h['low'], df_btc_1h['close'], window=14).adx().iloc[-1]
        rsi_1h = ta.momentum.rsi(df_btc_1h['close'], window=14).iloc[-1]
        son_kapanis = df_btc_1h['close'].iloc[-1]

        # ADX güçlü ise TREND MODU, düşükse TESTERE MODU
        if adx_1h >= 22.0:
            ANLIK_PIYASA_MODU = "TREND"
            if ema9_1h > ema21_1h and rsi_1h > 48.0:
                return "LONG"
            elif ema9_1h < ema21_1h and rsi_1h < 52.0:
                return "SHORT"
        else:
            ANLIK_PIYASA_MODU = "TESTERE"
            # Testere modunda daha esnek ve güvenli yön tayini
            if son_kapanis >= ema9_1h:
                return "LONG"
            else:
                return "SHORT"
            
        return "NOTR"
    except Exception:
        ANLIK_PIYASA_MODU = "TESTERE"
        return "NOTR"

def hibrit_tp_sl_hesapla(df, giris_fiyati, yon, satis_duvari, alis_duvari, btc_yonu="NOTR"):
    try:
        global ANLIK_PIYASA_MODU
        
        if ANLIK_PIYASA_MODU == "TREND":
            # Trend Modu: Yüksek Hedef ROE (%25 - %35 aralığına denk gelecek şekilde)
            hedef_fiyat_yuzdesi = 0.055 # %5.5 fiyat hareketi * 5 kaldıraç = ~%27.5 - %32.5 ROE
            sl_fiyat_yuzdesi = 0.025   # %2.5 stop mesafesi
        else:
            # Testere Modu: Düşük, nokta atışı ve güvenli ROE (%6 - %12 arası)
            atr_yuzde = atr_ve_volatilite_hesapla(df)
            dinamik_faktor = max(0.8, min(2.0, atr_yuzde * 1.0))
            hedef_fiyat_yuzdesi = max(0.012, min(0.022, 0.015 * dinamik_faktor))
            sl_fiyat_yuzdesi = max(0.010, min(0.020, 0.012 * dinamik_faktor))
        
        if yon == 'LONG':
            tp_fiyat = giris_fiyati * (1 + hedef_fiyat_yuzdesi)
            sl_fiyat = giris_fiyati * (1 - sl_fiyat_yuzdesi)
            kapat_yon = 'sell'
            hedef_roe = ((tp_fiyat - giris_fiyati) / giris_fiyati) * 100 * KALDIRAC
        else:
            tp_fiyat = giris_fiyati * (1 - hedef_fiyat_yuzdesi)
            sl_fiyat = giris_fiyati * (1 + sl_fiyat_yuzdesi)
            kapat_yon = 'buy'
            hedef_roe = ((giris_fiyati - tp_fiyat) / giris_fiyati) * 100 * KALDIRAC
            
        return tp_fiyat, sl_fiyat, kapat_yon, hedef_roe
    except Exception:
        if yon == 'LONG':
            return giris_fiyati * 1.025, giris_fiyati * 0.97, 'sell', 12.5
        else:
            return giris_fiyati * 0.975, giris_fiyati * 1.03, 'buy', 12.5

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
        return

    with state_lock:
        bas_sayi = int(ANALitik_HAFIZA.get("basarili_islem_sayisi", 0))
        basarisiz_sayi = int(ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0))
        if basarili: 
            bas_sayi += 1
        else:
            basarisiz_sayi += 1
            if cezali_mi:
                simdiki_zaman = time.time()
                SON_ZARAR_ZAMANLARI = [t for t in SON_ZARAR_ZAMANLARI if simdiki_zaman - t < 1800]
                SON_ZARAR_ZAMANLARI.append(simdiki_zaman)
                if len(SON_ZARAR_ZAMANLARI) >= 3:
                    GLOBAL_COOLDOWN_BITIS = simdiki_zaman + (1.5 * 3600)
                    telegram_mesaj_gonder("🚨 *GENEL SİGORTA ATTI*\n⚠️ Bot ardışık zararlar nedeniyle 1.5 saat durduruldu!")

        ANALitik_HAFIZA["basarili_islem_sayisi"] = bas_sayi
        ANALitik_HAFIZA["basarisiz_islem_sayisi"] = basarisiz_sayi

        COIN_COOLDOWNLAR[symbol] = {"zaman": float(time.time() + COOLDOWN_SURESI_SANIYE), "son_yon": yon}

        if symbol in AKTIF_GRID_SISTEMLERI: del AKTIF_GRID_SISTEMLERI[symbol]
            
    hafizayi_kaydet()
    if sebep_mesaji: telegram_mesaj_gonder(sebep_mesaji)

def dikey_bariyer_kontrol(pozisyon, mevcut_mumlar):
    try:
        global ANLIK_PIYASA_MODU
        islem_yonu = pozisyon.get("yon", "LONG")
        giris_fiyati = pozisyon.get("giris_fiyati", 0)
        anlik_fiyat = pozisyon.get("anlik_fiyat", giris_fiyati)
        kaldirac = pozisyon.get("kaldirac", KALDIRAC)

        fiyat_degisim_yuzdesi = (anlik_fiyat - giris_fiyati) / giris_fiyati if islem_yonu == "LONG" else (giris_fiyati - anlik_fiyat) / giris_fiyati
        kaldiracli_roe = fiyat_degisim_yuzdesi * kaldirac * 100

        # Trend modunda yüksek kâr hedefi (%30), Testere modunda daha erken ve güvenli kâr cebelleşmesi (%12)
        hedef_roe_siniri = 30.0 if ANLIK_PIYASA_MODU == "TREND" else 12.0
        if kaldiracli_roe >= hedef_roe_siniri:
            return {"kapat_ilsi": True, "neden": f"Hedef TP Seviyesine Ulaşıldı! ({ANLIK_PIYASA_MODU} Mod - ROE: %{kaldiracli_roe:.2f})"}

        # Testere modunda anlık zıt mumlarla pozisyonu patlatma mekanizması İPTAL EDİLDİ.
        # Sadece Trend modunda aşırı hacimli ters mumlarda koruma çalışır.
        if ANLIK_PIYASA_MODU == "TREND":
            if len(mevcut_mumlar) < 5: return {"kapat_ilsi": False, "neden": "Yetersiz mum."}
            son_mumlar = mevcut_mumlar[-5:]
            hacimler = [m[5] for m in son_mumlar]
            govdeler = [abs(m[4] - m[1]) for m in son_mumlar]
            ortalama_govde = sum(govdeler[:-1]) / len(govdeler[:-1]) if len(govdeler) > 1 else 1e-8

            son_mum_govde = govdeler[-1]
            son_mum_yonu_kirmizi = son_mumlar[-1][4] < son_mumlar[-1][1]
            son_mum_yonu_yesil = son_mumlar[-1][4] > son_mumlar[-1][1]

            if islem_yonu == "LONG" and son_mum_yonu_kirmizi and son_mum_govde > (ortalama_govde * 3.5) and hacimler[-1] > (sum(hacimler[:-1])/4.0) * 2:
                return {"kapat_ilsi": True, "neden": f"Sert Satış Mum Kırılımı (ROE: %{kaldiracli_roe:.2f})"}
            elif islem_yonu == "SHORT" and son_mum_yonu_yesil and son_mum_govde > (ortalama_govde * 3.5) and hacimler[-1] > (sum(hacimler[:-1])/4.0) * 2:
                return {"kapat_ilsi": True, "neden": f"Sert Alış Mum Kırılımı (ROE: %{kaldiracli_roe:.2f})"}

        return {"kapat_ilsi": False, "neden": "Devam."}
    except Exception:
        return {"kapat_ilsi": False, "neden": "Hata."}

async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID):
        return
    try:
        global ANLIK_PIYASA_MODU
        balance = await asyncio.to_thread(exchange.fetch_balance)
        total = float(balance['total'].get('USDT', 0))
        borsa_poslari = [p for p in await asyncio.to_thread(exchange.fetch_positions) if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
        toplam_pnl = sum(float(p.get('unrealizedPnl', 0)) for p in borsa_poslari)
        
        basarili = int(ANALitik_HAFIZA.get("basarili_islem_sayisi", 0))
        basarisiz = int(ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0))
        toplam_islem = basarili + basarisiz
        basari_orani = (basarili / toplam_islem * 100) if toplam_islem > 0 else 0.0

        pos_detaylari = ""
        for p in borsa_poslari:
            sym = p['symbol']
            yon = str(p.get('side', '')).upper()
            if not yon:
                yon = "LONG" if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0 else "SHORT"
            giris = float(p.get('entryPrice', 0))
            kaldirac_val = int(p.get('leverage', KALDIRAC))
            ticker_data = await asyncio.to_thread(exchange.fetch_ticker, sym)
            guncel_fiyat = float(ticker_data['last'])
            fark = (guncel_fiyat - giris) / giris if yon == "LONG" else (giris - guncel_fiyat) / giris
            roe = fark * 100 * kaldirac_val
            pos_detaylari += f"\n• `{sym}` | {yon} | Giriş: `{giris}`\n  Anlık ROE: `%{roe:+.2f}`"

        mod_ikon = "🚀" if ANLIK_PIYASA_MODU == "TREND" else "⚙️"
        mesaj = (
            f"📊 **BOT DURUM RAPORU**\n\n"
            f"{mod_ikon} **Çalışma Modu: `{ANLIK_PIYASA_MODU} MODU`**\n"
            f"👑 Ana BTC Yönü: `{btc_trend_kontrolu()}`\n"
            f"💰 Kasa: `{total:.2f} USDT` | Toplam PnL: `{toplam_pnl:+.2f} USDT`\n"
            f"📌 Açık Pozisyon: `{len(borsa_poslari)} / {MAKSIMUM_TOPLAM_POZISYON}`"
            f"{pos_detaylari}\n\n"
            f"✅ Başarılı TP: `{basarili}` | ❌ Başarısız SL: `{basarisiz}`\n"
            f"📈 Başarı Oranı: `%{basari_orani:.1f}`"
        )
        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID):
        return
    global BOT_CALISIYOR_MU, GLOBAL_COOLDOWN_BITIS
    BOT_CALISIYOR_MU = True
    GLOBAL_COOLDOWN_BITIS = 0.0
    await update.message.reply_text("🟢 Bot aktif edildi!")

async def durdur_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID):
        return
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ Bot durduruldu.")

async def kapat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID):
        return
    try:
        positions = await asyncio.to_thread(exchange.fetch_positions)
        for pos in positions:
            kontrat = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
            if kontrat > 0:
                yon = str(pos.get('side', '')).upper()
                if not yon: yon = "LONG"
                pozisyonu_kapat(pos['symbol'], yon, kontrat, f"🛑 Manuel Kapatma", basarili=False, cezali_mi=True)
        await update.message.reply_text("✅ Tüm pozisyonlar ve bekleyen emirler kapatıldı.")
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, GLOBAL_COOLDOWN_BITIS, ANLIK_PIYASA_MODU
    print("🚀 Bot Arka Plan Döngüsü Aktif...", flush=True)
    try:
        exchange.load_markets()
        yapay_zekayi_egit_ve_guncelle()
    except Exception as e:
        print(f"⚠️ İlk yükleme hatası: {e}", flush=True)
    
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5)
                continue

            if time.time() < GLOBAL_COOLDOWN_BITIS:
                time.sleep(15)
                continue

            btc_yonu = btc_trend_kontrolu()
            print(f"👑 Güncel Piyasa Modu: {ANLIK_PIYASA_MODU} | BTC Yönü: {btc_yonu}", flush=True)

            try:
                raw_positions = exchange.fetch_positions()
                aktif_borsa_map = {}
                aktif_semboller_listesi = []
                for p in raw_positions:
                    kontrat_miktari = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                    if kontrat_miktari > 0:
                        sym = p['symbol']
                        aktif_borsa_map[sym] = p
                        aktif_semboller_listesi.append(sym)
            except Exception as e:
                print(f"⚠️ Pozisyonlar çekilirken hata: {e}", flush=True)
                aktif_borsa_map = {}
                aktif_semboller_listesi = []

            try:
                anlik_aktif_semboller = [p['symbol'] for p in raw_positions if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
                
                for eski_sym in list(AKTIF_GRID_SISTEMLERI.keys()):
                    if eski_sym not in anlik_aktif_semboller:
                        sistem_bilgisi = AKTIF_GRID_SISTEMLERI[eski_sym]
                        giris_fiyati = sistem_bilgisi.get("giris_fiyati", 0) if isinstance(sistem_bilgisi, dict) else 0
                        yon = sistem_bilgisi.get("yon", "LONG") if isinstance(sistem_bilgisi, dict) else "LONG"
                        giris_rsi = sistem_bilgisi.get("giris_rsi", 50.0) if isinstance(sistem_bilgisi, dict) else 50.0
                        
                        islem_karli_mi = False
                        try:
                            ticker = exchange.fetch_ticker(eski_sym)
                            cikis_fiyati = float(ticker['last'])
                            
                            my_trades = exchange.fetch_my_trades(eski_sym, limit=5)
                            gercek_pnl = 0.0
                            if my_trades:
                                son_trade = my_trades[-1]
                                gercek_pnl = float(son_trade.get('realizedPnl', 0) or 0)
                                islem_karli_mi = gercek_pnl > 0
                            else:
                                if yon == "LONG": islem_karli_mi = cikis_fiyati > giris_fiyati
                                else: islem_karli_mi = cikis_fiyati < giris_fiyati
                                
                        except Exception as err:
                            print(f"⚠️ Kâr kontrolü hata ({eski_sym}): {err}", flush=True)
                            islem_karli_mi = True

                        with state_lock:
                            bas_sayi = int(ANALitik_HAFIZA.get("basarili_islem_sayisi", 0))
                            basarisiz_sayi = int(ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0))
                            
                            if islem_karli_mi:
                                bas_sayi += 1
                                sonuc_mesaj_tipi = "✅ *POZİSYON KÂRLA KAPANDI (TP)*"
                            else:
                                basarisiz_sayi += 1
                                sonuc_mesaj_tipi = "❌ *POZİSYON ZARARLA KAPANDI (SL)*"
                                
                            ANALitik_HAFIZA["basarili_islem_sayisi"] = bas_sayi
                            ANALitik_HAFIZA["basarisiz_islem_sayisi"] = basarisiz_sayi

                            egitim_listesi = ANALitik_HAFIZA.get("egitim_verileri", [])
                            feature_vector = [giris_rsi, float(giris_fiyati), float(btc_yonu == "LONG"), 1.0, 0.0, 0.0]
                            label_val = 1 if islem_karli_mi else 0
                            egitim_listesi.append(feature_vector + [label_val])
                            if len(egitim_listesi) > 500:
                                egitim_listesi.pop(0)
                            ANALitik_HAFIZA["egitim_verileri"] = egitim_listesi
                            
                            COIN_COOLDOWNLAR[eski_sym] = {"zaman": float(time.time() + COOLDOWN_SURESI_SANIYE), "son_yon": yon}
                            if eski_sym in AKTIF_GRID_SISTEMLERI:
                                del AKTIF_GRID_SISTEMLERI[eski_sym]
                                
                        hafizayi_kaydet()
                        yapay_zekayi_egit_ve_guncelle()
                        telegram_mesaj_gonder(f"{sonuc_mesaj_tipi}\n📌 `{eski_sym}` | Cooldown süresi başlatıldı.")
                
                if len(raw_positions) > 0 or not raw_positions:
                    time.sleep(1)
                    double_check_positions = exchange.fetch_positions()
                    double_active_symbols = [p['symbol'] for p in double_check_positions if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
                    
                    tum_acik_emirler = exchange.fetch_open_orders()
                    for emir in tum_acik_emirler:
                        emir_sembol = emir.get('symbol')
                        if emir_sembol and emir_sembol not in double_active_symbols:
                            try:
                                exchange.cancel_order(emir['id'], emir_sembol)
                            except Exception:
                                pass
            except Exception as eo_err:
                print(f"⚠️ Açık emirler kontrol hatası: {eo_err}", flush=True)

            taranan_sinyaller = []

            print("🔍 Coinler taranıyor...", flush=True)
            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU: break
                
                with state_lock:
                    cooldown_veri = COIN_COOLDOWNLAR.get(symbol)
                    if cooldown_veri:
                        zaman_kontrol = cooldown_veri.get("zaman", 0) if isinstance(cooldown_veri, dict) else float(cooldown_veri)
                        if (zaman_kontrol - time.time()) > 0:
                            continue

                try:
                    oi_degeri, oi_degisim = acik_pozisyon_oi_kontrolu(symbol)
                    book_durum, alis_orani, duvar_tipi, satis_duvari, alis_duvari = emir_defteri_derinlik_analizi(symbol, limit=30)
                    
                    ticker = exchange.fetch_ticker(symbol)
                    anlik_fiyat = float(ticker['last'])

                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe='5m', limit=50)
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

                    ema5 = ta.trend.ema_indicator(df['close'], window=5).iloc[-1]
                    ema13 = ta.trend.ema_indicator(df['close'], window=13).iloc[-1]
                    rsi = ta.momentum.rsi(df['close'], window=14).iloc[-1]
                    adx_val = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14).adx().iloc[-1]
                    atr = atr_ve_volatilite_hesapla(df)

                    if ai_model_egitildi:
                        try:
                            pred_input = np.array([[rsi, anlik_fiyat, float(btc_yonu == "LONG"), 1.0, 0.0, 0.0]])
                            ai_tahmin = ai_model.predict(pred_input)[0]
                            if ai_tahmin == 0:
                                continue
                        except Exception:
                            pass

                    if ema5 > ema13 and anlik_fiyat >= ema5:
                        grid_yonu = "LONG"
                    elif ema5 < ema13 and anlik_fiyat <= ema5:
                        grid_yonu = "SHORT"
                    else:
                        grid_yonu = "LONG" if ema5 > ema13 else "SHORT"

                    ceza_puani = 0
                    if grid_yonu == "LONG" and (duvar_tipi == "SATIS_DUVARI_VAR" or book_durum == "SELL_PRESSURE"):
                        ceza_puani += 20
                    elif grid_yonu == "SHORT" and (duvar_tipi == "ALIS_DUVARI_VAR" or book_durum == "BUY_PRESSURE"):
                        ceza_puani += 20

                    temel_puan = 70 if adx_val >= 20 else 45
                    anlik_momentum_bonus = 10 if (grid_yonu == "LONG" and anlik_fiyat > ema5) or (grid_yonu == "SHORT" and anlik_fiyat < ema5) else 0
                    derinlik_bonus = 5 if (grid_yonu == "LONG" and book_durum == "BUY_PRESSURE") or (grid_yonu == "SHORT" and book_durum == "SELL_PRESSURE") else 0
                    fonlama_puani, _ = fonlama_orani_analizi(symbol, grid_yonu)

                    sinyal_puani = temel_puan + anlik_momentum_bonus + fonlama_puani + derinlik_bonus - ceza_puani

                    taranan_sinyaller.append({
                        "symbol": symbol, "puan": sinyal_puani, "yon": grid_yonu, 
                        "rsi": rsi, "adx": adx_val, "ema_fark": float(ema5 - ema13), 
                        "fiyat": anlik_fiyat, "atr": atr, "df": df,
                        "satis_duvari": satis_duvari, "alis_duvari": alis_duvari
                    })
                except Exception as ex:
                    continue

            taranan_sinyaller.sort(key=lambda x: x["puan"], reverse=True)

            for symbol, pos in list(aktif_borsa_map.items()):
                try:
                    yon = str(pos.get('side', '')).upper()
                    if not yon:
                        size_val = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
                        yon = "LONG" if size_val > 0 else "SHORT"

                    merkez = float(pos.get('entryPrice', 0) or 0)
                    if merkez <= 0: continue

                    kontrat = float(pos.get('contracts', 0) or pos.get('size', 0) or 1.0)
                    kaldirac_val = int(pos.get('leverage', KALDIRAC))
                    guncel_fiyat = float(exchange.fetch_ticker(symbol)['last'])
                    
                    if yon == "LONG":
                        fark_yuzdesi = (guncel_fiyat - merkez) / merkez
                    else:
                        fark_yuzdesi = (merkez - guncel_fiyat) / merkez
                        
                    roe = fark_yuzdesi * 100 * kaldirac_val

                    # Stop-reverse / Ani yön değiştirme testere modunda TAMAMEN KAPATILDI.
                    # Sadece Trend modunda trend tersine dönerse aktifleşir.
                    trend_zitti_mi = False
                    if ANLIK_PIYASA_MODU == "TREND":
                        if btc_yonu == "LONG" and yon == "SHORT": trend_zitti_mi = True
                        elif btc_yonu == "SHORT" and yon == "LONG": trend_zitti_mi = True

                    sl_limit_degeri = -22.0 if ANLIK_PIYASA_MODU == "TREND" else -15.0
                    if roe <= sl_limit_degeri:
                        pozisyonu_kapat(symbol, yon, kontrat, f"🛑 *ZARAR KESİLDİ (SL)*\n📌 `{symbol}` | ROE: `%{roe:.2f}`", basarili=False, cezali_mi=True)
                        continue
                    
                    elif trend_zitti_mi: 
                        pozisyonu_kapat(
                            symbol, yon, kontrat, 
                            f"✅ *ANİ TREND DEĞİŞİKLİĞİ (STOP-REVERSE)*\n📌 `{symbol}` | Yön: `{yon}` | ROE: `%{roe:.2f}`", 
                            basarili=(roe > 0), cezali_mi=False
                        )
                        continue

                    current_coin_df = next((s["df"] for s in taranan_sinyaller if s["symbol"] == symbol), None)
                    if current_coin_df is not None:
                        veri_paketi = {"symbol": symbol, "yon": yon, "giris_fiyati": merkez, "anlik_fiyat": guncel_fiyat, "kaldirac": kaldirac_val}
                        bariyer_durum = dikey_bariyer_kontrol(veri_paketi, current_coin_df.values.tolist())
                        
                        tp_tetik_roe = 28.0 if ANLIK_PIYASA_MODU == "TREND" else 10.0
                        if bariyer_durum["kapat_ilsi"] and (roe >= tp_tetik_roe or roe < -4.0):
                            pozisyonu_kapat(symbol, yon, kontrat, f"🎯 *ACİL ÇIKIŞ / KÂR AL*\n📌 `{symbol}`\nSebep: `{bariyer_durum['neden']}`", basarili=(roe > 0), cezali_mi=True)
                        
                except Exception as e:
                    continue

            for sinyal in taranan_sinyaller:
                if not BOT_CALISIYOR_MU: break
                
                if sinyal["symbol"] in aktif_semboller_listesi:
                    continue

                if len(aktif_borsa_map) >= MAKSIMUM_TOPLAM_POZISYON:
                    break
                if sinyal["puan"] < 50: continue

                islem_engellendi = False
                if btc_yonu == "LONG" and sinyal["yon"] != "LONG":
                    islem_engellendi = True
                elif btc_yonu == "SHORT" and sinyal["yon"] != "SHORT":
                    islem_engellendi = True
                elif btc_yonu == "NOTR":
                    islem_engellendi = True

                if islem_engellendi:
                    continue

                try:
                    bakiye_bilgisi = exchange.fetch_balance()
                    toplam_bakiye = float(bakiye_bilgisi['total'].get('USDT', 0))
                    serbest_bakiye = float(bakiye_bilgisi.get('free', {}).get('USDT', 0) or bakiye_bilgisi.get('USDT', {}).get('free', 0) or 0)

                    exchange.set_leverage(KALDIRAC, sinyal["symbol"])
                    market = exchange.market(sinyal["symbol"])
                    
                    hedef_butce = toplam_bakiye * 0.20
                    kullanilacak_tutar = min(hedef_butce, serbest_bakiye)
                    
                    if kullanilacak_tutar < 1.0 or serbest_bakiye < 1.0:
                        continue

                    miktar = float(exchange.amount_to_precision(
                        sinyal["symbol"], 
                        max((kullanilacak_tutar * KALDIRAC) / sinyal["fiyat"] / float(market.get('contractSize', 1.0)), 
                        float(market['limits']['amount']['min'] or 1.0))
                    ))
                    
                    islem_yonu = 'buy' if sinyal["yon"] == 'LONG' else 'sell'
                    giris_fiyati = sinyal["fiyat"]
                    
                    exchange.create_order(sinyal["symbol"], 'market', islem_yonu, miktar)
                    
                    tp_fiyat, sl_fiyat, kapat_yon, hedef_roe = hibrit_tp_sl_hesapla(
                        sinyal["df"], giris_fiyati, sinyal["yon"], 
                        sinyal["satis_duvari"], sinyal["alis_duvari"],
                        btc_yonu=btc_yonu
                    )

                    try:
                        exchange.create_order(sinyal["symbol"], 'limit', kapat_yon, miktar, tp_fiyat, {'reduceOnly': True})
                        exchange.create_order(sinyal["symbol"], 'stop', kapat_yon, miktar, sl_fiyat, {'stopPrice': sl_fiyat, 'reduceOnly': True})
                    except Exception as order_err:
                        err_msg = f"⚠️ TP/SL Emir Oluşturma Hatası ({sinyal['symbol']}): {order_err}"
                        print(err_msg, flush=True)
                        telegram_mesaj_gonder(err_msg)

                    with state_lock:
                        AKTIF_GRID_SISTEMLERI[sinyal["symbol"]] = {
                            "giris_fiyati": giris_fiyati,
                            "yon": sinyal["yon"],
                            "giris_rsi": float(sinyal["rsi"]), 
                            "giris_zamani": time.time()
                        }
                        aktif_semboller_listesi.append(sinyal["symbol"])
                    hafizayi_kaydet()
                    
                    print(f"⚡ İşlem açıldı [{ANLIK_PIYASA_MODU}]: {sinyal['symbol']} | Yön: {sinyal['yon']} | Puan: {sinyal['puan']}", flush=True)
                    telegram_mesaj_gonder(f"⚡ *İŞLEM AÇILDI ({ANLIK_PIYASA_MODU} MOD)*\n📌 `{sinyal['symbol']}` | Yön: `{sinyal['yon']}` | Puan: `{sinyal['puan']}`\n🎯 Hedef ROE: `%{hedef_roe:.1f}`")
                    break
                except Exception as e:
                    print(f"❌ İşlem açma hatası ({sinyal['symbol']}): {e}", flush=True)

        except Exception as e:
            print(f"⚠️ Döngü genel hata: {e}", flush=True)
        
        time.sleep(8)

async def main():
    app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    try:
        requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook?drop_pending_updates=True", timeout=5)
    except Exception: pass

    app_tg.add_handler(CommandHandler("durum", durum_komutu))
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))
    
    try:
        await app_tg.initialize()
        await app_tg.start()
        await app_tg.updater.start_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
    except Exception as tg_err:
        print(f"⚠️ Telegram başlatma istisnası yakalandı: {tg_err}", flush=True)
    
    print("🤖 Telegram Bot Asenkron Olarak Dinlemede...", flush=True)

    tarayici_thread = threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True)
    tarayici_thread.start()

    stop_event = asyncio.Event()
    await stop_event.wait()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("🛑 Bot kapatıldı.", flush=True)
