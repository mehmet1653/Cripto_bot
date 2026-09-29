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

if os.path.exists('/etc/secrets/.env'):
    load_dotenv('/etc/secrets/.env', override=True)
    print("✅ .env yüklendi", flush=True)
else:
    load_dotenv(override=True)

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot aktif!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌ SUPABASE boş!", flush=True); sys.exit(1)
if not TELEGRAM_TOKEN or not CHAT_ID:
    print("❌ TELEGRAM boş!", flush=True); sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

exchange = ccxt.gate({
    'apiKey': os.environ.get("GATE_API_KEY", "").strip(),
    'secret': os.environ.get("GATE_SECRET", "").strip(),
    'enableRateLimit': True,
    'timeout': 30000,
    'options': {'defaultType': 'swap'}
})
exchange.set_sandbox_mode(True)

# 🆕 FALLBACK LISTE (volatilite taraması başarısız olursa)
YEDEK_LISTE = ['SOL/USDT:USDT', 'XRP/USDT:USDT', 'DOGE/USDT:USDT', 'LTC/USDT:USDT', 'LINK/USDT:USDT']

# 🆕 VOLATİLİTE AYARLARI
COIN_SAYISI = 10                    # Kaç coin taranacak
MIN_HACIM_USD = 10_000_000          # Minimum 24h hacim (10M USDT)
CACHE_SURESI = 30 * 60              # Liste 30 dk cache'lensin
TAKIP_EDILENLER = YEDEK_LISTE.copy()
SON_LISTE_GUNCELLE = 0

BOT_CALISIYOR_MU = True
state_lock = threading.Lock()
tarayici_kilidi = threading.Lock()
liste_kilidi = threading.Lock()
SON_BTC_YONU = "YATAY (Testere)"

KALDIRAC_MIN = 5
KALDIRAC_ORTA = 6
KALDIRAC_MAX = 7
ATR_ESIK_YUKSEK = 0.008
ATR_ESIK_DUSUK = 0.004

KOMISYON_ORANI = 0.0008

BEKLENEN_HAREKET_TP_ORANI = 0.50
BEKLENEN_HAREKET_SL_ORANI = 0.35

MOD3_TP_ORANI = 0.50
MOD3_SL_ORANI = 0.35

MIN_NET_RR = 0.8

RSI_ASIRI_UST = 85
RSI_ASIRI_ALT = 15

COIN_TESTERE_RSI_UST = 65
COIN_TESTERE_RSI_ALT = 35

TREND_SKOR_ESIK = 6
ZAYIF_TREND_ESIK = 4

KIRILIM_MIN_KRITER = 3

KIRILIM_KORUMA_BEKLEME = 300
KIRILIM_HACIM_BB = 2.5
KIRILIM_HACIM_MUM = 2.0

# 🆕 VOLATİL COİN SEÇİMİ
def volatil_coinleri_bul():
    global TAKIP_EDILENLER, SON_LISTE_GUNCELLE
    
    with liste_kilidi:
        # Cache kontrolü
        if time.time() - SON_LISTE_GUNCELLE < CACHE_SURESI and TAKIP_EDILENLER:
            return TAKIP_EDILENLER
    
    print("🔍 [COİN SEÇİMİ] Volatil coinler taranıyor...", flush=True)
    
    try:
        tickers = exchange.fetch_tickers()
        
        skorlar = []
        for sym, t in tickers.items():
            # Sadece USDT perpetual
            if not sym.endswith(':USDT'):
                continue
            if sym == 'BTC/USDT:USDT':  # BTC hariç (referans)
                continue
            
            try:
                high = float(t.get('high') or 0)
                low = float(t.get('low') or 0)
                vol = float(t.get('quoteVolume') or 0)
                
                if high <= 0 or low <= 0 or vol < MIN_HACIM_USD:
                    continue
                
                # Volatilite skoru: range × √hacim
                range_orani = (high - low) / low
                skor = range_orani * (vol ** 0.5)
                
                skorlar.append((sym, skor, range_orani, vol))
            except:
                continue
        
        if not skorlar:
            print("⚠️ [COİN SEÇİMİ] Hiç coin bulunamadı, yedek liste kullanılıyor", flush=True)
            return YEDEK_LISTE
        
        skorlar.sort(key=lambda x: x[1], reverse=True)
        secilenler = [s[0] for s in skorlar[:COIN_SAYISI]]
        
        print(f"📊 [COİN SEÇİMİ] {len(skorlar)} coin arasından ilk {len(secilenler)} seçildi:", flush=True)
        for i, s in enumerate(skorlar[:COIN_SAYISI], 1):
            print(f"   {i}. {s[0]} | Range:%{s[2]*100:.2f} | Hacim:{s[3]/1_000_000:.1f}M | Skor:{s[1]:.0f}", flush=True)
        
        with liste_kilidi:
            TAKIP_EDILENLER = secilenler
            SON_LISTE_GUNCELLE = time.time()
        
        return secilenler
    except Exception as e:
        print(f"⚠️ [COİN SEÇİMİ] Hata: {e} — yedek liste kullanılıyor", flush=True)
        return YEDEK_LISTE

def hafizayi_yukle():
    print("💾 Hafıza yükleniyor...", flush=True)
    try:
        response = supabase.table("bot_hafiza").select("*").eq("id", 1).execute()
        if response.data and len(response.data) > 0:
            veri = response.data[0]
            print("💾 Yüklendi.", flush=True)
            return {
                "aktif_sistemler": veri.get("aktif_sistemler", {}),
                "analitik": veri.get("analitik", {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0}),
                "cooldownlar": veri.get("cooldownlar", {})
            }
    except Exception as e:
        print(f"⚠️ Hafıza: {e}", flush=True)
    return {"aktif_sistemler": {}, "analitik": {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0}, "cooldownlar": {}}

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
                "id": 1, "aktif_sistemler": AKTIF_GRID_SISTEMLERI,
                "analitik": payload_analitik, "cooldownlar": clean_cooldowns
            }).execute()
        except Exception as e:
            print(f"⚠️ Kayıt: {e}", flush=True)

kalici_veri = hafizayi_yukle()
AKTIF_GRID_SISTEMLERI = kalici_veri.get("aktif_sistemler", {})
ANALitik_HAFIZA = kalici_veri.get("analitik", {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0})
COIN_COOLDOWNLAR = kalici_veri.get("cooldownlar", {})

MAKSIMUM_TOPLAM_POZISYON = 2
COOLDOWN_SURESI_SANIYE = 15 * 60

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
        print(f"⚠️ Kaldıraç: {e}", flush=True)
        return KALDIRAC_MIN, 0.01

def piyasa_rejimini_tespit_et():
    global SON_BTC_YONU
    try:
        ohlcv_btc = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='1h', limit=50)
        df = pd.DataFrame(ohlcv_btc, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

        adx_ind = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14)
        adx = adx_ind.adx().iloc[-1]
        adx_pos = adx_ind.adx_pos().iloc[-1]
        adx_neg = adx_ind.adx_neg().iloc[-1]

        bb = ta.volatility.BollingerBands(close=df['close'], window=20, window_dev=2)
        bbw = (bb.bollinger_hband().iloc[-1] - bb.bollinger_lband().iloc[-1]) / bb.bollinger_mavg().iloc[-1]

        ema9 = ta.trend.ema_indicator(df['close'], window=9).iloc[-1]
        ema21 = ta.trend.ema_indicator(df['close'], window=21).iloc[-1]
        ema50 = ta.trend.ema_indicator(df['close'], window=50).iloc[-1]
        ema_fark = abs(ema9 - ema21) / ema21 * 100

        fiyat = df['close'].iloc[-1]
        ema50_fark = abs(fiyat - ema50) / ema50 * 100

        son_10 = df['close'].iloc[-10:].values
        yukari = sum(1 for i in range(1, len(son_10)) if son_10[i] > son_10[i-1])
        tek_yonlu = max(yukari, len(son_10) - 1 - yukari) >= 7

        atr_seri = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range()
        atr_simdi = atr_seri.iloc[-1]
        atr_once = atr_seri.iloc[-11] if len(atr_seri) >= 11 else atr_simdi
        atr_artiyor = atr_simdi > atr_once * 1.1

        skor = 0
        if adx >= 30: skor += 2
        if adx >= 45: skor += 1
        if bbw >= 0.03: skor += 2
        if bbw >= 0.05: skor += 1
        if ema_fark >= 0.5: skor += 2
        if ema50_fark >= 1.0: skor += 1
        if tek_yonlu: skor += 2
        if atr_artiyor: skor += 1

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

        print(f"📊 [REJİM] {rejim} | {trend_yonu} | Skor:{skor}/12 | ADX:{adx:.1f}", flush=True)
        return rejim, trend_yonu
    except Exception as e:
        print(f"⚠️ Rejim: {e}", flush=True)
        return "YATAY", "YATAY (Testere)"

def kirilim_tespit_et(symbol):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

        fiyat = df['close'].iloc[-1]

        bb = ta.volatility.BollingerBands(close=df['close'], window=20, window_dev=2)
        bb_ust = bb.bollinger_hband().iloc[-1]
        bb_alt = bb.bollinger_lband().iloc[-1]

        bb_ust_kirilim = fiyat > bb_ust
        bb_alt_kirilim = fiyat < bb_alt

        son_3 = df['close'].iloc[-3:].values
        artan = all(son_3[i] < son_3[i+1] for i in range(len(son_3)-1))
        azalan = all(son_3[i] > son_3[i+1] for i in range(len(son_3)-1))

        son_hacim = float(df['volume'].iloc[-1])
        ort_hacim = float(df['volume'].iloc[-21:-1].mean()) if len(df) >= 21 else 1.0
        hacim_orani = son_hacim / ort_hacim if ort_hacim > 0 else 1.0
        hacim_onay = hacim_orani > 1.3

        ema9 = ta.trend.ema_indicator(df['close'], window=9).iloc[-1]
        ema21 = ta.trend.ema_indicator(df['close'], window=21).iloc[-1]
        ema_ust = ema9 > ema21
        ema_alt = ema9 < ema21

        yukari_kriter = 0
        if bb_ust_kirilim: yukari_kriter += 1
        if artan: yukari_kriter += 1
        if hacim_onay: yukari_kriter += 1
        if ema_ust: yukari_kriter += 1

        asagi_kriter = 0
        if bb_alt_kirilim: asagi_kriter += 1
        if azalan: asagi_kriter += 1
        if hacim_onay: asagi_kriter += 1
        if ema_alt: asagi_kriter += 1

        if yukari_kriter >= KIRILIM_MIN_KRITER:
            return "LONG", True, f"Yukarı ({yukari_kriter}/4) x{hacim_orani:.1f}"
        elif asagi_kriter >= KIRILIM_MIN_KRITER:
            return "SHORT", True, f"Aşağı ({asagi_kriter}/4) x{hacim_orani:.1f}"
        else:
            return "NONE", False, f"Yok (Üst:{yukari_kriter}/4 Alt:{asagi_kriter}/4)"
    except Exception as e:
        print(f"⚠️ Kırılım ({symbol}): {e}", flush=True)
        return "NONE", False, "Hata"

def beklenen_hareket_hesapla(symbol, anlik_fiyat, ticker_data):
    tahminler = []
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=30)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range().iloc[-1]
        tahminler.append(atr * 4)
    except: pass

    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=30)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range().iloc[-1]
        tahminler.append(atr * 2)
    except: pass

    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='4h', limit=30)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range().iloc[-1]
        tahminler.append(atr)
    except: pass

    try:
        high_24h = float(ticker_data.get('high') or anlik_fiyat)
        low_24h = float(ticker_data.get('low') or anlik_fiyat)
        tahminler.append((high_24h - low_24h) * 0.3)
    except: pass

    if not tahminler:
        return anlik_fiyat * 0.005
    return float(min(max(tahminler), anlik_fiyat * 0.05))

def akilli_seviye_hesapla(symbol, anlik_fiyat, yon, ticker_data, kaldirac, mod=""):
    beklenen = beklenen_hareket_hesapla(symbol, anlik_fiyat, ticker_data)

    if "MOD3" in mod:
        tp_orani = MOD3_TP_ORANI
        sl_orani = MOD3_SL_ORANI
    else:
        tp_orani = BEKLENEN_HAREKET_TP_ORANI
        sl_orani = BEKLENEN_HAREKET_SL_ORANI

    tp_mesafe = beklenen * tp_orani
    sl_mesafe = beklenen * sl_orani

    komisyon = anlik_fiyat * KOMISYON_ORANI
    net_rr = (tp_mesafe - komisyon) / (sl_mesafe + komisyon) if (sl_mesafe + komisyon) > 0 else 0

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

def telegram_mesaj_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": mesaj, "parse_mode": "Markdown"}, timeout=5)
    except: pass

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
            kaldirac_val = int(p.get('leverage', 5))
            ticker_data = await asyncio.to_thread(exchange.fetch_ticker, sym)
            guncel_fiyat = float(ticker_data['last'])
            fark = (guncel_fiyat - giris) / giris if yon == "LONG" else (giris - guncel_fiyat) / giris
            roe = fark * 100 * kaldirac_val
            pos_detaylari += f"\n• `{sym}` | {yon} ({kaldirac_val}x) | Giriş: `{giris}`\n  Anlık ROE: `%{roe:+.2f}`"

        liste_str = ", ".join([s.split('/')[0] for s in TAKIP_EDILENLER[:10]])

        mesaj = (
            f"📊 *BOT DURUM* [DİNAMİK COİN]\n\n"
            f"🌐 Rejim: `{rejim}` (BTC: `{btc_yon}`)\n"
            f"💰 Kasa: `{total:.2f} USDT` | PnL: `{toplam_pnl:+.2f}`\n"
            f"📌 Açık: `{len(borsa_poslari)} / {MAKSIMUM_TOPLAM_POZISYON}`\n"
            f"🎯 Takip: `{liste_str}`"
            f"{pos_detaylari}\n\n"
            f"✅ TP: `{basarili}` | ❌ SL: `{basarisiz}`\n"
            f"📈 Başarı: `%{basari_orani:.1f}`"
        )
        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = True
    await update.message.reply_text("🟢 Bot aktif! (Dinamik coin seçimi - 10 coin)")

async def durdur_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ Bot durduruldu.")

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
    print("🚀 [BAŞLANGIÇ] Dinamik coin seçimi aktif...", flush=True)
    try:
        exchange.load_markets()
    except: pass

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

            # 🆕 DİNAMİK COİN LİSTESİ
            volatil_coinleri_bul()

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

            # POZİSYON KAPANIŞ
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
                                bas += 1; tip = "✅ *KÂRLA KAPANDI*"
                            else:
                                basz += 1; tip = "❌ *ZARARLA KAPANDI*"
                            ANALitik_HAFIZA["basarili_islem_sayisi"] = bas
                            ANALitik_HAFIZA["basarisiz_islem_sayisi"] = basz
                            COIN_COOLDOWNLAR[eski_sym] = {"zaman": float(time.time() + COOLDOWN_SURESI_SANIYE), "son_yon": yon}
                            if eski_sym in AKTIF_GRID_SISTEMLERI:
                                del AKTIF_GRID_SISTEMLERI[eski_sym]

                        hafizayi_kaydet()
                        print(f"💰 [KAPANIŞ] {eski_sym} | Çıkış: {cikis}", flush=True)
                        telegram_mesaj_gonder(f"{tip}\n📌 `{eski_sym}` | Çıkış: `{cikis}`")
            except Exception as e:
                print(f"⚠️ Kapanış: {e}", flush=True)

            # ANİ KIRILIM KORUMASI
            try:
                with state_lock:
                    aktif_p = list(AKTIF_GRID_SISTEMLERI.items())

                for sym_k, kayit_k in aktif_p:
                    if sym_k not in AKTIF_GRID_SISTEMLERI:
                        continue

                    yon_k = kayit_k.get("yon", "LONG")
                    giris_k = float(kayit_k.get("giris_fiyati", 0))
                    giris_zaman = float(kayit_k.get("giris_zamani", 0))
                    mod_k = str(kayit_k.get("mod", ""))

                    if time.time() - giris_zaman < KIRILIM_KORUMA_BEKLEME:
                        continue

                    if "MOD2" not in mod_k and "MOD3" not in mod_k:
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
                        ort_hacim_k = float(df_k['volume'].iloc[-21:-1].mean()) if len(df_k) >= 21 else 1.0
                        hacim_orani_k = son_hacim_k / ort_hacim_k if ort_hacim_k > 0 else 1.0
                        hacim_patlama = hacim_orani_k > KIRILIM_HACIM_BB

                        son_3_k = df_k['close'].iloc[-3:].values
                        artan_k = all(son_3_k[i] < son_3_k[i+1] for i in range(len(son_3_k)-1))
                        azalan_k = all(son_3_k[i] > son_3_k[i+1] for i in range(len(son_3_k)-1))
                        mum_kirilim = artan_k or azalan_k

                        kirilim_var = False
                        kirilim_sebep = ""

                        if bb_disari and hacim_patlama:
                            if yon_k == "LONG" and bb_yon_k == "aşağı":
                                kirilim_var = True
                                kirilim_sebep = f"BB↓ Hacim x{hacim_orani_k:.1f}"
                            elif yon_k == "SHORT" and bb_yon_k == "yukarı":
                                kirilim_var = True
                                kirilim_sebep = f"BB↑ Hacim x{hacim_orani_k:.1f}"
                        elif mum_kirilim and hacim_orani_k > KIRILIM_HACIM_MUM:
                            if yon_k == "LONG" and azalan_k:
                                kirilim_var = True
                                kirilim_sebep = f"3 mum↓ x{hacim_orani_k:.1f}"
                            elif yon_k == "SHORT" and artan_k:
                                kirilim_var = True
                                kirilim_sebep = f"3 mum↑ x{hacim_orani_k:.1f}"

                        if kirilim_var:
                            print(f"🚨 [ANİ KIRILIM] {sym_k} | {yon_k} | {kirilim_sebep}", flush=True)

                            try:
                                try: exchange.cancel_all_orders(sym_k)
                                except: pass
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
                                f"📍 {giris_k} → {anlik_k}\n📊 ROE: %{pnl:+.2f}\n⚡ {kirilim_sebep}"
                            )
                    except Exception as e:
                        print(f"   ⚠️ Kırılım: {e}", flush=True)
            except Exception as e:
                print(f"⚠️ Kırılım genel: {e}", flush=True)

            print(f"{'─'*55}", flush=True)
            print(f"🔍 [TARAMA] Rejim: {piyasa_rejimi} | BTC: {btc_yonu} | {len(TAKIP_EDILENLER)} coin", flush=True)
            print(f"{'─'*55}", flush=True)

            taranan = []

            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU: break

                with state_lock:
                    cd = COIN_COOLDOWNLAR.get(symbol)
                    if cd:
                        z = cd.get("zaman", 0) if isinstance(cd, dict) else float(cd)
                        kalan = int(z - time.time())
                        if kalan > 0:
                            continue  # sessizce atla (log kalabalığı olmasın)

                try:
                    ticker = exchange.fetch_ticker(symbol)
                    fiyat = float(ticker['last'])

                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    rsi = ta.momentum.rsi(df['close'], window=14).iloc[-1]

                    kirilim_yonu, kirilim_var, sebep = kirilim_tespit_et(symbol)

                    if piyasa_rejimi == "YATAY":
                        if rsi < COIN_TESTERE_RSI_ALT:
                            islem_yonu = "SHORT"
                            mod = f"MOD2 YATAY (RSI<{COIN_TESTERE_RSI_ALT})"
                        elif rsi > COIN_TESTERE_RSI_UST:
                            islem_yonu = "LONG"
                            mod = f"MOD2 YATAY (RSI>{COIN_TESTERE_RSI_UST})"
                        else:
                            continue

                    elif piyasa_rejimi in ["TREND", "TREND_ZAYIF"]:
                        if kirilim_var:
                            if btc_yonu == "SHORT" and kirilim_yonu == "SHORT":
                                islem_yonu = "SHORT"
                                mod = f"MOD1 TREND SHORT"
                            elif btc_yonu == "LONG" and kirilim_yonu == "LONG":
                                islem_yonu = "LONG"
                                mod = f"MOD1 TREND LONG"
                            else:
                                continue
                        else:
                            if rsi < COIN_TESTERE_RSI_ALT:
                                islem_yonu = "LONG"
                                mod = f"MOD3 TERS (RSI:{rsi:.1f})"
                            elif rsi > COIN_TESTERE_RSI_UST:
                                islem_yonu = "SHORT"
                                mod = f"MOD3 TERS (RSI:{rsi:.1f})"
                            else:
                                continue
                    else:
                        continue

                    kaldirac, atr_orani = dinamik_kaldirac_hesapla(symbol, fiyat)

                    tp_fiyat, sl_fiyat, kapat_yon, hedef_roe, net_rr = akilli_seviye_hesapla(
                        symbol, fiyat, islem_yonu, ticker, kaldirac, mod
                    )

                    if "MOD3" not in mod and net_rr < MIN_NET_RR:
                        continue

                    print(
                        f"✅ [{symbol}] {islem_yonu} | {mod} | {kaldirac}x | TP:%{hedef_roe:.1f} R/R:{net_rr:.2f}",
                        flush=True
                    )

                    taranan.append({
                        "symbol": symbol, "yon": islem_yonu,
                        "fiyat": fiyat, "mod": mod,
                        "tp_fiyat": tp_fiyat, "sl_fiyat": sl_fiyat,
                        "kapat_yon": kapat_yon, "hedef_roe": hedef_roe,
                        "kaldirac": kaldirac, "net_rr": net_rr
                    })
                except Exception as e:
                    continue

            taranan.sort(key=lambda x: x["net_rr"], reverse=True)

            if taranan:
                print(f"\n📊 [SIRALAMA] {len(taranan)} sinyal:", flush=True)
                for i, s in enumerate(taranan, 1):
                    print(f"   {i}. {s['symbol']} | R/R:{s['net_rr']:.2f} | {s['kaldirac']}x | {s['yon']} | {s['mod'][:25]}", flush=True)

            acilan = 0

            for sinyal in taranan:
                if not BOT_CALISIYOR_MU: break
                if len(aktif_borsa_map) >= MAKSIMUM_TOPLAM_POZISYON: break
                if acilan >= MAKSIMUM_TOPLAM_POZISYON: break
                if sinyal["symbol"] in aktif_semboller_listesi: continue

                try:
                    print(f"🚀 [EMİR] {sinyal['symbol']} | {sinyal['yon']} | {sinyal['kaldirac']}x", flush=True)

                    bakiye = exchange.fetch_balance()
                    toplam_b = float(bakiye['total'].get('USDT', 0))
                    serbest_b = float(bakiye.get('free', {}).get('USDT', 0) or 0)

                    kaldirac = sinyal["kaldirac"]
                    exchange.set_leverage(kaldirac, sinyal["symbol"])
                    market = exchange.market(sinyal["symbol"])

                    kullan = min(toplam_b * 0.4, serbest_b)
                    if kullan < 1.0: continue

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

                    sl_basarili = False
                    try:
                        exchange.create_order(sinyal["symbol"], 'limit', kapat_y, miktar, tp, {'reduceOnly': True})
                        exchange.create_order(sinyal["symbol"], 'stop', kapat_y, miktar, sl, {'stopPrice': sl, 'reduceOnly': True})
                        sl_basarili = True
                    except Exception as e:
                        print(f"   ⚠️ TP/SL hatası: {e}", flush=True)

                    if not sl_basarili:
                        print(f"   🚨 SL basılamadı, acil kapatma!", flush=True)
                        try:
                            exchange.create_order(sinyal["symbol"], 'market', kapat_y, miktar, None, {'reduceOnly': True})
                        except: pass
                        continue

                    with state_lock:
                        AKTIF_GRID_SISTEMLERI[sinyal["symbol"]] = {
                            "giris_fiyati": giris, "yon": sinyal["yon"],
                            "tp_fiyat": tp, "sl_fiyat": sl,
                            "giris_zamani": time.time(),
                            "mod": sinyal["mod"],
                            "kaldirac": kaldirac
                        }
                        aktif_semboller_listesi.append(sinyal["symbol"])
                        aktif_borsa_map[sinyal["symbol"]] = {"dummy": True, "symbol": sinyal["symbol"], "contracts": 1}
                        acilan += 1

                    hafizayi_kaydet()

                    print(f"✅ [AÇILDI] {sinyal['symbol']} {sinyal['yon']} @ {giris} ({kaldirac}x)", flush=True)

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
            try: tarayici_kilidi.release()
            except: pass

        time.sleep(5)

async def main():
    web_thread = threading.Thread(target=run_web, daemon=True)
    web_thread.start()

    app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    try:
        requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook?drop_pending_updates=True", timeout=5)
    except: pass

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
