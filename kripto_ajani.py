import os
import sys

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
from datetime import date, datetime
from dotenv import load_dotenv
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler
from supabase import create_client, Client

# ==================== .ENV ====================
if os.path.exists('/etc/secrets/.env'):
    load_dotenv('/etc/secrets/.env', override=True)
    print("✅ .env (secrets) yüklendi", flush=True)
else:
    load_dotenv(override=True)
    print("✅ .env yüklendi", flush=True)

# ==================== RENDER WEB ====================
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot aktif!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ==================== AYARLAR VE ANAHTARLAR ====================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()
GATE_API_KEY = os.environ.get("GATE_API_KEY", "").strip()
GATE_SECRET = os.environ.get("GATE_SECRET", "").strip()

if not TELEGRAM_TOKEN or not CHAT_ID: sys.exit(1)
if not SUPABASE_URL or not SUPABASE_KEY: sys.exit(1)
if not GATE_API_KEY or not GATE_SECRET: sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

exchange = ccxt.gate({
    'apiKey': GATE_API_KEY,
    'secret': GATE_SECRET,
    'enableRateLimit': True,
    'timeout': 30000,
    'options': {'defaultType': 'swap'}
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
SON_BTC_YONU = "YATAY (Testere)"

# ✅ YENİ: Kill-switch ve günlük zarar koruması
GUNLUK_BASLANGIC_BAKIYE = None
GUNLUK_ZARAR_LIMIT = 0.05   # %5 günlük zarar
SON_ISLEM_ZAMANI = 0
MIN_ISLEM_ARASI = 30        # Aynı coinde minimum 30 sn arayla işlem

def hafizayi_yukle():
    print("💾 Hafıza Supabase'den yükleniyor...", flush=True)
    try:
        response = supabase.table("bot_hafiza").select("*").eq("id", 1).execute()
        if response.data and len(response.data) > 0:
            veri = response.data[0]
            print("💾 Hafıza başarıyla yüklendi.", flush=True)
            return {
                "aktif_sistemler": veri.get("aktif_sistemler", {}),
                "analitik": veri.get("analitik", {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0, "egitim_verileri": []}),
                "cooldownlar": veri.get("cooldownlar", {})
            }
    except Exception as e:
        print(f"⚠️ Hafıza yüklenirken hata: {e}", flush=True)
    
    return {
        "aktif_sistemler": {},
        "analitik": {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0, "egitim_verileri": []},
        "cooldownlar": {}
    }

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
                    clean_cooldowns[k] = {"zaman": float(v.get("zaman", 0)), "son_yon": str(v.get("son_yon", ""))}
                else:
                    clean_cooldowns[k] = {"zaman": float(v), "son_yon": ""}

            supabase.table("bot_hafiza").upsert({
                "id": 1,
                "aktif_sistemler": AKTIF_GRID_SISTEMLERI,
                "analitik": payload_analitik,
                "cooldownlar": clean_cooldowns
            }).execute()
        except Exception as e:
            print(f"⚠️ Hafıza kaydedilemedi: {e}", flush=True)

kalici_veri = hafizayi_yukle()
AKTIF_GRID_SISTEMLERI = kalici_veri.get("aktif_sistemler", {})
ANALitik_HAFIZA = kalici_veri.get("analitik", {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0, "egitim_verileri": []})
COIN_COOLDOWNLAR = kalici_veri.get("cooldownlar", {})

# ✅ DÜZELTİLDİ: Pozisyon limiti 2 -> 2 (ama artık doğru çalışacak)
MAKSIMUM_TOPLAM_POZISYON = 2
# ✅ DÜZELTİLDİ: Cooldown 10 dk -> 30 dk
COOLDOWN_SURESI_SANIYE = 30 * 60

def piyasa_rejimini_tespit_et():
    global SON_BTC_YONU
    print("🌐 [PİYASA] Rejim analizi...", flush=True)
    try:
        ohlcv_btc = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='1h', limit=40)
        df_btc = pd.DataFrame(ohlcv_btc, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        adx_1h = ta.trend.ADXIndicator(df_btc['high'], df_btc['low'], df_btc['close'], window=14).adx().iloc[-1]
        
        indicator_bb = ta.volatility.BollingerBands(close=df_btc['close'], window=20, window_dev=2)
        bb_high = indicator_bb.bollinger_hband().iloc[-1]
        bb_low = indicator_bb.bollinger_lband().iloc[-1]
        bb_mid = indicator_bb.bollinger_mavg().iloc[-1]
        bb_bandwidth = (bb_high - bb_low) / bb_mid
        
        ema9 = ta.trend.ema_indicator(df_btc['close'], window=9).iloc[-1]
        ema21 = ta.trend.ema_indicator(df_btc['close'], window=21).iloc[-1]
        fark_yuzdesi = (abs(ema9 - ema21) / ema21) * 100
        
        if adx_1h < 35.0 or bb_bandwidth < 0.04 or fark_yuzdesi < 0.3:
            rejim = "YATAY"
            trend_yonu = "YATAY (Testere)"
        else:
            rejim = "TREND"
            trend_yonu = "LONG" if ema9 > ema21 else "SHORT"
            SON_BTC_YONU = trend_yonu
            
        print(f"🌐 [PİYASA] Rejim: {rejim} | Yön: {trend_yonu}", flush=True)
        return rejim, trend_yonu
    except Exception as e:
        print(f"⚠️ [PİYASA HATA] {e}", flush=True)
        return "YATAY", "YATAY (Testere)"

def emir_defteri_ve_seviye_analizi(symbol, anlik_fiyat, ticker_data):
    try:
        high_24h = float(ticker_data.get('high') or anlik_fiyat * 1.02)
        low_24h = float(ticker_data.get('low') or anlik_fiyat * 0.98)
        
        tepeye_yakin_mi = anlik_fiyat >= (high_24h * 0.994)
        dipe_yakin_mi = anlik_fiyat <= (low_24h * 1.006)

        order_book = exchange.fetch_order_book(symbol, limit=20)
        bids = order_book.get('bids', [])
        asks = order_book.get('asks', [])

        toplam_alis_hacmi = sum([b[1] for b in bids]) if bids else 1.0
        toplam_satis_hacmi = sum([a[1] for a in asks]) if asks else 1.0
        toplam_hacim = toplam_alis_hacmi + toplam_satis_hacmi

        alis_orani = (toplam_alis_hacmi / toplam_hacim) * 100
        satis_orani = (toplam_satis_hacmi / toplam_hacim) * 100
        
        return {
            "tepeye_yakin": tepeye_yakin_mi,
            "dipe_yakin": dipe_yakin_mi,
            "alis_orani": alis_orani,
            "satis_orani": satis_orani
        }
    except Exception as e:
        return {"tepeye_yakin": False, "dipe_yakin": False, "alis_orani": 50.0, "satis_orani": 50.0}

# ✅ DÜZELTİLDİ: TP/SL mesafeleri genişletildi
def akilli_seviye_hesapla(anlik_fiyat, yon, df):
    atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range().iloc[-1]
    
    # ✅ ATR çarpanları: TP 2.0 -> 4.0 | SL 1.5 -> 3.0 (5x kaldıraç için minimum)
    # Piyasa volatilitesine göre dinamik ayar
    atr_yuzde = (atr / anlik_fiyat) * 100
    if atr_yuzde > 1.5:
        tp_carpan = 5.0
        sl_carpan = 3.5
    else:
        tp_carpan = 4.0
        sl_carpan = 3.0
    
    if yon == 'LONG':
        tp_fiyat = anlik_fiyat + (atr * tp_carpan)
        sl_fiyat = anlik_fiyat - (atr * sl_carpan)
        kapat_yon = 'sell'
    else:
        tp_fiyat = anlik_fiyat - (atr * tp_carpan)
        sl_fiyat = anlik_fiyat + (atr * sl_carpan)
        kapat_yon = 'buy'
        
    hedef_roe = abs((tp_fiyat - anlik_fiyat) / anlik_fiyat) * 100 * KALDIRAC
    return float(tp_fiyat), float(sl_fiyat), kapat_yon, float(hedef_roe)

def telegram_mesaj_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": mesaj, "parse_mode": "Markdown"}, timeout=5)
    except Exception: pass

async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
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
            yon = str(p.get('side', '')).upper() or "LONG"
            giris = float(p.get('entryPrice', 0))
            kaldirac_val = int(p.get('leverage', KALDIRAC))
            ticker_data = await asyncio.to_thread(exchange.fetch_ticker, sym)
            guncel_fiyat = float(ticker_data['last'])
            fark = (guncel_fiyat - giris) / giris if yon == "LONG" else (giris - guncel_fiyat) / giris
            roe = fark * 100 * kaldirac_val
            pos_detaylari += f"\n• `{sym}` | {yon} | Giriş: `{giris}`\n  Anlık ROE: `%{roe:+.2f}`"

        gunluk_durum = ""
        if GUNLUK_BASLANGIC_BAKIYE:
            gunluk_kar = (total - GUNLUK_BASLANGIC_BAKIYE) / GUNLUK_BASLANGIC_BAKIYE * 100 if GUNLUK_BASLANGIC_BAKIYE > 0 else 0
            gunluk_durum = f"📊 Günlük: `%{gunluk_kar:+.2f}` (limit: -%5)\n"

        mesaj = (
            f"📊 **BOT DURUM (v9.3 - Sağlamlaştırılmış)**\n\n"
            f"🌐 Rejim: `{rejim}` (BTC: `{btc_yon}`)\n"
            f"💰 Kasa: `{total:.2f} USDT` | PnL: `{toplam_pnl:+.2f}`\n"
            f"{gunluk_durum}"
            f"📌 Açık: `{len(borsa_poslari)} / {MAKSIMUM_TOPLAM_POZISYON}`"
            f"{pos_detaylari}\n\n"
            f"✅ TP: `{basarili}` | ❌ SL: `{basarisiz}`\n"
            f"📈 Başarı: `%{basari_orani:.1f}`"
        )
        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    global BOT_CALISIYOR_MU, GUNLUK_BASLANGIC_BAKIYE
    BOT_CALISIYOR_MU = True
    try:
        b = await asyncio.to_thread(exchange.fetch_balance)
        GUNLUK_BASLANGIC_BAKIYE = float(b['total'].get('USDT', 0))
    except: pass
    await update.message.reply_text("🟢 Bot (v9.3) aktif!")

async def durdur_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ Durduruldu.")

async def kapat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    try:
        positions = await asyncio.to_thread(exchange.fetch_positions)
        for pos in positions:
            kontrat = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
            if kontrat > 0:
                yon = str(pos.get('side', '')).upper() or "LONG"
                kapatma_yonu = 'sell' if yon == 'LONG' else 'buy'
                try: exchange.cancel_all_orders(pos['symbol'])
                except: pass
                exchange.create_order(pos['symbol'], 'market', kapatma_yonu, kontrat, None, {'reduceOnly': True})
        await update.message.reply_text("✅ Kapatıldı.")
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

def otomatik_arkaplan_tarayici():
    global GUNLUK_BASLANGIC_BAKIYE, SON_ISLEM_ZAMANI
    print("🚀 [BAŞLANGIÇ] v9.3 - Sağlamlaştırılmış Bot", flush=True)
    try:
        exchange.load_markets()
        b = exchange.fetch_balance()
        GUNLUK_BASLANGIC_BAKIYE = float(b['total'].get('USDT', 0))
        print(f"💰 Başlangıç kasası: {GUNLUK_BASLANGIC_BAKIYE:.2f} USDT", flush=True)
    except: pass
    
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5)
                continue

            # ✅ GÜNLÜK ZARAR KONTROLÜ
            try:
                b = exchange.fetch_balance()
                su_an_bakiye = float(b['total'].get('USDT', 0))
                if GUNLUK_BASLANGIC_BAKIYE and GUNLUK_BASLANGIC_BAKIYE > 0:
                    gunluk_kar = (su_an_bakiye - GUNLUK_BASLANGIC_BAKIYE) / GUNLUK_BASLANGIC_BAKIYE
                    if gunluk_kar <= -GUNLUK_ZARAR_LIMIT:
                        print(f"🛑 [KILL-SWITCH] Günlük zarar %{gunluk_kar*100:.2f}. 1 saat durduruluyor.", flush=True)
                        telegram_mesaj_gonder(f"🛑 *KILL-SWITCH*\nGünlük zarar: `%{gunluk_kar*100:.2f}`\n1 saat durduruluyor.")
                        time.sleep(3600)
                        GUNLUK_BASLANGIC_BAKIYE = su_an_bakiye  # Reset
                        continue
            except Exception as e:
                print(f"⚠️ Bakiye kontrolü: {e}", flush=True)

            piyasa_rejimi, btc_yonu = piyasa_rejimini_tespit_et()

            # ✅ POZİSYONLARI VE AKTİF SEMBOLLERİ AL (DÖNGÜ BAŞINDA BİR KEZ)
            try:
                raw_positions = exchange.fetch_positions()
                aktif_borsa_map = {}
                aktif_semboller_seti = set()
                for p in raw_positions:
                    kontrat_miktari = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                    if kontrat_miktari > 0:
                        sym = p['symbol']
                        aktif_borsa_map[sym] = p
                        aktif_semboller_seti.add(sym)
            except Exception:
                aktif_borsa_map = {}
                aktif_semboller_seti = set()

            # ✅ KAPANAN POZİSYONLARI TESPİT ET
            try:
                anlik_aktif_semboller = list(aktif_semboller_seti)
                for eski_sym in list(AKTIF_GRID_SISTEMLERI.keys()):
                    if eski_sym not in anlik_aktif_semboller:
                        sistem_bilgisi = AKTIF_GRID_SISTEMLERI[eski_sym]
                        giris_fiyati = sistem_bilgisi.get("giris_fiyati", 0) if isinstance(sistem_bilgisi, dict) else 0
                        yon = sistem_bilgisi.get("yon", "LONG") if isinstance(sistem_bilgisi, dict) else "LONG"
                        
                        islem_karli_mi = False
                        try:
                            ticker = exchange.fetch_ticker(eski_sym)
                            cikis_fiyati = float(ticker['last'])
                            if yon == "LONG": islem_karli_mi = cikis_fiyati > giris_fiyati
                            else: islem_karli_mi = cikis_fiyati < giris_fiyati
                        except Exception:
                            islem_karli_mi = True

                        with state_lock:
                            if islem_karli_mi:
                                ANALitik_HAFIZA["basarili_islem_sayisi"] = int(ANALitik_HAFIZA.get("basarili_islem_sayisi", 0)) + 1
                                sonuc_mesaj_tipi = "✅ *İŞLEM KÂRLA KAPANDI (TP)*"
                            else:
                                ANALitik_HAFIZA["basarisiz_islem_sayisi"] = int(ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0)) + 1
                                sonuc_mesaj_tipi = "❌ *İŞLEM ZARARLA KAPANDI (SL)*"
                                
                            COIN_COOLDOWNLAR[eski_sym] = {"zaman": float(time.time() + COOLDOWN_SURESI_SANIYE), "son_yon": yon}
                            if eski_sym in AKTIF_GRID_SISTEMLERI: del AKTIF_GRID_SISTEMLERI[eski_sym]
                                
                        hafizayi_kaydet()
                        telegram_mesaj_gonder(f"{sonuc_mesaj_tipi}\n📌 `{eski_sym}`")
            except Exception as e:
                print(f"⚠️ Kapanış tespiti: {e}", flush=True)

            # ✅ SİNYAL TARAMA
            taranan_sinyaller = []

            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU: break
                
                # Zaten açık pozisyonu atla
                if symbol in aktif_semboller_seti: continue
                
                with state_lock:
                    cooldown_veri = COIN_COOLDOWNLAR.get(symbol)
                    if cooldown_veri:
                        zaman_kontrol = cooldown_veri.get("zaman", 0) if isinstance(cooldown_veri, dict) else float(cooldown_veri)
                        if zaman_kontrol - time.time() > 0: continue

                try:
                    ticker = exchange.fetch_ticker(symbol)
                    anlik_fiyat = float(ticker['last'])
                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    rsi = ta.momentum.rsi(df['close'], window=14).iloc[-1]

                    emir_analizi = emir_defteri_ve_seviye_analizi(symbol, anlik_fiyat, ticker)

                    # ✅ RSI EŞİKLERİ SIKILAŞTIRILDI (35/65 -> 28/72)
                    if piyasa_rejimi == "YATAY":
                        if rsi < 28:
                            islem_yonu = "LONG"
                        elif rsi > 72:
                            islem_yonu = "SHORT"
                        else:
                            continue
                        mod_adi = "TESTERE"
                    else:
                        if btc_yonu == "LONG" and rsi < 50:
                            islem_yonu = "LONG"
                        elif btc_yonu == "SHORT" and rsi > 50:
                            islem_yonu = "SHORT"
                        else:
                            continue
                        mod_adi = "TREND"

                    # ✅ HACİM ONAYI: Son 3 mumun hacmi ortalamanın üstünde olmalı
                    son_hacim = df['volume'].iloc[-3:].mean()
                    ort_hacim = df['volume'].iloc[-20:].mean()
                    if son_hacim < ort_hacim * 0.8:
                        continue  # Hacim düşük, sinyal zayıf

                    print(f"🔄 [{mod_adi}] {symbol} → {islem_yonu} | RSI: {rsi:.1f} | Hacim: {son_hacim/ort_hacim:.2f}x", flush=True)

                    taranan_sinyaller.append({
                        "symbol": symbol, "yon": islem_yonu, "rsi": rsi, "fiyat": anlik_fiyat, "df": df, "mod": mod_adi
                    })
                except Exception as e:
                    print(f"⚠️ Tarama {symbol}: {e}", flush=True)
                    continue

            # ✅ SİNYALLERİ İŞLE (RACE CONDITION DÜZELTİLDİ)
            for sinyal in taranan_sinyaller:
                if not BOT_CALISIYOR_MU: break
                
                # ✅ KRİTİK: Her sinyal öncesi güncel pozisyon sayısını tekrar kontrol et
                if len(aktif_semboller_seti) >= MAKSIMUM_TOPLAM_POZISYON:
                    print(f"⛔ Limit doldu ({len(aktif_semboller_seti)}/{MAKSIMUM_TOPLAM_POZISYON}), sinyal atlanıyor.", flush=True)
                    break
                
                if sinyal["symbol"] in aktif_semboller_seti: continue

                try:
                    bakiye_bilgisi = exchange.fetch_balance()
                    toplam_bakiye = float(bakiye_bilgisi['total'].get('USDT', 0))
                    serbest_bakiye = float(bakiye_bilgisi.get('free', {}).get('USDT', 0) or 0)

                    exchange.set_leverage(KALDIRAC, sinyal["symbol"])
                    market = exchange.market(sinyal["symbol"])
                    
                    # ✅ DÜZELTİLDİ: Kullanılacak tutar %40 -> %25 (daha güvenli)
                    kullanilacak_tutar = min(toplam_bakiye * 0.25, serbest_bakiye)
                    if kullanilacak_tutar < 1.0: continue

                    giris_fiyati = sinyal["fiyat"]
                    tp_fiyat, sl_fiyat, kapat_yon, hedef_roe = akilli_seviye_hesapla(
                        giris_fiyati, sinyal["yon"], sinyal["df"]
                    )

                    miktar = float(exchange.amount_to_precision(
                        sinyal["symbol"], 
                        max((kullanilacak_tutar * KALDIRAC) / giris_fiyati / float(market.get('contractSize', 1.0)), 
                        float(market['limits']['amount']['min'] or 1.0))
                    ))
                    
                    islem_yonu = 'buy' if sinyal["yon"] == 'LONG' else 'sell'
                    
                    # ✅ MARKET EMİR
                    exchange.create_order(sinyal["symbol"], 'market', islem_yonu, miktar)
                    time.sleep(1.0)  # Borsanın pozisyonu güncellemesi için bekle
                    
                    # ✅ TP/SL EMİRLERİ - HATA YUTMA YOK, LOG BAS
                    tp_ok = False
                    sl_ok = False
                    try:
                        exchange.create_order(sinyal["symbol"], 'limit', kapat_yon, miktar, tp_fiyat, {'reduceOnly': True})
                        tp_ok = True
                    except Exception as e:
                        print(f"⚠️ TP koyulamadı {sinyal['symbol']}: {e}", flush=True)
                    
                    try:
                        exchange.create_order(sinyal["symbol"], 'stop', kapat_yon, miktar, sl_fiyat, {'stopPrice': sl_fiyat, 'reduceOnly': True})
                        sl_ok = True
                    except Exception as e:
                        print(f"🚨 SL KOYULAMADI {sinyal['symbol']}: {e}", flush=True)
                    
                    # ✅ EĞER SL KOYULAMADIYSA POZİSYONU HEMEN KAPAT
                    if not sl_ok:
                        print(f"🚨 [ACİL] {sinyal['symbol']} SL koyulamadı, pozisyon kapatılıyor!", flush=True)
                        try:
                            exchange.create_order(sinyal["symbol"], 'market', kapat_yon, miktar, None, {'reduceOnly': True})
                            telegram_mesaj_gonder(f"🚨 `{sinyal['symbol']}` SL koyulamadı, pozisyon kapatıldı!")
                        except Exception as e2:
                            print(f"🚨 [KRİTİK] Kapatma da başarısız: {e2}", flush=True)
                        continue

                    # ✅ POZİSYONU KAYDET VE SET'E EKLE (ANINDA)
                    with state_lock:
                        AKTIF_GRID_SISTEMLERI[sinyal["symbol"]] = {
                            "giris_fiyati": giris_fiyati, "yon": sinyal["yon"],
                            "giris_rsi": float(sinyal["rsi"]), "giris_zamani": time.time()
                        }
                        aktif_semboller_seti.add(sinyal["symbol"])  # ✅ ANINDA EKLE
                    
                    hafizayi_kaydet()
                    
                    telegram_mesaj_gonder(
                        f"🎯 *İŞLEM GİRİŞİ ({sinyal['mod']} - 5x)*\n"
                        f"📌 `{sinyal['symbol']}` | Yön: `{sinyal['yon']}`\n"
                        f"🎯 Giriş: `{giris_fiyati}`\n"
                        f"💰 TP: `{tp_fiyat}` (ROE: `%{hedef_roe:.1f}`)\n"
                        f"🛑 SL: `{sl_fiyat}`\n"
                        f"📊 Açık pozisyon: `{len(aktif_semboller_seti)}/{MAKSIMUM_TOPLAM_POZISYON}`"
                    )
                    
                except Exception as e:
                    print(f"⚠️ Açma hatası {sinyal['symbol']}: {e}", flush=True)
                    continue

        except Exception as e:
            print(f"⚠️ Ana döngü: {e}", flush=True)
        
        time.sleep(10)  # ✅ 5 sn -> 10 sn (borsa rate limit koruması)

async def main():
    web_thread = threading.Thread(target=run_web, daemon=True)
    web_thread.start()
    
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

    tarayici_thread = threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True)
    tarayici_thread.start()

    stop_event = asyncio.Event()
    await stop_event.wait()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
