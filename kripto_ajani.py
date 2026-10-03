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

# ==================== .ENV YÜKLEME ====================
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

# ==================== API ANAHTARLARI ====================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()
GATE_API_KEY = os.environ.get("GATE_API_KEY", "").strip()
GATE_SECRET = os.environ.get("GATE_SECRET", "").strip()

if not TELEGRAM_TOKEN or not CHAT_ID:
    print("❌ TELEGRAM boş!", flush=True); sys.exit(1)
if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌ SUPABASE boş!", flush=True); sys.exit(1)
if not GATE_API_KEY or not GATE_SECRET:
    print("❌ GATE boş!", flush=True); sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

exchange = ccxt.gate({
    'apiKey': GATE_API_KEY,
    'secret': GATE_SECRET,
    'enableRateLimit': True,
    'timeout': 30000,
    'options': {'defaultType': 'swap'}
})
exchange.set_sandbox_mode(True)

# ==================== HİBRİT HAVUZ ====================
CEKIRDEK_LISTE = [
    'SOL/USDT:USDT', 'XRP/USDT:USDT', 'DOGE/USDT:USDT', 'LTC/USDT:USDT', 'LINK/USDT:USDT'
]
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
        print(f"   🔄 Dinamik: {DINAMIK_LISTE}", flush=True)
        return True
    except Exception as e:
        print(f"⚠️ Havuz hatası: {e}", flush=True)
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
COOLDOWN_SURESI_SANIYE = 5 * 60

# Komisyon
KOMISYON_ORANI = 0.001
SPREAD_MALIYETI = 0.0005
MIN_NET_KAR = 0.002

# Mum sayısı
MUM_SAYISI = 5
MUM_ONAY = 4
HACIM_ESIGI = 1.3

# Mum boyutu çarpanları
SL_CARPAN = 1.5     # SL = mum_boyutu * 1.5
TP_CARPAN = 2.5     # TP = mum_boyutu * 2.5

# Trailing (mum boyutuna göre)
TRAILING_MIKRO = [
    (1.0, 0.3),     # Kâr = mum_boyutu * 1.0 → SL = başabaş + %0.3
    (1.5, 0.5),     # Kâr = mum_boyutu * 1.5 → SL = %0.5 kâra
    (2.0, 1.0),     # Kâr = mum_boyutu * 2.0 → SL = %1.0 kâra
    (3.0, 1.5),     # Kâr = mum_boyutu * 3.0 → SL = %1.5 kâra
]

# Max süre
MAKS_ACIK_KALMA_SURESI = 60 * 60  # 60 dakika (daha uzun, çünkü trend takip ediyor)

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

# ==================== MUM BOYUTU ====================
def mum_boyutu_hesapla(df, periyot=20):
    """Son N mumun ortalama boyutu (ATR gibi)."""
    try:
        son_n = df.tail(periyot)
        boyut = (son_n['high'] - son_n['low']).mean()
        return float(boyut)
    except:
        return 0

# ==================== MUM SİNYALİ ====================
def mum_sinyal(df_1m, anlik_fiyat):
    """
    Mumlara bakarak sinyal üret + mum boyutuna göre SL/TP hesapla.
    """
    try:
        if len(df_1m) < 25:
            return None, None, None, None, None

        # ===== MUM BOYUTU =====
        mum_boyut = mum_boyutu_hesapla(df_1m, 20)
        if mum_boyut <= 0:
            return None, None, None, None, None

        # ===== SON 5 MUM =====
        son_5 = df_1m.tail(MUM_SAYISI).reset_index(drop=True)
        yesil = sum(1 for i in range(MUM_SAYISI) if son_5['close'].iloc[i] > son_5['open'].iloc[i])
        kirmizi = MUM_SAYISI - yesil

        # ===== HACİM =====
        hacim_ort = df_1m['volume'].rolling(20).mean().iloc[-1]
        guncel_hacim = df_1m['volume'].iloc[-1]
        hacim_orani = guncel_hacim / hacim_ort if hacim_ort > 0 else 0

        if hacim_orani < HACIM_ESIGI:
            return None, None, None, None, None

        # ===== SON MUM =====
        son_mum_yesil = df_1m['close'].iloc[-1] > df_1m['open'].iloc[-1]
        son_mum_kirmizi = df_1m['close'].iloc[-1] < df_1m['open'].iloc[-1]

        # ===== SL/TP (MUM BOYUTUNA GÖRE) =====
        sl_mesafe = mum_boyut * SL_CARPAN
        tp_mesafe = mum_boyut * TP_CARPAN

        # ===== LONG =====
        if yesil >= MUM_ONAY and son_mum_yesil:
            tp = anlik_fiyat + tp_mesafe
            sl = anlik_fiyat - sl_mesafe
            
            # Komisyon kontrolü
            brut_kar_orani = tp_mesafe / anlik_fiyat
            net_kar = brut_kar_orani - KOMISYON_ORANI - SPREAD_MALIYETI
            if net_kar < MIN_NET_KAR:
                return None, None, None, None, None
            
            sebep = f"{yesil}/{MUM_SAYISI} yeşil | Hacim {hacim_orani:.1f}x | Mum {mum_boyut:.5f}"
            return "LONG", tp, sl, sebep, mum_boyut

        # ===== SHORT =====
        if kirmizi >= MUM_ONAY and son_mum_kirmizi:
            tp = anlik_fiyat - tp_mesafe
            sl = anlik_fiyat + sl_mesafe
            
            brut_kar_orani = tp_mesafe / anlik_fiyat
            net_kar = brut_kar_orani - KOMISYON_ORANI - SPREAD_MALIYETI
            if net_kar < MIN_NET_KAR:
                return None, None, None, None, None
            
            sebep = f"{kirmizi}/{MUM_SAYISI} kırmızı | Hacim {hacim_orani:.1f}x | Mum {mum_boyut:.5f}"
            return "SHORT", tp, sl, sebep, mum_boyut

        return None, None, None, None, None
    except Exception as e:
        print(f"⚠️ Mum sinyal hatası: {e}", flush=True)
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
            f"📊 *DURUM* [MUM BOYUTU TABANLI]\n\n"
            f"💰 Kasa: `{total:.2f} USDT` | PnL: `{pnl:+.2f}`\n"
            f"📌 Açık: `{len(pos)} / {MAKSIMUM_TOPLAM_POZISYON}`"
            f"{pos_detay}\n\n"
            f"✅ TP: `{bas}` | ❌ SL: `{basz}`\n"
            f"📈 Başarı: `%{oran:.1f}`\n\n"
            f"📋 Havuz: `{len(takip_listesi())}` coin"
        )
        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = True
    await update.message.reply_text("🟢 Bot aktif! (Mum Boyutu Tabanlı)")

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
            f"📌 Çekirdek: `{len(CEKIRDEK_LISTE)}` coin\n"
            f"🔄 Dinamik: `{len(DINAMIK_LISTE)}` coin\n\n"
            f"*Dinamik Liste:*\n" + "\n".join([f"• `{c}`" for c in DINAMIK_LISTE]),
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text("❌ Havuz güncellenemedi.")

# ==================== MİKRO TRAILING ====================
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
        kaldirac_v = int(bilgi.get("kaldirac", KALDIRAC))
        mum_boyut = float(bilgi.get("mum_boyut", 0))
        giris_zaman = float(bilgi.get("giris_zamani", 0))

        if time.time() - giris_zaman < 30: continue

        try:
            with borsa_kilidi:
                t = exchange.fetch_ticker(sym)
                anlik = float(t['last'])
        except: continue

        if mum_boyut <= 0: continue

        # Kâr mum_boyutunun kaç katı?
        if yon == "LONG":
            kar_mesafe = anlik - g
        else:
            kar_mesafe = g - anlik

        kar_orani = kar_mesafe / mum_boyut  # Mum boyutunun kaç katı?

        # Trailing seviyeleri (mum boyutuna göre)
        yeni_sl = None
        for esik, sl_kilit in TRAILING_MIKRO:
            if kar_orani >= esik:
                if yon == "LONG":
                    yeni_sl = g + (mum_boyut * sl_kilit)
                else:
                    yeni_sl = g - (mum_boyut * sl_kilit)
                break

        if yeni_sl is None: continue

        # İyileştirme var mı?
        iyilestirme = (yeni_sl > sl_kayitli * 1.0005) if yon == "LONG" else (yeni_sl < sl_kayitli * 0.9995)
        if not iyilestirme: continue

        # SL güncelle
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
            print(f"🔒 [TRAILING] {sym} | Kâr {kar_orani:.1f}x mum → SL: {yeni_sl:.6f}", flush=True)
            tg_gonder(f"🔒 *KÂR KİLİTLENDİ*\n📌 `{sym}` | {yon}\n📊 Kâr: `{kar_orani:.1f}x mum`\n🛑 Yeni SL: `{yeni_sl:.6f}`")
        except Exception as e:
            print(f"⚠️ Trailing hatası {sym}: {e}", flush=True)

# ==================== KAPANIŞ + SÜRE ====================
def kapanis_kontrol():
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

        # Kapanış
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
                if karli:
                    b += 1; tip = "✅ *KÂRLA KAPANDI*"
                else:
                    bz += 1; tip = "❌ *ZARARLA KAPANDI*"
                ANALITIK["basarili_islem_sayisi"] = b
                ANALITIK["basarisiz_islem_sayisi"] = bz
                COIN_COOLDOWN[sym] = {"zaman": float(time.time() + COOLDOWN_SURESI_SANIYE), "son_yon": y}
                if sym in AKTIF_POZISYONLAR:
                    del AKTIF_POZISYONLAR[sym]

            hafizayi_kaydet()
            print(f"💰 [KAPANIŞ] {sym} | Çıkış: {cikis}", flush=True)
            tg_gonder(f"{tip}\n📌 `{sym}` | Çıkış: `{cikis}`")
            continue

        # Süre
        giris_zaman = float(bilgi.get("giris_zamani", 0))
        gecen_sure = time.time() - giris_zaman
        if gecen_sure > MAKS_ACIK_KALMA_SURESI:
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
                print(f"⏰ [SÜRE DOLDU] {sym} | {dakika}dk", flush=True)
                tg_gonder(f"⏰ *SÜRE DOLDU*\n📌 `{sym}` | {dakika}dk")
            except Exception as e:
                print(f"⚠️ Süre hatası {sym}: {e}", flush=True)

# ==================== ANA TARAYICI ====================
def tarayici():
    global SON_HAVUZ_GUNCELLEME
    print("🚀 [BAŞLANGIÇ] MUM BOYUTU TABANLI + 7x...", flush=True)
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

            print(f"\n{'='*60}", flush=True)
            print(f"🔄 [DÖNGÜ #{dongu}] {time.strftime('%H:%M:%S')}", flush=True)
            print(f"{'='*60}", flush=True)

            # KAPANIŞ
            kapanis_kontrol()

            # TRAILING
            trailing_stop_kontrol()

            # Açık pozisyonlar
            try:
                with borsa_kilidi:
                    raw = exchange.fetch_positions()
                aktif_map = {p['symbol']: p for p in raw if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0}
                aktif_list = list(aktif_map.keys())
            except:
                raw = []
                aktif_map = {}
                aktif_list = []

            # TARAMA
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
                        ohlcv_1m = exchange.fetch_ohlcv(symbol, timeframe='1m', limit=60)
                        ticker = exchange.fetch_ticker(symbol)

                    df_1m = pd.DataFrame(ohlcv_1m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    anlik = float(ticker['last'])

                    # Mum boyutu
                    mum_boyut = mum_boyutu_hesapla(df_1m, 20)
                    hacim_ort = df_1m['volume'].rolling(20).mean().iloc[-1]
                    guncel_hacim = df_1m['volume'].iloc[-1]
                    hacim_orani = guncel_hacim / hacim_ort if hacim_ort > 0 else 0

                    # Son 5 mum
                    son_5 = df_1m.tail(5).reset_index(drop=True)
                    yesil = sum(1 for i in range(5) if son_5['close'].iloc[i] > son_5['open'].iloc[i])
                    kirmizi = 5 - yesil

                    print(f"\n🔍 [{symbol}] @ {anlik} | Mum: {mum_boyut:.5f} | Hacim: {hacim_orani:.1f}x | Yeşil: {yesil}/5", flush=True)

                    yon_s, tp_fiyat, sl_fiyat, sebep, mum_b = mum_sinyal(df_1m, anlik)

                    if yon_s is None:
                        print(f"   ⏭️ Sinyal yok", flush=True)
                        continue

                    rr = TP_CARPAN / SL_CARPAN
                    hedef_roe = (TP_CARPAN * mum_boyut / anlik) * 100 * KALDIRAC
                    kapat_yon = 'sell' if yon_s == 'LONG' else 'buy'

                    print(f"   🎯 SİNYAL! {yon_s} | {sebep} | R/R: {rr:.2f}", flush=True)

                    # POZİSYON AÇ
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
                            "mum_boyut": mum_b,
                            "mod": "MUM_BOYUT",
                            "sebep": sebep
                        }
                        aktif_list.append(symbol)
                        aktif_map[symbol] = {"dummy": True}

                    hafizayi_kaydet()
                    print(f"✅ [AÇILDI] {symbol} {yon_s} @ {anlik} | SL: {sl_fiyat} | TP: {tp_fiyat} | Mum: {mum_b:.5f}", flush=True)

                    tg_gonder(
                        f"🎯 *MUM BOYUTU SİNYALİ!*\n"
                        f"📌 `{symbol}` | {yon_s}\n"
                        f"📊 {sebep}\n"
                        f"🎯 Giriş: `{anlik}`\n"
                        f"💰 TP: `{tp_fiyat}` (ROE: `%{hedef_roe:.1f}`)\n"
                        f"🛑 SL: `{sl_fiyat}`\n"
                        f"📏 Mum Boyutu: `{mum_b:.5f}`\n"
                        f"📊 R/R: `{rr:.2f}`"
                    )
                except Exception as e:
                    print(f"⚠️ {symbol}: {e}", flush=True)
                    continue

        except Exception as e:
            print(f"⚠️ Ana döngü: {e}", flush=True)
        finally:
            try: tarayici_kilidi.release()
            except: pass

        time.sleep(5)

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
