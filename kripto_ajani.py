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
exchange.set_sandbox_mode(True)  # ⚠️ Gerçek hesaba geçerken False yap!

# ==================== DİNAMİK HAVUZ ====================
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
KALDIRAC = 5

GLOBAL_COOLDOWN_BITIS = 0.0
SON_BTC_YONU = "YATAY (Testere)"

KOMISYON_ORANI = 0.001
SPREAD_MALIYETI = 0.0005

TRAILING_SEVIYELER = [
    (15.0, 0.10),
    (10.0, 0.06),
    (7.0, 0.03),
    (5.0, 0.01),
]

KISMI_KAR_ROE = 5.0
KISMI_KAR_ORANI = 0.5

MAKS_ACIK_KALMA_SURESI = 4 * 60 * 60
MIN_KAR_ESIGI = 0.001

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
                "basarili_islem_sayisi": int(ANALITIK_HAFIZA.get("basarili_islem_sayisi", 0)),
                "basarisiz_islem_sayisi": int(ANALITIK_HAFIZA.get("basarisiz_islem_sayisi", 0)),
                "egitim_verileri": ANALITIK_HAFIZA.get("egitim_verileri", [])
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
ANALITIK_HAFIZA = kalici_veri.get("analitik", {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0, "egitim_verileri": []})
COIN_COOLDOWNLAR = kalici_veri.get("cooldownlar", {})

MAKSIMUM_TOPLAM_POZISYON = 3
COOLDOWN_SURESI_SANIYE = 10 * 60

# ==================== PİYASA REJİMİ ====================
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

        if adx_1h < 25.0 or bb_bandwidth < 0.02 or fark_yuzdesi < 0.15:
            rejim = "YATAY"
            trend_yonu = "YATAY (Testere)"
        else:
            rejim = "TREND"
            trend_yonu = "LONG" if ema9 > ema21 else "SHORT"
            SON_BTC_YONU = trend_yonu

        print(f"🌐 [PİYASA] Rejim: {rejim} | Yön: {trend_yonu} | ADX: {adx_1h:.2f} | BB: {bb_bandwidth:.4f} | Fark: %{fark_yuzdesi:.3f}", flush=True)
        return rejim, trend_yonu
    except Exception as e:
        print(f"⚠️ [PİYASA HATA] {e}. Varsayılan YATAY", flush=True)
        return "YATAY", "YATAY (Testere)"

# ==================== 3'LÜ SİNYAL SİSTEMİ ====================
def sinyal_uret(df, anlik_fiyat, emir_analizi, piyasa_rejimi):
    try:
        bb = ta.volatility.BollingerBands(df['close'], window=20, window_dev=2)
        bb_high = bb.bollinger_hband().iloc[-1]
        bb_low = bb.bollinger_lband().iloc[-1]

        stoch_rsi = ta.momentum.StochRSIIndicator(df['close'], window=14).stochrsi().iloc[-1]

        hacim_ort = df['volume'].rolling(20).mean().iloc[-1]
        guncel_hacim = df['volume'].iloc[-1]
        hacim_patlamasi = guncel_hacim > (hacim_ort * 1.8)

        fiyat_yukari = df['close'].iloc[-1] > df['close'].iloc[-2]

        bb_low_temas = anlik_fiyat <= bb_low * 1.002
        bb_high_temas = anlik_fiyat >= bb_high * 0.998
        stoch_asiri_satim = stoch_rsi < 0.2
        stoch_asiri_alim = stoch_rsi > 0.8

        # DÜZ İŞLEM: Alt bant + aşırı satım = LONG
        if bb_low_temas and stoch_asiri_satim:
            return "LONG", f"BB alt bant + StochRSI aşırı satım ({stoch_rsi:.2f})"

        # DÜZ İŞLEM: Üst bant + aşırı alım = SHORT
        if bb_high_temas and stoch_asiri_alim:
            return "SHORT", f"BB üst bant + StochRSI aşırı alım ({stoch_rsi:.2f})"

        # Hacim patlaması: yönü fiyat belirler
        if hacim_patlamasi:
            yon = "LONG" if fiyat_yukari else "SHORT"
            return yon, f"Hacim patlaması ({guncel_hacim/hacim_ort:.1f}x)"

        return None, None
    except Exception:
        return None, None

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

# ==================== AKILLI SEVİYE ====================
def akilli_seviye_hesapla(anlik_fiyat, yon, df):
    atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range().iloc[-1]

    if yon == 'LONG':
        tp_fiyat = anlik_fiyat + (atr * 1.2)
        sl_fiyat = anlik_fiyat - (atr * 0.8)
        kapat_yon = 'sell'
    else:
        tp_fiyat = anlik_fiyat - (atr * 1.2)
        sl_fiyat = anlik_fiyat + (atr * 0.8)
        kapat_yon = 'buy'

    hedef_roe = abs((tp_fiyat - anlik_fiyat) / anlik_fiyat) * 100 * KALDIRAC
    return float(tp_fiyat), float(sl_fiyat), kapat_yon, float(hedef_roe)

# ==================== TRAILING STOP ====================
def trailing_stop_kontrol():
    with state_lock:
        aktif_kopya = list(AKTIF_GRID_SISTEMLERI.items())

    for sym, bilgi in aktif_kopya:
        if sym not in AKTIF_GRID_SISTEMLERI: continue
        if not isinstance(bilgi, dict): continue

        yon = bilgi.get("yon", "LONG")
        g = float(bilgi.get("giris_fiyati", 0))
        sl_kayitli = float(bilgi.get("sl_fiyat", 0))
        tp_kayitli = float(bilgi.get("tp_fiyat", 0))
        giris_zaman = float(bilgi.get("giris_zamani", 0))

        if time.time() - giris_zaman < 60: continue

        try:
            t = exchange.fetch_ticker(sym)
            anlik = float(t['last'])
        except: continue

        roe = ((anlik - g) / g * 100 * KALDIRAC) if yon == "LONG" else ((g - anlik) / g * 100 * KALDIRAC)

        yeni_sl = None
        for esik_roe, kilit_orani in TRAILING_SEVIYELER:
            if roe >= esik_roe:
                yeni_sl = g * (1 + kilit_orani / KALDIRAC) if yon == "LONG" else g * (1 - kilit_orani / KALDIRAC)
                break

        if yeni_sl is None: continue

        if yon == "LONG":
            iyilestirme = yeni_sl > sl_kayitli * 1.0005
        else:
            iyilestirme = yeni_sl < sl_kayitli * 0.9995

        if not iyilestirme: continue

        try:
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
                if sym in AKTIF_GRID_SISTEMLERI:
                    AKTIF_GRID_SISTEMLERI[sym]["sl_fiyat"] = yeni_sl
            print(f"🔒 [TRAILING] {sym} | ROE:%{roe:.1f} → SL:{yeni_sl:.6f}", flush=True)
            telegram_mesaj_gonder(f"🔒 *KÂR KİLİTLENDİ*\n📌 `{sym}` | {yon}\n📊 ROE: `%{roe:+.2f}`\n🛑 Yeni SL: `{yeni_sl:.6f}`")
        except Exception as e:
            print(f"⚠️ Trailing hatası {sym}: {e}", flush=True)

# ==================== KISMİ KÂR AL ====================
def kismi_kar_al_kontrol():
    with state_lock:
        aktif_kopya = list(AKTIF_GRID_SISTEMLERI.items())

    for sym, bilgi in aktif_kopya:
        if sym not in AKTIF_GRID_SISTEMLERI: continue
        if not isinstance(bilgi, dict): continue
        if bilgi.get("kismi_kar_alindi", False): continue

        yon = bilgi.get("yon", "LONG")
        g = float(bilgi.get("giris_fiyati", 0))

        try:
            t = exchange.fetch_ticker(sym)
            anlik = float(t['last'])
        except: continue

        roe = ((anlik - g) / g * 100 * KALDIRAC) if yon == "LONG" else ((g - anlik) / g * 100 * KALDIRAC)

        if roe >= KISMI_KAR_ROE:
            try:
                exchange.cancel_all_orders(sym)
                miktar = None
                for p in exchange.fetch_positions():
                    if p['symbol'] == sym:
                        miktar = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                        break
                if not miktar or miktar <= 0: continue

                yarim = miktar * KISMI_KAR_ORANI
                ky = 'sell' if yon == 'LONG' else 'buy'
                exchange.create_order(sym, 'market', ky, yarim, None, {'reduceOnly': True})

                kalan = miktar - yarim
                tp_kayitli = float(bilgi.get("tp_fiyat", 0))
                sl_kayitli = float(bilgi.get("sl_fiyat", 0))
                if tp_kayitli > 0:
                    exchange.create_order(sym, 'limit', ky, kalan, tp_kayitli, {'reduceOnly': True})
                if sl_kayitli > 0:
                    exchange.create_order(sym, 'stop', ky, kalan, sl_kayitli, {'stopPrice': sl_kayitli, 'reduceOnly': True})

                with state_lock:
                    if sym in AKTIF_GRID_SISTEMLERI:
                        AKTIF_GRID_SISTEMLERI[sym]["kismi_kar_alindi"] = True

                print(f"💰 [KISMİ KÂR] {sym} | ROE:%{roe:.1f} → %50 kapatıldı", flush=True)
                telegram_mesaj_gonder(f"💰 *KISMİ KÂR ALINDI*\n📌 `{sym}` | {yon}\n📊 ROE: `%{roe:+.2f}`\n✂️ %50 kapatıldı, kalan koşuyor.")
            except Exception as e:
                print(f"⚠️ Kısmi kâr hatası {sym}: {e}", flush=True)

# ==================== ZOMBİ İŞLEM KAPATICI ====================
def zombi_islem_kapat():
    with state_lock:
        aktif_kopya = list(AKTIF_GRID_SISTEMLERI.items())

    for sym, bilgi in aktif_kopya:
        if sym not in AKTIF_GRID_SISTEMLERI: continue
        if not isinstance(bilgi, dict): continue

        yon = bilgi.get("yon", "LONG")
        g = float(bilgi.get("giris_fiyati", 0))
        giris_zaman = float(bilgi.get("giris_zamani", 0))

        gecen_sure = time.time() - giris_zaman
        if gecen_sure < MAKS_ACIK_KALMA_SURESI:
            continue

        try:
            t = exchange.fetch_ticker(sym)
            anlik = float(t['last'])
        except:
            continue

        if yon == "LONG":
            brut_kar = (anlik - g) / g
        else:
            brut_kar = (g - anlik) / g

        net_kar = brut_kar - KOMISYON_ORANI - SPREAD_MALIYETI

        if net_kar >= MIN_KAR_ESIGI:
            try:
                exchange.cancel_all_orders(sym)
                miktar = None
                for p in exchange.fetch_positions():
                    if p['symbol'] == sym:
                        miktar = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                        break
                if not miktar or miktar <= 0: continue

                ky = 'sell' if yon == 'LONG' else 'buy'
                exchange.create_order(sym, 'market', ky, miktar, None, {'reduceOnly': True})

                with state_lock:
                    if sym in AKTIF_GRID_SISTEMLERI: del AKTIF_GRID_SISTEMLERI[sym]
                    COIN_COOLDOWNLAR[sym] = {"zaman": float(time.time() + COOLDOWN_SURESI_SANIYE), "son_yon": yon}

                hafizayi_kaydet()
                saat = int(gecen_sure // 3600)
                dakika = int((gecen_sure % 3600) // 60)
                print(f"⏰ [ZOMBİ KAPATILDI] {sym} | {saat}s {dakika}dk | Net: %{net_kar*100:.3f}", flush=True)
                telegram_mesaj_gonder(
                    f"⏰ *ZOMBİ İŞLEM KAPATILDI*\n"
                    f"📌 `{sym}` | {yon}\n"
                    f"⏱️ Süre: `{saat}s {dakika}dk`\n"
                    f"💰 Net Kâr: `%{net_kar*100:.3f}`"
                )
            except Exception as e:
                print(f"⚠️ Zombi kapatma hatası {sym}: {e}", flush=True)
        else:
            print(f"⏳ [ZOMBİ BEKLE] {sym} | {int(gecen_sure//60)}dk | Net: %{net_kar*100:.3f}", flush=True)

# ==================== TELEGRAM ====================
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
        basarili = int(ANALITIK_HAFIZA.get("basarili_islem_sayisi", 0))
        basarisiz = int(ANALITIK_HAFIZA.get("basarisiz_islem_sayisi", 0))
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
            kismi = AKTIF_GRID_SISTEMLERI.get(sym, {}).get("kismi_kar_alindi", False)
            etiket = " ✂️" if kismi else ""
            pos_detaylari += f"\n• `{sym}` | {yon}{etiket} | Giriş: `{giris}`\n  ROE: `%{roe:+.2f}`"

        mesaj = (
            f"📊 **DURUM (Düz İşlem + Trailing + Kısmi + Zombi)**\n\n"
            f"🌐 Rejim: `{rejim}` (BTC: `{btc_yon}`)\n"
            f"💰 Kasa: `{total:.2f} USDT` | PnL: `{toplam_pnl:+.2f}`\n"
            f"📌 Açık: `{len(borsa_poslari)} / {MAKSIMUM_TOPLAM_POZISYON}`"
            f"{pos_detaylari}\n\n"
            f"✅ TP: `{basarili}` | ❌ SL: `{basarisiz}`\n"
            f"📈 Başarı: `%{basari_orani:.1f}`\n\n"
            f"📋 Havuz: `{len(takip_listesi())}` coin\n"
            f"   Çekirdek: `{len(CEKIRDEK_LISTE)}` | Dinamik: `{len(DINAMIK_LISTE)}`"
        )
        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = True
    await update.message.reply_text("🟢 Bot aktif! (Düz İşlem + Trailing + Kısmi + Zombi)")

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
                except Exception: pass
                exchange.create_order(pos['symbol'], 'market', kapatma_yonu, kontrat, None, {'reduceOnly': True})
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

# ==================== ANA TARAYICI ====================
def otomatik_arkaplan_tarayici():
    global SON_HAVUZ_GUNCELLEME
    print("🚀 [BAŞLANGIÇ] Düz İşlem Modu (Ters İşlem Yok)...", flush=True)
    try:
        exchange.load_markets()
    except Exception: pass

    havuzu_guncelle()
    SON_HAVUZ_GUNCELLEME = time.time()

    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5)
                continue

            if time.time() - SON_HAVUZ_GUNCELLEME > HAVUZ_GUNCELLEME_SURESI:
                havuzu_guncelle()
                SON_HAVUZ_GUNCELLEME = time.time()

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

            # KAPANIŞ KONTROLÜ
            try:
                anlik_aktif_semboller = [p['symbol'] for p in raw_positions if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
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
                            bas_sayi = int(ANALITIK_HAFIZA.get("basarili_islem_sayisi", 0))
                            basarisiz_sayi = int(ANALITIK_HAFIZA.get("basarisiz_islem_sayisi", 0))

                            if islem_karli_mi:
                                bas_sayi += 1
                                sonuc_mesaj_tipi = "✅ *KÂRLA KAPANDI*"
                            else:
                                basarisiz_sayi += 1
                                sonuc_mesaj_tipi = "❌ *ZARARLA KAPANDI*"

                            ANALITIK_HAFIZA["basarili_islem_sayisi"] = bas_sayi
                            ANALITIK_HAFIZA["basarisiz_islem_sayisi"] = basarisiz_sayi
                            COIN_COOLDOWNLAR[eski_sym] = {"zaman": float(time.time() + COOLDOWN_SURESI_SANIYE), "son_yon": yon}
                            if eski_sym in AKTIF_GRID_SISTEMLERI: del AKTIF_GRID_SISTEMLERI[eski_sym]

                        hafizayi_kaydet()
                        telegram_mesaj_gonder(f"{sonuc_mesaj_tipi}\n📌 `{eski_sym}`")
            except Exception: pass

            # TRAILING + KISMİ + ZOMBİ
            trailing_stop_kontrol()
            kismi_kar_al_kontrol()
            zombi_islem_kapat()

            taranan_sinyaller = []

            for symbol in takip_listesi():
                if not BOT_CALISIYOR_MU: break

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

                    emir_analizi = emir_defteri_ve_seviye_analizi(symbol, anlik_fiyat, ticker)

                    yon, sebep = sinyal_uret(df, anlik_fiyat, emir_analizi, piyasa_rejimi)
                    if yon is None:
                        continue

                    # 🆕 TERS İŞLEM YOK! Her iki modda da DÜZ işlem
                    islem_yonu = yon
                    if piyasa_rejimi == "YATAY":
                        mod_adi = f"YATAY-Düz - {sebep}"
                    else:
                        mod_adi = f"TREND-Düz - {sebep}"

                    print(f"🔄 [{mod_adi}] {symbol} → {islem_yonu}", flush=True)

                    taranan_sinyaller.append({
                        "symbol": symbol, "yon": islem_yonu, "fiyat": anlik_fiyat, "df": df, "mod": mod_adi
                    })
                except Exception as e:
                    print(f"⚠️ {symbol} hata: {e}", flush=True)
                    continue

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

                    kullanilacak_tutar = min(toplam_bakiye * 0.3, serbest_bakiye)
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

                    exchange.create_order(sinyal["symbol"], 'market', islem_yonu, miktar)
                    time.sleep(0.5)
                    try:
                        exchange.create_order(sinyal["symbol"], 'limit', kapat_yon, miktar, tp_fiyat, {'reduceOnly': True})
                        exchange.create_order(sinyal["symbol"], 'stop', kapat_yon, miktar, sl_fiyat, {'stopPrice': sl_fiyat, 'reduceOnly': True})
                    except Exception as e:
                        print(f"   ⚠️ TP/SL: {e}", flush=True)

                    with state_lock:
                        AKTIF_GRID_SISTEMLERI[sinyal["symbol"]] = {
                            "giris_fiyati": giris_fiyati, "yon": sinyal["yon"],
                            "tp_fiyat": tp_fiyat, "sl_fiyat": sl_fiyat,
                            "giris_zamani": time.time(),
                            "kismi_kar_alindi": False
                        }
                        aktif_semboller_listesi.append(sinyal["symbol"])
                    hafizayi_kaydet()

                    print(f"   ✅ AÇILDI! {sinyal['symbol']} | {sinyal['yon']} | TP: {tp_fiyat} | SL: {sl_fiyat}", flush=True)

                    telegram_mesaj_gonder(
                        f"🎯 *İŞLEM AÇILDI ({sinyal['mod']} - 5x)*\n"
                        f"📌 `{sinyal['symbol']}` | {sinyal['yon']}\n"
                        f"🎯 Giriş: `{giris_fiyati}`\n"
                        f"💰 TP: `{tp_fiyat}` (ROE: `%{hedef_roe:.1f}`)\n"
                        f"🛑 SL: `{sl_fiyat}`\n"
                        f"✂️ ROE %5'te %50 kısmi kâr"
                    )
                    break
                except Exception as e:
                    print(f"⚠️ İşlem hatası {sinyal['symbol']}: {e}", flush=True)

        except Exception as e:
            print(f"⚠️ Ana döngü: {e}", flush=True)

        time.sleep(5)

# ==================== MAIN ====================
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
    app_tg.add_handler(CommandHandler("havuz", havuz_komutu))

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
