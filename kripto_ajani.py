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

# ==================== AYARLAR ====================
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

TAKIP_EDILENLER = [
    'ETH/USDT:USDT', 'SOL/USDT:USDT', 'XRP/USDT:USDT',
    'ADA/USDT:USDT', 'DOGE/USDT:USDT', 'AVAX/USDT:USDT'
]

# ==================== PUANLI KURGU PARAMETRELERİ ====================
MOD = {
    "aciklama": "🎯 Puanlı Sistem v2 (1h+15m, TP %2.5)",
    "zaman_ana": "1h",
    "zaman_sinyal": "15m",
    # Puanlama
    "puan_esigi": 82,
    "puan_baslangic": 50,
    "puan_trend": 20,
    "puan_rsi": 15,
    "puan_adx": 15,
    "puan_derinlik": 10,
    "puan_mum": 5,
    # RSI aralığı
    "rsi_min": 40, "rsi_max": 60,
    # ADX eşiği
    "adx_esik": 25,
    # TP/Stop
    "tp_sabit_pct": 0.025,      # %2.5 sabit TP
    "atr_stop_mult": 1.5,
    "min_stop_pct": 0.008,
    "max_stop_pct": 0.030,
    # Risk
    "islem_riski_pct": 0.02,    # %2
    "kaldirac": 7,              # 7x
    "maks_pozisyon": 3,
    "cooldown_dk": 30,          # stop sonrası 30 dk
    "ardisik_stop_limit": 2,    # 2 üst üste stop → cooldown
    "ardisik_stop_cooldown_dk": 60,  # 1 saat
    "gunluk_max_kayip_pct": 0.05,    # %5
}

# ==================== DURUMLAR ====================
BOT_CALISIYOR_MU = True
GUN_BASI_KASA = None
GUN_BASI_TARIH = None
AKTIF_POZISYONLAR = {}
COIN_COOLDOWNLAR = {}
GENEL_COOLDOWN = 0  # Ardışık stop sonrası genel cooldown
ARDISIK_STOP_SAYACI = 0
ANALITIK = {
    "basarili_islem_sayisi": 0,
    "basarisiz_islem_sayisi": 0,
    "toplam_kar": 0.0,
}

# ==================== SUPABASE ====================
def hafizayi_yukle():
    varsayilan = {
        "aktif_pozisyonlar": {},
        "cooldownlar": {},
        "analitik": ANALITIK.copy(),
        "genel_cooldown": 0,
        "ardisik_stop": 0,
    }
    try:
        r = supabase.table("bot_hafiza").select("*").eq("id", 50).execute()
        if r.data:
            v = r.data[0]
            return {
                "aktif_pozisyonlar": v.get("aktif_pozisyonlar", {}),
                "cooldownlar": v.get("cooldownlar", {}),
                "analitik": v.get("analitik", ANALITIK.copy()),
                "genel_cooldown": v.get("genel_cooldown", 0),
                "ardisik_stop": v.get("ardisik_stop", 0),
            }
    except Exception:
        pass
    try:
        supabase.table("bot_hafiza").upsert({"id": 50, **varsayilan}).execute()
    except Exception:
        pass
    return varsayilan

def hafizayi_kaydet():
    try:
        supabase.table("bot_hafiza").upsert({
            "id": 50,
            "aktif_pozisyonlar": AKTIF_POZISYONLAR,
            "cooldownlar": COIN_COOLDOWNLAR,
            "analitik": ANALITIK,
            "genel_cooldown": GENEL_COOLDOWN,
            "ardisik_stop": ARDISIK_STOP_SAYACI,
        }).execute()
    except Exception:
        pass

kalici = hafizayi_yukle()
AKTIF_POZISYONLAR = kalici["aktif_pozisyonlar"]
COIN_COOLDOWNLAR = kalici["cooldownlar"]
ANALITIK = kalici["analitik"]
GENEL_COOLDOWN = kalici["genel_cooldown"]
ARDISIK_STOP_SAYACI = kalici["ardisik_stop"]

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
def ema_hesapla(close, period):
    return close.ewm(span=period, adjust=False).mean()

def rsi_hesapla(close, period=14):
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def atr_hesapla(df, period=14):
    tr = pd.concat([
        df['high'] - df['low'],
        (df['high'] - df['close'].shift()).abs(),
        (df['low'] - df['close'].shift()).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()

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

def hacim_orani(df, period=20):
    try:
        ort = df['volume'].rolling(period).mean().iloc[-1]
        son = df['volume'].iloc[-1]
        return float(son / ort) if ort > 0 else 1.0
    except Exception:
        return 1.0

# ==================== EMİR DEFTERİ ====================
def emir_defteri_derinlik_analizi(symbol):
    try:
        order_book = exchange.fetch_order_book(symbol, limit=20)
        bids = order_book.get('bids', [])
        asks = order_book.get('asks', [])
        toplam_bid = sum(b[1] for b in bids)
        toplam_ask = sum(a[1] for a in asks)
        if toplam_bid + toplam_ask == 0:
            return "DENGELI"
        bid_orani = toplam_bid / (toplam_bid + toplam_ask)
        if bid_orani > 0.55:
            return "ALICI_BASKIN"
        elif bid_orani < 0.45:
            return "SATICI_BASKIN"
        return "DENGELI"
    except Exception:
        return "DENGELI"

# ==================== PUANLI SİNYAL ====================
def sinyal_uret(df_15m, df_1h, symbol):
    """5 faktörlü puanlama."""
    if len(df_15m) < 50 or len(df_1h) < 30:
        return None, "veri yetersiz", 0
    
    # 1. 1h TREND
    ema7 = ema_hesapla(df_1h['close'], 7).iloc[-1]
    ema21 = ema_hesapla(df_1h['close'], 21).iloc[-1]
    if pd.isna(ema7) or pd.isna(ema21):
        return None, "EMA NaN", 0
    trend_boga = ema7 > ema21
    grid_yonu = "LONG" if trend_boga else "SHORT"
    
    # 2. 15m RSI
    rsi = rsi_hesapla(df_15m['close'], 14).iloc[-1]
    if pd.isna(rsi):
        return None, "RSI NaN", 0
    
    # 3. 15m ADX
    adx_val = adx_hesapla(df_15m, 14)
    
    # 4. ATR
    atr = atr_hesapla(df_15m, 14).iloc[-1]
    fiyat = df_15m['close'].iloc[-1]
    if pd.isna(atr) or atr == 0:
        return None, "ATR NaN", 0
    atr_pct = (atr / fiyat) * 100
    if not (0.3 <= atr_pct <= 5.0):
        return None, f"ATR %{atr_pct:.2f} dışı", 0
    
    # 5. Emir defteri
    derinlik = emir_defteri_derinlik_analizi(symbol)
    
    # 6. Mum yapısı
    govde = abs(df_15m['close'].iloc[-1] - df_15m['open'].iloc[-1])
    fitil = df_15m['high'].iloc[-1] - df_15m['low'].iloc[-1]
    mum_guclu = (fitil > 0 and govde / fitil > 0.6)
    
    # ============ PUANLAMA ============
    puan = MOD['puan_baslangic']
    
    # Trend (+20)
    puan += MOD['puan_trend']
    
    # RSI nötr (+15)
    if MOD['rsi_min'] <= rsi <= MOD['rsi_max']:
        puan += MOD['puan_rsi']
    
    # ADX trend gücü (+15)
    if adx_val > MOD['adx_esik']:
        puan += MOD['puan_adx']
    
    # Emir defteri (+10)
    if derinlik == "ALICI_BASKIN" and grid_yonu == "LONG":
        puan += MOD['puan_derinlik']
    elif derinlik == "SATICI_BASKIN" and grid_yonu == "SHORT":
        puan += MOD['puan_derinlik']
    
    # Mum yapısı (+5)
    if mum_guclu:
        puan += MOD['puan_mum']
    
    # Eşik kontrolü
    if puan < MOD['puan_esigi']:
        return None, f"puan {puan}<{MOD['puan_esigi']}", puan
    
    # ============ STOP / TP ============
    stop_pct = max(MOD['min_stop_pct'], min(MOD['max_stop_pct'],
                   (atr_pct * MOD['atr_stop_mult']) / 100.0))
    tp_pct = MOD['tp_sabit_pct']  # SABİT %2.5
    
    son_fiyat = df_15m['close'].iloc[-1]
    
    return {
        "yon": grid_yonu,
        "giris": float(son_fiyat),
        "stop_pct": stop_pct,
        "tp_pct": tp_pct,
        "rsi": float(rsi),
        "adx": adx_val,
        "derinlik": derinlik,
        "puan": puan,
        "atr_pct": atr_pct
    }, f"OK (puan {puan})", puan

# ==================== BACKTEST ====================
def backtest_coin(symbol):
    try:
        ohlcv_1h = fetch_ohlcv_guvenli(symbol, MOD['zaman_ana'], limit=1000)
        ohlcv_15m = fetch_ohlcv_guvenli(symbol, MOD['zaman_sinyal'], limit=1000)
        if ohlcv_1h is None or ohlcv_15m is None:
            return None
        if len(ohlcv_1h) < 100 or len(ohlcv_15m) < 200:
            return None
        
        df_1h = pd.DataFrame(ohlcv_1h, columns=['timestamp','open','high','low','close','volume'])
        df_15m = pd.DataFrame(ohlcv_15m, columns=['timestamp','open','high','low','close','volume'])
        
        trades = []
        poz = None
        
        for i in range(60, len(df_15m)):
            ts = df_15m['timestamp'].iloc[i]
            df_1h_s = df_1h[df_1h['timestamp'] <= ts]
            if len(df_1h_s) < 30:
                continue
            
            bar = df_15m.iloc[i]
            high = bar['high']; low = bar['low']
            
            # Pozisyon yönetimi
            if poz is not None:
                if poz['yon'] == 'LONG':
                    if low <= poz['stop']:
                        trades.append({**poz, 'cikis': poz['stop'], 'sebep': 'SL'}); poz = None
                    elif high >= poz['tp']:
                        trades.append({**poz, 'cikis': poz['tp'], 'sebep': 'TP'}); poz = None
                else:
                    if high >= poz['stop']:
                        trades.append({**poz, 'cikis': poz['stop'], 'sebep': 'SL'}); poz = None
                    elif low <= poz['tp']:
                        trades.append({**poz, 'cikis': poz['tp'], 'sebep': 'TP'}); poz = None
                continue
            
            # Yeni sinyal
            df_15m_slice = df_15m.iloc[max(0, i-100):i+1].reset_index(drop=True)
            df_1h_slice = df_1h_s.reset_index(drop=True)
            
            # Backtest'te emir defteri yok, DENGELI varsayalım
            try:
                sig, neden, puan = sinyal_uret(df_15m_slice, df_1h_slice, None)
            except Exception:
                continue
            
            if sig:
                fiyat = sig['giris']
                if sig['yon'] == 'LONG':
                    stop = fiyat * (1 - sig['stop_pct'])
                    tp = fiyat * (1 + sig['tp_pct'])
                else:
                    stop = fiyat * (1 + sig['stop_pct'])
                    tp = fiyat * (1 - sig['tp_pct'])
                poz = {"yon": sig['yon'], "giris": float(fiyat),
                       "stop": float(stop), "tp": float(tp)}
        
        if not trades:
            return {"islem": 0, "win_rate": 0, "pf": 0, "max_dd": 0, "toplam": 0}
        
        kazanclar = []
        for t in trades:
            if t['yon'] == 'LONG':
                pct = (t['cikis'] - t['giris']) / t['giris']
            else:
                pct = (t['giris'] - t['cikis']) / t['giris']
            pct -= KOMISYON_ORANI
            kazanclar.append(pct)
        
        k = np.array(kazanclar)
        kaz = k[k > 0]; kay = k[k < 0]
        win = len(kaz) / len(k) * 100 if len(k) else 0
        pf = abs(kaz.sum() / kay.sum()) if len(kay) and kay.sum() != 0 else 999
        equity = np.cumprod(1 + k)
        peak = np.maximum.accumulate(equity)
        dd = (equity - peak) / peak
        max_dd = dd.min() * 100 if len(dd) else 0
        
        return {
            "islem": len(trades),
            "win_rate": round(win, 2),
            "pf": round(pf, 3) if pf != 999 else 999,
            "max_dd": round(max_dd, 2),
            "toplam": round((equity[-1] - 1) * 100, 2) if len(equity) else 0
        }
    except Exception as e:
        return None

# ==================== POZİSYON AÇMA ====================
def pozisyon_ac(symbol, sig):
    try:
        bal = exchange.fetch_balance()
        kasa = float(bal['total'].get('USDT', 0))
        if kasa < 10:
            return False
        if not set_leverage_and_margin_safely(symbol, MOD['kaldirac']):
            return False
        
        risk_usdt = kasa * MOD['islem_riski_pct']
        stop_pct = sig['stop_pct']
        poz_degeri = risk_usdt / stop_pct
        
        try:
            market_info = exchange.market(symbol)
            cs = float(market_info.get('contractSize', 1.0))
            ham = poz_degeri / (sig['giris'] * cs)
            miktar = float(exchange.amount_to_precision(symbol, max(ham, 0.001)))
            if miktar <= 0:
                return False
        except Exception:
            return False
        
        tum_emirleri_iptal_et(symbol)
        emir = exchange.create_order(symbol, 'market',
            'buy' if sig['yon'] == 'LONG' else 'sell', miktar)
        giris = float(emir.get('average') or emir.get('price') or sig['giris'])
        time.sleep(0.5)
        
        yon = sig['yon']
        tp_pct = sig['tp_pct']
        
        if yon == 'LONG':
            stop = giris * (1 - stop_pct); tp = giris * (1 + tp_pct); kapat_yon = 'sell'
        else:
            stop = giris * (1 + stop_pct); tp = giris * (1 - tp_pct); kapat_yon = 'buy'
        
        stop = float(exchange.price_to_precision(symbol, stop))
        tp = float(exchange.price_to_precision(symbol, tp))
        
        try:
            exchange.create_order(symbol, 'stop', kapat_yon, miktar, stop,
                {'stopPrice': stop, 'triggerPrice': stop, 'reduceOnly': True})
        except Exception:
            try:
                exchange.create_order(symbol, 'stop_market', kapat_yon, miktar, stop,
                    {'stopPrice': stop, 'triggerPrice': stop, 'reduceOnly': True})
            except Exception: pass
        
        try:
            exchange.create_order(symbol, 'limit', kapat_yon, miktar, tp, {'reduceOnly': True})
        except Exception: pass
        
        AKTIF_POZISYONLAR[symbol] = {
            "yon": yon, "giris": giris, "stop": stop, "tp": tp,
            "miktar": miktar, "puan": sig['puan'],
            "giris_zaman": int(time.time()*1000)
        }
        hafizayi_kaydet()
        
        telegram_mesaj_gonder(
            f"🎯 *İŞLEM AÇILDI*\n"
            f"📌 `{symbol[:12]}` | *{yon}*\n"
            f"💰 Giriş: `{giris}` | SL: `{stop}` | TP: `{tp}`\n"
            f"📊 Puan: `{sig['puan']}` | RSI: `{sig['rsi']:.1f}` | ADX: `{sig['adx']:.1f}`\n"
            f"📚 Derinlik: `{sig['derinlik']}` | Risk: `{risk_usdt:.2f}` USDT"
        )
        return True
    except Exception as e:
        print(f"❌ Pozisyon açma: {e}", flush=True)
        return False

# ==================== FLASK ====================
@app.route('/')
def home():
    return f"Puanlı v2 | Poz: {len(AKTIF_POZISYONLAR)} | Ardışık Stop: {ARDISIK_STOP_SAYACI}"

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
        
        bs = ANALITIK.get("basarili_islem_sayisi", 0)
        bz = ANALITIK.get("basarisiz_islem_sayisi", 0)
        tot = bs + bz
        oran = (bs / tot * 100) if tot else 0
        
        # Cooldown durumu
        su_an = time.time()
        if GENEL_COOLDOWN > su_an:
            cd_dk = int((GENEL_COOLDOWN - su_an) / 60)
            cd_durum = f"⏸️ Genel Cooldown: {cd_dk} dk"
        else:
            cd_durum = "▶️ Aktif"
        
        detay = ""
        if poslari:
            detay = "\n📋 *Aktif:*\n"
            for p in poslari:
                detay += f"• `{p.get('symbol')[:12]}` | {str(p.get('side','')).upper()} | `{float(p.get('unrealizedPnl',0)):+.2f}`\n"
        
        mesaj = (
            f"🎯 *PUANLI SİSTEM v2*\n\n"
            f"💰 Toplam: `{total:.2f}` USDT (Serbest: `{free:.2f}`)\n"
            f"📈 PnL: `{pnl:+.2f}` USDT\n"
            f"📌 Pozisyon: `{len(poslari)}/{MOD['maks_pozisyon']}`\n"
            f"🎯 {cd_durum}\n"
            f"🔥 Ardışık Stop: `{ARDISIK_STOP_SAYACI}`\n"
            f"⚙️ Zaman: `{MOD['zaman_ana']}+{MOD['zaman_sinyal']}` | Kaldıraç: `{MOD['kaldirac']}x`\n"
            f"📊 Puan eşiği: `{MOD['puan_esigi']}` | TP: `%{MOD['tp_sabit_pct']*100}`\n\n"
            f"✅ TP: `{bs}` | ❌ Stop: `{bz}` | Başarı: `%{oran:.1f}`\n"
            f"{detay}"
        )
        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def baslat_komutu(update, context):
    global BOT_CALISIYOR_MU, ARDISIK_STOP_SAYACI, GENEL_COOLDOWN
    BOT_CALISIYOR_MU = True
    ARDISIK_STOP_SAYACI = 0
    GENEL_COOLDOWN = 0
    hafizayi_kaydet()
    await update.message.reply_text("▶️ *Bot Aktif!* Cooldown sıfırlandı.", parse_mode='Markdown')

async def durdur_komutu(update, context):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ *Durduruldu.*", parse_mode='Markdown')

async def kapat_komutu(update, context):
    await update.message.reply_text("🛑 *Her şey kapatılıyor...*", parse_mode='Markdown')
    try:
        for pos in exchange.fetch_positions():
            k = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
            if k > 0:
                sym = pos['symbol']
                yon = str(pos.get('side', '')).upper()
                kapat = 'sell' if yon == 'LONG' else 'buy'
                tum_emirleri_iptal_et(sym)
                exchange.create_order(sym, 'market', kapat, k, None, {'reduce_only': True})
        AKTIF_POZISYONLAR.clear()
        hafizayi_kaydet()
        await update.message.reply_text("✅ *Kapatıldı.*", parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"⚠️ {e}", parse_mode='Markdown')

async def temizle_komutu(update, context):
    global AKTIF_POZISYONLAR, COIN_COOLDOWNLAR, ARDISIK_STOP_SAYACI, GENEL_COOLDOWN
    AKTIF_POZISYONLAR = {}
    COIN_COOLDOWNLAR = {}
    ARDISIK_STOP_SAYACI = 0
    GENEL_COOLDOWN = 0
    hafizayi_kaydet()
    await update.message.reply_text("🗑️ *Temizlendi.*", parse_mode='Markdown')

async def backtest_komutu(update, context):
    await update.message.reply_text("⏳ *Backtest başladı* — 30 sn", parse_mode='Markdown')
    
    def run():
        try:
            satirlar = [f"*📊 PUANLI v2 BACKTEST*\n```"]
            satirlar.append(f"{'COIN':<14} {'İŞL':>4} {'WIN%':>6} {'PF':>6} {'DD%':>6} {'TOT%':>7}")
            satirlar.append("-" * 56)
            toplam = 0
            for c in TAKIP_EDILENLER:
                r = backtest_coin(c)
                if r is None:
                    satirlar.append(f"{c[:12]:<14} HATA")
                else:
                    satirlar.append(
                        f"{c[:12]:<14} {r['islem']:>4} {r['win_rate']:>6} {r['pf']:>6} {r['max_dd']:>6} {r['toplam']:>7}"
                    )
                    toplam += r['toplam']
            satirlar.append("-" * 56)
            ort = toplam / len(TAKIP_EDILENLER)
            satirlar.append(f"Ortalama: {ort:.2f}%")
            satirlar.append("```")
            telegram_mesaj_gonder("\n".join(satirlar))
        except Exception as e:
            telegram_mesaj_gonder(f"❌ {e}")
    
    threading.Thread(target=run, daemon=True).start()

async def test_komutu(update, context):
    """Şu anki piyasada puanları göster."""
    satirlar = ["🎯 *PUAN TESTİ*\n"]
    for symbol in TAKIP_EDILENLER:
        try:
            ohlcv_1h = fetch_ohlcv_guvenli(symbol, MOD['zaman_ana'], limit=50)
            ohlcv_15m = fetch_ohlcv_guvenli(symbol, MOD['zaman_sinyal'], limit=100)
            if ohlcv_1h is None or ohlcv_15m is None:
                continue
            df_1h = pd.DataFrame(ohlcv_1h, columns=['timestamp','open','high','low','close','volume'])
            df_15m = pd.DataFrame(ohlcv_15m, columns=['timestamp','open','high','low','close','volume'])
            sig, neden, puan = sinyal_uret(df_15m, df_1h, symbol)
            if sig:
                satirlar.append(f"`{symbol[:10]}` {sig['yon']} | Puan: `{puan}` ✅")
            else:
                satirlar.append(f"`{symbol[:10]}` Puan: `{puan}` ({neden[:20]})")
        except Exception:
            satirlar.append(f"`{symbol[:10]}` HATA")
    await update.message.reply_text("\n".join(satirlar), parse_mode='Markdown')

# ==================== ANA DÖNGÜ ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, ANALITIK, GENEL_COOLDOWN, ARDISIK_STOP_SAYACI

    print(f"🎯 [PUANLI v2] Başladı", flush=True)
    print(f"📊 Puan eşiği: {MOD['puan_esigi']} | TP: %{MOD['tp_sabit_pct']*100} | Kaldıraç: {MOD['kaldirac']}x", flush=True)

    try:
        exchange.load_markets()
    except Exception:
        pass

    dongu_sayaci = 0
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5); continue

            # Günlük limit
            gunluk = gunluk_kontrol()
            if gunluk is not None and gunluk <= -MOD['gunluk_max_kayip_pct']:
                telegram_mesaj_gonder(f"🛑 *Günlük zarar limiti!* (%{gunluk*100:.1f})")
                BOT_CALISIYOR_MU = False
                continue

            # Genel cooldown kontrolü (ardışık stop sonrası)
            su_an = time.time()
            if GENEL_COOLDOWN > su_an:
                time.sleep(30)
                continue

            try:
                raw = exchange.fetch_positions()
                aktif_borsa = {p['symbol']: p for p in raw
                               if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0}
            except Exception:
                aktif_borsa = {}

            # Kapanan pozisyonları işle
            for sym in list(AKTIF_POZISYONLAR.keys()):
                if sym not in aktif_borsa:
                    AKTIF_POZISYONLAR.pop(sym, None)
                    basarili = False
                    try:
                        tum_emirleri_iptal_et(sym)
                        closed = exchange.fetch_closed_orders(sym, limit=5)
                        pnl_real = 0.0
                        if closed:
                            son = sorted(closed, key=lambda x: x['timestamp'] or 0)[-2:]
                            for o in son:
                                pnl_real += float(o.get('info', {}).get('pnl', 0) or 0)
                        basarili = pnl_real > 0
                    except Exception: pass
                    
                    if basarili:
                        ANALITIK["basarili_islem_sayisi"] = ANALITIK.get("basarili_islem_sayisi", 0) + 1
                        ARDISIK_STOP_SAYACI = 0  # Kâr → ardışık sayacı sıfırla
                        telegram_mesaj_gonder(f"🎉 *Kâr* → `{sym[:12]}` 🟢")
                    else:
                        ANALITIK["basarisiz_islem_sayisi"] = ANALITIK.get("basarisiz_islem_sayisi", 0) + 1
                        ARDISIK_STOP_SAYACI += 1
                        COIN_COOLDOWNLAR[sym] = time.time() + MOD['cooldown_dk'] * 60
                        telegram_mesaj_gonder(f"❌ *Stop* → `{sym[:12]}` 🔴 (Ardışık: {ARDISIK_STOP_SAYACI})")
                        
                        # Ardışık stop limiti kontrolü
                        if ARDISIK_STOP_SAYACI >= MOD['ardisik_stop_limit']:
                            GENEL_COOLDOWN = time.time() + MOD['ardisik_stop_cooldown_dk'] * 60
                            telegram_mesaj_gonder(
                                f"🛑 *{MOD['ardisik_stop_limit']} ARDIŞIK STOP!*\n"
                                f"Bot {MOD['ardisik_stop_cooldown_dk']} dk durduruluyor.\n"
                                f"Sonra otomatik devam edecek."
                            )
                            ARDISIK_STOP_SAYACI = 0
                    
                    hafizayi_kaydet()

            # Yeni sinyal tarama
            sinyaller = []
            debug = []
            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU: break
                if symbol in aktif_borsa: continue
                if len(aktif_borsa) >= MOD['maks_pozisyon']: break
                if su_an < COIN_COOLDOWNLAR.get(symbol, 0): continue
                
                try:
                    ohlcv_1h = fetch_ohlcv_guvenli(symbol, MOD['zaman_ana'], limit=50)
                    ohlcv_15m = fetch_ohlcv_guvenli(symbol, MOD['zaman_sinyal'], limit=100)
                    if ohlcv_1h is None or ohlcv_15m is None:
                        continue
                    df_1h = pd.DataFrame(ohlcv_1h, columns=['timestamp','open','high','low','close','volume'])
                    df_15m = pd.DataFrame(ohlcv_15m, columns=['timestamp','open','high','low','close','volume'])
                    sig, neden, puan = sinyal_uret(df_15m, df_1h, symbol)
                    if sig:
                        sinyaller.append({"symbol": symbol, **sig})
                        debug.append(f"{symbol.split('/')[0]}:✅{sig['yon']}(p{puan})")
                    else:
                        debug.append(f"{symbol.split('/')[0]}:p{puan}")
                except Exception:
                    continue

            # İşlem aç
            for s in sinyaller:
                if not BOT_CALISIYOR_MU: break
                if len(aktif_borsa) >= MOD['maks_pozisyon']: break
                if pozisyon_ac(s['symbol'], s):
                    aktif_borsa[s['symbol']] = True

            dongu_sayaci += 1
            if dongu_sayaci % 40 == 0:
                ozet = " | ".join(debug[:6])
                print(f"🔍 #{dongu_sayaci} | Poz: {len(AKTIF_POZISYONLAR)} | Ardışık: {ARDISIK_STOP_SAYACI} | {ozet}", flush=True)

        except Exception as e:
            print(f"⚠️ Döngü: {e}", flush=True)
        time.sleep(20)

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
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))
    app_tg.add_handler(CommandHandler("temizle", temizle_komutu))
    app_tg.add_handler(CommandHandler("backtest", backtest_komutu))
    app_tg.add_handler(CommandHandler("test", test_komutu))

    while True:
        try:
            app_tg.run_polling(
                drop_pending_updates=True,
                allowed_updates=Update.ALL_TYPES,
                read_timeout=30,
                write_timeout=30,
                connect_timeout=30,
                pool_timeout=30,
            )
        except Exception as e:
            print(f"⚠️ Telegram hatası: {e} — 5 sn sonra tekrar başlatılıyor...", flush=True)
            time.sleep(5)
