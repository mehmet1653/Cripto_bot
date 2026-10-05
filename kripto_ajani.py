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
import numpy as np
import ta
from datetime import date
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
    return "Bot aktif (v9.0 Adaptif)!"

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

# ==================== ADAPTİF AYARLAR (v9.0) ====================
BOT_CALISIYOR_MU = True
state_lock = threading.Lock()

# Hedef: Günlük %5 (Ancak piyasa koşullarına göre esner)
GUNLUK_HEDEF_KAR = 0.05 
BASLANGIC_BAKIYE = None
GUNLUK_KAR = 0.0

# Risk Yönetimi (Dinamik değişecek)
KALDIRAC = 3
MAKSIMUM_TOPLAM_POZISYON = 3
POZISYON_ORANI = 0.10
COOLDOWN_SURESI_SANIYE = 15 * 60

KOMISYON_ORANI = 0.001
SPREAD_MALIYETI = 0.0005
TOPLAM_MALIYET_ORANI = (KOMISYON_ORANI * 2) + SPREAD_MALIYETI

# Adaptif Kademeli Kar (Öğrenme ile güncellenir)
KADEMELI_KAR = [
    (2.0, 0.30),  # %2'de %30 kapat
    (4.0, 0.30),  # %4'te %30 kapat
    (6.0, 0.40),  # %6'da kalanı kapat
]

TRAILING_KAR = [
    (2.0, 0.010),
    (4.0, 0.025),
    (6.0, 0.040),
]

# Zaman limiti (Piyasaya göre değişebilir)
MAKS_ACIK_KALMA = 4 * 60 * 60

ARDISIK_ZARAR_LIMIT = 4
ARDISIK_ZARAR_SAYACI = 0
SON_ARDISIK_ZARAR_ZAMANI = 0
KILL_SWITCH_AKTIF = False

# ==================== ADAPTİF ÖĞRENME (v9.0) ====================
OGRENME_HAFIZASI = {
    "rejim_basarilar": {},   # Hangi rejimde kaç kazandı
    "rsi_basarilar": {},     # Hangi RSI aralığı kazandırdı
    "son_islemler": [],      # Son 20 işlemin detayı
    "optimal_kaldirac": 3,
    "optimal_pozisyon": 0.10
}

def ogrenme_kaydet(symbol, rejim, rsi, yon, net_kar):
    """Her kapanan işlemi öğrenme hafızasına kaydeder"""
    with state_lock:
        OGRENME_HAFIZASI["son_islemler"].append({
            "zaman": time.time(),
            "symbol": symbol,
            "rejim": rejim,
            "rsi": round(rsi, 1),
            "yon": yon,
            "net_kar": round(net_kar, 4)
        })
        # Son 20 işlemi tut
        if len(OGRENME_HAFIZASI["son_islemler"]) > 20:
            OGRENME_HAFIZASI["son_islemler"].pop(0)
        
        # Rejim bazlı başarı
        if rejim not in OGRENME_HAFIZASI["rejim_basarilar"]:
            OGRENME_HAFIZASI["rejim_basarilar"][rejim] = {"kar": 0, "zarar": 0}
        
        if net_kar > 0:
            OGRENME_HAFIZASI["rejim_basarilar"][rejim]["kar"] += 1
        else:
            OGRENME_HAFIZASI["rejim_basarilar"][rejim]["zarar"] += 1
        
        # RSI aralığı bazlı başarı (5'lik dilimler)
        rsi_aralik = f"{int(rsi//5)*5}-{int(rsi//5)*5+5}"
        if rsi_aralik not in OGRENME_HAFIZASI["rsi_basarilar"]:
            OGRENME_HAFIZASI["rsi_basarilar"][rsi_aralik] = {"kar": 0, "zarar": 0}
        
        if net_kar > 0:
            OGRENME_HAFIZASI["rsi_basarilar"][rsi_aralik]["kar"] += 1
        else:
            OGRENME_HAFIZASI["rsi_basarilar"][rsi_aralik]["zarar"] += 1

def adaptif_kaldirac_ayarla():
    """Son işlemlere göre kaldıracı otomatik ayarlar"""
    global KALDIRAC, POZISYON_ORANI
    with state_lock:
        son = OGRENME_HAFIZASI["son_islemler"][-10:]  # Son 10 işlem
        if len(son) < 5:
            return
        
        kazanan = sum(1 for i in son if i["net_kar"] > 0)
        oran = kazanan / len(son)
        
        # Kazanma oranı %60'ın üzerindeyse kaldıracı artır
        if oran >= 0.60:
            KALDIRAC = min(5, KALDIRAC + 1)
            POZISYON_ORANI = min(0.15, POZISYON_ORANI + 0.02)
        # Kazanma oranı %40'ın altındaysa kaldıracı düşür
        elif oran <= 0.40:
            KALDIRAC = max(1, KALDIRAC - 1)
            POZISYON_ORANI = max(0.05, POZISYON_ORANI - 0.02)
        
        print(f"🧠 [ADAPTİF] Kazanma: %{oran*100:.0f} | Kaldıraç: {KALDIRAC}x | Pozisyon: %{POZISYON_ORANI*100:.0f}", flush=True)

def gunluk_kar_kontrol():
    """Günlük hedefe ulaşıldıysa botu durdurur"""
    global GUNLUK_KAR, BASLANGIC_BAKIYE
    try:
        balance = exchange.fetch_balance()
        total = float(balance['total'].get('USDT', 0))
        
        if BASLANGIC_BAKIYE is None:
            BASLANGIC_BAKIYE = total
        
        GUNLUK_KAR = (total - BASLANGIC_BAKIYE) / BASLANGIC_BAKIYE if BASLANGIC_BAKIYE > 0 else 0
        
        # Günlük hedefe ulaşıldıysa
        if GUNLUK_KAR >= GUNLUK_HEDEF_KAR:
            tg_gonder(f"🎉 GÜNLÜK HEDEF TUTTU!\n💰 Kar: %{GUNLUK_KAR*100:.2f}\n⏸️ Bot 1 saat dinleniyor.")
            return True
        
        # Günlük zarar limiti (Stop-loss)
        if GUNLUK_KAR <= -0.03:  # %3 zarar
            tg_gonder(f"🛑 GÜNLÜK ZARAR LİMİTİ!\n📉 Zarar: %{GUNLUK_KAR*100:.2f}\n⏸️ Bot 2 saat dinleniyor.")
            return True
    except:
        pass
    return False

# ==================== DİNAMİK COIN LİSTESİ ====================
DINAMIK_LISTE = []
SON_DINAMIK_GUNCELLEME = 0
DINAMIK_GUNCELLEME_SURESI = 1800  # 30 dk (Daha sık güncelle)
DINAMIK_LISTE_BOYUT = 15

KARA_LISTE = [
    'BTC/USDT:USDT', 'ETH/USDT:USDT', 'AVAX/USDT:USDT',
    'USDC/USDT:USDT', 'USDT/USDT:USDT', 'DAI/USDT:USDT',
    'FDUSD/USDT:USDT', 'TUSD/USDT:USDT', 'BUSD/USDT:USDT',
    'USDE/USDT:USDT', 'PYUSD/USDT:USDT'
]

def dinamik_liste_guncelle():
    global DINAMIK_LISTE, SON_DINAMIK_GUNCELLEME
    
    if time.time() - SON_DINAMIK_GUNCELLEME < DINAMIK_GUNCELLEME_SURESI:
        return
    
    print(f"\n🔄 [DİNAMİK LİSTE] Güncelleniyor...", flush=True)
    SON_DINAMIK_GUNCELLEME = time.time()
    
    try:
        tickers = exchange.fetch_tickers()
        adaylar = []
        
        for sym, t in tickers.items():
            if ':USDT' not in sym: continue
            if sym in KARA_LISTE: continue
            
            try:
                hacim = float(t.get('quoteVolume', 0) or 0)
                degisim = abs(float(t.get('percentage', 0) or 0))
                
                if hacim < 3_000_000: continue
                if degisim < 0.5: continue
                
                skor = (hacim / 1_000_000) * degisim
                adaylar.append((sym, skor, degisim, hacim))
            except:
                continue
        
        adaylar.sort(key=lambda x: x[1], reverse=True)
        DINAMIK_LISTE = [a[0] for a in adaylar[:DINAMIK_LISTE_BOYUT]]
        
        print(f"✅ [DİNAMİK] {len(DINAMIK_LISTE)} coin:", flush=True)
        for s, skor, deg, hac in adaylar[:DINAMIK_LISTE_BOYUT]:
            print(f"   • {s} | %{deg:.1f} | {hac/1_000_000:.0f}M", flush=True)
        
    except Exception as e:
        print(f"⚠️ Liste hatası: {e}", flush=True)

def takip_listesi():
    return DINAMIK_LISTE

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
                "cooldownlar": veri.get("cooldownlar", {}),
                "ogrenme": veri.get("ogrenme", OGRENME_HAFIZASI)
            }
    except Exception as e:
        print(f"⚠️ Hafıza: {e}", flush=True)
    return {
        "aktif_sistemler": {},
        "analitik": {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0},
        "cooldownlar": {},
        "ogrenme": OGRENME_HAFIZASI
    }

def hafizayi_kaydet():
    with state_lock:
        try:
            payload = {
                "basarili_islem_sayisi": int(ANALITIK.get("basarili_islem_sayisi", 0)),
                "basarisiz_islem_sayisi": int(ANALITIK.get("basarisiz_islem_sayisi", 0))
            }
            clean_cd = {}
            for k, v in COIN_COOLDOWNLAR.items():
                if isinstance(v, dict):
                    clean_cd[k] = {
                        "zaman": float(v.get("zaman", 0)),
                        "son_yon": str(v.get("son_yon", "")),
                        "son_cikis_fiyat": float(v.get("son_cikis_fiyat", 0))
                    }
            
            supabase.table("bot_hafiza").upsert({
                "id": 1,
                "aktif_sistemler": AKTIF_SISTEMLER,
                "analitik": payload,
                "cooldownlar": clean_cd,
                "ogrenme": OGRENME_HAFIZASI
            }).execute()
        except Exception as e:
            print(f"⚠️ Kayıt: {e}", flush=True)

kalici = hafizayi_yukle()
AKTIF_SISTEMLER = kalici.get("aktif_sistemler", {})
ANALITIK = kalici.get("analitik", {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0})
COIN_COOLDOWNLAR = kalici.get("cooldownlar", {})
OGRENME_HAFIZASI = kalici.get("ogrenme", OGRENME_HAFIZASI)

# ==================== YARDIMCI EMİR ====================
def emir_sl_mi(order):
    try:
        otype = str(order.get('type', '')).lower()
        info_type = str(order.get('info', {}).get('type', '')).lower()
        if otype in ['stop', 'stop_market', 'stop_limit']: return True
        if 'stop' in otype or 'conditional' in otype: return True
        if 'conditional' in info_type or 'stop' in info_type: return True
        if order.get('stopPrice') or order.get('triggerPrice'): return True
        return False
    except: return False

def eski_sl_sil(symbol, yeni_sl_id):
    try:
        orders = exchange.fetch_open_orders(symbol)
        for o in orders:
            if o['id'] == yeni_sl_id: continue
            if emir_sl_mi(o):
                try: exchange.cancel_order(o['id'], symbol)
                except: pass
    except: pass

def pozisyon_emirlerini_temizle(symbol):
    try:
        orders = exchange.fetch_open_orders(symbol)
        for o in orders:
            try: exchange.cancel_order(o['id'], symbol)
            except: pass
    except: pass

def yeni_sl_koy(symbol, miktar, yeni_sl, yon):
    ky = 'sell' if yon == 'LONG' else 'buy'
    try:
        order = exchange.create_order(symbol, 'stop', ky, miktar, yeni_sl, {'stopPrice': yeni_sl, 'reduceOnly': True})
        time.sleep(0.3)
        eski_sl_sil(symbol, order['id'])
        return True
    except Exception as e:
        print(f"⚠️ SL {symbol}: {e}", flush=True)
        return False

def pozisyon_kapat(symbol, miktar, yon, oran=1.0):
    ky = 'sell' if yon == 'LONG' else 'buy'
    try:
        kapat = float(exchange.amount_to_precision(symbol, miktar * oran))
        if kapat <= 0: return False
        exchange.create_order(symbol, 'market', ky, kapat, None, {'reduceOnly': True})
        time.sleep(0.3)
        if oran >= 1.0: pozisyon_emirlerini_temizle(symbol)
        return True
    except Exception as e:
        print(f"⚠️ Kapatma {symbol}: {e}", flush=True)
        return False

# ==================== PİYASA REJİMİ ====================
def piyasa_rejimini_tespit_et():
    try:
        ohlcv = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='1h', limit=50)
        df = pd.DataFrame(ohlcv, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        
        adx = ta.trend.ADXIndicator(df['h'], df['l'], df['c'], window=14).adx().iloc[-1]
        
        bb = ta.volatility.BollingerBands(close=df['c'], window=20, window_dev=2)
        bb_high = bb.bollinger_hband().iloc[-1]
        bb_low = bb.bollinger_lband().iloc[-1]
        bb_mid = bb.bollinger_mavg().iloc[-1]
        bb_bw = (bb_high - bb_low) / bb_mid if bb_mid > 0 else 0
        
        ema9 = ta.trend.ema_indicator(df['c'], window=9).iloc[-1]
        ema21 = ta.trend.ema_indicator(df['c'], window=21).iloc[-1]
        fark = abs(ema9 - ema21) / ema21 * 100 if ema21 > 0 else 0
        
        if adx < 30 or bb_bw < 0.03 or fark < 0.2:
            return "YATAY", "TESTERE"
        else:
            yon = "LONG" if ema9 > ema21 else "SHORT"
            return "TREND", yon
    except Exception as e:
        print(f"⚠️ Rejim: {e}", flush=True)
        return "YATAY", "TESTERE"

# ==================== EMİR DEFTERİ ====================
def emir_defteri_analizi(symbol):
    try:
        ob = exchange.fetch_order_book(symbol, limit=20)
        bids = ob.get('bids', [])
        asks = ob.get('asks', [])
        t_alis = sum([b[1] for b in bids]) if bids else 1
        t_satis = sum([a[1] for a in asks]) if asks else 1
        t_toplam = t_alis + t_satis
        return (t_alis / t_toplam * 100) if t_toplam > 0 else 50
    except: return 50

# ==================== ADAPTİF SL/TP (v9.0) ====================
def akilli_seviye(anlik, yon, df):
    atr = ta.volatility.AverageTrueRange(df['h'], df['l'], df['c'], window=14).average_true_range().iloc[-1]
    
    # Adaptif çarpan (piyasa volatilitesine göre)
    atr_yuzde = (atr / anlik) * 100
    if atr_yuzde > 2.0:
        tp_carpan = 6.0
        sl_carpan = 4.5
    elif atr_yuzde > 1.0:
        tp_carpan = 5.0
        sl_carpan = 4.0
    else:
        tp_carpan = 4.0
        sl_carpan = 3.5
    
    if yon == 'LONG':
        tp = anlik + (atr * tp_carpan)
        sl = anlik - (atr * sl_carpan)
    else:
        tp = anlik - (atr * tp_carpan)
        sl = anlik + (atr * sl_carpan)
    
    return float(tp), float(sl)

# ==================== TELEGRAM ====================
def tg_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": mesaj}, timeout=5)
    except: pass

async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    try:
        balance = await asyncio.to_thread(exchange.fetch_balance)
        total = float(balance['total'].get('USDT', 0))
        pos = [p for p in await asyncio.to_thread(exchange.fetch_positions) if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
        pnl = sum(float(p.get('unrealizedPnl', 0)) for p in pos)
        
        rejim, yon_btc = await asyncio.to_thread(piyasa_rejimini_tespit_et)
        
        bas = int(ANALITIK.get("basarili_islem_sayisi", 0))
        basz = int(ANALITIK.get("basarisiz_islem_sayisi", 0))
        top = bas + basz
        oran = (bas / top * 100) if top > 0 else 0
        
        pos_detay = ""
        for p in pos:
            sym = p['symbol']
            y = str(p.get('side', '')).upper()
            g = float(p.get('entryPrice', 0))
            t = await asyncio.to_thread(exchange.fetch_ticker, sym)
            gf = float(t['last'])
            f = (gf - g) / g if y == "LONG" else (g - gf) / g
            roe = f * 100 * KALDIRAC
            bilgi = AKTIF_SISTEMLER.get(sym, {})
            kademe = len(bilgi.get("alinan_kademeler", []))
            acilis_rejim = bilgi.get("acilis_rejim", "?")
            sure_dk = int((time.time() - bilgi.get("giris_zamani", time.time())) / 60)
            pos_detay += f"\n🟢 {sym} | {y} ({KALDIRAC}x)\n  Giriş: `{g:.4f}` | ROE: `%{roe:+.2f}`\n  Kademe: {kademe}/3 | {acilis_rejim} | {sure_dk} dk"
        
        # Son işlemlerin kazanma oranı
        son_10 = OGRENME_HAFIZASI["son_islemler"][-10:]
        son_kaz = sum(1 for i in son_10 if i["net_kar"] > 0)
        son_oran = (son_kaz / len(son_10) * 100) if son_10 else 0
        
        mesaj = (
            f"📊 DURUM [v9.0 ADAPTİF]\n\n"
            f"🌐 BTC Rejim: `{rejim}` ({yon_btc})\n"
            f"💰 Kasa: `{total:.2f} USDT`\n"
            f"📈 PnL: `{pnl:+.2f} USDT`\n"
            f"🎯 Günlük Hedef: `%5` | Şu An: `%{GUNLUK_KAR*100:+.2f}`\n"
            f"📌 Açık: `{len(pos)} / {MAKSIMUM_TOPLAM_POZISYON}`"
            f"{pos_detay}\n\n"
            f"🧠 Adaptif Kaldıraç: `{KALDIRAC}x` | Pozisyon: `%{POZISYON_ORANI*100:.0f}`\n"
            f"📈 Son 10 İşlem Başarı: `%{son_oran:.0f}`\n"
            f"✅ TP: `{bas}` | ❌ SL: `{basz}`\n"
            f"📈 Toplam Başarı: `%{oran:.1f}` ({top} işlem)\n"
            f"⚡ Kill-Switch: `{'AKTİF' if KILL_SWITCH_AKTIF else 'Pasif'}`"
        )
        await update.message.reply_text(mesaj)
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    global BOT_CALISIYOR_MU, KILL_SWITCH_AKTIF, ARDISIK_ZARAR_SAYACI, BASLANGIC_BAKIYE
    BOT_CALISIYOR_MU = True
    KILL_SWITCH_AKTIF = False
    ARDISIK_ZARAR_SAYACI = 0
    BASLANGIC_BAKIYE = None
    await update.message.reply_text("🟢 Bot aktif! (v9.0 - Adaptif Otonom Sistem)")

async def durdur_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ Durduruldu.")

async def kapat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    try:
        positions = await asyncio.to_thread(exchange.fetch_positions)
        for p in positions:
            k = float(p.get('contracts', 0) or p.get('size', 0) or 0)
            if k > 0:
                y = str(p.get('side', '')).upper() or "LONG"
                pozisyon_kapat(p['symbol'], k, y, oran=1.0)
        await update.message.reply_text("✅ Kapatıldı.")
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def liste_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    try:
        liste_str = "📋 *DİNAMİK LİSTE*\n\n"
        for i, s in enumerate(DINAMIK_LISTE, 1):
            liste_str += f"{i}. {s.replace('/USDT:USDT','')}\n"
        await update.message.reply_text(liste_str)
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

# ==================== KADEMELİ KÂR + REJİM KONTROLÜ ====================
def kar_zarar_yonetimi():
    with state_lock:
        kopya = list(AKTIF_SISTEMLER.items())
    
    try:
        guncel_rejim, guncel_btc_yon = piyasa_rejimini_tespit_et()
    except:
        guncel_rejim, guncel_btc_yon = "YATAY", "TESTERE"
    
    for sym, bilgi in kopya:
        if sym not in AKTIF_SISTEMLER: continue
        if not isinstance(bilgi, dict): continue
        
        yon = bilgi.get("yon", "LONG")
        g = float(bilgi.get("giris_fiyati", 0))
        giris_zaman = float(bilgi.get("giris_zamani", 0))
        alinan = bilgi.get("alinan_kademeler", [])
        acilis_rejim = bilgi.get("acilis_rejim", "")
        acilis_btc_yon = bilgi.get("acilis_btc_yon", "")
        
        if time.time() - giris_zaman < 60: continue
        
        try:
            t = exchange.fetch_ticker(sym)
            anlik = float(t['last'])
        except: continue
        
        roe = ((anlik - g) / g * 100 * KALDIRAC) if yon == "LONG" else ((g - anlik) / g * 100 * KALDIRAC)
        
        miktar = None
        for p in exchange.fetch_positions():
            if p['symbol'] == sym:
                miktar = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                break
        
        if not miktar or miktar <= 0: continue
        
        # 1. REJİM DEĞİŞİM KONTROLÜ
        if acilis_rejim != guncel_rejim:
            if pozisyon_kapat(sym, miktar, yon, oran=1.0):
                print(f"🔄 [REJİM] {sym} | {acilis_rejim}→{guncel_rejim} | ROE:%{roe:.1f}", flush=True)
                tg_gonder(f"🔄 REJİM DEĞİŞTİ\n📌 {sym}\n💰 ROE: %{roe:+.2f}")
            continue
        
        # 2. BTC YÖN DEĞİŞİKLİĞİ
        if acilis_rejim == "TREND" and acilis_btc_yon != guncel_btc_yon:
            if pozisyon_kapat(sym, miktar, yon, oran=1.0):
                print(f"🔄 [BTC YÖN] {sym} | ROE:%{roe:.1f}", flush=True)
                tg_gonder(f"🔄 BTC YÖN DEĞİŞTİ\n📌 {sym}\n💰 ROE: %{roe:+.2f}")
            continue
        
        # 3. ZAMAN LİMİTİ
        gecen = time.time() - giris_zaman
        if gecen > MAKS_ACIK_KALMA:
            if pozisyon_kapat(sym, miktar, yon, oran=1.0):
                print(f"⏰ [ZAMAN] {sym} | {int(gecen/60)}dk | ROE:%{roe:.1f}", flush=True)
                tg_gonder(f"⏰ ZAMAN DOLDU\n📌 {sym}\n💰 ROE: %{roe:+.2f}")
            continue
        
        # 4. KADEMELİ KÂR (Adaptif)
        for i, (esik, oran) in enumerate(KADEMELI_KAR):
            if i in alinan: continue
            if roe >= esik:
                if pozisyon_kapat(sym, miktar, yon, oran=oran):
                    with state_lock:
                        if sym in AKTIF_SISTEMLER:
                            yeni = AKTIF_SISTEMLER[sym].get("alinan_kademeler", [])
                            yeni.append(i)
                            AKTIF_SISTEMLER[sym]["alinan_kademeler"] = yeni
                    print(f"💰 [KADEME] {sym} | ROE:%{roe:.1f} → %{int(oran*100)}", flush=True)
                    tg_gonder(f"💰 KADEMELİ KÂR\n📌 {sym}\n📊 ROE: %{roe:+.2f}")
                break

# ==================== TRAILING ====================
def trailing_kontrol():
    with state_lock:
        kopya = list(AKTIF_SISTEMLER.items())
    
    for sym, bilgi in kopya:
        if sym not in AKTIF_SISTEMLER: continue
        if not isinstance(bilgi, dict): continue
        
        yon = bilgi.get("yon", "LONG")
        g = float(bilgi.get("giris_fiyati", 0))
        sl_kayitli = float(bilgi.get("sl_fiyat", 0))
        giris_zaman = float(bilgi.get("giris_zamani", 0))
        
        if time.time() - giris_zaman < 60: continue
        
        try:
            t = exchange.fetch_ticker(sym)
            anlik = float(t['last'])
        except: continue
        
        roe = ((anlik - g) / g * 100 * KALDIRAC) if yon == "LONG" else ((g - anlik) / g * 100 * KALDIRAC)
        
        yeni_sl = None
        for esik, kilit in TRAILING_KAR:
            if roe >= esik:
                if yon == "LONG":
                    yeni_sl = g * (1 + (TOPLAM_MALIYET_ORANI + kilit) / KALDIRAC)
                else:
                    yeni_sl = g * (1 - (TOPLAM_MALIYET_ORANI + kilit) / KALDIRAC)
                break
        
        if yeni_sl is None: continue
        
        iyilestirme = (yeni_sl > sl_kayitli * 1.0005) if yon == "LONG" else (yeni_sl < sl_kayitli * 0.9995)
        if not iyilestirme: continue
        
        miktar = None
        for p in exchange.fetch_positions():
            if p['symbol'] == sym:
                miktar = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                break
        
        if not miktar or miktar <= 0: continue
        
        if yeni_sl_koy(sym, miktar, yeni_sl, yon):
            with state_lock:
                if sym in AKTIF_SISTEMLER:
                    AKTIF_SISTEMLER[sym]["sl_fiyat"] = yeni_sl
            print(f"🔒 [TRAILING] {sym} | ROE:%{roe:.1f} → SL:{yeni_sl:.6f}", flush=True)

# ==================== ANA DÖNGÜ ====================
def ana_dongu():
    global ARDISIK_ZARAR_SAYACI, SON_ARDISIK_ZARAR_ZAMANI, KILL_SWITCH_AKTIF, BASLANGIC_BAKIYE, GUNLUK_KAR
    
    print("🚀 [BAŞLANGIÇ] v9.0 - Adaptif Otonom Sistem", flush=True)
    try:
        exchange.load_markets()
    except: pass
    
    dinamik_liste_guncelle()
    
    # Günlük başlangıç bakiyesini al
    try:
        b = exchange.fetch_balance()
        BASLANGIC_BAKIYE = float(b['total'].get('USDT', 0))
    except: pass
    
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5); continue
            
            # Günlük hedef/zarar kontrolü
            if gunluk_kar_kontrol():
                time.sleep(3600)  # 1 saat dinlen
                continue
            
            if KILL_SWITCH_AKTIF:
                if time.time() - SON_ARDISIK_ZARAR_ZAMANI > 1800:  # 30 dk
                    KILL_SWITCH_AKTIF = False
                    ARDISIK_ZARAR_SAYACI = 0
                    tg_gonder("✅ Kill-switch kapandı")
                else:
                    time.sleep(30); continue
            
            dinamik_liste_guncelle()
            rejim, btc_yonu = piyasa_rejimini_tespit_et()
            
            # Adaptif kaldıraç güncelle
            adaptif_kaldirac_ayarla()
            
            # KAPANIŞ KONTROLÜ
            try:
                raw_pos = exchange.fetch_positions()
                anlik_aktif = [p['symbol'] for p in raw_pos if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
                
                aktif_map = {}
                for p in raw_pos:
                    k = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                    if k > 0:
                        aktif_map[p['symbol']] = p
                
                for eski in list(AKTIF_SISTEMLER.keys()):
                    if eski not in anlik_aktif:
                        bilgi = AKTIF_SISTEMLER[eski]
                        g = float(bilgi.get("giris_fiyati", 0))
                        y = bilgi.get("yon", "LONG")
                        rsi_kayit = float(bilgi.get("giris_rsi", 50))
                        rejim_kayit = bilgi.get("acilis_rejim", "?")
                        
                        cikis = g
                        try:
                            t = exchange.fetch_ticker(eski)
                            cikis = float(t['last'])
                        except: pass
                        
                        net = ((cikis - g) / g - TOPLAM_MALIYET_ORANI) if y == "LONG" else ((g - cikis) / g - TOPLAM_MALIYET_ORANI)
                        
                        # Öğrenme kaydı
                        ogrenme_kaydet(eski, rejim_kayit, rsi_kayit, y, net)
                        
                        if net > 0:
                            tip = "✅ KÂRLA KAPANDI"
                            karli = True
                        else:
                            tip = "❌ ZARARLA KAPANDI"
                            karli = False
                        
                        with state_lock:
                            if karli:
                                ANALITIK["basarili_islem_sayisi"] = int(ANALITIK.get("basarili_islem_sayisi", 0)) + 1
                                ARDISIK_ZARAR_SAYACI = 0
                            else:
                                ANALITIK["basarisiz_islem_sayisi"] = int(ANALITIK.get("basarisiz_islem_sayisi", 0)) + 1
                                ARDISIK_ZARAR_SAYACI += 1
                                SON_ARDISIK_ZARAR_ZAMANI = time.time()
                            
                            COIN_COOLDOWNLAR[eski] = {
                                "zaman": float(time.time() + COOLDOWN_SURESI_SANIYE),
                                "son_yon": y,
                                "son_cikis_fiyat": float(cikis)
                            }
                            if eski in AKTIF_SISTEMLER:
                                del AKTIF_SISTEMLER[eski]
                        
                        pozisyon_emirlerini_temizle(eski)
                        hafizayi_kaydet()
                        print(f"💰 [KAPANIŞ] {eski} | Çıkış:{cikis} | Net:%{net*100:.2f}", flush=True)
                        tg_gonder(f"{tip}\n📌 {eski}\n📊 Net: %{net*100:+.2f}")
                        
                        if ARDISIK_ZARAR_SAYACI >= ARDISIK_ZARAR_LIMIT:
                            KILL_SWITCH_AKTIF = True
                            tg_gonder(f"🚨 KILL-SWITCH AKTİF!")
            except Exception as e:
                print(f"⚠️ Kapanış: {e}", flush=True)
            
            # KÂR/ZARAR YÖNETİMİ + TRAILING
            kar_zarar_yonetimi()
            trailing_kontrol()
            
            # SİNYAL TARAMA
            adaylar = []
            
            for symbol in takip_listesi():
                if not BOT_CALISIYOR_MU: break
                if symbol in aktif_map: continue
                
                with state_lock:
                    cd = COIN_COOLDOWNLAR.get(symbol)
                    if cd:
                        z = cd.get("zaman", 0) if isinstance(cd, dict) else float(cd)
                        if z > time.time(): continue
                
                try:
                    ticker = exchange.fetch_ticker(symbol)
                    anlik = float(ticker['last'])
                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=100)
                    if len(ohlcv) < 50: continue
                    
                    df = pd.DataFrame(ohlcv, columns=['t', 'o', 'h', 'l', 'c', 'v'])
                    
                    rsi = ta.momentum.rsi(df['c'], window=14).iloc[-1]
                    emir_orani = emir_defteri_analizi(symbol)
                    
                    yon = None
                    sebep = ""
                    
                    if rejim == "YATAY":
                        if rsi < 22:
                            yon = "LONG"
                            sebep = f"TESTERE LONG (RSI:{rsi:.0f})"
                        elif rsi > 78:
                            yon = "SHORT"
                            sebep = f"TESTERE SHORT (RSI:{rsi:.0f})"
                    else:
                        if btc_yonu == "LONG" and 40 < rsi < 55 and emir_orani > 55:
                            yon = "LONG"
                            sebep = f"TREND LONG (RSI:{rsi:.0f})"
                        elif btc_yonu == "SHORT" and 45 < rsi < 60 and emir_orani < 45:
                            yon = "SHORT"
                            sebep = f"TREND SHORT (RSI:{rsi:.0f})"
                    
                    if yon is None: continue
                    
                    tp, sl = akilli_seviye(anlik, yon, df)
                    
                    if yon == "LONG":
                        net_kar = ((tp - anlik) / anlik) - TOPLAM_MALIYET_ORANI
                    else:
                        net_kar = ((anlik - tp) / anlik) - TOPLAM_MALIYET_ORANI
                    
                    if net_kar < 0.005: continue
                    
                    adaylar.append({
                        "symbol": symbol, "yon": yon, "tp": tp, "sl": sl,
                        "rsi": rsi, "fiyat": anlik, "sebep": sebep
                    })
                except: continue
            
            kapasite = MAKSIMUM_TOPLAM_POZISYON - len(aktif_map)
            
            for aday in adaylar[:kapasite]:
                if not BOT_CALISIYOR_MU: break
                if aday["symbol"] in aktif_map: continue
                
                try:
                    bakiye = exchange.fetch_balance()
                    toplam_b = float(bakiye['total'].get('USDT', 0))
                    serbest_b = float(bakiye.get('free', {}).get('USDT', 0) or 0)
                    
                    exchange.set_leverage(KALDIRAC, aday["symbol"])
                    market = exchange.market(aday["symbol"])
                    
                    kullan = min(toplam_b * POZISYON_ORANI, serbest_b)
                    if kullan < 1: continue
                    
                    giris = aday["fiyat"]
                    miktar = float(exchange.amount_to_precision(
                        aday["symbol"],
                        max((kullan * KALDIRAC) / giris / float(market.get('contractSize', 1.0)),
                            float(market['limits']['amount']['min'] or 1.0))
                    ))
                    
                    iy = 'buy' if aday["yon"] == 'LONG' else 'sell'
                    ky = 'sell' if aday["yon"] == 'LONG' else 'buy'
                    
                    exchange.create_order(aday["symbol"], 'market', iy, miktar)
                    time.sleep(0.5)
                    
                    sl_ok = False
                    try:
                        exchange.create_order(aday["symbol"], 'limit', ky, miktar, aday["tp"], {'reduceOnly': True})
                        time.sleep(0.3)
                        exchange.create_order(aday["symbol"], 'stop', ky, miktar, aday["sl"], {'stopPrice': aday["sl"], 'reduceOnly': True})
                        sl_ok = True
                    except Exception as e:
                        print(f"   ⚠️ TP/SL: {e}", flush=True)
                    
                    if not sl_ok:
                        try: pozisyon_kapat(aday["symbol"], miktar, aday["yon"], oran=1.0)
                        except: pass
                        continue
                    
                    with state_lock:
                        AKTIF_SISTEMLER[aday["symbol"]] = {
                            "giris_fiyati": giris,
                            "yon": aday["yon"],
                            "tp_fiyat": aday["tp"],
                            "sl_fiyat": aday["sl"],
                            "giris_zamani": time.time(),
                            "kaldirac": KALDIRAC,
                            "giris_rsi": float(aday["rsi"]),
                            "alinan_kademeler": [],
                            "acilis_rejim": rejim,
                            "acilis_btc_yon": btc_yonu
                        }
                        aktif_map[aday["symbol"]] = {"dummy": True}
                    
                    hafizayi_kaydet()
                    print(f"   ✅ [AÇILDI] {aday['symbol']} {aday['yon']} @ {giris}", flush=True)
                    
                    tg_gonder(
                        f"🎯 SİNYAL! [{rejim}]\n"
                        f"📌 {aday['symbol']} | {aday['yon']}\n"
                        f"📊 {aday['sebep']}\n"
                        f"🎯 Giriş: {giris:.4f} | TP: {aday['tp']:.4f} | SL: {aday['sl']:.4f}\n"
                        f"⚙️ Kaldıraç: {KALDIRAC}x | Marj: {kullan:.2f} USDT"
                    )
                except Exception as e:
                    print(f"   ⚠️ Açma {aday['symbol']}: {e}", flush=True)
                    continue
        
        except Exception as e:
            print(f"⚠️ Ana döngü: {e}", flush=True)
        
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
    app_tg.add_handler(CommandHandler("liste", liste_komutu))
    
    await app_tg.initialize()
    await app_tg.start()
    await app_tg.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    
    t = threading.Thread(target=ana_dongu, daemon=True)
    t.start()
    
    stop = asyncio.Event()
    await stop.wait()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
