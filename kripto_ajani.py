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
exchange.set_sandbox_mode(True)  # ⚠️ Gerçek hesaba geçerken False yap!

TAKIP_EDILENLER = ['SOL/USDT:USDT', 'XRP/USDT:USDT', 'DOGE/USDT:USDT', 'LTC/USDT:USDT', 'LINK/USDT:USDT']

BOT_CALISIYOR_MU = True
state_lock = threading.Lock()
borsa_kilidi = threading.Lock()
tarayici_kilidi = threading.Lock()

# 🆕 KALDIRAÇLAR
KALDIRAC_TREND = 5
KALDIRAC_RANGE = 10

MAKSIMUM_TOPLAM_POZISYON = 2
COOLDOWN_SURESI_SANIYE = 30 * 60
TREND_COOLDOWN_SANIYE = 60 * 60
SWING_LOOKBACK = 50

# 🆕 KOMİSYON VE MALİYET AYARLARI
KOMISYON_ORANI = 0.001       # Gidiş-dönüş toplam komisyon (~%0.1)
SPREAD_MALIYETI = 0.0005     # Spread maliyeti (~%0.05)
MIN_NET_KAR = 0.003          # Minimum net kâr hedefi (~%0.3)
MIN_RR = 2.0                 # Trend modu için min Risk/Ödül
TP_GERI_CEKME = 0.005        # Direnç/desteğin %0.5 öncesi TP

# Trailing (Sadece Trend modu)
TRAILING_SEVIYELER = [
    (15.0, 0.10), (10.0, 0.05), (6.0, 0.02), (3.0, 0.01),
]

def hafizayi_yukle():
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
        print(f"⚠️ Hafıza: {e}", flush=True)
    return {"aktif_sistemler": {}, "analitik": {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0}, "cooldownlar": {}}

def hafizayi_kaydet():
    with state_lock:
        try:
            payload = {
                "basarili_islem_sayisi": int(ANALITIK.get("basarili_islem_sayisi", 0)),
                "basarisiz_islem_sayisi": int(ANALITIK.get("basarisiz_islem_sayisi", 0))
            }
            clean_cd = {}
            for k, v in COIN_COOLDOWN.items():
                if isinstance(v, dict):
                    clean_cd[k] = {"zaman": float(v.get("zaman", 0)), "son_yon": str(v.get("son_yon", ""))}
                else:
                    clean_cd[k] = {"zaman": float(v), "son_yon": ""}
            supabase.table("bot_hafiza").upsert({
                "id": 1, "aktif_sistemler": AKTIF_POZISYONLAR,
                "analitik": payload, "cooldownlar": clean_cd
            }).execute()
        except Exception as e:
            print(f"⚠️ Kayıt: {e}", flush=True)

kalici = hafizayi_yukle()
AKTIF_POZISYONLAR = kalici.get("aktif_sistemler", {})
ANALITIK = kalici.get("analitik", {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0})
COIN_COOLDOWN = kalici.get("cooldownlar", {})

# 🆕 PİYASA REJİMİ TESPİTİ
def piyasa_rejimi_tespit_et(df):
    try:
        adx = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14).adx().iloc[-1]
        bb = ta.volatility.BollingerBands(df['close'], window=20, window_dev=2)
        bb_ust = bb.bollinger_hband().iloc[-1]
        bb_alt = bb.bollinger_lband().iloc[-1]
        anlik = df['close'].iloc[-1]
        bb_genislik = ((bb_ust - bb_alt) / anlik) * 100

        if adx < 20 and bb_genislik < 3.0:
            return "YATAY"
        elif adx > 25:
            return "TREND"
        else:
            return "BELIRSIZ"
    except:
        return "BELIRSIZ"

def swing_noktalari_bul(df, lookback=SWING_LOOKBACK):
    highs, lows = [], []
    baslangic = max(2, len(df) - lookback)
    for i in range(baslangic, len(df) - 2):
        h, l = df['high'].iloc[i], df['low'].iloc[i]
        if (h > df['high'].iloc[i-1] and h > df['high'].iloc[i-2] and h > df['high'].iloc[i+1] and h > df['high'].iloc[i+2]):
            highs.append((i, h))
        if (l < df['low'].iloc[i-1] and l < df['low'].iloc[i-2] and l < df['low'].iloc[i+1] and l < df['low'].iloc[i+2]):
            lows.append((i, l))
    return highs, lows

def likidite_seviyeleri(df, highs, lows, anlik_fiyat):
    def cluster(levels, tolerance=0.003):
        if not levels: return []
        sorted_l = sorted(levels)
        result = [sorted_l[0]]
        for lvl in sorted_l[1:]:
            if abs(lvl - result[-1]) / result[-1] > tolerance:
                result.append(lvl)
        return result
    high_levels = cluster([h[1] for h in highs])
    low_levels = cluster([l[1] for l in lows])
    destekler = sorted([l for l in low_levels if l < anlik_fiyat], reverse=True)[:3]
    direncler = sorted([h for h in high_levels if h > anlik_fiyat])[:3]
    return destekler, direncler

# 🆕 TREND MODU: ATR TABANLI SL
def sweep_tespit_et(df, destekler, direncler, anlik_fiyat):
    if len(df) < 3:
        return None, None, None, None

    son_mum = df.iloc[-1]
    onceki = df.iloc[-2]
    iki_onceki = df.iloc[-3]
    atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range().iloc[-1]

    for destek in destekler:
        kirildi, en_dusuk = False, 999999999
        if iki_onceki['low'] < destek: kirildi, en_dusuk = True, min(iki_onceki['low'], en_dusuk)
        if onceki['low'] < destek: kirildi, en_dusuk = True, min(onceki['low'], en_dusuk)
        if kirildi and son_mum['close'] > destek and anlik_fiyat > destek:
            if (destek - en_dusuk) / destek < 0.02:
                # ATR tabanlı SL (1.5 ATR altı)
                sl_fiyat = en_dusuk - (atr * 1.5)
                # Maksimum %2 SL sınırı
                if (anlik_fiyat - sl_fiyat) / anlik_fiyat > 0.02:
                    sl_fiyat = anlik_fiyat * 0.98
                return "LONG", destek, sl_fiyat, en_dusuk

    for direnc in direncler:
        kirildi, en_yuksek = False, 0
        if iki_onceki['high'] > direnc: kirildi, en_yuksek = True, max(iki_onceki['high'], en_yuksek)
        if onceki['high'] > direnc: kirildi, en_yuksek = True, max(onceki['high'], en_yuksek)
        if kirildi and son_mum['close'] < direnc and anlik_fiyat < direnc:
            if (en_yuksek - direnc) / direnc < 0.02:
                sl_fiyat = en_yuksek + (atr * 1.5)
                if (sl_fiyat - anlik_fiyat) / anlik_fiyat > 0.02:
                    sl_fiyat = anlik_fiyat * 1.02
                return "SHORT", direnc, sl_fiyat, en_yuksek

    return None, None, None, None

# 🆕 TP HESAPLAMA (Komisyon farkındalıklı)
def tp_hesapla(yon, giris, sl, destekler, direncler, atr):
    if yon == "LONG":
        for d in direncler:
            if d > giris:
                tp = d * (1 - TP_GERI_CEKME)  # Direncin %0.5 öncesi
                rr = (tp - giris) / (giris - sl) if giris > sl else 0
                if rr >= MIN_RR:
                    return tp, rr
        tp = giris + (atr * 3.0)
        return tp, (tp - giris) / (giris - sl) if giris > sl else 0
    else:
        for d in sorted(destekler, reverse=True):
            if d < giris:
                tp = d * (1 + TP_GERI_CEKME)
                rr = (giris - tp) / (sl - giris) if sl > giris else 0
                if rr >= MIN_RR:
                    return tp, rr
        tp = giris - (atr * 3.0)
        return tp, (giris - tp) / (sl - giris) if sl > giris else 0

# 🆕 YATAY MOD: ATR TABANLI SL (0.5 ATR)
def range_sinyal_uret(df, destekler, direncler, anlik, atr):
    if not destekler or not direncler:
        return None, None, None

    en_yakin_destek = destekler[0]
    en_yakin_direnc = direncler[0]
    mesafe_destek = (anlik - en_yakin_destek) / anlik
    mesafe_direnc = (en_yakin_direnc - anlik) / anlik

    # LONG: Desteğe yakın, TP direncin hemen öncesi
    if mesafe_destek < 0.005 and mesafe_direnc > 0.01:
        sl = en_yakin_destek - (atr * 0.5)  # Desteğin 0.5 ATR altı
        tp = en_yakin_direnc * (1 - TP_GERI_CEKME)  # Direncin %0.5 öncesi
        return "LONG", tp, sl

    # SHORT: Dirençe yakın, TP desteğin hemen öncesi
    if mesafe_direnc < 0.005 and mesafe_destek > 0.01:
        sl = en_yakin_direnc + (atr * 0.5)
        tp = en_yakin_destek * (1 + TP_GERI_CEKME)
        return "SHORT", tp, sl

    return None, None, None

# 🆕 KOMİSYON KONTROLÜ
def net_kar_yeterli_mi(yon, giris, tp):
    """Brüt kâr, komisyon + spread maliyetini karşılıyor mu?"""
    if yon == "LONG":
        brut_kar_orani = (tp - giris) / giris
    else:
        brut_kar_orani = (giris - tp) / giris
    net_kar = brut_kar_orani - KOMISYON_ORANI - SPREAD_MALIYETI
    return net_kar >= MIN_NET_KAR, net_kar

def tg_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": mesaj, "parse_mode": "Markdown"}, timeout=5)
    except: pass

# ==================== TELEGRAM ====================
async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    try:
        with borsa_kilidi:
            balance = await asyncio.to_thread(exchange.fetch_balance)
            total = float(balance['total'].get('USDT', 0))
            pos = [p for p in await asyncio.to_thread(exchange.fetch_positions) if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
            pnl = sum(float(p.get('unrealizedPnl', 0)) for p in pos)
        bas = int(ANALITIK.get("basarili_islem_sayisi", 0))
        basz = int(ANALITIK.get("basarisiz_islem_sayisi", 0))
        top = bas + basz
        oran = (bas / top * 100) if top > 0 else 0

        pos_detay = ""
        for p in pos:
            sym = p['symbol']
            y = str(p.get('side', '')).upper() or "?"
            g = float(p.get('entryPrice', 0))
            k = int(p.get('leverage', 5))
            mod = AKTIF_POZISYONLAR.get(sym, {}).get("mod", "?")
            t = await asyncio.to_thread(exchange.fetch_ticker, sym)
            gf = float(t['last'])
            f = (gf - g) / g if y == "LONG" else (g - gf) / g
            roe = f * 100 * k
            pos_detay += f"\n• `{sym}` | {y} ({k}x) [{mod}]\n  Giriş: `{g}` | ROE: `%{roe:+.2f}`"

        mesaj = (
            f"📊 *DURUM* [HİBRİT: RANGE + TREND]\n\n"
            f"💰 Kasa: `{total:.2f} USDT` | PnL: `{pnl:+.2f}`\n"
            f"📌 Açık: `{len(pos)} / {MAKSIMUM_TOPLAM_POZISYON}`"
            f"{pos_detay}\n\n"
            f"✅ TP: `{bas}` | ❌ SL: `{basz}`\n"
            f"📈 Başarı: `%{oran:.1f}`"
        )
        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = True
    await update.message.reply_text("🟢 Bot aktif! (Hibrit Mod)")

async def durdur_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ Durduruldu.")

async def kapat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    try:
        with borsa_kilidi:
            positions = await asyncio.to_thread(exchange.fetch_positions)
            for p in positions:
                k = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                if k > 0:
                    y = str(p.get('side', '')).upper() or "LONG"
                    ky = 'sell' if y == 'LONG' else 'buy'
                    try: exchange.cancel_all_orders(p['symbol'])
                    except: pass
                    exchange.create_order(p['symbol'], 'market', ky, k, None, {'reduceOnly': True})
        await update.message.reply_text("✅ Kapatıldı.")
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

# ==================== TRAILING STOP (Sadece TREND) ====================
def trailing_stop_kontrol():
    with state_lock:
        aktif_kopya = list(AKTIF_POZISYONLAR.items())

    for sym, bilgi in aktif_kopya:
        if bilgi.get("mod") != "TREND": continue
        if sym not in AKTIF_POZISYONLAR: continue

        yon = bilgi.get("yon", "LONG")
        g = float(bilgi.get("giris_fiyati", 0))
        sl_kayitli = float(bilgi.get("sl_fiyat", 0))
        kaldirac_v = int(bilgi.get("kaldirac", KALDIRAC_TREND))
        giris_zaman = float(bilgi.get("giris_zamani", 0))

        if time.time() - giris_zaman < 120: continue

        try:
            with borsa_kilidi:
                t = exchange.fetch_ticker(sym)
                anlik = float(t['last'])
        except: continue

        roe = ((anlik - g) / g * 100 * kaldirac_v) if yon == "LONG" else ((g - anlik) / g * 100 * kaldirac_v)

        yeni_sl = None
        for esik_roe, kilit_orani in TRAILING_SEVIYELER:
            if roe >= esik_roe:
                yeni_sl = g * (1 + kilit_orani / kaldirac_v) if yon == "LONG" else g * (1 - kilit_orani / kaldirac_v)
                break

        if yeni_sl is None: continue

        iyilestirme = (yeni_sl > sl_kayitli * 1.0005) if yon == "LONG" else (yeni_sl < sl_kayitli * 0.9995)
        if not iyilestirme: continue

        try:
            with borsa_kilidi:
                exchange.cancel_all_orders(sym)
                miktar = None
                for p in exchange.fetch_positions():
                    if p['symbol'] == sym:
                        miktar = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                        break
                if not miktar or miktar <= 0: continue
                ky = 'sell' if yon == 'LONG' else 'buy'
                exchange.create_order(sym, 'stop', ky, miktar, yeni_sl, {'stopPrice': yeni_sl, 'reduceOnly': True})
                tp_kayitli = float(bilgi.get("tp_fiyat", 0))
                if tp_kayitli > 0:
                    exchange.create_order(sym, 'limit', ky, miktar, tp_kayitli, {'reduceOnly': True})

            with state_lock:
                if sym in AKTIF_POZISYONLAR:
                    AKTIF_POZISYONLAR[sym]["sl_fiyat"] = yeni_sl
            print(f"🔒 [TRAILING] {sym} | ROE:%{roe:.1f} → SL:{yeni_sl:.6f}", flush=True)
            tg_gonder(f"🔒 *KÂR KİLİTLENDİ*\n📌 `{sym}` | {yon}\n📊 ROE: `%{roe:+.2f}`\n🛑 Yeni SL: `{yeni_sl:.6f}`")
        except Exception as e:
            print(f"⚠️ Trailing hatası {sym}: {e}", flush=True)

# ==================== ANA TARAYICI ====================
def tarayici():
    print("🚀 [BAŞLANGIÇ] HİBRİT MOD (Range + Trend) + Komisyon Korumalı...", flush=True)
    try:
        exchange.load_markets()
    except: pass

    dongu = 0
    while True:
        if not tarayici_kilidi.acquire(blocking=False):
            time.sleep(2); continue
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5); continue
            dongu += 1
            print(f"\n{'='*55}\n🔄 [DÖNGÜ #{dongu}] {time.strftime('%H:%M:%S')}", flush=True)

            try:
                with borsa_kilidi:
                    raw = exchange.fetch_positions()
                aktif_map = {p['symbol']: p for p in raw if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0}
                aktif_list = list(aktif_map.keys())
            except:
                raw, aktif_map, aktif_list = [], {}, []

            # KAPANIŞ KONTROLÜ
            try:
                anlik_aktif = list(aktif_map.keys())
                for eski in list(AKTIF_POZISYONLAR.keys()):
                    if eski not in anlik_aktif:
                        bilgi = AKTIF_POZISYONLAR[eski]
                        g = bilgi.get("giris_fiyati", 0)
                        y = bilgi.get("yon", "LONG")
                        tp_k = bilgi.get("tp_fiyat", g)
                        sl_k = bilgi.get("sl_fiyat", g)
                        karli, cikis = False, g
                        try:
                            with borsa_kilidi:
                                t = exchange.fetch_ticker(eski)
                            cikis = float(t['last'])
                            karli = abs(cikis - tp_k) < abs(cikis - sl_k)
                        except:
                            karli = cikis > g if y == "LONG" else cikis < g

                        with state_lock:
                            b = int(ANALITIK.get("basarili_islem_sayisi", 0))
                            bz = int(ANALITIK.get("basarisiz_islem_sayisi", 0))
                            if karli:
                                b += 1; tip = "✅ *KÂRLA KAPANDI*"
                            else:
                                bz += 1; tip = "❌ *ZARARLA KAPANDI*"
                            ANALITIK["basarili_islem_sayisi"] = b
                            ANALITIK["basarisiz_islem_sayisi"] = bz
                            COIN_COOLDOWN[eski] = {"zaman": float(time.time() + COOLDOWN_SURESI_SANIYE), "son_yon": y}
                            if eski in AKTIF_POZISYONLAR: del AKTIF_POZISYONLAR[eski]
                        hafizayi_kaydet()
                        print(f"💰 [KAPANIŞ] {eski} | Çıkış: {cikis}", flush=True)
                        tg_gonder(f"{tip}\n📌 `{eski}` | Çıkış: `{cikis}`")
            except Exception as e:
                print(f"⚠️ Kapanış: {e}", flush=True)

            # MOD DEĞİŞİMİ KONTROLÜ
            try:
                for sym in list(AKTIF_POZISYONLAR.keys()):
                    bilgi = AKTIF_POZISYONLAR[sym]
                    if bilgi.get("mod") == "RANGE":
                        with borsa_kilidi:
                            ohlcv = exchange.fetch_ohlcv(sym, timeframe='15m', limit=60)
                        df_k = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                        rejim = piyasa_rejimi_tespit_et(df_k)
                        if rejim == "TREND":
                            with borsa_kilidi:
                                pos = [p for p in exchange.fetch_positions() if p['symbol'] == sym and float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
                                for p in pos:
                                    k = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                                    y = str(p.get('side', '')).upper()
                                    ky = 'sell' if y == 'LONG' else 'buy'
                                    try: exchange.cancel_all_orders(sym)
                                    except: pass
                                    exchange.create_order(sym, 'market', ky, k, None, {'reduceOnly': True})
                            with state_lock:
                                if sym in AKTIF_POZISYONLAR: del AKTIF_POZISYONLAR[sym]
                                COIN_COOLDOWN[sym] = {"zaman": float(time.time() + TREND_COOLDOWN_SANIYE), "son_yon": "TREND_KAPAT"}
                            hafizayi_kaydet()
                            tg_gonder(f"🚨 *MOD DEĞİŞTİ!* `{sym}` Range pozisyonu kapatıldı. 1 saat bekleme.")
                            print(f"🚨 [MOD DEĞİŞİMİ] {sym} Range pozisyonu kapatıldı.", flush=True)
            except Exception as e:
                print(f"⚠️ Mod değişim kontrolü: {e}", flush=True)

            trailing_stop_kontrol()

            # SİNYAL TARAMA
            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU: break
                if len(aktif_map) >= MAKSIMUM_TOPLAM_POZISYON: break
                if symbol in aktif_list: continue

                with state_lock:
                    cd = COIN_COOLDOWN.get(symbol)
                    if cd:
                        z = cd.get("zaman", 0) if isinstance(cd, dict) else float(cd)
                        if z - time.time() > 0: continue

                try:
                    with borsa_kilidi:
                        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=SWING_LOOKBACK + 20)
                        ticker = exchange.fetch_ticker(symbol)
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    anlik = float(ticker['last'])

                    rejim = piyasa_rejimi_tespit_et(df)
                    atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range().iloc[-1]

                    highs, lows = swing_noktalari_bul(df)
                    if len(highs) < 2 or len(lows) < 2: continue
                    destekler, direncler = likidite_seviyeleri(df, highs, lows, anlik)
                    if not destekler and not direncler: continue

                    yon, sl_fiyat, tp_fiyat, rr, mod = None, None, None, 0, rejim

                    if rejim == "YATAY":
                        yon, tp_fiyat, sl_fiyat = range_sinyal_uret(df, destekler, direncler, anlik, atr)
                        if yon:
                            # 🆕 KOMİSYON KONTROLÜ
                            yeterli, net_kar = net_kar_yeterli_mi(yon, anlik, tp_fiyat)
                            if not yeterli:
                                print(f"   ⏭️ [{symbol}] {yon} RANGE | Net kâr yetersiz: %{net_kar*100:.3f}", flush=True)
                                yon = None
                            else:
                                rr = abs(tp_fiyat - anlik) / abs(anlik - sl_fiyat) if abs(anlik - sl_fiyat) > 0 else 0
                                print(f"🎯 [{symbol}] RANGE SİNYALİ! {yon} | Net kâr: %{net_kar*100:.3f} | R/R: {rr:.2f}", flush=True)

                    elif rejim == "TREND":
                        yon, likidite_lvl, sl_fiyat, sweep_uc = sweep_tespit_et(df, destekler, direncler, anlik)
                        if yon:
                            tp_fiyat, rr = tp_hesapla(yon, anlik, sl_fiyat, destekler, direncler, atr)
                            # 🆕 KOMİSYON KONTROLÜ
                            yeterli, net_kar = net_kar_yeterli_mi(yon, anlik, tp_fiyat)
                            if rr < MIN_RR or not yeterli:
                                print(f"   ⏭️ [{symbol}] {yon} TREND | R/R: {rr:.2f} | Net kâr: %{net_kar*100:.3f}", flush=True)
                                yon = None
                            else:
                                print(f"🎯 [{symbol}] TREND (SWEEP) SİNYALİ! {yon} | R/R: {rr:.2f} | Net kâr: %{net_kar*100:.3f}", flush=True)

                    if yon is None: continue

                    kaldirac = KALDIRAC_RANGE if mod == "YATAY" else KALDIRAC_TREND
                    with borsa_kilidi:
                        bakiye = exchange.fetch_balance()
                        toplam_b = float(bakiye['total'].get('USDT', 0))
                        serbest_b = float(bakiye.get('free', {}).get('USDT', 0) or 0)
                        exchange.set_leverage(kaldirac, symbol)
                        market = exchange.market(symbol)

                    kullan = min(toplam_b * 0.3, serbest_b)
                    if kullan < 1: continue

                    miktar = float(exchange.amount_to_precision(
                        symbol,
                        max((kullan * kaldirac) / anlik / float(market.get('contractSize', 1.0)),
                            float(market['limits']['amount']['min'] or 1.0))
                    ))

                    iy = 'buy' if yon == 'LONG' else 'sell'
                    kapat_y = 'sell' if yon == 'LONG' else 'buy'

                    with borsa_kilidi:
                        exchange.create_order(symbol, 'market', iy, miktar)
                    time.sleep(0.5)

                    sl_ok = False
                    try:
                        with borsa_kilidi:
                            exchange.create_order(symbol, 'limit', kapat_y, miktar, tp_fiyat, {'reduceOnly': True})
                            exchange.create_order(symbol, 'stop', kapat_y, miktar, sl_fiyat, {'stopPrice': sl_fiyat, 'reduceOnly': True})
                        sl_ok = True
                    except Exception as e:
                        print(f"   ⚠️ TP/SL: {e}", flush=True)

                    if not sl_ok:
                        try:
                            with borsa_kilidi:
                                exchange.create_order(symbol, 'market', kapat_y, miktar, None, {'reduceOnly': True})
                        except: pass
                        continue

                    with state_lock:
                        AKTIF_POZISYONLAR[symbol] = {
                            "giris_fiyati": anlik, "yon": yon, "tp_fiyat": tp_fiyat, "sl_fiyat": sl_fiyat,
                            "giris_zamani": time.time(), "kaldirac": kaldirac, "mod": mod,
                            "likidite_lvl": locals().get('likidite_lvl', 0), "sweep_uc": locals().get('sweep_uc', 0)
                        }
                        aktif_list.append(symbol)
                        aktif_map[symbol] = {"dummy": True}

                    hafizayi_kaydet()
                    print(f"✅ [AÇILDI] {symbol} {yon} ({mod}) @ {anlik} | SL: {sl_fiyat} | TP: {tp_fiyat} | Net: %{net_kar*100:.3f}", flush=True)

                    tg_gonder(
                        f"🎯 *SİNYAL ({mod})!*\n"
                        f"📌 `{symbol}` | {yon} | {kaldirac}x\n"
                        f"🎯 Giriş: `{anlik}`\n"
                        f"💰 TP: `{tp_fiyat}`\n"
                        f"🛑 SL: `{sl_fiyat}`\n"
                        f"📊 R/R: `{rr:.2f}` | Net: `%{net_kar*100:.3f}`"
                    )
                except Exception as e:
                    print(f"⚠️ {symbol}: {e}", flush=True)
                    continue
        except Exception as e:
            print(f"⚠️ Ana döngü: {e}", flush=True)
        finally:
            try: tarayici_kilidi.release()
            except: pass
        time.sleep(10)

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

    t = threading.Thread(target=tarayici, daemon=True)
    t.start()

    stop = asyncio.Event()
    await stop.wait()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
