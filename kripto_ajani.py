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
    'timeout': 20000,
    'options': {'defaultType': 'swap'}
})
exchange.set_sandbox_mode(True)

KOMISYON_ORANI = 0.001

# ==================== DÜŞÜK FİYATLI 4 COİN ====================
TAKIP_EDILENLER = [
    'XRP/USDT:USDT',    # ~1.36 USDT
    'DOGE/USDT:USDT',   # ~0.086 USDT
    'ADA/USDT:USDT',    # ~0.5 USDT
    'TRX/USDT:USDT'     # ~0.15 USDT
]

# ==================== GRID PARAMETRELERİ ====================
GRID = {
    "aciklama": "📊 Grid Bot v6 (Düşük Fiyatlı + Sinyal Bazlı)",
    "grid_aralik_pct": 0.01,
    "grid_sayisi": 8,
    "grid_kar_pct": 0.006,
    "kaldirac": 3,
    "toplam_yatirim_pct": 0.60,      # %50 → %60 (bütçe artırıldı)
    "max_coin": 4,
    # Koruma
    "adx_dur_esik": 25,
    "fiyat_disi_pct": 0.03,
    "gunluk_max_kayip_pct": 0.03,
    "min_hacim_usdt": 10_000_000,
    # Cooldown
    "grid_cooldown_dk": 15,
    # YATAY PİYASA FİLTRELERİ
    "yatay_adx_max": 22,             # 20 → 22 (biraz esnek)
    "yatay_bollinger_max": 2.5,      # 2.0 → 2.5 (biraz esnek)
    "yatay_20mum_max": 3.5,          # 3.0 → 3.5
    "yatay_hacim_min": 0.4,          # 0.5 → 0.4
}

# ==================== DURUMLAR ====================
BOT_CALISIYOR_MU = True
AKTIF_GRIDLER = {}
GRID_COOLDOWNLAR = {}
GUN_BASI_KASA = None
GUN_BASI_TARIH = None
ANALITIK = {
    "toplam_kar": 0.0,
    "toplam_grid_tetiklenme": 0,
    "grid_kurulum": 0,
    "trend_kapatma": 0,
    "yatay_red": 0,
    "emir_hatasi": 0,
}

# ==================== SUPABASE ====================
def hafizayi_yukle():
    varsayilan = {
        "aktif_gridler": {},
        "grid_cooldownlar": {},
        "analitik": ANALITIK.copy(),
    }
    try:
        r = supabase.table("bot_hafiza").select("*").eq("id", 100).execute()
        if r.data:
            v = r.data[0]
            return {
                "aktif_gridler": v.get("aktif_gridler", {}),
                "grid_cooldownlar": v.get("grid_cooldownlar", {}),
                "analitik": v.get("analitik", ANALITIK.copy()),
            }
    except Exception:
        pass
    try:
        supabase.table("bot_hafiza").upsert({"id": 100, **varsayilan}).execute()
    except Exception:
        pass
    return varsayilan

def hafizayi_kaydet():
    try:
        supabase.table("bot_hafiza").upsert({
            "id": 100,
            "aktif_gridler": AKTIF_GRIDLER,
            "grid_cooldownlar": GRID_COOLDOWNLAR,
            "analitik": ANALITIK,
        }).execute()
    except Exception:
        pass

kalici = hafizayi_yukle()
AKTIF_GRIDLER = kalici["aktif_gridler"]
GRID_COOLDOWNLAR = kalici["grid_cooldownlar"]
ANALITIK = kalici["analitik"]

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

def fetch_ohlcv_guvenli(symbol, timeframe, limit=200):
    for attempt in range(3):
        try:
            return exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        except Exception:
            if attempt == 2:
                return None
            time.sleep(1)
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

def hacim_uygun_mu(symbol):
    try:
        ticker = exchange.fetch_ticker(symbol)
        quote_volume = ticker.get('quoteVolume', 0) or 0
        return quote_volume >= GRID['min_hacim_usdt'], quote_volume
    except Exception:
        return True, 0

# ==================== YATAY PİYASA KONTROLÜ ====================
def yatay_mi(symbol):
    try:
        ohlcv = fetch_ohlcv_guvenli(symbol, '15m', limit=50)
        if ohlcv is None:
            return False, "veri yok"
        df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
        
        if len(df) < 30:
            return False, "yetersiz veri"
        
        # 1. ADX
        adx = adx_hesapla(df, 14)
        if adx > GRID['yatay_adx_max']:
            return False, f"ADX {adx:.1f} > {GRID['yatay_adx_max']}"
        
        # 2. Bollinger genişliği
        sma = df['close'].rolling(20).mean()
        std = df['close'].rolling(20).std()
        ust = sma + (std * 2)
        alt = sma - (std * 2)
        boll_gen = ((ust - alt) / sma * 100).iloc[-1]
        if pd.isna(boll_gen) or boll_gen > GRID['yatay_bollinger_max']:
            return False, f"Bollinger %{boll_gen:.2f} > %{GRID['yatay_bollinger_max']}"
        
        # 3. Son 20 mum aralığı
        son_20 = df.tail(20)
        if len(son_20) < 20:
            return False, "yetersiz 20 mum"
        max_min = ((son_20['high'].max() - son_20['low'].min()) / son_20['close'].mean()) * 100
        if max_min > GRID['yatay_20mum_max']:
            return False, f"20mum %{max_min:.2f} > %{GRID['yatay_20mum_max']}"
        
        # 4. Hacim
        hacim_ort = df['volume'].rolling(20).mean().iloc[-1]
        son_hacim = df['volume'].iloc[-1]
        if pd.isna(hacim_ort) or son_hacim < hacim_ort * GRID['yatay_hacim_min']:
            return False, "hacim düşük"
        
        return True, f"YATAY OK (ADX {adx:.1f}, Boll %{boll_gen:.2f})"
    except Exception as e:
        return False, f"hata: {str(e)[:30]}"

# ==================== TREND KORUMASI ====================
def trend_kontrol(symbol):
    try:
        ohlcv = fetch_ohlcv_guvenli(symbol, '15m', limit=50)
        if ohlcv is None:
            return False, "veri yok"
        df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
        
        adx = adx_hesapla(df, 14)
        if adx > GRID['adx_dur_esik']:
            return True, f"ADX {adx:.1f} > {GRID['adx_dur_esik']}"
        
        if symbol in AKTIF_GRIDLER:
            grid = AKTIF_GRIDLER[symbol]
            son_fiyat = df['close'].iloc[-1]
            if son_fiyat < grid['alt'] * (1 - GRID['fiyat_disi_pct']):
                return True, "Fiyat grid altı"
            if son_fiyat > grid['ust'] * (1 + GRID['fiyat_disi_pct']):
                return True, "Fiyat grid üstü"
        
        return False, "OK"
    except Exception:
        return False, "hata"

# ==================== GRID OLUŞTURMA ====================
def grid_olustur(symbol):
    try:
        # 1. Hacim filtresi
        uygun, hacim = hacim_uygun_mu(symbol)
        if not uygun:
            return None, f"hacim düşük"
        
        # 2. Yatay piyasa kontrolü
        yatay, mesaj = yatay_mi(symbol)
        if not yatay:
            ANALITIK["yatay_red"] = ANALITIK.get("yatay_red", 0) + 1
            return None, mesaj
        
        fiyat = exchange.fetch_ticker(symbol)['last']
        
        alt = fiyat * (1 - GRID['grid_aralik_pct'])
        ust = fiyat * (1 + GRID['grid_aralik_pct'])
        adim = (ust - alt) / GRID['grid_sayisi']
        
        bal = exchange.fetch_balance()
        kasa = float(bal['total'].get('USDT', 0))
        if kasa < 10:
            return None, "yetersiz kasa"
        
        toplam_yatirim = kasa * GRID['toplam_yatirim_pct'] / GRID['max_coin']
        miktar_per_grid = (toplam_yatirim / GRID['grid_sayisi']) / fiyat
        
        if not set_leverage_and_margin_safely(symbol, GRID['kaldirac']):
            return None, "kaldıraç hata"
        
        tum_emirleri_iptal_et(symbol)
        
        # Minimum miktar kontrolü
        market_info = exchange.market(symbol)
        min_amount = market_info.get('limits', {}).get('amount', {}).get('min', 0)
        
        # Miktar minimum altındaysa, minimuma çıkar
        if min_amount and miktar_per_grid < min_amount:
            miktar_per_grid = min_amount * 1.1  # %10 üstü
            print(f"⚠️ {symbol} miktar min altıydı → {miktar_per_grid:.4f} yapıldı", flush=True)
        
        emirler = []
        for i in range(GRID['grid_sayisi']):
            seviye = alt + (adim * i)
            if seviye < fiyat * 0.998:
                try:
                    miktar = float(exchange.amount_to_precision(symbol, miktar_per_grid))
                    if miktar <= 0:
                        continue
                    if min_amount and miktar < min_amount:
                        print(f"⚠️ {symbol} emir #{i}: min altı, atlandı", flush=True)
                        continue
                    emir = exchange.create_order(
                        symbol, 'limit', 'buy', miktar,
                        float(exchange.price_to_precision(symbol, seviye))
                    )
                    emirler.append({
                        "tip": "AL", "fiyat": seviye, "miktar": miktar,
                        "id": emir.get('id'), "durum": "acik"
                    })
                except Exception as e:
                    print(f"⚠️ Emir hatası #{i}: {e}", flush=True)
                    ANALITIK["emir_hatasi"] = ANALITIK.get("emir_hatasi", 0) + 1
                    continue
        
        if len(emirler) == 0:
            return None, "0 emir (min miktar sorunu)"
        
        AKTIF_GRIDLER[symbol] = {
            "ust": ust, "alt": alt, "adim": adim,
            "fiyat_baslangic": fiyat,
            "emirler": emirler,
            "toplam_kar": 0.0,
            "kurulum_zaman": int(time.time()*1000),
        }
        ANALITIK["grid_kurulum"] = ANALITIK.get("grid_kurulum", 0) + 1
        hafizayi_kaydet()
        
        telegram_mesaj_gonder(
            f"📊 *GRID KURULDU*\n"
            f"📌 `{symbol[:12]}`\n"
            f"💰 `{fiyat}` | Aralık: `{alt:.4f}-{ust:.4f}`\n"
            f"📈 {len(emirler)} AL emri | Miktar: `{miktar_per_grid:.4f}`\n"
            f"🎯 {mesaj}"
        )
        return True, "OK"
    except Exception as e:
        return None, str(e)[:50]

def grid_kapat(symbol, sebep):
    global GRID_COOLDOWNLAR
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
        except Exception:
            pass
        
        if symbol in AKTIF_GRIDLER:
            del AKTIF_GRIDLER[symbol]
        
        GRID_COOLDOWNLAR[symbol] = time.time() + GRID['grid_cooldown_dk'] * 60
        hafizayi_kaydet()
        telegram_mesaj_gonder(f"🛑 *KAPATILDI* → `{symbol[:12]}`\nSebep: {sebep}")
    except Exception as e:
        telegram_mesaj_gonder(f"⚠️ {e}")

def grid_takip(symbol):
    if symbol not in AKTIF_GRIDLER:
        return
    grid = AKTIF_GRIDLER[symbol]
    
    try:
        acik_emirler = exchange.fetch_open_orders(symbol)
        acik_id_set = set(str(e['id']) for e in acik_emirler)
        
        for emir in grid['emirler']:
            if emir['durum'] == 'acik' and str(emir['id']) not in acik_id_set:
                emir['durum'] = 'tetiklendi'
                ANALITIK["toplam_grid_tetiklenme"] = ANALITIK.get("toplam_grid_tetiklenme", 0) + 1
                
                satis_fiyat = emir['fiyat'] * (1 + GRID['grid_kar_pct'])
                try:
                    satis_emir = exchange.create_order(
                        symbol, 'limit', 'sell', emir['miktar'],
                        float(exchange.price_to_precision(symbol, satis_fiyat)),
                        {'reduceOnly': True}
                    )
                    emir['satis_id'] = satis_emir.get('id')
                    emir['satis_fiyat'] = satis_fiyat
                    telegram_mesaj_gonder(f"⚡ `{symbol[:12]}` AL tetik @ `{emir['fiyat']:.4f}`")
                except Exception:
                    pass
        
        for emir in grid['emirler']:
            if emir['durum'] == 'tetiklendi' and 'satis_id' in emir:
                if str(emir['satis_id']) not in acik_id_set:
                    kar_pct = GRID['grid_kar_pct'] - (2 * KOMISYON_ORANI)
                    kar_usdt = (emir['fiyat'] * emir['miktar']) * kar_pct
                    grid['toplam_kar'] = grid.get('toplam_kar', 0) + kar_usdt
                    ANALITIK["toplam_kar"] = ANALITIK.get("toplam_kar", 0) + kar_usdt
                    
                    emir['durum'] = 'acik'
                    emir.pop('satis_id', None)
                    emir.pop('satis_fiyat', None)
                    
                    try:
                        yeni_al = exchange.create_order(
                            symbol, 'limit', 'buy', emir['miktar'],
                            float(exchange.price_to_precision(symbol, emir['fiyat']))
                        )
                        emir['id'] = yeni_al.get('id')
                        print(f"💰 {symbol} | +{kar_usdt:.4f} USDT", flush=True)
                        telegram_mesaj_gonder(f"💰 `{symbol[:12]}` kâr: `+{kar_usdt:.4f}` USDT")
                    except Exception:
                        pass
        
        hafizayi_kaydet()
    except Exception:
        pass

# ==================== FLASK ====================
@app.route('/')
def home():
    return f"Grid v6 | Aktif: {len(AKTIF_GRIDLER)} | Kâr: {ANALITIK.get('toplam_kar', 0):.4f}"

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
                acik_sayisi = len([e for e in g['emirler'] if e['durum'] == 'acik'])
                tetiklenen = len([e for e in g['emirler'] if e['durum'] == 'tetiklendi'])
                grid_detay += (
                    f"• `{sym[:10]}` | `{g['alt']:.4f}-{g['ust']:.4f}`\n"
                    f"  Açık:`{acik_sayisi}` Tetik:`{tetiklenen}` Kâr:`{g.get('toplam_kar',0):+.4f}`\n"
                )
        else:
            grid_detay = "\n📊 *Aktif Grid Yok*\n"
        
        su_an = time.time()
        cd = {s: int((t-su_an)/60) for s,t in GRID_COOLDOWNLAR.items() if t > su_an}
        cd_detay = ""
        if cd:
            cd_detay = "\n⏸️ *Cooldown:*\n"
            for s, dk in cd.items():
                cd_detay += f"• `{s[:10]}` → {dk} dk\n"
        
        mesaj = (
            f"📊 *GRID BOT v6*\n\n"
            f"💰 Toplam: `{total:.2f}` (Serbest: `{free:.2f}`)\n"
            f"📈 PnL: `{pnl:+.4f}`\n"
            f"💵 Kâr: `{ANALITIK.get('toplam_kar',0):+.4f}`\n"
            f"🎯 Tetik: `{ANALITIK.get('toplam_grid_tetiklenme',0)}`\n"
            f"🔥 Kapatma: `{ANALITIK.get('trend_kapatma',0)}`\n"
            f"🚫 Yatay Red: `{ANALITIK.get('yatay_red',0)}`\n"
            f"⚠️ Emir Hata: `{ANALITIK.get('emir_hatasi',0)}`\n"
            f"📊 Grid: `{len(AKTIF_GRIDLER)}/{GRID['max_coin']}`\n"
            f"{grid_detay}"
            f"{cd_detay}"
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

async def grid_kur_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 *Gridler kuruluyor (sinyal kontrolü ile)...*", parse_mode='Markdown')
    
    def run():
        for symbol in TAKIP_EDILENLER:
            if symbol in AKTIF_GRIDLER:
                continue
            if len(AKTIF_GRIDLER) >= GRID['max_coin']:
                break
            su_an = time.time()
            if su_an < GRID_COOLDOWNLAR.get(symbol, 0):
                continue
            ok, mesaj = grid_olustur(symbol)
            if ok:
                time.sleep(2)
            else:
                telegram_mesaj_gonder(f"⚠️ `{symbol[:12]}`: {mesaj}")
    
    threading.Thread(target=run, daemon=True).start()

async def kapat_komutu(update, context):
    await update.message.reply_text("🛑 *Kapatılıyor...*", parse_mode='Markdown')
    for sym in list(AKTIF_GRIDLER.keys()):
        grid_kapat(sym, "Manuel")
    await update.message.reply_text("✅ *Kapatıldı.*", parse_mode='Markdown')

async def temizle_komutu(update, context):
    global AKTIF_GRIDLER, GRID_COOLDOWNLAR
    AKTIF_GRIDLER = {}
    GRID_COOLDOWNLAR = {}
    hafizayi_kaydet()
    await update.message.reply_text("🗑️ *Temizlendi.*", parse_mode='Markdown')

async def test_komutu(update, context):
    satirlar = ["🎯 *YATAY PİYASA TESTİ*\n"]
    for symbol in TAKIP_EDILENLER:
        yatay, mesaj = yatay_mi(symbol)
        ikon = "✅" if yatay else "❌"
        satirlar.append(f"{ikon} `{symbol[:10]}`: {mesaj}")
    await update.message.reply_text("\n".join(satirlar), parse_mode='Markdown')

async def fiyat_komutu(update, context):
    """Coin fiyatlarını ve min miktarları göster."""
    satirlar = ["💰 *FİYATLAR + MİN MİKTAR*\n"]
    for symbol in TAKIP_EDILENLER:
        try:
            ticker = exchange.fetch_ticker(symbol)
            fiyat = ticker['last']
            market = exchange.market(symbol)
            min_amount = market.get('limits', {}).get('amount', {}).get('min', 0)
            min_usdt = min_amount * fiyat if min_amount else 0
            satirlar.append(
                f"`{symbol[:10]}` | Fiyat: `{fiyat}` | Min: `{min_amount}` (~`{min_usdt:.2f}` USDT)"
            )
        except Exception:
            satirlar.append(f"`{symbol[:10]}` HATA")
    await update.message.reply_text("\n".join(satirlar), parse_mode='Markdown')

async def grid_detay_komutu(update, context):
    if not AKTIF_GRIDLER:
        await update.message.reply_text("Aktif grid yok.", parse_mode='Markdown')
        return
    satirlar = ["📊 *GRID DETAYLARI:*\n"]
    for sym, g in AKTIF_GRIDLER.items():
        satirlar.append(f"\n*{sym[:12]}*")
        satirlar.append(f"Aralık: `{g['alt']:.4f} - {g['ust']:.4f}`")
        satirlar.append(f"Kâr: `{g.get('toplam_kar',0):+.4f}` USDT")
        satirlar.append(f"Emir: `{len(g['emirler'])}`")
        for e in g['emirler'][:3]:
            satirlar.append(f"  • AL @ `{e['fiyat']:.4f}` x `{e['miktar']:.4f}`")
    await update.message.reply_text("\n".join(satirlar), parse_mode='Markdown')

async def cooldown_komutu(update, context):
    global GRID_COOLDOWNLAR
    GRID_COOLDOWNLAR = {}
    hafizayi_kaydet()
    await update.message.reply_text("✅ *Cooldown sıfırlandı.*", parse_mode='Markdown')

# ==================== ANA DÖNGÜ ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, ANALITIK

    print(f"📊 [GRID v6] Başladı | {len(TAKIP_EDILENLER)} coin", flush=True)

    try:
        exchange.load_markets()
    except Exception:
        pass

    dongu_sayaci = 0
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5); continue

            gunluk = gunluk_kontrol()
            if gunluk is not None and gunluk <= -GRID['gunluk_max_kayip_pct']:
                telegram_mesaj_gonder(f"🛑 *Günlük limit!*")
                BOT_CALISIYOR_MU = False
                continue

            for sym in list(AKTIF_GRIDLER.keys()):
                dur, sebep = trend_kontrol(sym)
                if dur:
                    grid_kapat(sym, sebep)
                    ANALITIK["trend_kapatma"] = ANALITIK.get("trend_kapatma", 0) + 1
                    hafizayi_kaydet()

            for sym in list(AKTIF_GRIDLER.keys()):
                grid_takip(sym)

            su_an = time.time()
            for symbol in TAKIP_EDILENLER:
                if symbol not in AKTIF_GRIDLER:
                    if len(AKTIF_GRIDLER) >= GRID['max_coin']:
                        break
                    if su_an < GRID_COOLDOWNLAR.get(symbol, 0):
                        continue
                    ok, mesaj = grid_olustur(symbol)
                    if ok:
                        print(f"📊 Grid: {symbol}", flush=True)
                        time.sleep(2)

            dongu_sayaci += 1
            if dongu_sayaci % 40 == 0:
                print(f"🔍 #{dongu_sayaci} | Grid:{len(AKTIF_GRIDLER)} | Kâr:{ANALITIK.get('toplam_kar',0):+.4f}", flush=True)

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

    app_tg = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(30)
        .pool_timeout(30)
        .build()
    )
    
    app_tg.add_handler(CommandHandler("durum", durum_komutu))
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    app_tg.add_handler(CommandHandler("grid_kur", grid_kur_komutu))
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))
    app_tg.add_handler(CommandHandler("temizle", temizle_komutu))
    app_tg.add_handler(CommandHandler("grid_detay", grid_detay_komutu))
    app_tg.add_handler(CommandHandler("cooldown", cooldown_komutu))
    app_tg.add_handler(CommandHandler("test", test_komutu))
    app_tg.add_handler(CommandHandler("fiyat", fiyat_komutu))

    while True:
        try:
            app_tg.run_polling(drop_pending_updates=True)
        except Exception as e:
            print(f"⚠️ Telegram: {e}", flush=True)
            time.sleep(5)
