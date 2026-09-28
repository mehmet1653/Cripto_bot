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

# ==================== RENDER WEB ====================
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

print(f"✅ Env yüklendi", flush=True)

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

# ==================== DİNAMİK KALDIRAÇ ====================
KALDIRAC_MIN = 5
KALDIRAC_ORTA = 6
KALDIRAC_MAX = 7
ATR_ESIK_YUKSEK = 0.008
ATR_ESIK_DUSUK = 0.004

# ==================== TP/SL ====================
KOMISYON_ORANI = 0.0008
BEKLENEN_HAREKET_TP_ORANI = 0.50
BEKLENEN_HAREKET_SL_ORANI = 0.35
MIN_NET_RR = 0.8

# ==================== RSI AŞIRI UÇ ====================
RSI_ASIRI_UST = 85
RSI_ASIRI_ALT = 15

# ==================== REJİM SKOR EŞİKLERİ ====================
TREND_SKOR_ESIK = 6       # 6+ → TREND
ZAYIF_TREND_ESIK = 4      # 4-5 → TREND_ZAYIF

# ==================== HAFIZA ====================
def hafizayi_yukle():
    print("💾 Hafıza yükleniyor...", flush=True)
    try:
        response = supabase.table("bot_hafiza").select("*").eq("id", 1).execute()
        if response.data and len(response.data) > 0:
            veri = response.data[0]
            print("💾 Hafıza yüklendi.", flush=True)
            return {
                "aktif_sistemler": veri.get("aktif_sistemler", {}),
                "analitik": veri.get("analitik", {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0}),
                "cooldownlar": veri.get("cooldownlar", {})
            }
    except Exception as e:
        print(f"⚠️ Hafıza yükleme hatası: {e}", flush=True)
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
            print(f"⚠️ Hafıza kaydetme hatası: {e}", flush=True)

kalici_veri = hafizayi_yukle()
AKTIF_GRID_SISTEMLERI = kalici_veri.get("aktif_sistemler", {})
ANALitik_HAFIZA = kalici_veri.get("analitik", {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0})
COIN_COOLDOWNLAR = kalici_veri.get("cooldownlar", {})

MAKSIMUM_TOPLAM_POZISYON = 2
COOLDOWN_SURESI_SANIYE = 15 * 60

# ==================== DİNAMİK KALDIRAÇ ====================
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
        print(f"⚠️ Kaldıraç hatası ({symbol}): {e}", flush=True)
        return KALDIRAC_MIN, 0.01

# ==================== REJİM TESPİTİ (SKOR SİSTEMİ) ====================
def piyasa_rejimini_tespit_et():
    global SON_BTC_YONU
    try:
        ohlcv_btc = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='1h', limit=50)
        df = pd.DataFrame(ohlcv_btc, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

        # 1. ADX (trend gücü)
        adx_ind = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14)
        adx = adx_ind.adx().iloc[-1]
        adx_pos = adx_ind.adx_pos().iloc[-1]
        adx_neg = adx_ind.adx_neg().iloc[-1]

        # 2. Bollinger Bandwidth
        bb = ta.volatility.BollingerBands(close=df['close'], window=20, window_dev=2)
        bb_high = bb.bollinger_hband().iloc[-1]
        bb_low = bb.bollinger_lband().iloc[-1]
        bb_mid = bb.bollinger_mavg().iloc[-1]
        bbw = (bb_high - bb_low) / bb_mid

        # 3. EMA farkı
        ema9 = ta.trend.ema_indicator(df['close'], window=9).iloc[-1]
        ema21 = ta.trend.ema_indicator(df['close'], window=21).iloc[-1]
        ema50 = ta.trend.ema_indicator(df['close'], window=50).iloc[-1]
        ema_fark = abs(ema9 - ema21) / ema21 * 100

        # 4. Fiyat EMA50'ye göre
        fiyat = df['close'].iloc[-1]
        ema50_fark = abs(fiyat - ema50) / ema50 * 100

        # 5. Son 10 mum tek yönlü mü?
        son_10 = df['close'].iloc[-10:].values
        yukari = sum(1 for i in range(1, len(son_10)) if son_10[i] > son_10[i-1])
        asagi = len(son_10) - 1 - yukari
        tek_yonlu = max(yukari, asagi) >= 7

        # 6. ATR artıyor mu?
        atr_seri = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range()
        atr_simdi = atr_seri.iloc[-1]
        atr_10_once = atr_seri.iloc[-11] if len(atr_seri) >= 11 else atr_simdi
        atr_artiyor = atr_simdi > atr_10_once * 1.1

        # ==================== SKOR ====================
        skor = 0
        if adx >= 30: skor += 2
        if adx >= 45: skor += 1
        if bbw >= 0.03: skor += 2
        if bbw >= 0.05: skor += 1
        if ema_fark >= 0.5: skor += 2
        if ema50_fark >= 1.0: skor += 1
        if tek_yonlu: skor += 2
        if atr_artiyor: skor += 1

        # ==================== KARAR ====================
        if skor >= TREND_SKOR_ESIK:
            rejim = "TREND"
            if adx_pos > adx_neg and ema9 > ema21:
                trend_yonu = "LONG"
            elif adx_neg > adx_pos and ema9 < ema21:
                trend_yonu = "SHORT"
            else:
                trend_yonu = "LONG" if ema9 > ema21 else "SHORT"
            SON_BTC_YONU = trend_yonu
        elif skor >= ZAYIF_TREND_ESIK:
            rejim = "TREND_ZAYIF"
            trend_yonu = "LONG" if ema9 > ema21 else "SHORT"
            SON_BTC_YONU = trend_yonu
        else:
            rejim = "YATAY"
            trend_yonu = "YATAY (Testere)"

        print(
            f"📊 [BTC REJİM] {rejim} | {trend_yonu} | Skor:{skor}/12 | "
            f"ADX:{adx:.1f} BBW:{bbw:.4f} EMA%:{ema_fark:.2f} "
            f"TekYön:{tek_yonlu} ATR↑:{atr_artiyor}",
            flush=True
        )
        return rejim, trend_yonu
    except Exception as e:
        print(f"⚠️ Rejim hatası: {e}", flush=True)
        return "YATAY", "YATAY (Testere)"

# ==================== COİN UYUMU ====================
def coin_uyumlu_mu(symbol, btc_yonu):
    """BTC yönü ile coin yönü uyumlu mu?"""
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=30)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

        ema9 = ta.trend.ema_indicator(df['close'], window=9).iloc[-1]
        ema21 = ta.trend.ema_indicator(df['close'], window=21).iloc[-1]

        if btc_yonu == "LONG" and ema9 > ema21:
            return True
        elif btc_yonu == "SHORT" and ema9 < ema21:
            return True
        else:
            return False
    except Exception as e:
        print(f"⚠️ Coin uyum hatası ({symbol}): {e}", flush=True)
        return True

# ==================== EMİR DEFTERİ ====================
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

        return {
            "tepeye_yakin": tepeye_yakin_mi,
            "dipe_yakin": dipe_yakin_mi,
            "alis_orani": (toplam_alis_hacmi / toplam_hacim) * 100,
            "satis_orani": (toplam_satis_hacmi / toplam_hacim) * 100
        }
    except Exception:
        return {"tepeye_yakin": False, "dipe_yakin": False, "alis_orani": 50.0, "satis_orani": 50.0}

# ==================== BEKLENEN HAREKET ====================
def beklenen_hareket_hesapla(symbol, anlik_fiyat, ticker_data):
    tahminler = []

    try:
        ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=30)
        df = pd.DataFrame(ohlcv_15m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range().iloc[-1]
        tahminler.append(atr * 4)
    except Exception:
        pass

    try:
        ohlcv_1h = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=30)
        df = pd.DataFrame(ohlcv_1h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range().iloc[-1]
        tahminler.append(atr * 2)
    except Exception:
        pass

    try:
        ohlcv_4h = exchange.fetch_ohlcv(symbol, timeframe='4h', limit=30)
        df = pd.DataFrame(ohlcv_4h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range().iloc[-1]
        tahminler.append(atr)
    except Exception:
        pass

    try:
        high_24h = float(ticker_data.get('high') or anlik_fiyat)
        low_24h = float(ticker_data.get('low') or anlik_fiyat)
        tahminler.append((high_24h - low_24h) * 0.3)
    except Exception:
        pass

    if not tahminler:
        return anlik_fiyat * 0.005

    beklenen = max(tahminler)
    return float(min(beklenen, anlik_fiyat * 0.05))

# ==================== TP/SL ====================
def akilli_seviye_hesapla(symbol, anlik_fiyat, yon, ticker_data, kaldirac):
    beklenen = beklenen_hareket_hesapla(symbol, anlik_fiyat, ticker_data)

    tp_mesafe = beklenen * BEKLENEN_HAREKET_TP_ORANI
    sl_mesafe = beklenen * BEKLENEN_HAREKET_SL_ORANI

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
    await update.message.reply_text("🟢 Bot aktif! (Skor sistemi + Coin uyumu)")

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
        await update.message.reply_text("✅ Tüm pozisyonlar kapatıldı.")
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

# ==================== ANA DÖNGÜ ====================
def otomatik_arkaplan_tarayici():
    print("🚀 [BAŞLANGIÇ] Skor sistemi + Coin uyumu aktif...", flush=True)
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
                    kontrat = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                    if kontrat > 0:
                        sym = p['symbol']
                        aktif_borsa_map[sym] = p
                        aktif_semboller_listesi.append(sym)
            except Exception:
                raw_positions = []
                aktif_borsa_map = {}
                aktif_semboller_listesi = []

            # ==================== POZİSYON KAPANIŞ ====================
            try:
                anlik_aktif = [p['symbol'] for p in raw_positions if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
                for eski_sym in list(AKTIF_GRID_SISTEMLERI.keys()):
                    if eski_sym not in anlik_aktif:
                        bilgi = AKTIF_GRID_SISTEMLERI[eski_sym]
                        giris = bilgi.get("giris_fiyati", 0) if isinstance(bilgi, dict) else 0
                        yon = bilgi.get("yon", "LONG") if isinstance(bilgi, dict) else "LONG"
                        tp_k = bilgi.get("tp_fiyat", giris) if isinstance(bilgi, dict) else giris
                        sl_k = bilgi.get("sl_fiyat", giris) if isinstance(bilgi, dict) else giris

                        karli = False
                        cikis = giris
                        try:
                            t = exchange.fetch_ticker(eski_sym)
                            cikis = float(t['last'])
                            karli = abs(cikis - tp_k) < abs(cikis - sl_k)
                        except Exception:
                            karli = cikis > giris if yon == "LONG" else cikis < giris

                        with state_lock:
                            bas = int(ANALitik_HAFIZA.get("basarili_islem_sayisi", 0))
                            basz = int(ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0))
                            if karli:
                                bas += 1
                                tip = "✅ *KÂRLA KAPANDI*"
                            else:
                                basz += 1
                                tip = "❌ *ZARARLA KAPANDI*"
                            ANALitik_HAFIZA["basarili_islem_sayisi"] = bas
                            ANALitik_HAFIZA["basarisiz_islem_sayisi"] = basz
                            COIN_COOLDOWNLAR[eski_sym] = {"zaman": float(time.time() + COOLDOWN_SURESI_SANIYE), "son_yon": yon}
                            if eski_sym in AKTIF_GRID_SISTEMLERI:
                                del AKTIF_GRID_SISTEMLERI[eski_sym]

                        hafizayi_kaydet()
                        print(f"💰 [KAPANIŞ] {eski_sym} | Çıkış: {cikis}", flush=True)
                        telegram_mesaj_gonder(f"{tip}\n📌 `{eski_sym}` | Çıkış: `{cikis}`")
            except Exception as e:
                print(f"⚠️ Kapanış hatası: {e}", flush=True)

            # ==================== ANİ TREND KIRILIM ====================
            try:
                with state_lock:
                    aktif_p = list(AKTIF_GRID_SISTEMLERI.items())

                for sym_k, kayit_k in aktif_p:
                    if sym_k not in AKTIF_GRID_SISTEMLERI:
                        continue
                    yon_k = kayit_k.get("yon", "LONG")
                    giris_k = float(kayit_k.get("giris_fiyati", 0))
                    mod_k = str(kayit_k.get("mod", ""))
                    if mod_k and "TERS" not in mod_k and "Testere" not in mod_k:
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
                        bb_ust = float(bb_k.bollinger_hband().iloc[-1])
                        bb_alt = float(bb_k.bollinger_lband().iloc[-1])

                        bb_disari = False
                        bb_yon_k = ""
                        if anlik_k > bb_ust:
                            bb_disari = True
                            bb_yon_k = "yukarı"
                        elif anlik_k < bb_alt:
                            bb_disari = True
                            bb_yon_k = "aşağı"

                        son_h = float(df_k['volume'].iloc[-1])
                        ort_h = float(df_k['volume'].iloc[-21:-1].mean()) if len(df_k) >= 21 else 1.0
                        h_oran = son_h / ort_h if ort_h > 0 else 1.0
                        h_pat = h_oran > 1.8

                        son_3 = df_k['close'].iloc[-3:].values
                        artan = all(son_3[i] < son_3[i+1] for i in range(len(son_3)-1))
                        azalan = all(son_3[i] > son_3[i+1] for i in range(len(son_3)-1))

                        kirilim = False
                        sebep = ""
                        if bb_disari and h_pat:
                            if yon_k == "LONG" and bb_yon_k == "aşağı":
                                kirilim = True
                                sebep = f"BB↓ + Hacim x{h_oran:.1f}"
                            elif yon_k == "SHORT" and bb_yon_k == "yukarı":
                                kirilim = True
                                sebep = f"BB↑ + Hacim x{h_oran:.1f}"
                        elif (artan or azalan) and h_oran > 1.5:
                            if yon_k == "LONG" and azalan:
                                kirilim = True
                                sebep = f"3 mum↓ + Hacim x{h_oran:.1f}"
                            elif yon_k == "SHORT" and artan:
                                kirilim = True
                                sebep = f"3 mum↑ + Hacim x{h_oran:.1f}"

                        if kirilim:
                            print(f"🚨 [KIRILIM] {sym_k} | {sebep}", flush=True)
                            try:
                                try:
                                    exchange.cancel_all_orders(sym_k)
                                except Exception:
                                    pass
                                tum = exchange.fetch_positions()
                                pos_list = [p for p in tum if p['symbol'] == sym_k]
                                for p in pos_list:
                                    kontrat = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                                    if kontrat > 0:
                                        kapat_y = 'sell' if yon_k == 'LONG' else 'buy'
                                        exchange.create_order(sym_k, 'market', kapat_y, kontrat, None, {'reduceOnly': True})
                            except Exception as e:
                                print(f"   ⚠️ Kapatma: {e}", flush=True)

                            pnl = (anlik_k - giris_k) / giris_k * 100 * 5 if yon_k == "LONG" else (giris_k - anlik_k) / giris_k * 100 * 5

                            with state_lock:
                                if pnl > 0:
                                    ANALitik_HAFIZA["basarili_islem_sayisi"] = int(ANALitik_HAFIZA.get("basarili_islem_sayisi", 0)) + 1
                                else:
                                    ANALitik_HAFIZA["basarisiz_islem_sayisi"] = int(ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0)) + 1
                                COIN_COOLDOWNLAR[sym_k] = {"zaman": float(time.time() + COOLDOWN_SURESI_SANIYE), "son_yon": yon_k}
                                if sym_k in AKTIF_GRID_SISTEMLERI:
                                    del AKTIF_GRID_SISTEMLERI[sym_k]

                            hafizayi_kaydet()
                            telegram_mesaj_gonder(
                                f"🚨 *ANİ KIRILIM*\n📌 `{sym_k}` | {yon_k}\n"
                                f"📍 {giris_k} → {anlik_k}\n📊 ROE: %{pnl:+.2f}\n⚡ {sebep}"
                            )
                    except Exception as e:
                        print(f"   ⚠️ Kırılım ({sym_k}): {e}", flush=True)
            except Exception as e:
                print(f"⚠️ Kırılım genel: {e}", flush=True)

            # ==================== SİNYAL TARAMASI ====================
            print(f"{'─'*55}", flush=True)
            print(f"🔍 [TARAMA] Rejim: {piyasa_rejimi} | BTC: {btc_yonu}", flush=True)
            print(f"{'─'*55}", flush=True)

            taranan = []

            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU:
                    break

                with state_lock:
                    cd = COIN_COOLDOWNLAR.get(symbol)
                    if cd:
                        z = cd.get("zaman", 0) if isinstance(cd, dict) else float(cd)
                        kalan = int(z - time.time())
                        if kalan > 0:
                            print(f"⏳ [{symbol}] Cooldown: {kalan}s", flush=True)
                            continue

                try:
                    ticker = exchange.fetch_ticker(symbol)
                    fiyat = float(ticker['last'])
                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    rsi = ta.momentum.rsi(df['close'], window=14).iloc[-1]

                    if piyasa_rejimi == "YATAY":
                        if rsi < 35:
                            islem_yonu = "SHORT"
                            mod = "TERS MOD (Testere)"
                        elif rsi > 65:
                            islem_yonu = "LONG"
                            mod = "TERS MOD (Testere)"
                        else:
                            print(f"🔍 [{symbol}] RSI {rsi:.1f} nötr", flush=True)
                            continue
                    elif piyasa_rejimi == "TREND":
                        # 🆕 COİN UYUMU KONTROLÜ
                        if not coin_uyumlu_mu(symbol, btc_yonu):
                            print(f"⏭️ [{symbol}] Coin BTC ile uyumsuz (BTC:{btc_yonu})", flush=True)
                            continue

                        if btc_yonu == "LONG":
                            if rsi >= RSI_ASIRI_UST:
                                print(f"⏭️ [{symbol}] RSI {rsi:.1f} ≥ {RSI_ASIRI_UST} aşırı alım", flush=True)
                                continue
                            islem_yonu = "LONG"
                            mod = "TREND LONG"
                        elif btc_yonu == "SHORT":
                            if rsi <= RSI_ASIRI_ALT:
                                print(f"⏭️ [{symbol}] RSI {rsi:.1f} ≤ {RSI_ASIRI_ALT} aşırı satım", flush=True)
                                continue
                            islem_yonu = "SHORT"
                            mod = "TREND SHORT"
                        else:
                            continue
                    else:  # TREND_ZAYIF
                        if not coin_uyumlu_mu(symbol, btc_yonu):
                            print(f"⏭️ [{symbol}] Coin BTC ile uyumsuz", flush=True)
                            continue
                        if btc_yonu == "LONG" and rsi < 60:
                            islem_yonu = "LONG"
                            mod = "TREND ZAYIF LONG"
                        elif btc_yonu == "SHORT" and rsi > 40:
                            islem_yonu = "SHORT"
                            mod = "TREND ZAYIF SHORT"
                        else:
                            continue

                    kaldirac, atr_orani = dinamik_kaldirac_hesapla(symbol, fiyat)

                    tp_fiyat, sl_fiyat, kapat_yon, hedef_roe, net_rr = akilli_seviye_hesapla(
                        symbol, fiyat, islem_yonu, ticker, kaldirac
                    )

                    if net_rr < MIN_NET_RR:
                        print(f"⏭️ [{symbol}] Net R/R {net_rr:.2f} < {MIN_NET_RR}", flush=True)
                        continue

                    print(
                        f"✅ [{symbol}] RSI:{rsi:.1f} ATR:%{atr_orani*100:.2f} {kaldirac}x | "
                        f"{islem_yonu} | TP:%{hedef_roe:.1f} R/R:{net_rr:.2f}",
                        flush=True
                    )

                    taranan.append({
                        "symbol": symbol, "yon": islem_yonu, "rsi": rsi,
                        "fiyat": fiyat, "mod": mod,
                        "tp_fiyat": tp_fiyat, "sl_fiyat": sl_fiyat,
                        "kapat_yon": kapat_yon, "hedef_roe": hedef_roe,
                        "kaldirac": kaldirac, "net_rr": net_rr
                    })
                except Exception as e:
                    print(f"⚠️ Tarama ({symbol}): {e}", flush=True)
                    continue

            taranan.sort(key=lambda x: x["net_rr"], reverse=True)

            if taranan:
                print(f"\n📊 [SIRALAMA] {len(taranan)} sinyal:", flush=True)
                for i, s in enumerate(taranan, 1):
                    print(f"   {i}. {s['symbol']} | R/R:{s['net_rr']:.2f} | {s['kaldirac']}x | TP:%{s['hedef_roe']:.1f}", flush=True)

            acilan = 0

            for sinyal in taranan:
                if not BOT_CALISIYOR_MU:
                    break
                if len(aktif_borsa_map) >= MAKSIMUM_TOPLAM_POZISYON:
                    print(f"   ⛔ Max pozisyon", flush=True)
                    break
                if acilan >= MAKSIMUM_TOPLAM_POZISYON:
                    break
                if sinyal["symbol"] in aktif_semboller_listesi:
                    continue

                try:
                    print(f"🚀 [EMİR] {sinyal['symbol']} | {sinyal['yon']} | {sinyal['kaldirac']}x", flush=True)

                    bakiye = exchange.fetch_balance()
                    toplam_b = float(bakiye['total'].get('USDT', 0))
                    serbest_b = float(bakiye.get('free', {}).get('USDT', 0) or 0)

                    kaldirac = sinyal["kaldirac"]
                    exchange.set_leverage(kaldirac, sinyal["symbol"])
                    market = exchange.market(sinyal["symbol"])

                    kullan = min(toplam_b * 0.4, serbest_b)
                    if kullan < 1.0:
                        print(f"   ⛔ Yetersiz bakiye", flush=True)
                        continue

                    giris = sinyal["fiyat"]
                    tp = sinyal["tp_fiyat"]
                    sl = sinyal["sl_fiyat"]
                    kapat_y = sinyal["kapat_yon"]

                    miktar = float(exchange.amount_to_precision(
                        sinyal["symbol"],
                        max((kullan * kaldirac) / giris / float(market.get('contractSize', 1.0)),
                            float(market['limits']['amount']['min'] or 1.0))
                    ))

                    islem_y = 'buy' if sinyal["yon"] == 'LONG' else 'sell'
                    exchange.create_order(sinyal["symbol"], 'market', islem_y, miktar)
                    time.sleep(0.5)

                    try:
                        exchange.create_order(sinyal["symbol"], 'limit', kapat_y, miktar, tp, {'reduceOnly': True})
                        exchange.create_order(sinyal["symbol"], 'stop', kapat_y, miktar, sl, {'stopPrice': sl, 'reduceOnly': True})
                    except Exception as e:
                        print(f"   ⚠️ TP/SL: {e}", flush=True)

                    with state_lock:
                        AKTIF_GRID_SISTEMLERI[sinyal["symbol"]] = {
                            "giris_fiyati": giris, "yon": sinyal["yon"],
                            "tp_fiyat": tp, "sl_fiyat": sl,
                            "giris_rsi": float(sinyal["rsi"]),
                            "giris_zamani": time.time(),
                            "mod": sinyal["mod"],
                            "kaldirac": kaldirac
                        }
                        aktif_semboller_listesi.append(sinyal["symbol"])
                        aktif_borsa_map[sinyal["symbol"]] = {"dummy": True, "symbol": sinyal["symbol"], "contracts": 1}
                        acilan += 1

                    hafizayi_kaydet()

                    print(f"✅ [AÇILDI] {sinyal['symbol']} {sinyal['yon']} @ {giris} ({kaldirac}x) | RoE:%{sinyal['hedef_roe']:.1f}", flush=True)

                    telegram_mesaj_gonder(
                        f"🎯 *İŞLEM ({sinyal['mod']} - {kaldirac}x)*\n"
                        f"📌 `{sinyal['symbol']}` | `{sinyal['yon']}`\n"
                        f"🎯 Giriş: `{giris}`\n"
                        f"💰 TP: `{tp}` (RoE: `%{sinyal['hedef_roe']:.1f}`)\n"
                        f"🛑 SL: `{sl}`\n"
                        f"📊 Net R/R: `{sinyal['net_rr']:.2f}`"
                    )
                except Exception as e:
                    print(f"⚠️ Emir: {e}", flush=True)
                    continue

        except Exception as e:
            print(f"⚠️ Ana döngü: {e}", flush=True)
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
