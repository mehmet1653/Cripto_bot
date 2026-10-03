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
from datetime import datetime
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

# ==================== FLASK ====================
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot aktif!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ==================== API ====================
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

# ==================== HAVUZ ====================
CEKIRDEK_LISTE = ['SOL/USDT:USDT', 'XRP/USDT:USDT', 'DOGE/USDT:USDT', 'LTC/USDT:USDT', 'LINK/USDT:USDT']
KARA_LISTE = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'AVAX/USDT:USDT']
DINAMIK_LISTE = []
SON_HAVUZ_GUNCELLEME = 0
HAVUZ_GUNCELLEME_SURESI = 3600

def havuzu_guncelle():
    global DINAMIK_LISTE
    try:
        print("🔄 [HAVUZ] Güncelleniyor...", flush=True)
        tickers = exchange.fetch_tickers()
        usdt_pairs = {}
        for k, v in tickers.items():
            if ':USDT' in k and k not in KARA_LISTE and k not in CEKIRDEK_LISTE:
                hacim = float(v.get('quoteVolume', 0) or 0)
                if hacim > 1_000_000:
                    usdt_pairs[k] = hacim
        sorted_pairs = sorted(usdt_pairs.items(), key=lambda x: x[1], reverse=True)
        DINAMIK_LISTE = [p[0] for p in sorted_pairs[:5]]
        print(f"✅ [HAVUZ] Çekirdek {len(CEKIRDEK_LISTE)} + Dinamik {len(DINAMIK_LISTE)}", flush=True)
        return True
    except Exception as e:
        print(f"⚠️ Havuz: {e}", flush=True)
        return False

def takip_listesi():
    return CEKIRDEK_LISTE + DINAMIK_LISTE

# ==================== DURUM ====================
BOT_CALISIYOR_MU = True
state_lock = threading.Lock()
borsa_kilidi = threading.Lock()
tarayici_kilidi = threading.Lock()

# ==================== AYARLAR ====================
KALDIRAC = 7
MAKSIMUM_TOPLAM_POZISYON = 3
COOLDOWN_SURESI_SANIYE = 10 * 60

# Komisyon
KOMISYON_ORANI = 0.001
SPREAD_MALIYETI = 0.0005
TOPLAM_MALIYET_ORANI = (KOMISYON_ORANI * 2) + SPREAD_MALIYETI
MIN_NET_KAR = 0.003

# Zaman dilimi
ZAMAN_DILIMI = '15m'
MUM_LIMIT = 200

# ==================== İNDİKATÖRLER ====================
ADX_TREND_ESIGI = 25       # ADX > 25 → trend modu
ADX_DURGUN_ESIGI = 20      # ADX < 20 → durgun modu
EMA_KISA = 20
EMA_UZUN = 50
BB_PERIYOT = 20
BB_STD = 2.0
RSI_PERIYOT = 14
ATR_PERIYOT = 14

# SL/TP çarpanları
ATR_SL_TREND = 1.5
ATR_SL_DURGUN = 1.2
MIN_NET_KAR = 0.003

# Trailing (ATR bazlı)
TRAILING_ATR = [
    (1.0, 0.5),   # Kâr = 1x ATR → SL başabaş+komisyon
    (1.5, 0.8),   # Kâr = 1.5x ATR → SL %0.8 kâra
    (2.5, 1.5),   # Kâr = 2.5x ATR → SL %1.5 kâra
    (4.0, 2.5),   # Kâr = 4x ATR → SL %2.5 kâra
]

MAKS_ACIK_KALMA_SURESI_TREND = 4 * 60 * 60    # 4 saat
MAKS_ACIK_KALMA_SURESI_DURGUN = 45 * 60       # 45 dakika

# ==================== KILL-SWITCH ====================
ARDISIK_ZARAR_LIMIT = 3
ARDISIK_ZARAR_BEKLEME = 3600
ARDISIK_ZARAR_SAYACI = 0
SON_ARDISIK_ZARAR_ZAMANI = 0
KILL_SWITCH_AKTIF = False

# ==================== HAFIZA ====================
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

# ==================== PİYASA MODU TESPİTİ ====================
def piyasa_modu_tespit(df):
    """
    Döner: (mod, adx, ema20, ema50, bb_ust, bb_alt, bb_orta, rsi, atr)
    mod: 'TREND_YUKARI', 'TREND_ASAGI', 'DURGUN', 'BELIRSIZ'
    """
    try:
        close = df['close']
        high = df['high']
        low = df['low']
        
        # ADX
        adx_ind = ta.trend.ADXIndicator(high=high, low=low, close=close, window=14)
        adx = adx_ind.adx().iloc[-1]
        
        # EMA
        ema20 = ta.trend.EMAIndicator(close=close, window=EMA_KISA).ema_indicator().iloc[-1]
        ema50 = ta.trend.EMAIndicator(close=close, window=EMA_UZUN).ema_indicator().iloc[-1]
        
        # Bollinger
        bb = ta.volatility.BollingerBands(close=close, window=BB_PERIYOT, window_dev=BB_STD)
        bb_ust = bb.bollinger_hband().iloc[-1]
        bb_alt = bb.bollinger_lband().iloc[-1]
        bb_orta = bb.bollinger_mavg().iloc[-1]
        
        # RSI
        rsi = ta.momentum.RSIIndicator(close=close, window=RSI_PERIYOT).rsi().iloc[-1]
        
        # ATR
        atr = ta.volatility.AverageTrueRange(high=high, low=low, close=close, window=ATR_PERIYOT).average_true_range().iloc[-1]
        
        anlik = close.iloc[-1]
        
        # Mod tespiti
        if adx > ADX_TREND_ESIGI:
            if ema20 > ema50 and anlik > ema20:
                mod = 'TREND_YUKARI'
            elif ema20 < ema50 and anlik < ema20:
                mod = 'TREND_ASAGI'
            else:
                mod = 'BELIRSIZ'
        elif adx < ADX_DURGUN_ESIGI:
            mod = 'DURGUN'
        else:
            mod = 'BELIRSIZ'
        
        return mod, adx, ema20, ema50, bb_ust, bb_alt, bb_orta, rsi, atr
    except Exception as e:
        return 'BELIRSIZ', 0, 0, 0, 0, 0, 0, 50, 0

# ==================== SİNYAL ÜRETİCİ ====================
def sinyal_uret(df, anlik_fiyat):
    """
    Döner: (yon, tp, sl, sebep, atr_degeri)
    """
    try:
        if len(df) < 60:
            return None, None, None, None, None
        
        mod, adx, ema20, ema50, bb_ust, bb_alt, bb_orta, rsi, atr = piyasa_modu_tespit(df)
        
        if atr <= 0:
            return None, None, None, None, None
        
        # ==================== TREND MODU ====================
        if mod == 'TREND_YUKARI':
            # EMA20'ye geri çekilme + sekme
            son_mum = df.iloc[-1]
            onceki = df.iloc[-2]
            
            # Fiyat EMA20'ye yaklaştı ve üstünde kapandı
            ema20_yakin = abs(son_mum['low'] - ema20) / ema20 < 0.005
            yesil_kapanis = son_mum['close'] > son_mum['open']
            ustunde = son_mum['close'] > ema20
            
            if ema20_yakin and yesil_kapanis and ustunde and rsi < 70:
                sl_mesafe = atr * ATR_SL_TREND
                tp_mesafe = atr * ATR_SL_TREND * 3  # 1:3 R/R
                tp = anlik_fiyat + tp_mesafe
                sl = anlik_fiyat - sl_mesafe
                
                brut = tp_mesafe / anlik_fiyat
                net = brut - TOPLAM_MALIYET_ORANI
                if net < MIN_NET_KAR:
                    return None, None, None, None, None
                
                sebep = f"TREND↑ | ADX:{adx:.1f} | EMA20 sekme | RSI:{rsi:.0f}"
                return "LONG", tp, sl, sebep, atr
        
        if mod == 'TREND_ASAGI':
            son_mum = df.iloc[-1]
            ema20_yakin = abs(son_mum['high'] - ema20) / ema20 < 0.005
            kirmizi_kapanis = son_mum['close'] < son_mum['open']
            altinda = son_mum['close'] < ema20
            
            if ema20_yakin and kirmizi_kapanis and altinda and rsi > 30:
                sl_mesafe = atr * ATR_SL_TREND
                tp_mesafe = atr * ATR_SL_TREND * 3
                tp = anlik_fiyat - tp_mesafe
                sl = anlik_fiyat + sl_mesafe
                
                brut = tp_mesafe / anlik_fiyat
                net = brut - TOPLAM_MALIYET_ORANI
                if net < MIN_NET_KAR:
                    return None, None, None, None, None
                
                sebep = f"TREND↓ | ADX:{adx:.1f} | EMA20 sekme | RSI:{rsi:.0f}"
                return "SHORT", tp, sl, sebep, atr
        
        # ==================== DURGUN MODU ====================
        if mod == 'DURGUN':
            son_mum = df.iloc[-1]
            
            # Bollinger alt bandına değdi + RSI aşırı satım → LONG
            alt_banda_degdi = son_mum['low'] <= bb_alt * 1.002
            asiri_satim = rsi < 35
            
            if alt_banda_degdi and asiri_satim:
                sl_mesafe = atr * ATR_SL_DURGUN
                # TP: orta bant (SMA20)
                tp_mesafe = bb_orta - anlik_fiyat
                if tp_mesafe <= 0:
                    return None, None, None, None, None
                
                tp = bb_orta
                sl = anlik_fiyat - sl_mesafe
                
                brut = tp_mesafe / anlik_fiyat
                net = brut - TOPLAM_MALIYET_ORANI
                if net < MIN_NET_KAR:
                    return None, None, None, None, None
                
                sebep = f"DURGUN | BB Alt + RSI:{rsi:.0f} | ADX:{adx:.1f}"
                return "LONG", tp, sl, sebep, atr
            
            # Bollinger üst bandına değdi + RSI aşırı alım → SHORT
            ust_banda_degdi = son_mum['high'] >= bb_ust * 0.998
            asiri_alim = rsi > 65
            
            if ust_banda_degdi and asiri_alim:
                sl_mesafe = atr * ATR_SL_DURGUN
                tp_mesafe = anlik_fiyat - bb_orta
                if tp_mesafe <= 0:
                    return None, None, None, None, None
                
                tp = bb_orta
                sl = anlik_fiyat + sl_mesafe
                
                brut = tp_mesafe / anlik_fiyat
                net = brut - TOPLAM_MALIYET_ORANI
                if net < MIN_NET_KAR:
                    return None, None, None, None, None
                
                sebep = f"DURGUN | BB Üst + RSI:{rsi:.0f} | ADX:{adx:.1f}"
                return "SHORT", tp, sl, sebep, atr
        
        # BELIRSIZ mod → işlem yok
        return None, None, None, None, None
    except Exception as e:
        print(f"⚠️ Sinyal hatası: {e}", flush=True)
        return None, None, None, None, None

# ==================== TELEGRAM ====================
def tg_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": mesaj, "parse_mode": "Markdown"}, timeout=5)
    except: pass

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
            k = int(p.get('leverage', KALDIRAC))
            t = await asyncio.to_thread(exchange.fetch_ticker, sym)
            gf = float(t['last'])
            f = (gf - g) / g if y == "LONG" else (g - gf) / g
            roe = f * 100 * k
            pos_detay += f"\n• `{sym}` | {y} ({k}x)\n  Giriş: `{g}` | ROE: `%{roe:+.2f}`"

        mesaj = (
            f"📊 *DURUM* [ADAPTİF BOT]\n\n"
            f"💰 Kasa: `{total:.2f} USDT` | PnL: `{pnl:+.2f}`\n"
            f"📌 Açık: `{len(pos)} / {MAKSIMUM_TOPLAM_POZISYON}`"
            f"{pos_detay}\n\n"
            f"✅ TP: `{bas}` | ❌ SL: `{basz}`\n"
            f"📈 Başarı: `%{oran:.1f}`\n"
            f"⚡ Kill-Switch: `{'AKTİF' if KILL_SWITCH_AKTIF else 'Pasif'}`\n"
            f"🔻 Ardışık Zarar: `{ARDISIK_ZARAR_SAYACI}`\n\n"
            f"📋 Havuz: `{len(takip_listesi())}` coin\n"
            f"⏱️ Zaman Dilimi: `{ZAMAN_DILIMI}`\n"
            f"💸 Toplam Maliyet: `%{TOPLAM_MALIYET_ORANI*100:.2f}`"
        )
        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    global BOT_CALISIYOR_MU, KILL_SWITCH_AKTIF, ARDISIK_ZARAR_SAYACI
    BOT_CALISIYOR_MU = True
    KILL_SWITCH_AKTIF = False
    ARDISIK_ZARAR_SAYACI = 0
    await update.message.reply_text("🟢 Bot aktif! (Adaptif Mod)")

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

async def havuz_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    await update.message.reply_text("🔄 Havuz güncelleniyor...")
    basarili = havuzu_guncelle()
    if basarili:
        await update.message.reply_text(
            f"✅ *Havuz Güncellendi!*\n\n"
            f"📌 Çekirdek: `{len(CEKIRDEK_LISTE)}`\n"
            f"🔄 Dinamik: `{len(DINAMIK_LISTE)}`\n\n"
            f"*Dinamik:*\n" + "\n".join([f"• `{c}`" for c in DINAMIK_LISTE]),
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text("❌ Havuz güncellenemedi.")

# ==================== TRAILING (ATR bazlı) ====================
def trailing_stop_kontrol():
    with state_lock:
        aktif_kopya = list(AKTIF_POZISYONLAR.items())

    for sym, bilgi in aktif_kopya:
        if sym not in AKTIF_POZISYONLAR: continue
        if not isinstance(bilgi, dict): continue

        yon = bilgi.get("yon", "LONG")
        g = float(bilgi.get("giris_fiyati", 0))
        sl_kayitli = float(bilgi.get("sl_fiyat", 0))
        tp_kayitli = float(bilgi.get("tp_fiyat", 0))
        atr_degeri = float(bilgi.get("atr_degeri", 0))
        giris_zaman = float(bilgi.get("giris_zamani", 0))
        mod = bilgi.get("mod", "TREND")

        if time.time() - giris_zaman < 30: continue
        if atr_degeri <= 0: continue

        try:
            with borsa_kilidi:
                t = exchange.fetch_ticker(sym)
                anlik = float(t['last'])
        except: continue

        if yon == "LONG":
            kar_mesafe = anlik - g
        else:
            kar_mesafe = g - anlik

        kar_orani = kar_mesafe / atr_degeri

        # Durgun modda daha hızlı kâr kilitle
        carpanlar = TRAILING_ATR if mod == "TREND" else [(0.8, 0.3), (1.2, 0.6), (2.0, 1.2)]

        yeni_sl = None
        for esik, sl_kilit in carpanlar:
            if kar_orani >= esik:
                if yon == "LONG":
                    yeni_sl = g + (atr_degeri * sl_kilit) + (g * TOPLAM_MALIYET_ORANI)
                else:
                    yeni_sl = g - (atr_degeri * sl_kilit) - (g * TOPLAM_MALIYET_ORANI)
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
                if tp_kayitli > 0:
                    exchange.create_order(sym, 'limit', ky, miktar, tp_kayitli, {'reduceOnly': True})

            with state_lock:
                if sym in AKTIF_POZISYONLAR:
                    AKTIF_POZISYONLAR[sym]["sl_fiyat"] = yeni_sl
            print(f"🔒 [TRAILING] {sym} | Kâr {kar_orani:.1f}x ATR → SL: {yeni_sl:.6f}", flush=True)
            tg_gonder(f"🔒 *KÂR KİLİTLENDİ*\n📌 `{sym}` | {yon}\n📊 Kâr: `{kar_orani:.1f}x ATR`\n🛑 SL: `{yeni_sl:.6f}`")
        except Exception as e:
            print(f"⚠️ Trailing: {e}", flush=True)

# ==================== KAPANIŞ ====================
def kapanis_kontrol():
    global ARDISIK_ZARAR_SAYACI, SON_ARDISIK_ZARAR_ZAMANI, KILL_SWITCH_AKTIF
    with state_lock:
        aktif_kopya = list(AKTIF_POZISYONLAR.items())

    try:
        with borsa_kilidi:
            raw_positions = exchange.fetch_positions()
        aktif_borsa = [p['symbol'] for p in raw_positions if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
    except:
        aktif_borsa = []

    for sym, bilgi in aktif_kopya:
        if sym not in AKTIF_POZISYONLAR: continue
        if not isinstance(bilgi, dict): continue

        if sym not in aktif_borsa:
            g = bilgi.get("giris_fiyati", 0)
            y = bilgi.get("yon", "LONG")
            tp_k = bilgi.get("tp_fiyat", g)
            sl_k = bilgi.get("sl_fiyat", g)
            karli = False
            cikis = g
            try:
                with borsa_kilidi:
                    t = exchange.fetch_ticker(sym)
                cikis = float(t['last'])
                karli = abs(cikis - tp_k) < abs(cikis - sl_k)
            except:
                karli = cikis > g if y == "LONG" else cikis < g

            with state_lock:
                b = int(ANALITIK.get("basarili_islem_sayisi", 0))
                bz = int(ANALITIK.get("basarisiz_islem_sayisi", 0))
                
                if y == "LONG":
                    brut = (cikis - g) / g
                else:
                    brut = (g - cikis) / g
                net = brut - TOPLAM_MALIYET_ORANI
                
                if karli:
                    b += 1; tip = "✅ *KÂRLA KAPANDI*"
                    ARDISIK_ZARAR_SAYACI = 0
                else:
                    bz += 1; tip = "❌ *ZARARLA KAPANDI*"
                    ARDISIK_ZARAR_SAYACI += 1
                    SON_ARDISIK_ZARAR_ZAMANI = time.time()
                
                ANALITIK["basarili_islem_sayisi"] = b
                ANALITIK["basarisiz_islem_sayisi"] = bz
                COIN_COOLDOWN[sym] = {"zaman": float(time.time() + COOLDOWN_SURESI_SANIYE), "son_yon": y}
                if sym in AKTIF_POZISYONLAR:
                    del AKTIF_POZISYONLAR[sym]

            hafizayi_kaydet()
            print(f"💰 [KAPANIŞ] {sym} | Çıkış: {cikis} | Net: %{net*100:.2f}", flush=True)
            tg_gonder(
                f"{tip}\n📌 `{sym}` | Çıkış: `{cikis}`\n"
                f"📊 Net Kâr: `%{net*100:.2f}`\n"
                f"🔻 Ardışık Zarar: `{ARDISIK_ZARAR_SAYACI}`"
            )

            if ARDISIK_ZARAR_SAYACI >= ARDISIK_ZARAR_LIMIT:
                KILL_SWITCH_AKTIF = True
                tg_gonder(f"🚨 *KILL-SWITCH AKTİF!*\n{ARDISIK_ZARAR_LIMIT} ardışık zarar.\n⏸️ 1 saat bekle.")
            continue

        # Süre kontrolü (moda göre)
        giris_zaman = float(bilgi.get("giris_zamani", 0))
        mod = bilgi.get("mod", "TREND")
        max_sure = MAKS_ACIK_KALMA_SURESI_TREND if mod == "TREND" else MAKS_ACIK_KALMA_SURESI_DURGUN
        gecen_sure = time.time() - giris_zaman
        
        if gecen_sure > max_sure:
            try:
                with borsa_kilidi:
                    exchange.cancel_all_orders(sym)
                    miktar = None
                    for p in raw_positions:
                        if p['symbol'] == sym:
                            miktar = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                            break
                    if not miktar or miktar <= 0: continue
                    y = bilgi.get("yon", "LONG")
                    ky = 'sell' if y == 'LONG' else 'buy'
                    exchange.create_order(sym, 'market', ky, miktar, None, {'reduceOnly': True})

                with state_lock:
                    if sym in AKTIF_POZISYONLAR: del AKTIF_POZISYONLAR[sym]
                    COIN_COOLDOWN[sym] = {"zaman": float(time.time() + COOLDOWN_SURESI_SANIYE), "son_yon": y}

                hafizayi_kaydet()
                dakika = int(gecen_sure // 60)
                print(f"⏰ [SÜRE DOLDU] {sym} | {mod} | {dakika}dk", flush=True)
                tg_gonder(f"⏰ *SÜRE DOLDU ({mod})*\n📌 `{sym}` | {dakika}dk")
            except Exception as e:
                print(f"⚠️ Süre: {e}", flush=True)

# ==================== ANA TARAYICI ====================
def tarayici():
    global SON_HAVUZ_GUNCELLEME, KILL_SWITCH_AKTIF, ARDISIK_ZARAR_SAYACI
    print(f"🚀 [BAŞLANGIÇ] ADAPTİF BOT | {ZAMAN_DILIMI} | Trend+Durgun", flush=True)
    try:
        exchange.load_markets()
    except: pass

    havuzu_guncelle()
    SON_HAVUZ_GUNCELLEME = time.time()

    dongu = 0
    while True:
        if not tarayici_kilidi.acquire(blocking=False):
            time.sleep(2); continue
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5); continue
            dongu += 1

            if time.time() - SON_HAVUZ_GUNCELLEME > HAVUZ_GUNCELLEME_SURESI:
                havuzu_guncelle()
                SON_HAVUZ_GUNCELLEME = time.time()

            if KILL_SWITCH_AKTIF:
                if time.time() - SON_ARDISIK_ZARAR_ZAMANI > ARDISIK_ZARAR_BEKLEME:
                    KILL_SWITCH_AKTIF = False
                    ARDISIK_ZARAR_SAYACI = 0
                    tg_gonder("✅ *KILL-SWITCH KAPANDI*")
                else:
                    kalan = int((ARDISIK_ZARAR_BEKLEME - (time.time() - SON_ARDISIK_ZARAR_ZAMANI))/60)
                    print(f"⏸️ [KILL-SWITCH] {kalan} dk kaldı", flush=True)
                    time.sleep(30)
                    continue

            print(f"\n{'='*60}", flush=True)
            print(f"🔄 [DÖNGÜ #{dongu}] {time.strftime('%H:%M:%S')}", flush=True)
            print(f"{'='*60}", flush=True)

            kapanis_kontrol()
            trailing_stop_kontrol()

            try:
                with borsa_kilidi:
                    raw = exchange.fetch_positions()
                aktif_map = {p['symbol']: p for p in raw if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0}
                aktif_list = list(aktif_map.keys())
            except:
                raw = []
                aktif_map = {}
                aktif_list = []

            for symbol in takip_listesi():
                if not BOT_CALISIYOR_MU: break
                if len(aktif_map) >= MAKSIMUM_TOPLAM_POZISYON: break
                if symbol in aktif_list: continue

                with state_lock:
                    cd = COIN_COOLDOWN.get(symbol)
                    if cd:
                        z = cd.get("zaman", 0) if isinstance(cd, dict) else float(cd)
                        if z - time.time() > 0:
                            continue

                try:
                    with borsa_kilidi:
                        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=ZAMAN_DILIMI, limit=MUM_LIMIT)
                        ticker = exchange.fetch_ticker(symbol)

                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    df = df.iloc[:-1].reset_index(drop=True)
                    anlik = float(ticker['last'])

                    # Piyasa modu tespit
                    mod, adx, ema20, ema50, bb_ust, bb_alt, bb_orta, rsi, atr = piyasa_modu_tespit(df)
                    
                    print(f"   🔎 [{symbol}] Fiyat: {anlik:.6f} | Mod: {mod} | ADX: {adx:.1f} | RSI: {rsi:.0f} | ATR: {atr:.6f}", flush=True)

                    yon_s, tp_fiyat, sl_fiyat, sebep, atr_b = sinyal_uret(df, anlik)

                    if yon_s is None:
                        print(f"      ⏭️ Sinyal yok", flush=True)
                        continue
                    
                    print(f"      🎯 SİNYAL! {yon_s} | {sebep}", flush=True)

                    kapat_yon = 'sell' if yon_s == 'LONG' else 'buy'
                    rr = 3.0  # Trend modu için 1:3, durgun için daha düşük

                    with borsa_kilidi:
                        bakiye = exchange.fetch_balance()
                        toplam_b = float(bakiye['total'].get('USDT', 0))
                        serbest_b = float(bakiye.get('free', {}).get('USDT', 0) or 0)
                        exchange.set_leverage(KALDIRAC, symbol)
                        market = exchange.market(symbol)

                    kullan = min(toplam_b * 0.2, serbest_b)
                    if kullan < 1: continue

                    miktar = float(exchange.amount_to_precision(
                        symbol,
                        max((kullan * KALDIRAC) / anlik / float(market.get('contractSize', 1.0)),
                            float(market['limits']['amount']['min'] or 1.0))
                    ))

                    iy = 'buy' if yon_s == 'LONG' else 'sell'

                    with borsa_kilidi:
                        exchange.create_order(symbol, 'market', iy, miktar)
                    time.sleep(0.3)

                    sl_ok = False
                    try:
                        with borsa_kilidi:
                            exchange.create_order(symbol, 'limit', kapat_yon, miktar, tp_fiyat, {'reduceOnly': True})
                            exchange.create_order(symbol, 'stop', kapat_yon, miktar, sl_fiyat, {'stopPrice': sl_fiyat, 'reduceOnly': True})
                        sl_ok = True
                    except Exception as e:
                        print(f"   ⚠️ TP/SL: {e}", flush=True)

                    if not sl_ok:
                        try:
                            with borsa_kilidi:
                                exchange.create_order(symbol, 'market', kapat_yon, miktar, None, {'reduceOnly': True})
                        except: pass
                        continue

                    with state_lock:
                        AKTIF_POZISYONLAR[symbol] = {
                            "giris_fiyati": anlik, "yon": yon_s,
                            "tp_fiyat": tp_fiyat, "sl_fiyat": sl_fiyat,
                            "giris_zamani": time.time(),
                            "kaldirac": KALDIRAC,
                            "atr_degeri": atr_b,
                            "mod": mod,
                            "sebep": sebep
                        }
                        aktif_list.append(symbol)
                        aktif_map[symbol] = {"dummy": True}

                    hafizayi_kaydet()
                    print(f"✅ [AÇILDI] {symbol} {yon_s} | {mod} | @ {anlik}", flush=True)

                    tg_gonder(
                        f"🎯 *SİNYAL! ({mod})*\n"
                        f"📌 `{symbol}` | {yon_s}\n"
                        f"📊 {sebep}\n"
                        f"🎯 Giriş: `{anlik}`\n"
                        f"💰 TP: `{tp_fiyat}`\n"
                        f"🛑 SL: `{sl_fiyat}`\n"
                        f"📏 ATR: `{atr_b:.6f}`\n"
                        f"💸 Maliyet: `%{TOPLAM_MALIYET_ORANI*100:.2f}`"
                    )
                except Exception as e:
                    print(f"⚠️ {symbol}: {e}", flush=True)
                    continue

        except Exception as e:
            print(f"⚠️ Ana döngü: {e}", flush=True)
        finally:
            try: tarayici_kilidi.release()
            except: pass

        time.sleep(15)

# ==================== MAIN ====================
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
    app_tg.add_handler(CommandHandler("havuz", havuz_komutu))

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
