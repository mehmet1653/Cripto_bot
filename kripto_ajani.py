import os
import time
import threading
import sys
import requests
import ccxt
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler
from supabase import create_client, Client

sys.stdout.reconfigure(line_buffering=True)
app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "6929517567")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
GATE_API_KEY = os.environ.get("GATE_API_KEY", "")
GATE_SECRET = os.environ.get("GATE_SECRET", "")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

exchange = ccxt.gate({
    'apiKey': GATE_API_KEY,
    'secret': GATE_SECRET,
    'enableRateLimit': True,
    'timeout': 15000,
    'options': {'defaultType': 'swap'}
})
exchange.set_sandbox_mode(True)

# Hacimli ve oturaklı coinler (BTC/ETH çıkarıldı)
TAKIP_EDILENLER = [
    'SOL/USDT:USDT', 'XRP/USDT:USDT', 'BNB/USDT:USDT', 'DOGE/USDT:USDT'
]

KOMISYON_ORANI = 0.001

# ==================== MOD ====================
MOD = {
    "aciklama": "🎯 Grid + Trend Koruması (Hacimli Coinler)",
    "zaman_dilimi": "15m",
    # Grid
    "grid_aralik_pct": 0.03,       # ±%3
    "grid_sayisi": 10,
    "grid_kar_pct": 0.006,
    # Risk
    "islem_riski_pct": 0.05,
    "kaldirac": 3,
    # Trend koruması
    "adx_dur_esik": 35,
    "ani_hareket_pct": 0.04,
    "grid_disi_dur_pct": 0.03,
    # Günlük
    "gunluk_max_kayip_pct": 0.05,
    "gunluk_max_kar_pct": 0.10,
    # Min hacim (hacim filtresi)
    "min_hacim_usdt": 100_000_000,  # $100M/gün
}

BOT_CALISIYOR_MU = True
GUN_BASI_KASA = None
GUN_BASI_TARIH = None
AKTIF_GRIDLER = {}
ANALITIK = {"toplam_kar": 0.0, "toplam_islem": 0, "trend_kapatma": 0, "grid_disi": 0}

# ==================== SUPABASE ====================
def hafizayi_yukle():
    varsayilan = {"aktif_gridler": {}, "analitik": ANALITIK.copy(), "coin_params": {}}
    try:
        r = supabase.table("bot_hafiza").select("*").eq("id", 2).execute()
        if r.data:
            v = r.data[0]
            return {
                "aktif_gridler": v.get("aktif_gridler", {}),
                "analitik": v.get("analitik", ANALITIK.copy()),
                "coin_params": v.get("coin_params", {})
            }
    except Exception:
        pass
    try:
        supabase.table("bot_hafiza").upsert({"id": 2, **varsayilan}).execute()
    except Exception:
        pass
    return varsayilan

def hafizayi_kaydet():
    try:
        supabase.table("bot_hafiza").upsert({
            "id": 2,
            "aktif_gridler": AKTIF_GRIDLER,
            "analitik": ANALITIK,
            "coin_params": COIN_PARAMS,
        }).execute()
    except Exception:
        pass

kalici = hafizayi_yukle()
AKTIF_GRIDLER = kalici.get("aktif_gridler", {})
ANALITIK = kalici.get("analitik", ANALITIK.copy())
COIN_PARAMS = kalici.get("coin_params", {})

def coin_mod_al(symbol):
    """Coin özel parametre varsa onu kullan, yoksa default."""
    base = dict(MOD)
    if symbol in COIN_PARAMS:
        base.update(COIN_PARAMS[symbol])
    return base

# ==================== YARDIMCI ====================
def telegram_mesaj_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": mesaj, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception:
        pass

def tum_emirleri_iptal_et(symbol):
    try:
        for e in exchange.fetch_open_orders(symbol):
            try: exchange.cancel_order(e['id'], symbol)
            except Exception: pass
    except Exception: pass
    try: exchange.cancel_all_orders(symbol)
    except Exception: pass

def set_leverage_and_margin_safely(symbol, leverage):
    try:
        exchange.set_leverage(leverage, symbol)
        try: exchange.set_margin_mode('isolated', symbol)
        except Exception: pass
        return True
    except Exception:
        return False

def gunluk_kontrol():
    global GUN_BASI_KASA, GUN_BASI_TARIH
    bugun = datetime.now(timezone.utc).date()
    if GUN_BASI_TARIH != bugun:
        try:
            bal = exchange.fetch_balance()
            GUN_BASI_KASA = float(bal['total'].get('USDT', 0))
            GUN_BASI_TARIH = bugun
        except Exception:
            return None
    if not GUN_BASI_KASA or GUN_BASI_KASA <= 0:
        return None
    try:
        bal = exchange.fetch_balance()
        su_an = float(bal['total'].get('USDT', 0))
        return (su_an - GUN_BASI_KASA) / GUN_BASI_KASA
    except Exception:
        return None

# ==================== GÖSTERGELER ====================
def adx_hesapla(df, period=14):
    try:
        up = df['high'].diff()
        down = -df['low'].diff()
        plus_dm = np.where((up > down) & (up > 0), up, 0.0)
        minus_dm = np.where((down > up) & (down > 0), down, 0.0)
        tr = pd.concat([
            df['high'] - df['low'],
            (df['high'] - df['close'].shift()).abs(),
            (df['low'] - df['close'].shift()).abs()
        ], axis=1).max(axis=1)
        atr14 = tr.rolling(period).mean()
        plus_di = 100 * pd.Series(plus_dm, index=df.index).rolling(period).mean() / atr14
        minus_di = 100 * pd.Series(minus_dm, index=df.index).rolling(period).mean() / atr14
        dx = (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan)) * 100
        val = dx.rolling(period).mean().iloc[-1]
        return float(val) if not pd.isna(val) else 0.0
    except Exception:
        return 0.0

def atr_hesapla(df, period=14):
    tr = pd.concat([
        df['high'] - df['low'],
        (df['high'] - df['close'].shift()).abs(),
        (df['low'] - df['close'].shift()).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()

# ==================== HACİM FİLTRESİ ====================
def hacim_uygun_mu(symbol):
    """24h hacim $100M üzeri mi?"""
    try:
        ticker = exchange.fetch_ticker(symbol)
        quote_volume = ticker.get('quoteVolume', 0) or 0
        return quote_volume >= MOD['min_hacim_usdt'], quote_volume
    except Exception:
        return True, 0

# ==================== TREND KORUMASI ====================
def trend_kontrol(symbol):
    """
    3 katmanlı trend koruması:
    1. ADX > esik
    2. Son 5 mumda ani hareket
    3. Grid dışı
    """
    mod = coin_mod_al(symbol)
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, mod['zaman_dilimi'], limit=50)
        df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
        adx = adx_hesapla(df, 14)

        if adx > mod['adx_dur_esik']:
            return True, f"ADX:{adx:.1f}"

        son_5 = df.tail(5)
        degisim = abs((son_5['close'].iloc[-1] - son_5['close'].iloc[0]) / son_5['close'].iloc[0])
        if degisim > mod['ani_hareket_pct']:
            return True, f"Ani %{degisim*100:.1f}"

        if symbol in AKTIF_GRIDLER:
            grid = AKTIF_GRIDLER[symbol]
            son_fiyat = df['close'].iloc[-1]
            alt = grid['alt']; ust = grid['ust']
            if son_fiyat < alt * (1 - mod['grid_disi_dur_pct']):
                return True, f"Grid altı"
            if son_fiyat > ust * (1 + mod['grid_disi_dur_pct']):
                return True, f"Grid üstü"

        return False, "OK"
    except Exception as e:
        return False, f"hata"

# ==================== GRID OLUŞTURMA ====================
def grid_olustur(symbol):
    mod = coin_mod_al(symbol)
    try:
        # Hacim filtresi
        uygun, hacim = hacim_uygun_mu(symbol)
        if not uygun:
            return None, f"hacim düşük (${hacim/1e6:.0f}M)"

        fiyat = exchange.fetch_ticker(symbol)['last']
        alt = fiyat * (1 - mod['grid_aralik_pct'])
        ust = fiyat * (1 + mod['grid_aralik_pct'])

        bal = exchange.fetch_balance()
        kasa = float(bal['total'].get('USDT', 0))
        if kasa < 10:
            return None, "yetersiz kasa"

        toplam_yatirim = kasa * mod['islem_riski_pct']
        adim = (ust - alt) / mod['grid_sayisi']
        miktar_per_grid = (toplam_yatirim / mod['grid_sayisi']) / fiyat

        if not set_leverage_and_margin_safely(symbol, mod['kaldirac']):
            return None, "kaldıraç hata"

        tum_emirleri_iptal_et(symbol)

        emirler = []
        for i in range(mod['grid_sayisi']):
            seviye = alt + (adim * i)
            if seviye < fiyat * 0.998:
                try:
                    miktar = float(exchange.amount_to_precision(symbol, miktar_per_grid))
                    if miktar <= 0: continue
                    emir = exchange.create_order(
                        symbol, 'limit', 'buy', miktar,
                        float(exchange.price_to_precision(symbol, seviye))
                    )
                    emirler.append({"tip": "AL", "fiyat": seviye, "miktar": miktar, "id": emir.get('id')})
                except Exception: pass

        AKTIF_GRIDLER[symbol] = {
            "ust": ust, "alt": alt, "adim": adim,
            "fiyat_baslangic": fiyat,
            "emir_sayisi": len(emirler),
            "toplam_kar": 0.0,
            "acilis_zaman": int(time.time()*1000)
        }
        hafizayi_kaydet()

        telegram_mesaj_gonder(
            f"🎯 *GRID KURULDU*\n"
            f"📌 `{symbol}`\n"
            f"💰 Fiyat: `{fiyat}` | Aralık: `{alt:.4f}-{ust:.4f}`\n"
            f"📐 {len(emirler)} AL emri"
        )
        return True, "OK"
    except Exception as e:
        return None, str(e)[:50]

def grid_kapat(symbol, sebep):
    try:
        tum_emirleri_iptal_et(symbol)
        try:
            raw = exchange.fetch_positions([symbol])
            for p in raw:
                k = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                if k > 0:
                    yon = str(p.get('side', '')).upper()
                    kapat = 'sell' if yon == 'LONG' else 'buy'
                    exchange.create_order(symbol, 'market', kapat, k, None, {'reduce_only': True})
        except Exception: pass

        if symbol in AKTIF_GRIDLER:
            del AKTIF_GRIDLER[symbol]
        hafizayi_kaydet()
        telegram_mesaj_gonder(f"🛑 *KAPATILDI* → `{symbol}`\nSebep: {sebep}")
    except Exception as e:
        telegram_mesaj_gonder(f"⚠️ Kapatma hatası: {e}")
# ==================== BACKTEST (GRID SIMÜLASYONU) ====================
def backtest_grid(symbol, params, gun_sayisi=180):
    """
    Grid backtest — basitleştirilmiş simülasyon.
    Her mumda grid seviyeleri kontrol edilir.
    """
    try:
        limit = min(gun_sayisi * 24 * 4, 1000)  # 15m için
        ohlcv = exchange.fetch_ohlcv(symbol, params['zaman_dilimi'], limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
        if len(df) < 200:
            return None

        # Başlangıç grid'i kur
        baslangic = 100  # mum 100'den başla
        fiyat_baslangic = df['close'].iloc[baslangic]
        alt = fiyat_baslangic * (1 - params['grid_aralik_pct'])
        ust = fiyat_baslangic * (1 + params['grid_aralik_pct'])
        adim = (ust - alt) / params['grid_sayisi']

        # Grid seviyeleri
        grid_seviyeler = [alt + adim * i for i in range(params['grid_sayisi'] + 1)]

        # Simülasyon
        acik_pozisyonlar = []  # [(seviye_fiyat, miktar)]
        kazanclar = []
        toplam_kar = 0
        trend_kapatma = 0
        grid_disi = 0

        for i in range(baslangic, len(df)):
            fiyat = df['close'].iloc[i]
            high = df['high'].iloc[i]
            low = df['low'].iloc[i]

            # Trend koruması
            df_slice = df.iloc[max(0, i-50):i+1]
            adx = adx_hesapla(df_slice, 14)

            # Ani hareket
            if i >= 5:
                son_5 = df.iloc[i-5:i+1]
                degisim = abs((son_5['close'].iloc[-1] - son_5['close'].iloc[0]) / son_5['close'].iloc[0])
            else:
                degisim = 0

            # Grid dışı
            disi = (fiyat < alt * (1 - params['grid_disi_dur_pct'])) or \
                   (fiyat > ust * (1 + params['grid_disi_dur_pct']))

            # Trend kapatma koşulları
            if adx > params['adx_dur_esik']:
                trend_kapatma += 1
                # Açık pozisyonları kapat
                for (sf, mk) in acik_pozisyonlar:
                    pct = (fiyat - sf) / sf - KOMISYON_ORANI
                    kazanclar.append(pct)
                acik_pozisyonlar = []
                # Yeni grid kur
                fiyat_baslangic = fiyat
                alt = fiyat_baslangic * (1 - params['grid_aralik_pct'])
                ust = fiyat_baslangic * (1 + params['grid_aralik_pct'])
                adim = (ust - alt) / params['grid_sayisi']
                grid_seviyeler = [alt + adim * k for k in range(params['grid_sayisi'] + 1)]
                continue

            if degisim > params['ani_hareket_pct']:
                trend_kapatma += 1
                for (sf, mk) in acik_pozisyonlar:
                    pct = (fiyat - sf) / sf - KOMISYON_ORANI
                    kazanclar.append(pct)
                acik_pozisyonlar = []
                fiyat_baslangic = fiyat
                alt = fiyat_baslangic * (1 - params['grid_aralik_pct'])
                ust = fiyat_baslangic * (1 + params['grid_aralik_pct'])
                adim = (ust - alt) / params['grid_sayisi']
                grid_seviyeler = [alt + adim * k for k in range(params['grid_sayisi'] + 1)]
                continue

            if disi:
                grid_disi += 1
                for (sf, mk) in acik_pozisyonlar:
                    pct = (fiyat - sf) / sf - KOMISYON_ORANI
                    kazanclar.append(pct)
                acik_pozisyonlar = []
                fiyat_baslangic = fiyat
                alt = fiyat_baslangic * (1 - params['grid_aralik_pct'])
                ust = fiyat_baslangic * (1 + params['grid_aralik_pct'])
                adim = (ust - alt) / params['grid_sayisi']
                grid_seviyeler = [alt + adim * k for k in range(params['grid_sayisi'] + 1)]
                continue

            # Grid seviyelerine değdi mi?
            for seviye in grid_seviyeler:
                # AL tetiklendi mi?
                if low <= seviye <= high or (low <= seviye and fiyat > seviye):
                    # Bu seviyeden al
                    acik_pozisyonlar.append((seviye, 1.0))
                    # Üstündeki satış seviyesine ulaşıldı mı?
                    satis_seviye = seviye * (1 + params['grid_kar_pct'])
                    if high >= satis_seviye:
                        # Sat
                        satis_kar = (satis_seviye - seviye) / seviye - KOMISYON_ORANI
                        kazanclar.append(satis_kar)
                        acik_pozisyonlar = [p for p in acik_pozisyonlar if p[0] != seviye]
                        toplam_kar += satis_kar

        if not kazanclar:
            return None

        k = np.array(kazanclar)
        kaz = k[k > 0]; kay = k[k < 0]
        win = len(kaz) / len(k) * 100 if len(k) else 0
        pf = abs(kaz.sum() / kay.sum()) if len(kay) and kay.sum() != 0 else 999
        equity = np.cumprod(1 + k)
        peak = np.maximum.accumulate(equity)
        dd = (equity - peak) / peak
        max_dd = dd.min() * 100 if len(dd) else 0

        return {
            "symbol": symbol,
            "islem": len(kazanclar),
            "win_rate": round(win, 2),
            "pf": round(pf, 3) if pf != 999 else 999,
            "max_dd": round(max_dd, 2),
            "toplam": round((equity[-1] - 1) * 100, 2) if len(equity) else 0,
            "trend_kapatma": trend_kapatma,
            "grid_disi": grid_disi
        }
    except Exception as e:
        return {"symbol": symbol, "islem": -1, "hata": str(e)[:40]}

# ==================== AKILLI OPTİMİZASYON ====================
def optimize_grid(symbol, gun_sayisi=90):
    """
    Akıllı optimize:
    1. Grid aralığı dene
    2. Grid sayısı dene
    3. Trend eşikleri dene
    """
    # Veri çek
    limit = min(gun_sayisi * 24 * 4, 1000)
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, MOD['zaman_dilimi'], limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
    except Exception:
        return None
    if len(df) < 200:
        return None

    # Test edilecek kombinasyonlar
    aralik_list = [0.02, 0.03, 0.05]
    sayi_list = [5, 10, 15]
    adx_list = [30, 35, 40]
    hareket_list = [0.03, 0.04, 0.05]

    en_iyi = None
    test_sayaci = 0
    for aralik in aralik_list:
        for sayi in sayi_list:
            for adx_e in adx_list:
                for hareket_e in hareket_list:
                    test_sayaci += 1
                    params = dict(MOD)
                    params['grid_aralik_pct'] = aralik
                    params['grid_sayisi'] = sayi
                    params['adx_dur_esik'] = adx_e
                    params['ani_hareket_pct'] = hareket_e

                    r = backtest_grid(symbol, params, gun_sayisi=gun_sayisi)
                    if r and r.get('islem', 0) >= 10:
                        if en_iyi is None or r['pf'] > en_iyi['pf']:
                            en_iyi = {
                                "params": {
                                    "grid_aralik_pct": aralik,
                                    "grid_sayisi": sayi,
                                    "adx_dur_esik": adx_e,
                                    "ani_hareket_pct": hareket_e,
                                    "grid_disi_dur_pct": MOD['grid_disi_dur_pct'],
                                    "grid_kar_pct": MOD['grid_kar_pct'],
                                    "kaldirac": MOD['kaldirac'],
                                    "islem_riski_pct": MOD['islem_riski_pct'],
                                    "zaman_dilimi": MOD['zaman_dilimi'],
                                    "min_hacim_usdt": MOD['min_hacim_usdt'],
                                    "gunluk_max_kayip_pct": MOD['gunluk_max_kayip_pct'],
                                    "gunluk_max_kar_pct": MOD['gunluk_max_kar_pct'],
                                },
                                "pf": r['pf'], "win_rate": r['win_rate'],
                                "toplam": r['toplam'], "max_dd": r['max_dd'],
                                "islem": r['islem'],
                                "trend_kapatma": r.get('trend_kapatma', 0),
                                "grid_disi": r.get('grid_disi', 0)
                            }
    return en_iyi

# ==================== FLASK ====================
@app.route('/')
def home():
    return f"Grid Bot | Grid: {len(AKTIF_GRIDLER)} | Kar: {ANALITIK.get('toplam_kar',0):.2f}"

# ==================== TELEGRAM ====================
async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        balance = exchange.fetch_balance()
        total = float(balance['total'].get('USDT', 0))
        free = float(balance['free'].get('USDT', 0))
        try:
            raw = exchange.fetch_positions()
            poslari = [p for p in raw if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
        except Exception:
            poslari = []
        pnl = sum(float(p.get('unrealizedPnl', 0)) for p in poslari)

        grid_detay = ""
        if AKTIF_GRIDLER:
            grid_detay = "\n📊 *Aktif Gridler:*\n"
            for sym, g in AKTIF_GRIDLER.items():
                grid_detay += f"• `{sym[:12]}` | `{g['alt']:.4f}-{g['ust']:.4f}` | Kâr: `{g.get('toplam_kar',0):+.2f}`\n"
        else:
            grid_detay = "\n📊 *Aktif Grid Yok*\n"

        opt_sayisi = len(COIN_PARAMS)
        mesaj = (
            f"🎯 *GRID + TREND KORUMASI*\n\n"
            f"💰 Toplam: `{total:.2f}` USDT (Serbest: `{free:.2f}`)\n"
            f"📈 Açık PnL: `{pnl:+.2f}` USDT\n"
            f"💵 Toplam Kâr: `{ANALITIK.get('toplam_kar',0):+.2f}` USDT\n"
            f"🎯 İşlem: `{ANALITIK.get('toplam_islem',0)}`\n"
            f"🛑 Trend Kapatma: `{ANALITIK.get('trend_kapatma',0)}`\n"
            f"📊 Grid Dışı: `{ANALITIK.get('grid_disi',0)}`\n"
            f"🧠 Optimize: `{opt_sayisi}/4`\n"
            f"{grid_detay}"
        )
        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def baslat_komutu(update, context):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = True
    await update.message.reply_text("▶️ *Bot Aktif!*", parse_mode='Markdown')

async def durdur_komutu(update, context):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ *Durduruldu.*", parse_mode='Markdown')

async def grid_kur_komutu(update, context):
    for symbol in TAKIP_EDILENLER:
        ok, mesaj = grid_olustur(symbol)
        if not ok:
            telegram_mesaj_gonder(f"⚠️ `{symbol[:12]}`: {mesaj}")

async def kapat_komutu(update, context):
    await update.message.reply_text("🛑 *Tüm gridler kapatılıyor...*", parse_mode='Markdown')
    for sym in list(AKTIF_GRIDLER.keys()):
        grid_kapat(sym, "Manuel")
    await update.message.reply_text("✅ *Hepsi kapatıldı.*", parse_mode='Markdown')

async def optimize_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global COIN_PARAMS, OPTIMIZE_CALISIYOR
    if OPTIMIZE_CALISIYOR:
        await update.message.reply_text("⏳ Zaten çalışıyor.")
        return
    OPTIMIZE_CALISIYOR = True
    await update.message.reply_text(
        f"🧠 *Akıllı Optimize Başladı*\n"
        f"{len(TAKIP_EDILENLER)} coin × 81 kombinasyon\n"
        f"Tahmini: 15-25 dk.",
        parse_mode='Markdown'
    )

    def run():
        global COIN_PARAMS, OPTIMIZE_CALISIYOR
        try:
            telegram_mesaj_gonder("🔬 *Optimize başladı...*")
            for c in TAKIP_EDILENLER:
                en_iyi = optimize_grid(c, gun_sayisi=90)
                if en_iyi is None:
                    telegram_mesaj_gonder(f"⚠️ `{c[:12]}` — yeterli sinyal yok")
                    continue
                COIN_PARAMS[c] = en_iyi['params']
                hafizayi_kaydet()
                p = en_iyi['params']
                telegram_mesaj_gonder(
                    f"🏆 *{c[:12]}*\n"
                    f"📐 Aralık: `±%{p['grid_aralik_pct']*100}` | Sayı: `{p['grid_sayisi']}`\n"
                    f"🎚️ ADX: `{p['adx_dur_esik']}` | Hareket: `%{p['ani_hareket_pct']*100}`\n"
                    f"📊 İŞL:`{en_iyi['islem']}` WIN:`{en_iyi['win_rate']}%` "
                    f"PF:`{en_iyi['pf']}` TOT:`{en_iyi['toplam']}%`"
                )
                time.sleep(1)
            telegram_mesaj_gonder(f"✅ *Optimize tamamlandı!* `/backtest` yaz.")
        except Exception as e:
            telegram_mesaj_gonder(f"❌ {e}")
        finally:
            OPTIMIZE_CALISIYOR = False

    threading.Thread(target=run, daemon=True).start()

async def backtest_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BACKTEST_CALISIYOR
    if BACKTEST_CALISIYOR:
        await update.message.reply_text("⏳ Zaten çalışıyor.")
        return
    BACKTEST_CALISIYOR = True
    await update.message.reply_text("⏳ *Backtest başladı* — 5-10 dk.", parse_mode='Markdown')

    def run():
        global BACKTEST_CALISIYOR
        try:
            satirlar = ["*📊 GRID BACKTEST*\n```"]
            satirlar.append(f"{'COIN':<14} {'İŞL':>4} {'WIN%':>6} {'PF':>6} {'DD%':>6} {'TOT%':>7}")
            satirlar.append("-" * 52)
            toplam = 0
            for c in TAKIP_EDILENLER:
                params = coin_mod_al(c)
                r = backtest_grid(c, params, gun_sayisi=90)
                if r is None:
                    satirlar.append(f"{c[:12]:<14} HATA")
                elif r.get('islem', -1) == -1:
                    satirlar.append(f"{c[:12]:<14} HATA: {r.get('hata','?')[:20]}")
                else:
                    satirlar.append(
                        f"{c[:12]:<14} {r['islem']:>4} {r['win_rate']:>6} {r['pf']:>6} {r['max_dd']:>6} {r['toplam']:>7}"
                    )
                    toplam += r['toplam']
            satirlar.append("-" * 52)
            ort = toplam / len(TAKIP_EDILENLER)
            satirlar.append(f"Ortalama: {ort:.2f}%")
            satirlar.append(f"Optimize: {len(COIN_PARAMS)}/4")
            satirlar.append("```")
            telegram_mesaj_gonder("\n".join(satirlar))
        except Exception as e:
            telegram_mesaj_gonder(f"❌ {e}")
        finally:
            BACKTEST_CALISIYOR = False

    threading.Thread(target=run, daemon=True).start()

# ==================== ANA DÖNGÜ ====================
OPTIMIZE_CALISIYOR = False
BACKTEST_CALISIYOR = False

def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU
    print(f"🎯 [GRID BOT] Başladı", flush=True)
    try:
        exchange.load_markets()
    except Exception:
        pass

    dongu_sayaci = 0
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5); continue

            gunluk_degisim = gunluk_kontrol()
            if gunluk_degisim is not None:
                if gunluk_degisim <= -MOD['gunluk_max_kayip_pct']:
                    telegram_mesaj_gonder(f"🛑 *Günlük zarar limiti!* (%{gunluk_degisim*100:.1f})")
                    BOT_CALISIYOR_MU = False
                    for sym in list(AKTIF_GRIDLER.keys()):
                        grid_kapat(sym, "Günlük limit")
                    continue
                if gunluk_degisim >= MOD['gunluk_max_kar_pct']:
                    telegram_mesaj_gonder(f"✅ *Günlük kâr hedefi!* (%+{gunluk_degisim*100:.1f})")
                    for sym in list(AKTIF_GRIDLER.keys()):
                        grid_kapat(sym, "Kâr hedefi")

            # Trend koruması
            for sym in list(AKTIF_GRIDLER.keys()):
                dur, sebep = trend_kontrol(sym)
                if dur:
                    grid_kapat(sym, sebep)
                    if "ADX" in sebep or "Ani" in sebep:
                        ANALITIK["trend_kapatma"] = ANALITIK.get("trend_kapatma", 0) + 1
                    else:
                        ANALITIK["grid_disi"] = ANALITIK.get("grid_disi", 0) + 1
                    hafizayi_kaydet()

            # Grid yoksa kur
            for symbol in TAKIP_EDILENLER:
                if symbol not in AKTIF_GRIDLER:
                    ok, mesaj = grid_olustur(symbol)
                    if ok:
                        print(f"✅ Grid kuruldu: {symbol}", flush=True)

            dongu_sayaci += 1
            if dongu_sayaci % 20 == 0:
                print(f"🔍 #{dongu_sayaci} | Grid: {len(AKTIF_GRIDLER)} | Kâr: {ANALITIK.get('toplam_kar',0):.2f}", flush=True)

        except Exception as e:
            print(f"⚠️ {e}", flush=True)
        time.sleep(15)

def flask_web_server():
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

# ==================== BAŞLAT ====================
if __name__ == '__main__':
    threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True).start()
    threading.Thread(target=flask_web_server, daemon=True).start()

    app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app_tg.add_handler(CommandHandler("durum", durum_komutu))
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    app_tg.add_handler(CommandHandler("grid", grid_kur_komutu))
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))
    app_tg.add_handler(CommandHandler("optimize", optimize_komutu))
    app_tg.add_handler(CommandHandler("backtest", backtest_komutu))

    app_tg.run_polling(drop_pending_updates=True)
