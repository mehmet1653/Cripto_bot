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
exchange.set_sandbox_mode(True)  # ⚠️ Gerçek hesaba geçerken False yap!

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

KALDIRAC = 5
MAKSIMUM_TOPLAM_POZISYON = 3
COOLDOWN_SURESI_SANIYE = 30 * 60

MIN_RR = 1.8
TP_GERI_CEKME = 0.003

# Trailing
TRAILING_SEVIYELER = [
    (15.0, 0.10),
    (10.0, 0.05),
    (6.0, 0.02),
    (3.0, 0.01),
]

# Kısmi kâr
KISMI_KAR_ROE = 5.0
KISMI_KAR_ORANI = 0.5

# Zombi
MAKS_ACIK_KALMA_SURESI = 4 * 60 * 60
MIN_KAR_ESIGI = 0.001

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

# ==================== MUM TABANLI TREND TESPİTİ ====================
def trend_tespit_et(df):
    """
    Mumlardan trend tespiti:
    - Bant genişliği (son 10 mum) → YATAY mı TREND mi?
    - Yeşil/kırmızı mum sayısı → YUKARI mı ASAGI mı?
    """
    try:
        son_10 = df.tail(10).reset_index(drop=True)
        son_10_yuksek = son_10['high'].max()
        son_10_dusuk = son_10['low'].min()
        bant_genisligi = ((son_10_yuksek - son_10_dusuk) / son_10_dusuk) * 100

        yesil_sayisi = 0
        kirmizi_sayisi = 0
        for i in range(10):
            if son_10['close'].iloc[i] > son_10['open'].iloc[i]:
                yesil_sayisi += 1
            else:
                kirmizi_sayisi += 1

        # REJİM
        if bant_genisligi < 1.5:
            rejim = "YATAY"
            yon = "BELIRSIZ"
        else:
            rejim = "TREND"
            if yesil_sayisi >= 6:
                yon = "YUKARI"
            elif kirmizi_sayisi >= 6:
                yon = "ASAGI"
            else:
                yon = "BELIRSIZ"

        print(f"   📊 [MUM] Bant: %{bant_genisligi:.2f} | Yeşil: {yesil_sayisi}/10 | Kırmızı: {kirmizi_sayisi}/10 | Rejim: {rejim} | Yön: {yon}", flush=True)
        return rejim, yon
    except Exception as e:
        print(f"⚠️ Trend tespit hatası: {e}", flush=True)
        return "YATAY", "BELIRSIZ"

# ==================== SWEEP ====================
def swing_noktalari_bul(df, lookback=50):
    highs, lows = [], []
    baslangic = max(2, len(df) - lookback)
    for i in range(baslangic, len(df) - 2):
        h = df['high'].iloc[i]
        l = df['low'].iloc[i]
        if (h > df['high'].iloc[i-1] and h > df['high'].iloc[i-2] and
            h > df['high'].iloc[i+1] and h > df['high'].iloc[i+2]):
            highs.append((i, h))
        if (l < df['low'].iloc[i-1] and l < df['low'].iloc[i-2] and
            l < df['low'].iloc[i+1] and l < df['low'].iloc[i+2]):
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

def sweep_tespit_et(df_15m, df_5m, destekler, direncler, anlik_fiyat, yon):
    """Teyitli sweep: 5m mum onayı + hacim."""
    if len(df_15m) < 3: return None, None, None, None

    son_mum = df_15m.iloc[-1]
    onceki = df_15m.iloc[-2]
    iki_onceki = df_15m.iloc[-3]
    atr = ta.volatility.AverageTrueRange(df_15m['high'], df_15m['low'], df_15m['close'], window=14).average_true_range().iloc[-1]

    # 5m teyit
    son_mum_yesil_5m = df_5m['close'].iloc[-1] > df_5m['open'].iloc[-1]
    son_mum_kirmizi_5m = df_5m['close'].iloc[-1] < df_5m['open'].iloc[-1]
    hacim_ort_5m = df_5m['volume'].rolling(20).mean().iloc[-1]
    guncel_hacim_5m = df_5m['volume'].iloc[-1]
    hacim_teyit = guncel_hacim_5m > (hacim_ort_5m * 1.3)

    stoch_5m_seri = ta.momentum.StochRSIIndicator(df_5m['close'], window=14).stochrsi()
    stoch_5m_simdi = stoch_5m_seri.iloc[-1]
    stoch_5m_onceki = stoch_5m_seri.iloc[-2]
    stoch_donus_yukari_5m = stoch_5m_onceki < 0.3 and stoch_5m_simdi > 0.3
    stoch_donus_asagi_5m = stoch_5m_onceki > 0.7 and stoch_5m_simdi < 0.7

    long_izinli = (yon in ["YUKARI", "BELIRSIZ"])
    short_izinli = (yon in ["ASAGI", "BELIRSIZ"])

    # LONG sweep
    if long_izinli:
        for destek in destekler:
            kirildi, en_dusuk = False, 999999999
            if iki_onceki['low'] < destek: kirildi, en_dusuk = True, min(iki_onceki['low'], en_dusuk)
            if onceki['low'] < destek: kirildi, en_dusuk = True, min(onceki['low'], en_dusuk)
            if kirildi and son_mum['close'] > destek and anlik_fiyat > destek:
                if (destek - en_dusuk) / destek < 0.02:
                    if son_mum_yesil_5m and hacim_teyit and stoch_donus_yukari_5m:
                        sl_fiyat = en_dusuk - (atr * 1.2)
                        if (anlik_fiyat - sl_fiyat) / anlik_fiyat > 0.02:
                            sl_fiyat = anlik_fiyat * 0.98
                        return "LONG", destek, sl_fiyat, en_dusuk

    # SHORT sweep
    if short_izinli:
        for direnc in direncler:
            kirildi, en_yuksek = False, 0
            if iki_onceki['high'] > direnc: kirildi, en_yuksek = True, max(iki_onceki['high'], en_yuksek)
            if onceki['high'] > direnc: kirildi, en_yuksek = True, max(onceki['high'], en_yuksek)
            if kirildi and son_mum['close'] < direnc and anlik_fiyat < direnc:
                if (en_yuksek - direnc) / direnc < 0.02:
                    if son_mum_kirmizi_5m and hacim_teyit and stoch_donus_asagi_5m:
                        sl_fiyat = en_yuksek + (atr * 1.2)
                        if (sl_fiyat - anlik_fiyat) / anlik_fiyat > 0.02:
                            sl_fiyat = anlik_fiyat * 1.02
                        return "SHORT", direnc, sl_fiyat, en_yuksek

    return None, None, None, None

# ==================== BREAKOUT / BREAKDOWN ====================
def breakout_sinyal(df_15m, df_5m, anlik_fiyat, yon):
    """Teyitli breakout/breakdown: 2 mum onayı + hacim."""
    try:
        bb = ta.volatility.BollingerBands(df_15m['close'], window=20, window_dev=2)
        bb_high = bb.bollinger_hband().iloc[-1]
        bb_low = bb.bollinger_lband().iloc[-1]

        hacim_ort = df_15m['volume'].rolling(20).mean().iloc[-1]
        guncel_hacim = df_15m['volume'].iloc[-1]
        hacim_teyit = guncel_hacim > (hacim_ort * 1.5)

        son_2 = df_5m.tail(2)
        son_2_yesil = (son_2['close'].iloc[0] > son_2['open'].iloc[0] and son_2['close'].iloc[1] > son_2['open'].iloc[1])
        son_2_kirmizi = (son_2['close'].iloc[0] < son_2['open'].iloc[0] and son_2['close'].iloc[1] < son_2['open'].iloc[1])

        long_izinli = (yon in ["YUKARI", "BELIRSIZ"])
        short_izinli = (yon in ["ASAGI", "BELIRSIZ"])

        # LONG breakout
        if long_izinli:
            if anlik_fiyat > bb_high * 1.001 and son_2_yesil and hacim_teyit:
                return "LONG"

        # SHORT breakdown
        if short_izinli:
            if anlik_fiyat < bb_low * 0.999 and son_2_kirmizi and hacim_teyit:
                return "SHORT"

        return None
    except:
        return None

# ==================== BANT DÖNÜŞÜ (YATAY MOD) ====================
def bant_donusu_sinyal(df_15m, df_5m, anlik_fiyat, yon):
    """YATAY modda bant dönüşü: 5m mum + Stoch dönüş + hacim."""
    try:
        bb = ta.volatility.BollingerBands(df_15m['close'], window=20, window_dev=2)
        bb_high = bb.bollinger_hband().iloc[-1]
        bb_low = bb.bollinger_lband().iloc[-1]

        hacim_ort = df_15m['volume'].rolling(20).mean().iloc[-1]
        guncel_hacim = df_15m['volume'].iloc[-1]
        hacim_teyit = guncel_hacim > (hacim_ort * 1.3)

        son_mum_yesil_5m = df_5m['close'].iloc[-1] > df_5m['open'].iloc[-1]
        son_mum_kirmizi_5m = df_5m['close'].iloc[-1] < df_5m['open'].iloc[-1]

        stoch_5m_seri = ta.momentum.StochRSIIndicator(df_5m['close'], window=14).stochrsi()
        stoch_simdi = stoch_5m_seri.iloc[-1]
        stoch_onceki = stoch_5m_seri.iloc[-2]
        stoch_donus_yukari = stoch_onceki < 0.3 and stoch_simdi > 0.3
        stoch_donus_asagi = stoch_onceki > 0.7 and stoch_simdi < 0.7

        long_izinli = (yon in ["YUKARI", "BELIRSIZ"])
        short_izinli = (yon in ["ASAGI", "BELIRSIZ"])

        # LONG: Alt banda değdi
        if long_izinli:
            if anlik_fiyat <= bb_low * 1.005 and son_mum_yesil_5m and stoch_donus_yukari and hacim_teyit:
                return "LONG"

        # SHORT: Üst banda değdi
        if short_izinli:
            if anlik_fiyat >= bb_high * 0.995 and son_mum_kirmizi_5m and stoch_donus_asagi and hacim_teyit:
                return "SHORT"

        return None
    except:
        return None

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
            f"📊 *DURUM* [MUM TABANLI SİSTEM]\n\n"
            f"💰 Kasa: `{total:.2f} USDT` | PnL: `{pnl:+.2f}`\n"
            f"📌 Açık: `{len(pos)} / {MAKSIMUM_TOPLAM_POZISYON}`"
            f"{pos_detay}\n\n"
            f"✅ TP: `{bas}` | ❌ SL: `{basz}`\n"
            f"📈 Başarı: `%{oran:.1f}`\n\n"
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
    await update.message.reply_text("🟢 Bot aktif! (Mum Tabanlı Sistem)")

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

# ==================== TRAILING ====================
def trailing_stop_kontrol():
    with state_lock:
        aktif_kopya = list(AKTIF_POZISYONLAR.items())

    for sym, bilgi in aktif_kopya:
        if sym not in AKTIF_POZISYONLAR: continue
        if not isinstance(bilgi, dict): continue

        yon = bilgi.get("yon", "LONG")
        g = float(bilgi.get("giris_fiyati", 0))
        sl_kayitli = float(bilgi.get("sl_fiyat", 0))
        kaldirac_v = int(bilgi.get("kaldirac", KALDIRAC))
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

# ==================== KISMİ KÂR ====================
def kismi_kar_al_kontrol():
    with state_lock:
        aktif_kopya = list(AKTIF_POZISYONLAR.items())

    for sym, bilgi in aktif_kopya:
        if sym not in AKTIF_POZISYONLAR: continue
        if not isinstance(bilgi, dict): continue
        if bilgi.get("kismi_kar_alindi", False): continue

        yon = bilgi.get("yon", "LONG")
        g = float(bilgi.get("giris_fiyati", 0))

        try:
            with borsa_kilidi:
                t = exchange.fetch_ticker(sym)
                anlik = float(t['last'])
        except: continue

        roe = ((anlik - g) / g * 100 * KALDIRAC) if yon == "LONG" else ((g - anlik) / g * 100 * KALDIRAC)

        if roe >= KISMI_KAR_ROE:
            try:
                with borsa_kilidi:
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
                    if sym in AKTIF_POZISYONLAR:
                        AKTIF_POZISYONLAR[sym]["kismi_kar_alindi"] = True

                print(f"💰 [KISMİ KÂR] {sym} | ROE:%{roe:.1f} → %50 kapatıldı", flush=True)
                tg_gonder(f"💰 *KISMİ KÂR ALINDI*\n📌 `{sym}` | {yon}\n📊 ROE: `%{roe:+.2f}`\n✂️ %50 kapatıldı.")
            except Exception as e:
                print(f"⚠️ Kısmi kâr hatası {sym}: {e}", flush=True)

# ==================== ZOMBİ ====================
def zombi_islem_kapat():
    with state_lock:
        aktif_kopya = list(AKTIF_POZISYONLAR.items())

    for sym, bilgi in aktif_kopya:
        if sym not in AKTIF_POZISYONLAR: continue
        if not isinstance(bilgi, dict): continue

        yon = bilgi.get("yon", "LONG")
        g = float(bilgi.get("giris_fiyati", 0))
        giris_zaman = float(bilgi.get("giris_zamani", 0))

        gecen_sure = time.time() - giris_zaman
        if gecen_sure < MAKS_ACIK_KALMA_SURESI:
            continue

        try:
            with borsa_kilidi:
                t = exchange.fetch_ticker(sym)
                anlik = float(t['last'])
        except:
            continue

        if yon == "LONG":
            brut_kar = (anlik - g) / g
        else:
            brut_kar = (g - anlik) / g

        net_kar = brut_kar - 0.001 - 0.0005

        if net_kar >= MIN_KAR_ESIGI:
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
                    exchange.create_order(sym, 'market', ky, miktar, None, {'reduceOnly': True})

                with state_lock:
                    if sym in AKTIF_POZISYONLAR: del AKTIF_POZISYONLAR[sym]
                    COIN_COOLDOWN[sym] = {"zaman": float(time.time() + COOLDOWN_SURESI_SANIYE), "son_yon": yon}

                hafizayi_kaydet()
                saat = int(gecen_sure // 3600)
                dakika = int((gecen_sure % 3600) // 60)
                print(f"⏰ [ZOMBİ KAPATILDI] {sym} | {saat}s {dakika}dk | Net: %{net_kar*100:.3f}", flush=True)
                tg_gonder(f"⏰ *ZOMBİ KAPATILDI*\n📌 `{sym}` | {yon}\n⏱️ {saat}s {dakika}dk\n💰 Net: `%{net_kar*100:.3f}`")
            except Exception as e:
                print(f"⚠️ Zombi hatası {sym}: {e}", flush=True)

# ==================== TP HESAPLA ====================
def tp_hesapla_sweep(yon, giris, sl, destekler, direncler, atr):
    if yon == "LONG":
        for d in direncler:
            if d > giris:
                tp = d * (1 - TP_GERI_CEKME)
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

def akilli_seviye(anlik, yon, df):
    atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range().iloc[-1]
    if yon == "LONG":
        tp = anlik + (atr * 2.0)
        sl = anlik - (atr * 1.2)
        ky = 'sell'
    else:
        tp = anlik - (atr * 2.0)
        sl = anlik + (atr * 1.2)
        ky = 'buy'
    roe = abs((tp - anlik) / anlik) * 100 * KALDIRAC
    return tp, sl, ky, roe

# ==================== ANA TARAYICI ====================
def tarayici():
    global SON_HAVUZ_GUNCELLEME
    print("🚀 [BAŞLANGIÇ] MUM TABANLI SİSTEM + HİBRİT HAVUZ...", flush=True)
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
            print(f"📋 Havuz: {len(takip_listesi())} coin", flush=True)
            print(f"{'='*60}", flush=True)

            try:
                with borsa_kilidi:
                    raw = exchange.fetch_positions()
                aktif_map = {p['symbol']: p for p in raw if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0}
                aktif_list = list(aktif_map.keys())
            except:
                raw = []
                aktif_map = {}
                aktif_list = []

            # KAPANIŞ
            try:
                anlik_aktif = list(aktif_map.keys())
                for eski in list(AKTIF_POZISYONLAR.keys()):
                    if eski not in anlik_aktif:
                        bilgi = AKTIF_POZISYONLAR[eski]
                        g = bilgi.get("giris_fiyati", 0)
                        y = bilgi.get("yon", "LONG")
                        tp_k = bilgi.get("tp_fiyat", g)
                        sl_k = bilgi.get("sl_fiyat", g)
                        karli = False
                        cikis = g
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
                            if eski in AKTIF_POZISYONLAR:
                                del AKTIF_POZISYONLAR[eski]

                        hafizayi_kaydet()
                        print(f"💰 [KAPANIŞ] {eski} | Çıkış: {cikis}", flush=True)
                        tg_gonder(f"{tip}\n📌 `{eski}` | Çıkış: `{cikis}`")
            except Exception as e:
                print(f"⚠️ Kapanış: {e}", flush=True)

            trailing_stop_kontrol()
            kismi_kar_al_kontrol()
            zombi_islem_kapat()

            # SİNYAL TARAMA
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
                        ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=60)
                        ohlcv_5m = exchange.fetch_ohlcv(symbol, timeframe='5m', limit=60)
                        ticker = exchange.fetch_ticker(symbol)

                    df_15m = pd.DataFrame(ohlcv_15m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    df_5m = pd.DataFrame(ohlcv_5m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    anlik = float(ticker['last'])

                    print(f"\n🔍 [{symbol}] @ {anlik}", flush=True)

                    # TREND TESPİT
                    rejim, yon = trend_tespit_et(df_15m)

                    # SWEEP
                    highs, lows = swing_noktalari_bul(df_15m)
                    destekler, direncler = [], []
                    if len(highs) >= 2 and len(lows) >= 2:
                        destekler, direncler = likidite_seviyeleri(df_15m, highs, lows, anlik)
                        if destekler:
                            print(f"   🎯 Destek: {[f'{d:.4f}' for d in destekler]}", flush=True)
                        if direncler:
                            print(f"   🎯 Direnç: {[f'{d:.4f}' for d in direncler]}", flush=True)

                    yon_s, likidite, sl_s, sweep_uc = sweep_tespit_et(df_15m, df_5m, destekler, direncler, anlik, yon)

                    mod = None
                    tp_fiyat = None
                    sl_fiyat = None
                    kapat_yon = None
                    hedef_roe = 0
                    rr = 0
                    sebep = ""

                    if yon_s:
                        # SWEEP
                        atr = ta.volatility.AverageTrueRange(df_15m['high'], df_15m['low'], df_15m['close'], window=14).average_true_range().iloc[-1]
                        tp_fiyat, rr = tp_hesapla_sweep(yon_s, anlik, sl_s, destekler, direncler, atr)
                        if rr >= MIN_RR:
                            mod = "SWEEP"
                            sl_fiyat = sl_s
                            kapat_yon = 'sell' if yon_s == 'LONG' else 'buy'
                            hedef_roe = abs((tp_fiyat - anlik) / anlik) * 100 * KALDIRAC
                            sebep = f"Sweep {yon_s} | Likidite: {likidite:.4f}"
                            print(f"   🎯 [{mod}] SİNYAL! {yon_s} | R/R: {rr:.2f}", flush=True)
                        else:
                            print(f"   ⏭️ Sweep R/R düşük: {rr:.2f}", flush=True)
                    else:
                        print(f"   ⏭️ Sweep yok", flush=True)

                    # BREAKOUT
                    if mod is None and rejim == "TREND":
                        yon_b = breakout_sinyal(df_15m, df_5m, anlik, yon)
                        if yon_b:
                            tp_fiyat, sl_fiyat, kapat_yon, hedef_roe = akilli_seviye(anlik, yon_b, df_15m)
                            rr = abs(tp_fiyat - anlik) / abs(anlik - sl_fiyat) if abs(anlik - sl_fiyat) > 0 else 0
                            if rr >= MIN_RR:
                                mod = "BREAKOUT"
                                yon_s = yon_b
                                sebep = f"Breakout {yon_b}"
                                print(f"   🎯 [BREAKOUT] SİNYAL! {yon_b} | R/R: {rr:.2f}", flush=True)
                        else:
                            print(f"   ⏭️ Breakout yok", flush=True)

                    # BANT DÖNÜŞÜ
                    if mod is None and rejim == "YATAY":
                        yon_bd = bant_donusu_sinyal(df_15m, df_5m, anlik, yon)
                        if yon_bd:
                            tp_fiyat, sl_fiyat, kapat_yon, hedef_roe = akilli_seviye(anlik, yon_bd, df_15m)
                            rr = abs(tp_fiyat - anlik) / abs(anlik - sl_fiyat) if abs(anlik - sl_fiyat) > 0 else 0
                            if rr >= MIN_RR:
                                mod = "BANT DÖNÜŞÜ"
                                yon_s = yon_bd
                                sebep = f"Bant Dönüşü {yon_bd}"
                                print(f"   🎯 [BANT DÖNÜŞÜ] SİNYAL! {yon_bd} | R/R: {rr:.2f}", flush=True)
                        else:
                            print(f"   ⏭️ Bant dönüşü yok", flush=True)

                    if mod is None:
                        continue

                    # POZİSYON AÇ
                    with borsa_kilidi:
                        bakiye = exchange.fetch_balance()
                        toplam_b = float(bakiye['total'].get('USDT', 0))
                        serbest_b = float(bakiye.get('free', {}).get('USDT', 0) or 0)
                        exchange.set_leverage(KALDIRAC, symbol)
                        market = exchange.market(symbol)

                    kullan = min(toplam_b * 0.3, serbest_b)
                    if kullan < 1: continue

                    miktar = float(exchange.amount_to_precision(
                        symbol,
                        max((kullan * KALDIRAC) / anlik / float(market.get('contractSize', 1.0)),
                            float(market['limits']['amount']['min'] or 1.0))
                    ))

                    iy = 'buy' if yon_s == 'LONG' else 'sell'
                    kapat_y = 'sell' if yon_s == 'LONG' else 'buy'

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
                            "giris_fiyati": anlik, "yon": yon_s,
                            "tp_fiyat": tp_fiyat, "sl_fiyat": sl_fiyat,
                            "giris_zamani": time.time(),
                            "kaldirac": KALDIRAC,
                            "mod": mod,
                            "kismi_kar_alindi": False
                        }
                        aktif_list.append(symbol)
                        aktif_map[symbol] = {"dummy": True}

                    hafizayi_kaydet()
                    print(f"✅ [AÇILDI] {symbol} {yon_s} ({mod}) @ {anlik} | SL: {sl_fiyat} | TP: {tp_fiyat} | R/R: {rr:.2f}", flush=True)

                    tg_gonder(
                        f"🎯 *{mod}!*\n"
                        f"📌 `{symbol}` | {yon_s}\n"
                        f"📝 Sebep: `{sebep}`\n"
                        f"🎯 Giriş: `{anlik}`\n"
                        f"💰 TP: `{tp_fiyat}` (ROE: `%{hedef_roe:.1f}`)\n"
                        f"🛑 SL: `{sl_fiyat}`\n"
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

        time.sleep(10)

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
