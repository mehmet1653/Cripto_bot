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
from dotenv import load_dotenv
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler
from supabase import create_client, Client

# ==================== .env YÜKLE ====================
if os.path.exists('/etc/secrets/.env'):
    load_dotenv('/etc/secrets/.env', override=True)
    print("✅ .env yüklendi: /etc/secrets/.env", flush=True)
else:
    load_dotenv(override=True)
    print("⚠️ /etc/secrets/.env bulunamadı, normal .env deneniyor", flush=True)

# ==================== RENDER WEB SUNUCUSU ====================
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot aktif ve calisiyor!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ==================== AYARLAR ====================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_KEY:
    print(f"❌ HATA: SUPABASE_URL veya SUPABASE_KEY boş!", flush=True)
    sys.exit(1)

if not TELEGRAM_TOKEN or not CHAT_ID:
    print(f"❌ HATA: TELEGRAM_TOKEN veya CHAT_ID boş!", flush=True)
    sys.exit(1)

print(f"✅ Env değişkenleri yüklendi (Supabase: {SUPABASE_URL[:30]}...)", flush=True)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

exchange = ccxt.gate({
    'apiKey': os.environ.get("GATE_API_KEY", "").strip(),
    'secret': os.environ.get("GATE_SECRET", "").strip(),
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
tarayici_kilidi = threading.Lock()
SON_BTC_YONU = "YATAY (Testere)"

# ==================== DİNAMİK KALDIRAÇ AYARLARI ====================
KALDIRAC_MIN = 5
KALDIRAC_ORTA = 6
KALDIRAC_MAX = 7
ATR_ESIK_YUKSEK = 0.008
ATR_ESIK_DUSUK = 0.004

# ==================== TP/SL AYARLARI (GÜNCELLENDİ) ====================
KOMISYON_ORANI = 0.0015
BEKLENEN_HAREKET_TP_ORANI = 0.50   # 🆕 0.65 → 0.50 (TP yakınlaştı)
BEKLENEN_HAREKET_SL_ORANI = 0.35   # 🆕 0.30 → 0.35 (SL uzaklaştı)
MIN_NET_RR = 1.2

# ==================== HAFIZA ====================
def hafizayi_yukle():
    print("💾 Hafıza Supabase'den yükleniyor...", flush=True)
    try:
        response = supabase.table("bot_hafiza").select("*").eq("id", 1).execute()
        if response.data and len(response.data) > 0:
            veri = response.data[0]
            print("💾 Hafıza başarıyla yüklendi.", flush=True)
            return {
                "aktif_sistemler": veri.get("aktif_sistemler", {}),
                "analitik": veri.get("analitik", {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0}),
                "cooldownlar": veri.get("cooldownlar", {})
            }
    except Exception as e:
        print(f"⚠️ Hafıza yüklenirken hata: {e}", flush=True)
    return {
        "aktif_sistemler": {},
        "analitik": {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0},
        "cooldownlar": {}
    }

def hafizayi_kaydet():
    with state_lock:
        try:
            payload_analitik = {
                "basarili_islem_sayisi": int(ANALitik_HAFIZA.get("basarili_islem_sayisi", 0)),
                "basarisiz_islem_sayisi": int(ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0))
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
ANALitik_HAFIZA = kalici_veri.get("analitik", {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0})
COIN_COOLDOWNLAR = kalici_veri.get("cooldownlar", {})

MAKSIMUM_TOPLAM_POZISYON = 2
COOLDOWN_SURESI_SANIYE = 15 * 60

# ==================== DİNAMİK KALDIRAÇ HESABI ====================
def dinamik_kaldirac_hesapla(symbol, anlik_fiyat):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=30)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range().iloc[-1]
        atr_orani = atr / anlik_fiyat

        if atr_orani < ATR_ESIK_DUSUK:
            return KALDIRAC_MAX, atr_orani
        elif atr_orani < ATR_ESIK_YUKSEK:
            return KALDIRAC_ORTA, atr_orani
        else:
            return KALDIRAC_MIN, atr_orani
    except Exception as e:
        print(f"⚠️ Kaldıraç hesap hatası ({symbol}): {e}", flush=True)
        return KALDIRAC_MIN, 0.01

# ==================== REJİM TESPİTİ ====================
def piyasa_rejimini_tespit_et():
    global SON_BTC_YONU
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

        if adx_1h >= 40.0:
            rejim = "TREND"
            trend_yonu = "LONG" if ema9 > ema21 else "SHORT"
            SON_BTC_YONU = trend_yonu
            karar_sebebi = f"ADX güçlü ({adx_1h:.1f}) → TREND"
        elif adx_1h < 35.0 or bb_bandwidth < 0.04 or fark_yuzdesi < 0.3:
            rejim = "YATAY"
            trend_yonu = "YATAY (Testere)"
            karar_sebebi = "Koşullar zayıf → YATAY"
        else:
            rejim = "TREND"
            trend_yonu = "LONG" if ema9 > ema21 else "SHORT"
            SON_BTC_YONU = trend_yonu
            karar_sebebi = "3 koşul uygun → TREND"

        print(
            f"📊 [BTC REJİM] Rejim: {rejim} | Yön: {trend_yonu} | "
            f"ADX: {adx_1h:.1f} | BBW: {bb_bandwidth:.4f} | EMA%: {fark_yuzdesi:.2f} | "
            f"({karar_sebebi})",
            flush=True
        )
        return rejim, trend_yonu
    except Exception as e:
        print(f"⚠️ Rejim tespit hatası: {e}", flush=True)
        return "YATAY", "YATAY (Testere)"

# ==================== EMİR DEFTERİ ANALİZİ ====================
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
    except Exception:
        return {"tepeye_yakin": False, "dipe_yakin": False, "alis_orani": 50.0, "satis_orani": 50.0}

# ==================== BEKLENEN HAREKET ====================
def beklenen_hareket_hesapla(symbol, anlik_fiyat, ticker_data):
    tahminler = []

    try:
        ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=30)
        df_15m = pd.DataFrame(ohlcv_15m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        atr_15m = ta.volatility.AverageTrueRange(df_15m['high'], df_15m['low'], df_15m['close'], window=14).average_true_range().iloc[-1]
        tahminler.append(atr_15m * 4)
    except Exception:
        pass

    try:
        ohlcv_1h = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=30)
        df_1h = pd.DataFrame(ohlcv_1h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        atr_1h = ta.volatility.AverageTrueRange(df_1h['high'], df_1h['low'], df_1h['close'], window=14).average_true_range().iloc[-1]
        tahminler.append(atr_1h * 2)
    except Exception:
        pass

    try:
        ohlcv_4h = exchange.fetch_ohlcv(symbol, timeframe='4h', limit=30)
        df_4h = pd.DataFrame(ohlcv_4h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        atr_4h = ta.volatility.AverageTrueRange(df_4h['high'], df_4h['low'], df_4h['close'], window=14).average_true_range().iloc[-1]
        tahminler.append(atr_4h)
    except Exception:
        pass

    try:
        high_24h = float(ticker_data.get('high') or anlik_fiyat)
        low_24h = float(ticker_data.get('low') or anlik_fiyat)
        gunluk_range = high_24h - low_24h
        tahminler.append(gunluk_range * 0.3)
    except Exception:
        pass

    if not tahminler:
        return anlik_fiyat * 0.005

    beklenen = max(tahminler)
    max_hareket = anlik_fiyat * 0.05
    beklenen = min(beklenen, max_hareket)

    return float(beklenen)

# ==================== TP/SL HESABI ====================
def akilli_seviye_hesapla(symbol, anlik_fiyat, yon, ticker_data, kaldirac):
    beklenen_hareket = beklenen_hareket_hesapla(symbol, anlik_fiyat, ticker_data)

    tp_mesafe = beklenen_hareket * BEKLENEN_HAREKET_TP_ORANI
    sl_mesafe = beklenen_hareket * BEKLENEN_HAREKET_SL_ORANI

    komisyon = anlik_fiyat * KOMISYON_ORANI
    net_kar = tp_mesafe - komisyon
    net_zarar = sl_mesafe + komisyon
    net_rr = net_kar / net_zarar if net_zarar > 0 else 0

    if yon == 'LONG':
        tp_fiyat = anlik_fiyat + tp_mesafe
        sl_fiyat = anlik_fiyat - sl_mesafe
        kapat_yon = 'sell'
    else:
        tp_fiyat = anlik_fiyat - tp_mesafe
        sl_fiyat = anlik_fiyat + sl_mesafe
        kapat_yon = 'buy'

    hedef_roe = (tp_mesafe / anlik_fiyat) * 100 * kaldirac

    return float(tp_fiyat), float(sl_fiyat), kapat_yon, float(hedef_roe), float(net_rr)

# ==================== TELEGRAM ====================
def telegram_mesaj_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": mesaj, "parse_mode": "Markdown"},
            timeout=5
        )
    except Exception:
        pass

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
            yon = str(p.get('side', '')).upper() or "LONG"
            giris = float(p.get('entryPrice', 0))
            kaldirac_val = int(p.get('leverage', 5))
            ticker_data = await asyncio.to_thread(exchange.fetch_ticker, sym)
            guncel_fiyat = float(ticker_data['last'])
            fark = (guncel_fiyat - giris) / giris if yon == "LONG" else (giris - guncel_fiyat) / giris
            roe = fark * 100 * kaldirac_val
            pos_detaylari += f"\n• `{sym}` | {yon} ({kaldirac_val}x) | Giriş: `{giris}`\n  Anlık ROE: `%{roe:+.2f}`"

        mesaj = (
            f"📊 *BOT DURUM RAPORU*\n\n"
            f"🌐 Piyasa Rejimi: `{rejim}` (BTC Yön: `{btc_yon}`)\n"
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
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = True
    await update.message.reply_text("🟢 Bot aktif! (TP yakın + SL uzak)")

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
                yon = str(pos.get('side', '')).upper() or "LONG"
                kapatma_yonu = 'sell' if yon == 'LONG' else 'buy'
                try:
                    exchange.cancel_all_orders(pos['symbol'])
                except Exception:
                    pass
                exchange.create_order(pos['symbol'], 'market', kapatma_yonu, kontrat, None, {'reduceOnly': True})
        await update.message.reply_text("✅ Tüm pozisyonlar ve emirler kapatıldı.")
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

# ==================== ANA DÖNGÜ ====================
def otomatik_arkaplan_tarayici():
    print("🚀 [BAŞLANGIÇ] Bot Aktif (TP yakın + SL uzak)...", flush=True)
    try:
        exchange.load_markets()
    except Exception:
        pass

    dongu_sayaci = 0

    while True:
        if not tarayici_kilidi.acquire(blocking=False):
            time.sleep(2)
            continue

        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5)
                continue

            dongu_sayaci += 1
            print(f"\n{'='*55}", flush=True)
            print(f"🔄 [DÖNGÜ #{dongu_sayaci}] {time.strftime('%H:%M:%S')}", flush=True)

            piyasa_rejimi, btc_yonu = piyasa_rejimini_tespit_et()

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
            except Exception:
                raw_positions = []
                aktif_borsa_map = {}
                aktif_semboller_listesi = []

            try:
                anlik_aktif_semboller = [p['symbol'] for p in raw_positions if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
                for eski_sym in list(AKTIF_GRID_SISTEMLERI.keys()):
                    if eski_sym not in anlik_aktif_semboller:
                        sistem_bilgisi = AKTIF_GRID_SISTEMLERI[eski_sym]
                        giris_fiyati = sistem_bilgisi.get("giris_fiyati", 0) if isinstance(sistem_bilgisi, dict) else 0
                        yon = sistem_bilgisi.get("yon", "LONG") if isinstance(sistem_bilgisi, dict) else "LONG"
                        tp_kayitli = sistem_bilgisi.get("tp_fiyat", giris_fiyati) if isinstance(sistem_bilgisi, dict) else giris_fiyati
                        sl_kayitli = sistem_bilgisi.get("sl_fiyat", giris_fiyati) if isinstance(sistem_bilgisi, dict) else giris_fiyati

                        islem_karli_mi = False
                        cikis_fiyati = giris_fiyati
                        try:
                            ticker = exchange.fetch_ticker(eski_sym)
                            cikis_fiyati = float(ticker['last'])
                            tp_uzaklik = abs(cikis_fiyati - tp_kayitli)
                            sl_uzaklik = abs(cikis_fiyati - sl_kayitli)
                            islem_karli_mi = tp_uzaklik < sl_uzaklik
                        except Exception:
                            islem_karli_mi = cikis_fiyati > giris_fiyati if yon == "LONG" else cikis_fiyati < giris_fiyati

                        with state_lock:
                            bas_sayi = int(ANALitik_HAFIZA.get("basarili_islem_sayisi", 0))
                            basarisiz_sayi = int(ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0))

                            if islem_karli_mi:
                                bas_sayi += 1
                                sonuc_mesaj_tipi = "✅ *İŞLEM KÂRLA KAPANDI (TP)*"
                            else:
                                basarisiz_sayi += 1
                                sonuc_mesaj_tipi = "❌ *İŞLEM ZARARLA KAPANDI (SL)*"

                            ANALitik_HAFIZA["basarili_islem_sayisi"] = bas_sayi
                            ANALitik_HAFIZA["basarisiz_islem_sayisi"] = basarisiz_sayi

                            COIN_COOLDOWNLAR[eski_sym] = {
                                "zaman": float(time.time() + COOLDOWN_SURESI_SANIYE),
                                "son_yon": yon
                            }
                            if eski_sym in AKTIF_GRID_SISTEMLERI:
                                del AKTIF_GRID_SISTEMLERI[eski_sym]

                        hafizayi_kaydet()
                        print(f"💰 [KAPANIŞ] {eski_sym} | {yon} | Çıkış: {cikis_fiyati}", flush=True)
                        telegram_mesaj_gonder(f"{sonuc_mesaj_tipi}\n📌 `{eski_sym}` | Çıkış: `{cikis_fiyati}`")
            except Exception as e:
                print(f"⚠️ Kapanış kontrol hatası: {e}", flush=True)

            # ==================== ANİ TREND KIRILIM KONTROLÜ ====================
            try:
                with state_lock:
                    aktif_pozisyonlar = list(AKTIF_GRID_SISTEMLERI.items())

                for sym_k, kayit_k in aktif_pozisyonlar:
                    if sym_k not in AKTIF_GRID_SISTEMLERI:
                        continue

                    yon_k = kayit_k.get("yon", "LONG")
                    giris_k = float(kayit_k.get("giris_fiyati", 0))
                    mod_k = str(kayit_k.get("mod", ""))

                    if mod_k and "TERS MOD" not in mod_k and "Testere" not in mod_k:
                        continue

                    try:
                        t_k = exchange.fetch_ticker(sym_k)
                        anlik_k = float(t_k['last'])
                    except Exception:
                        continue

                    try:
                        ohlcv_k = exchange.fetch_ohlcv(sym_k, timeframe='15m', limit=25)
                        df_k = pd.DataFrame(ohlcv_k, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

                        bb_k = ta.volatility.BollingerBands(close=df_k['close'], window=20, window_dev=2)
                        bb_ust_k = float(bb_k.bollinger_hband().iloc[-1])
                        bb_alt_k = float(bb_k.bollinger_lband().iloc[-1])

                        bb_disari = False
                        bb_yon_k = ""
                        if anlik_k > bb_ust_k:
                            bb_disari = True
                            bb_yon_k = "yukarı"
                        elif anlik_k < bb_alt_k:
                            bb_disari = True
                            bb_yon_k = "aşağı"

                        son_hacim_k = float(df_k['volume'].iloc[-1])
                        ort_hacim_k = float(df_k['volume'].iloc[-21:-1].mean()) if len(df_k) >= 21 else float(df_k['volume'].mean())
                        hacim_orani_k = son_hacim_k / ort_hacim_k if ort_hacim_k > 0 else 1.0
                        hacim_patlama = hacim_orani_k > 1.8

                        son_3_k = df_k['close'].iloc[-3:].values
                        artan_k = all(son_3_k[i] < son_3_k[i+1] for i in range(len(son_3_k)-1))
                        azalan_k = all(son_3_k[i] > son_3_k[i+1] for i in range(len(son_3_k)-1))
                        mum_kirilim = artan_k or azalan_k

                        kirilim_var = False
                        kirilim_sebep = ""

                        if bb_disari and hacim_patlama:
                            if yon_k == "LONG" and bb_yon_k == "aşağı":
                                kirilim_var = True
                                kirilim_sebep = f"BB aşağı + Hacim x{hacim_orani_k:.1f}"
                            elif yon_k == "SHORT" and bb_yon_k == "yukarı":
                                kirilim_var = True
                                kirilim_sebep = f"BB yukarı + Hacim x{hacim_orani_k:.1f}"
                        elif mum_kirilim and hacim_orani_k > 1.5:
                            if yon_k == "LONG" and azalan_k:
                                kirilim_var = True
                                kirilim_sebep = f"3 mum aşağı + Hacim x{hacim_orani_k:.1f}"
                            elif yon_k == "SHORT" and artan_k:
                                kirilim_var = True
                                kirilim_sebep = f"3 mum yukarı + Hacim x{hacim_orani_k:.1f}"

                        if kirilim_var:
                            print(f"🚨 [ANİ KIRILIM] {sym_k} | {yon_k} | {kirilim_sebep}", flush=True)

                            try:
                                try:
                                    exchange.cancel_all_orders(sym_k)
                                except Exception:
                                    pass

                                tum_pos = exchange.fetch_positions()
                                positions = [p for p in tum_pos if p['symbol'] == sym_k]

                                for p in positions:
                                    kontrat = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                                    if kontrat > 0:
                                        kapat_yon = 'sell' if yon_k == 'LONG' else 'buy'
                                        exchange.create_order(sym_k, 'market', kapat_yon, kontrat, None, {'reduceOnly': True})
                                        print(f"   ✅ {sym_k} ani kırılım ile kapatıldı", flush=True)
                            except Exception as e:
                                print(f"   ⚠️ Kapatma hatası: {e}", flush=True)

                            if yon_k == "LONG":
                                pnl_yuzde = (anlik_k - giris_k) / giris_k * 100 * 5
                            else:
                                pnl_yuzde = (giris_k - anlik_k) / giris_k * 100 * 5

                            with state_lock:
                                if pnl_yuzde > 0:
                                    ANALitik_HAFIZA["basarili_islem_sayisi"] = int(ANALitik_HAFIZA.get("basarili_islem_sayisi", 0)) + 1
                                else:
                                    ANALitik_HAFIZA["basarisiz_islem_sayisi"] = int(ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0)) + 1

                                COIN_COOLDOWNLAR[sym_k] = {
                                    "zaman": float(time.time() + COOLDOWN_SURESI_SANIYE),
                                    "son_yon": yon_k
                                }
                                if sym_k in AKTIF_GRID_SISTEMLERI:
                                    del AKTIF_GRID_SISTEMLERI[sym_k]

                            hafizayi_kaydet()
                            telegram_mesaj_gonder(
                                f"🚨 *ANİ TREND KIRILIMI*\n"
                                f"📌 `{sym_k}` | {yon_k}\n"
                                f"📍 Giriş: `{giris_k}` → Çıkış: `{anlik_k}`\n"
                                f"📊 Sonuç ROE: `%{pnl_yuzde:+.2f}`\n"
                                f"⚡ Sebep: {kirilim_sebep}"
                            )
                    except Exception as e:
                        print(f"   ⚠️ Kırılım kontrolü ({sym_k}): {e}", flush=True)
            except Exception as e:
                print(f"⚠️ Ani kırılım genel hata: {e}", flush=True)

            # ==================== YENİ SİNYAL TARAMASI ====================
            print(f"{'─'*55}", flush=True)
            print(f"🔍 [COİN TARAMA] Rejim: {piyasa_rejimi} | BTC: {btc_yonu}", flush=True)
            print(f"{'─'*55}", flush=True)

            taranan_sinyaller = []

            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU:
                    break

                with state_lock:
                    cooldown_veri = COIN_COOLDOWNLAR.get(symbol)
                    if cooldown_veri:
                        zaman_kontrol = cooldown_veri.get("zaman", 0) if isinstance(cooldown_veri, dict) else float(cooldown_veri)
                        kalan = int(zaman_kontrol - time.time())
                        if kalan > 0:
                            print(f"⏳ [{symbol}] Cooldown: {kalan}s", flush=True)
                            continue

                try:
                    ticker = exchange.fetch_ticker(symbol)
                    anlik_fiyat = float(ticker['last'])
                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    rsi = ta.momentum.rsi(df['close'], window=14).iloc[-1]

                    atr_log = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range().iloc[-1]
                    son_hacim_log = float(df['volume'].iloc[-2])
                    ort_hacim_log = float(df['volume'].iloc[-21:-1].mean())
                    hacim_orani_log = son_hacim_log / ort_hacim_log if ort_hacim_log > 0 else 1.0

                    emir_analizi = emir_defteri_ve_seviye_analizi(symbol, anlik_fiyat, ticker)

                    if piyasa_rejimi == "YATAY":
                        if rsi < 35:
                            islem_yonu = "SHORT"
                            mod_adi = "TERS MOD (Testere)"
                        elif rsi > 65:
                            islem_yonu = "LONG"
                            mod_adi = "TERS MOD (Testere)"
                        else:
                            print(f"🔍 [{symbol}] RSI {rsi:.1f} nötr — beklemede", flush=True)
                            continue
                    else:
                        if btc_yonu == "LONG" and rsi < 55:
                            islem_yonu = "LONG"
                            mod_adi = "NORMAL TREND MODU"
                        elif btc_yonu == "SHORT" and rsi > 45:
                            islem_yonu = "SHORT"
                            mod_adi = "NORMAL TREND MODU"
                        else:
                            print(f"🔍 [{symbol}] Trend koşulu uygun değil (RSI {rsi:.1f})", flush=True)
                            continue

                    kaldirac, atr_orani = dinamik_kaldirac_hesapla(symbol, anlik_fiyat)

                    tp_fiyat, sl_fiyat, kapat_yon, hedef_roe, net_rr = akilli_seviye_hesapla(
                        symbol, anlik_fiyat, islem_yonu, ticker, kaldirac
                    )

                    if net_rr < MIN_NET_RR:
                        print(
                            f"⏭️ [{symbol}] Net R/R düşük ({net_rr:.2f} < {MIN_NET_RR}) | "
                            f"{kaldirac}x | TP:%{hedef_roe:.1f} RoE",
                            flush=True
                        )
                        continue

                    print(
                        f"✅ [{symbol}] Fiyat:{anlik_fiyat:.4f} | RSI:{rsi:.1f} | "
                        f"ATR:%{atr_orani*100:.2f} | {kaldirac}x | "
                        f"→ {islem_yonu} | TP:%{hedef_roe:.1f} RoE | Net R/R:{net_rr:.2f}",
                        flush=True
                    )

                    taranan_sinyaller.append({
                        "symbol": symbol, "yon": islem_yonu, "rsi": rsi,
                        "fiyat": anlik_fiyat, "df": df, "mod": mod_adi,
                        "tp_fiyat": tp_fiyat, "sl_fiyat": sl_fiyat,
                        "kapat_yon": kapat_yon, "hedef_roe": hedef_roe,
                        "kaldirac": kaldirac, "net_rr": net_rr
                    })
                except Exception as e:
                    print(f"⚠️ Tarama hatası ({symbol}): {e}", flush=True)
                    continue

            # ==================== SKOR SIRALAMASI ====================
            taranan_sinyaller.sort(key=lambda x: x["net_rr"], reverse=True)

            if taranan_sinyaller:
                print(f"\n📊 [SKOR SIRALAMASI] {len(taranan_sinyaller)} sinyal bulundu:", flush=True)
                for i, s in enumerate(taranan_sinyaller, 1):
                    print(f"   {i}. {s['symbol']} | Net R/R: {s['net_rr']:.2f} | {s['kaldirac']}x | TP:%{s['hedef_roe']:.1f} RoE", flush=True)

            # ==================== EN İYİ 2'Yİ AÇ ====================
            acilan_sayisi = 0

            for sinyal in taranan_sinyaller:
                if not BOT_CALISIYOR_MU:
                    break
                if len(aktif_borsa_map) >= MAKSIMUM_TOPLAM_POZISYON:
                    print(f"   ⛔ Max pozisyon dolu, dur.", flush=True)
                    break
                if acilan_sayisi >= MAKSIMUM_TOPLAM_POZISYON:
                    break
                if sinyal["symbol"] in aktif_semboller_listesi:
                    continue

                try:
                    print(f"🚀 [EMİR GÖNDERİLİYOR] {sinyal['symbol']} | {sinyal['yon']} | {sinyal['mod']} | {sinyal['kaldirac']}x | Net R/R:{sinyal['net_rr']:.2f}", flush=True)

                    bakiye_bilgisi = exchange.fetch_balance()
                    toplam_bakiye = float(bakiye_bilgisi['total'].get('USDT', 0))
                    serbest_bakiye = float(bakiye_bilgisi.get('free', {}).get('USDT', 0) or 0)

                    kaldirac = sinyal["kaldirac"]
                    exchange.set_leverage(kaldirac, sinyal["symbol"])
                    market = exchange.market(sinyal["symbol"])

                    kullanilacak_tutar = min(toplam_bakiye * 0.4, serbest_bakiye)
                    if kullanilacak_tutar < 1.0:
                        print(f"   ⛔ Yetersiz bakiye: {kullanilacak_tutar}", flush=True)
                        continue

                    giris_fiyati = sinyal["fiyat"]
                    tp_fiyat = sinyal["tp_fiyat"]
                    sl_fiyat = sinyal["sl_fiyat"]
                    kapat_yon = sinyal["kapat_yon"]
                    hedef_roe = sinyal["hedef_roe"]

                    miktar = float(exchange.amount_to_precision(
                        sinyal["symbol"],
                        max((kullanilacak_tutar * kaldirac) / giris_fiyati / float(market.get('contractSize', 1.0)),
                            float(market['limits']['amount']['min'] or 1.0))
                    ))

                    islem_yonu = 'buy' if sinyal["yon"] == 'LONG' else 'sell'

                    exchange.create_order(sinyal["symbol"], 'market', islem_yonu, miktar)
                    time.sleep(0.5)

                    try:
                        exchange.create_order(sinyal["symbol"], 'limit', kapat_yon, miktar, tp_fiyat, {'reduceOnly': True})
                        exchange.create_order(sinyal["symbol"], 'stop', kapat_yon, miktar, sl_fiyat, {'stopPrice': sl_fiyat, 'reduceOnly': True})
                    except Exception as e:
                        print(f"   ⚠️ TP/SL emir hatası: {e}", flush=True)

                    with state_lock:
                        AKTIF_GRID_SISTEMLERI[sinyal["symbol"]] = {
                            "giris_fiyati": giris_fiyati,
                            "yon": sinyal["yon"],
                            "tp_fiyat": tp_fiyat,
                            "sl_fiyat": sl_fiyat,
                            "giris_rsi": float(sinyal["rsi"]),
                            "giris_zamani": time.time(),
                            "mod": sinyal["mod"],
                            "kaldirac": kaldirac
                        }
                        aktif_semboller_listesi.append(sinyal["symbol"])
                        aktif_borsa_map[sinyal["symbol"]] = {"dummy": True, "symbol": sinyal["symbol"], "contracts": 1}
                        acilan_sayisi += 1

                    hafizayi_kaydet()

                    print(f"✅ [AÇILDI] {sinyal['symbol']} {sinyal['yon']} @ {giris_fiyati} ({kaldirac}x) | TP:{tp_fiyat} SL:{sl_fiyat} | RoE:%{hedef_roe:.1f} | Açılan: {acilan_sayisi}/{MAKSIMUM_TOPLAM_POZISYON}", flush=True)

                    telegram_mesaj_gonder(
                        f"🎯 *İŞLEM GİRİŞİ ({sinyal['mod']} - {kaldirac}x)*\n"
                        f"📌 `{sinyal['symbol']}` | Yön: `{sinyal['yon']}`\n"
                        f"🎯 Giriş: `{giris_fiyati}`\n"
                        f"💰 Hedef TP: `{tp_fiyat}` (Hedef ROE: `%{hedef_roe:.1f}`)\n"
                        f"🛑 Stop-Loss: `{sl_fiyat}`\n"
                        f"📊 Net R/R: `{sinyal['net_rr']:.2f}`"
                    )
                except Exception as e:
                    print(f"⚠️ Emir hatası: {e}", flush=True)
                    continue

        except Exception as e:
            print(f"⚠️ Ana döngü hatası: {e}", flush=True)
        finally:
            try:
                tarayici_kilidi.release()
            except Exception:
                pass

        time.sleep(5)

# ==================== ANA ====================
async def main():
    web_thread = threading.Thread(target=run_web, daemon=True)
    web_thread.start()

    app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    try:
        requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook?drop_pending_updates=True", timeout=5)
    except Exception:
        pass

    app_tg.add_handler(CommandHandler("durum", durum_komutu))
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))

    await app_tg.initialize()
    await app_tg.start()
    await app_tg.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

    tarayici_thread = threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True)
    tarayici_thread.start()

    stop_event = asyncio.Event()
    await stop_event.wait()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
