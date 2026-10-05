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

# ==================== AYARLAR ====================
BOT_CALISIYOR_MU = True
state_lock = threading.Lock()

KALDIRAC = 5
MAKSIMUM_TOPLAM_POZISYON = 3
POZISYON_ORANI = 0.15
COOLDOWN_SURESI_SANIYE = 20 * 60

KOMISYON_ORANI = 0.001
SPREAD_MALIYETI = 0.0005
TOPLAM_MALIYET_ORANI = (KOMISYON_ORANI * 2) + SPREAD_MALIYETI

# ✅ KADEMELİ KÂR (HIZLI)
KADEMELI_KAR = [
    (1.5, 0.40),
    (3.0, 0.30),
    (5.0, 0.30),
]

# ✅ ZAMAN LİMİTİ (ESNETİLDİ)
MAKS_ACIK_KALMA = 6 * 60 * 60   # 60 dk → 6 saat

# ✅ TRAILING KÂR GARANTİ
TRAILING_KAR = [
    (1.5, 0.005),
    (3.0, 0.015),
    (5.0, 0.03),
]

ARDISIK_ZARAR_LIMIT = 5
ARDISIK_ZARAR_SAYACI = 0
SON_ARDISIK_ZARAR_ZAMANI = 0
KILL_SWITCH_AKTIF = False

# ==================== DİNAMİK COIN LİSTESİ ====================
DINAMIK_LISTE = []
SON_DINAMIK_GUNCELLEME = 0
DINAMIK_GUNCELLEME_SURESI = 3600
DINAMIK_LISTE_BOYUT = 10

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
                
                if hacim < 5_000_000: continue
                if degisim < 1.0: continue
                
                skor = (hacim / 1_000_000) * degisim
                adaylar.append((sym, skor, degisim, hacim))
            except:
                continue
        
        adaylar.sort(key=lambda x: x[1], reverse=True)
        DINAMIK_LISTE = [a[0] for a in adaylar[:DINAMIK_LISTE_BOYUT]]
        
        print(f"✅ [DİNAMİK] {len(DINAMIK_LISTE)} coin:", flush=True)
        for s, skor, deg, hac in adaylar[:DINAMIK_LISTE_BOYUT]:
            print(f"   • {s} | %{deg:.1f} | {hac/1_000_000:.0f}M | Skor: {skor:.0f}", flush=True)
        
        if DINAMIK_LISTE:
            liste_str = "\n".join([f"• {s.replace('/USDT:USDT','')} (%{d:.1f})" for s, _, d, _ in adaylar[:DINAMIK_LISTE_BOYUT]])
            tg_gonder(f"🔄 DİNAMİK LİSTE\n\n{liste_str}")
        
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
                "cooldownlar": veri.get("cooldownlar", {})
            }
    except Exception as e:
        print(f"⚠️ Hafıza: {e}", flush=True)
    return {
        "aktif_sistemler": {},
        "analitik": {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0},
        "cooldownlar": {}
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
                "cooldownlar": clean_cd
            }).execute()
        except Exception as e:
            print(f"⚠️ Kayıt: {e}", flush=True)

kalici = hafizayi_yukle()
AKTIF_SISTEMLER = kalici.get("aktif_sistemler", {})
ANALITIK = kalici.get("analitik", {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0})
COIN_COOLDOWNLAR = kalici.get("cooldownlar", {})

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
        silinen = 0
        for o in orders:
            if o['id'] == yeni_sl_id: continue
            if emir_sl_mi(o):
                try: exchange.cancel_order(o['id'], symbol); silinen += 1
                except: pass
        return silinen
    except: return 0

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
        
        if adx < 32 or bb_bw < 0.035 or fark < 0.25:
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

# ==================== AKILLI SL/TP (GENİŞ) ====================
def akilli_seviye(anlik, yon, df):
    atr = ta.volatility.AverageTrueRange(df['h'], df['l'], df['c'], window=14).average_true_range().iloc[-1]
    
    if yon == 'LONG':
        tp = anlik + (atr * 2.5)
        sl = anlik - (atr * 2.5)
    else:
        tp = anlik - (atr * 2.5)
        sl = anlik + (atr * 2.5)
    
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
            u = float(p.get('unrealizedPnl', 0) or 0)
            bilgi = AKTIF_SISTEMLER.get(sym, {})
            kademe = len(bilgi.get("alinan_kademeler", []))
            acilis_rejim = bilgi.get("acilis_rejim", "?")
            sure_dk = int((time.time() - bilgi.get("giris_zamani", time.time())) / 60)
            nokta = "🟢" if u >= 0 else "🔴"
            pos_detay += f"\n{nokta} {sym} | {y} ({KALDIRAC}x)\n  Giriş: `{g:.4f}` | ROE: `%{roe:+.2f}`\n  Kademe: {kademe}/3 | Rejim: {acilis_rejim} | {sure_dk} dk"
        
        pnl_nokta = "🟢" if pnl >= 0 else "🔴"
        
        liste_str = "\n".join([f"• {s.replace('/USDT:USDT','')}" for s in DINAMIK_LISTE[:5]])
        
        mesaj = (
            f"📊 DURUM [v8.2]\n\n"
            f"🌐 BTC Rejim: `{rejim}` ({yon_btc})\n"
            f"💰 Kasa: `{total:.2f} USDT`\n"
            f"{pnl_nokta} Toplam PnL: `{pnl:+.2f} USDT`\n"
            f"📌 Açık: `{len(pos)} / {MAKSIMUM_TOPLAM_POZISYON}`"
            f"{pos_detay}\n\n"
            f"✅ TP: `{bas}` | ❌ SL: `{basz}`\n"
            f"📈 Başarı: `%{oran:.1f}` ({top} işlem)\n"
            f"⚡ Kill-Switch: `{'AKTİF' if KILL_SWITCH_AKTIF else 'Pasif'}`\n\n"
            f"📋 Dinamik Liste ({len(DINAMIK_LISTE)}):\n{liste_str}"
        )
        await update.message.reply_text(mesaj)
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    global BOT_CALISIYOR_MU, KILL_SWITCH_AKTIF, ARDISIK_ZARAR_SAYACI
    BOT_CALISIYOR_MU = True
    KILL_SWITCH_AKTIF = False
    ARDISIK_ZARAR_SAYACI = 0
    await update.message.reply_text("🟢 Bot aktif! (v8.2 - Rejim kontrolü + esnek süre)")

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

async def liste_guncelle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    global SON_DINAMIK_GUNCELLEME
    SON_DINAMIK_GUNCELLEME = 0
    await update.message.reply_text("🔄 Liste güncelleniyor...")
    await asyncio.to_thread(dinamik_liste_guncelle)
    liste_str = "\n".join([f"• {s.replace('/USDT:USDT','')}" for s in DINAMIK_LISTE])
    await update.message.reply_text(f"✅ Güncellendi:\n\n{liste_str}")

# ==================== KADEMELİ KÂR + REJİM KONTROLÜ ====================
def kar_zarar_yonetimi():
    with state_lock:
        kopya = list(AKTIF_SISTEMLER.items())
    
    # Güncel rejim ve BTC yönü (bir kere hesapla)
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
        
        # ✅ 1. REJİM DEĞİŞİM KONTROLÜ
        if acilis_rejim != guncel_rejim:
            if pozisyon_kapat(sym, miktar, yon, oran=1.0):
                print(f"🔄 [REJİM DEĞİŞTİ] {sym} | {acilis_rejim} → {guncel_rejim} | ROE:%{roe:.1f}", flush=True)
                tg_gonder(f"🔄 REJİM DEĞİŞTİ - KAPATILDI\n📌 {sym}\n📊 {acilis_rejim} → {guncel_rejim}\n💰 ROE: %{roe:+.2f}")
            continue
        
        # ✅ 2. BTC YÖN DEĞİŞİKLİĞİ (sadece TREND modunda)
        if acilis_rejim == "TREND" and acilis_btc_yon != guncel_btc_yon:
            if pozisyon_kapat(sym, miktar, yon, oran=1.0):
                print(f"🔄 [BTC YÖN] {sym} | {acilis_btc_yon} → {guncel_btc_yon} | ROE:%{roe:.1f}", flush=True)
                tg_gonder(f"🔄 BTC YÖN DEĞİŞTİ - KAPATILDI\n📌 {sym}\n📊 {acilis_btc_yon} → {guncel_btc_yon}\n💰 ROE: %{roe:+.2f}")
            continue
        
        # ✅ 3. ZAMAN LİMİTİ (6 saat)
        gecen = time.time() - giris_zaman
        if gecen > MAKS_ACIK_KALMA:
            if pozisyon_kapat(sym, miktar, yon, oran=1.0):
                print(f"⏰ [ZAMAN] {sym} | {int(gecen/60)}dk | ROE:%{roe:.1f}", flush=True)
                tg_gonder(f"⏰ ZAMAN DOLDU\n📌 {sym}\n🕐 {int(gecen/60)} dk\n💰 ROE: %{roe:+.2f}")
            continue
        
        # ✅ 4. KADEMELİ KÂR
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
                    tg_gonder(f"💰 KADEMELİ KÂR\n📌 {sym}\n📊 ROE: %{roe:+.2f}\n🎯 %{int(oran*100)} kapatıldı")
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
    global ARDISIK_ZARAR_SAYACI, SON_ARDISIK_ZARAR_ZAMANI, KILL_SWITCH_AKTIF
    
    print("🚀 [BAŞLANGIÇ] v8.2 - Rejim kontrolü aktif", flush=True)
    try:
        exchange.load_markets()
    except: pass
    
    dinamik_liste_guncelle()
    
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5); continue
            
            if KILL_SWITCH_AKTIF:
                if time.time() - SON_ARDISIK_ZARAR_ZAMANI > 3600:
                    KILL_SWITCH_AKTIF = False
                    ARDISIK_ZARAR_SAYACI = 0
                    tg_gonder("✅ Kill-switch kapandı")
                else:
                    time.sleep(30); continue
            
            dinamik_liste_guncelle()
            rejim, btc_yonu = piyasa_rejimini_tespit_et()
            
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
                        
                        cikis = g
                        try:
                            t = exchange.fetch_ticker(eski)
                            cikis = float(t['last'])
                        except: pass
                        
                        net = ((cikis - g) / g - TOPLAM_MALIYET_ORANI) if y == "LONG" else ((g - cikis) / g - TOPLAM_MALIYET_ORANI)
                        
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
                        tg_gonder(f"{tip}\n📌 {eski} | Çıkış: {cikis}\n📊 Net: %{net*100:+.2f}")
                        
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
                        if rsi < 28:
                            yon = "LONG"
                            sebep = f"TESTERE LONG (RSI:{rsi:.0f})"
                        elif rsi > 72:
                            yon = "SHORT"
                            sebep = f"TESTERE SHORT (RSI:{rsi:.0f})"
                    else:
                        if btc_yonu == "LONG" and 40 < rsi < 55 and emir_orani > 55:
                            yon = "LONG"
                            sebep = f"TREND LONG (RSI:{rsi:.0f}, Emir:{emir_orani:.0f}%)"
                        elif btc_yonu == "SHORT" and 45 < rsi < 60 and emir_orani < 45:
                            yon = "SHORT"
                            sebep = f"TREND SHORT (RSI:{rsi:.0f}, Emir:{emir_orani:.0f}%)"
                    
                    if yon is None: continue
                    
                    tp, sl = akilli_seviye(anlik, yon, df)
                    
                    if yon == "LONG":
                        net_kar = ((tp - anlik) / anlik) - TOPLAM_MALIYET_ORANI
                    else:
                        net_kar = ((anlik - tp) / anlik) - TOPLAM_MALIYET_ORANI
                    
                    if net_kar < 0.005: continue
                    
                    adaylar.append({
                        "symbol": symbol, "yon": yon, "tp": tp, "sl": sl,
                        "rsi": rsi, "fiyat": anlik, "sebep": sebep, "df": df
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
                    print(f"   ✅ [AÇILDI] {aday['symbol']} {aday['yon']} @ {giris} | {rejim}", flush=True)
                    
                    tg_gonder(
                        f"🎯 SİNYAL! [{rejim}]\n"
                        f"📌 {aday['symbol']} | {aday['yon']}\n"
                        f"📊 {aday['sebep']}\n"
                        f"🎯 Giriş: {giris:.4f}\n"
                        f"💰 TP: {aday['tp']:.4f}\n"
                        f"🛑 SL: {aday['sl']:.4f}\n"
                        f"💵 Marj: {kullan:.2f} USDT"
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
    app_tg.add_handler(CommandHandler("listeguncelle", liste_guncelle))
    
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
