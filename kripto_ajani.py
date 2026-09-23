import os
import sys

# Logların tamponda kalmadan anında akması için en üstte tanımlanmalı
os.environ['PYTHONUNBUFFERED'] = '1'
try:
    sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)
except Exception:
    sys.stdout.reconfigure(line_buffering=True)

import time
import threading
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
    'timeout': 30000,
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
COOLDOWN_SURESI_SANIYE = 15 * 60  # Testerede hızlı dönebilmek için süre biraz kısaltıldı

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

def emir_defteri_derinlik_analizi(symbol, limit=40):
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
        duvar_esigi = ortalama_kademe_hacmi * 3.5
        
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
        if en_yakin_satis_duvari and en_yakin_alis_duvari:
            duvar_durumu = "CIFTO_DUVAR_MEVCUT"
        elif en_yakin_satis_duvari:
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

def piyasa_rejimini_tespit_et():
    """
    Piyasann trendde mi yoksa testere (yatay) mı olduğunu ADX ile belirler.
    ADX < 22 ise piyasa YATAY (Testere) -> Mean Reversion modu aktif olur.
    ADX >= 22 ise piyasa TREND -> Trend Following modu aktif olur.
    """
    try:
        ohlcv_btc = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='1h', limit=30)
        df_btc = pd.DataFrame(ohlcv_btc, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        adx_1h = ta.trend.ADXIndicator(df_btc['high'], df_btc['low'], df_btc['close'], window=14).adx().iloc[-1]
        
        ema9 = ta.trend.ema_indicator(df_btc['close'], window=9).iloc[-1]
        ema21 = ta.trend.ema_indicator(df_btc['close'], window=21).iloc[-1]
        
        trend_yonu = "LONG" if ema9 > ema21 else "SHORT"
        
        if adx_1h < 22.0:
            return "YATAY", trend_yonu # Testere piyasası
        else:
            return "TREND", trend_yonu # Güçlü trend piyasası
    except Exception:
        return "YATAY", "LONG"

def akilli_seviye_hesapla(anlik_fiyat, yon, rejim, rsi, df):
    """
    REJİME GÖRE SEVİYE HESAPLAMA:
    - YATAY (Testere): RSI aşırı bölgelerde ise (RSI<35 Long, RSI>65 Short) hemen %1.5 - %2 ufak kar al (TP) ile döner.
    - TREND: Geniş hedefler ve trend yönünde işlem açar.
    """
    bollinger = ta.volatility.BollingerBands(df['close'], window=20, window_dev=2)
    bb_alt = bollinger.bollinger_lband().iloc[-1]
    bb_ust = bollinger.bollinger_hband().iloc[-1]
    
    if rejim == "YATAY":
        # Testere piyasasında ufak karlar (%1.5 - %2 kar al)
        if yon == 'LONG':
            giris_fiyati = min(anlik_fiyat, bb_alt * 1.002)
            tp_fiyat = giris_fiyati * 1.018  # Yakın ve hızlı kar al (%1.8)
            sl_fiyat = giris_fiyati * 0.990  # Dar ve güvenli stop
            kapat_yon = 'sell'
        else:
            giris_fiyati = max(anlik_fiyat, bb_ust * 0.998)
            tp_fiyat = giris_fiyati * 0.982  # Yakın ve hızlı kar al (%1.8)
            sl_fiyat = giris_fiyati * 1.010  # Dar ve güvenli stop
            kapat_yon = 'buy'
    else:
        # Güçlü Trend piyasasında büyük karlar
        if yon == 'LONG':
            giris_fiyati = anlik_fiyat * 0.995
            tp_fiyat = giris_fiyati * 1.045  # Geniş hedef (%4.5)
            sl_fiyat = giris_fiyati * 0.982
            kapat_yon = 'sell'
        else:
            giris_fiyati = anlik_fiyat * 1.005
            tp_fiyat = giris_fiyati * 0.955  # Geniş hedef (%4.5)
            sl_fiyat = giris_fiyati * 1.018
            kapat_yon = 'buy'
            
    hedef_roe = abs((tp_fiyat - giris_fiyati) / giris_fiyati) * 100 * KALDIRAC
    return float(giris_fiyati), float(tp_fiyat), float(sl_fiyat), kapat_yon, float(hedef_roe)

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
                if len(SON_ZARAR_ZAMANLARI) >= 4:
                    GLOBAL_COOLDOWN_BITIS = simdiki_zaman + 3600
                    telegram_mesaj_gonder("🚨 *GENEL SİGORTA ATTI*\n⚠️ Bot ardışık zararlar nedeniyle 1 saat korumaya alındı!")

        ANALitik_HAFIZA["basarili_islem_sayisi"] = bas_sayi
        ANALitik_HAFIZA["basarisiz_islem_sayisi"] = basarisiz_sayi

        COIN_COOLDOWNLAR[symbol] = {"zaman": float(time.time() + COOLDOWN_SURESI_SANIYE), "son_yon": yon}

        if symbol in AKTIF_GRID_SISTEMLERI: del AKTIF_GRID_SISTEMLERI[symbol]
            
    hafizayi_kaydet()
    if sebep_mesaji: telegram_mesaj_gonder(sebep_mesaji)

async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID):
        return
    try:
        balance = await asyncio.to_thread(exchange.fetch_balance)
        total = float(balance['total'].get('USDT', 0))
        borsa_poslari = [p for p in await asyncio.to_thread(exchange.fetch_positions) if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
        toplam_pnl = sum(float(p.get('unrealizedPnl', 0)) for p in borsa_poslari)
        
        rejim, btc_yon = piyasa_rejimini_tespit_et()
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

        mesaj = (
            f"📊 **BOT DURUM RAPORU (Hibrit Rejim Modu)**\n\n"
            f"🌐 Piyasa Rejimi: `{rejim}` (BTC Yön: `{btc_yon}`)\n"
            f"💰 Kasa: `{total:.2f} USDT` | Toplam PnL: `{toplam_pnl:+.2f} USDT`\n"
            f"📌 Açık Pozisyon: `{len(borsa_poslari)} / {MAKSIMUM_TOPLAM_POZISYON}`"
            f"{pos_detaylari}\n\n"
            f"✅ Başarılı TP: `{basarili}` | ❌ Başarısız SL: `{basarisiz}`\n"
            f"📈 Başarı Oranı: `%{basari_orani:.1f}`"
        )
        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(Hata: {e})

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID):
        return
    global BOT_CALISIYOR_MU, GLOBAL_COOLDOWN_BITIS
    BOT_CALISIYOR_MU = True
    GLOBAL_COOLDOWN_BITIS = 0.0
    await update.message.reply_text("🟢 Bot aktif edildi (Hibrit Rejim & Ufak Kar Toplama devrede)!")

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
                pozisyonu_kapat(pos['symbol'], yon, kontrat, "🛑 Manuel Kapatma", basarili=False, cezali_mi=True)
        await update.message.reply_text("✅ Tüm pozisyonlar ve bekleyen emirler kapatıldı.")
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, GLOBAL_COOLDOWN_BITIS
    print("🚀 Bot Arka Plan Döngüsü Aktif (Hibrit Rejim Modu)...", flush=True)
    try:
        exchange.load_markets()
        print("✅ Piyasalar yüklendi, yapay zeka eğitiliyor...", flush=True)
        yapay_zekayi_egit_ve_guncelle()
    except Exception as e:
        print(f"⚠️ İlk yükleme hatası: {e}", flush=True)
    
    while True:
        try:
            print("🔄 Yeni tarama döngüsü başlatılıyor...", flush=True)
            if not BOT_CALISIYOR_MU:
                time.sleep(5)
                continue

            if time.time() < GLOBAL_COOLDOWN_BITIS:
                time.sleep(15)
                continue

            piyasa_rejimi, btc_yonu = piyasa_rejimini_tespit_et()
            print(f"🌐 Algılanan Piyasa Rejimi: {piyasa_rejimi} | BTC Yön: {btc_yonu}", flush=True)

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
                aktif_borsa_map = {}
                aktif_semboller_listesi = []

            # ========================================================
            # 0. AÇIK EMİR KONTROLÜ VE KAPANAN POZİSYONLARI TESPİT ETME
            # ========================================================
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
                            if my_trades:
                                gercek_pnl = float(my_trades[-1].get('realizedPnl', 0) or 0)
                                islem_karli_mi = gercek_pnl > 0
                            else:
                                if yon == "LONG": islem_karli_mi = cikis_fiyati > giris_fiyati
                                else: islem_karli_mi = cikis_fiyati < giris_fiyati
                        except Exception:
                            islem_karli_mi = True

                        with state_lock:
                            bas_sayi = int(ANALitik_HAFIZA.get("basarili_islem_sayisi", 0))
                            basarisiz_sayi = int(ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0))
                            
                            if islem_karli_mi:
                                bas_sayi += 1
                                sonuc_mesaj_tipi = "✅ *İŞLEM KÂRLA KAPANDI (TP - Ufak Kar Alındı)*"
                            else:
                                basarisiz_sayi += 1
                                sonuc_mesaj_tipi = "❌ *İŞLEM ZARARLA KAPANDI (SL)*"
                                
                            ANALitik_HAFIZA["basarili_islem_sayisi"] = bas_sayi
                            ANALitik_HAFIZA["basarisiz_islem_sayisi"] = basarisiz_sayi

                            egitim_listesi = ANALitik_HAFIZA.get("egitim_verileri", [])
                            feature_vector = [giris_rsi, float(giris_fiyati), float(btc_yonu == "LONG"), 1.0, 0.0, 0.0]
                            label_val = 1 if islem_karli_mi else 0
                            egitim_listesi.append(feature_vector + [label_val])
                            if len(egitim_listesi) > 500: egitim_listesi.pop(0)
                            ANALitik_HAFIZA["egitim_verileri"] = egitim_listesi
                            
                            COIN_COOLDOWNLAR[eski_sym] = {"zaman": float(time.time() + COOLDOWN_SURESI_SANIYE), "son_yon": yon}
                            if eski_sym in AKTIF_GRID_SISTEMLERI: del AKTIF_GRID_SISTEMLERI[eski_sym]
                                
                        hafizayi_kaydet()
                        yapay_zekayi_egit_ve_guncelle()
                        telegram_mesaj_gonder(f"{sonuc_mesaj_tipi}\n📌 `{eski_sym}`")
            except Exception as eo_err:
                print(f"⚠️ Açık emirler kontrol hatası: {eo_err}", flush=True)

            taranan_sinyaller = []

            # ========================================================
            # 1. COİN TARAMA VE REJİME GÖRE İŞLEM FİLTRESİ
            # ========================================================
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
                    book_durum, alis_orani, duvar_tipi, satis_duvari, alis_duvari = emir_defteri_derinlik_analizi(symbol, limit=40)
                    
                    ticker = exchange.fetch_ticker(symbol)
                    anlik_fiyat = float(ticker['last'])

                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe='5m', limit=50)
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

                    rsi = ta.momentum.rsi(df['close'], window=14).iloc[-1]
                    atr = atr_ve_volatilite_hesapla(df)

                    # Rejime göre yön belirleme
                    if piyasa_rejimi == "YATAY":
                        # Testere piyasasında Mean Reversion: Aşırı satımsa LONG, aşırı alımsa SHORT
                        if rsi < 38:
                            islem_yonu = "LONG"
                        elif rsi > 62:
                            islem_yonu = "SHORT"
                        else:
                            continue # RSI orta bölgedeyse testerede işlem açma
                    else:
                        # Trend piyasasında ana trend yönü
                        islem_yonu = btc_yonu

                    sinyal_puani = 82 if piyasa_rejimi == "YATAY" else 85

                    taranan_sinyaller.append({
                        "symbol": symbol, "puan": sinyal_puani, "yon": islem_yonu, 
                        "rsi": rsi, "fiyat": anlik_fiyat, "df": df, "rejim": piyasa_rejimi
                    })
                except Exception as ex:
                    continue

            taranan_sinyaller.sort(key=lambda x: x["puan"], reverse=True)

            # ========================================================
            # 2. YENİ İŞLEM AÇMA (HİBRİT REJİM UYUMLU)
            # ========================================================
            for sinyal in taranan_sinyaller:
                if not BOT_CALISIYOR_MU: break
                if sinyal["symbol"] in aktif_semboller_listesi: continue
                if len(aktif_borsa_map) >= MAKSIMUM_TOPLAM_POZISYON: break

                try:
                    bakiye_bilgisi = exchange.fetch_balance()
                    toplam_bakiye = float(bakiye_bilgisi['total'].get('USDT', 0))
                    serbest_bakiye = float(bakiye_bilgisi.get('free', {}).get('USDT', 0) or 0)

                    exchange.set_leverage(KALDIRAC, sinyal["symbol"])
                    market = exchange.market(sinyal["symbol"])
                    
                    hedef_butce = toplam_bakiye * 0.3333
                    kullanilacak_tutar = min(hedef_butce, serbest_bakiye)
                    if kullanilacak_tutar < 1.0: continue

                    # Rejime göre akıllı giriş ve seviyeler (Ufak kar veya Trend hedefi)
                    giris_fiyati, tp_fiyat, sl_fiyat, kapat_yon, hedef_roe = akilli_seviye_hesapla(
                        sinyal["fiyat"], sinyal["yon"], sinyal["rejim"], sinyal["rsi"], sinyal["df"]
                    )

                    miktar = float(exchange.amount_to_precision(
                        sinyal["symbol"], 
                        max((kullanilacak_tutar * KALDIRAC) / giris_fiyati / float(market.get('contractSize', 1.0)), 
                        float(market['limits']['amount']['min'] or 1.0))
                    ))
                    
                    islem_yonu = 'buy' if sinyal["yon"] == 'LONG' else 'sell'
                    
                    # Emirleri gönder
                    exchange.create_order(sinyal["symbol"], 'limit', islem_yonu, miktar, giris_fiyati)
                    try:
                        exchange.create_order(sinyal["symbol"], 'limit', kapat_yon, miktar, tp_fiyat, {'reduceOnly': True})
                        exchange.create_order(sinyal["symbol"], 'stop', kapat_yon, miktar, sl_fiyat, {'stopPrice': sl_fiyat, 'reduceOnly': True})
                    except Exception as order_err:
                        print(f"⚠️ TP/SL Hata: {order_err}", flush=True)

                    with state_lock:
                        AKTIF_GRID_SISTEMLERI[sinyal["symbol"]] = {
                            "giris_fiyati": giris_fiyati, "yon": sinyal["yon"],
                            "giris_rsi": float(sinyal["rsi"]), "giris_zamani": time.time()
                        }
                        aktif_semboller_listesi.append(sinyal["symbol"])
                    hafizayi_kaydet()
                    
                    print(f"⚡ Hibrit İşlem Eklendi [{sinyal['rejim']}]: {sinyal['symbol']} | Yön: {sinyal['yon']} | TP: {tp_fiyat}", flush=True)
                    telegram_mesaj_gonder(
                        f"🎯 *HİBRİT REJİM İŞLEMİ [{sinyal['rejim']}]*\n"
                        f"📌 `{sinyal['symbol']}` | Yön: `{sinyal['yon']}`\n"
                        f"🎯 Giriş: `{giris_fiyati}`\n"
                        f"💰 Hedef TP: `{tp_fiyat}` (Hedef ROE: `%{hedef_roe:.1f}`)\n"
                        f"🛑 Stop-Loss: `{sl_fiyat}`"
                    )
                    break
                except Exception as e:
                    print(f"❌ İşlem açma hatası: {e}", flush=True)

        except Exception as e:
            print(f"⚠️ Döngü hata: {e}", flush=True)
        
        print("💤 Döngü tamamlandı, 8 saniye bekleniyor...", flush=True)
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
    
    await app_tg.initialize()
    await app_tg.start()
    await app_tg.updater.start_polling(drop_pending_updates=True)
    
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
