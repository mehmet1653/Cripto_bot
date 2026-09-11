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

# ==================== 6 COİN ====================
TAKIP_EDILENLER = [
    'SOL/USDT:USDT', 'XRP/USDT:USDT', 'BNB/USDT:USDT',
    'ETH/USDT:USDT', 'AVAX/USDT:USDT', 'ADA/USDT:USDT'
]

# ==================== SABİT KURGU ====================
# Kurgu Bulucu'nun bulduğu en iyi kurgu
KURGU = {
    "yon": "LONG_SHORT",
    "strateji": "mean_reversion",
    "gosterge": "bollinger",
    "cikis": "fixed_tp",
    "zaman": "1h",
}
KURGU_ADI = "LONG_SHORT+mean_reversion+bollinger+fixed_tp+1h"

# ==================== RİSK ====================
RISK = {
    "islem_riski_pct": 0.02,      # %2
    "kaldirac": 5,                # 5x
    "maks_pozisyon": 3,           # 3 pozisyon (6 coinden)
    "cooldown_dk": 30,            # 30 dk cooldown
    "gunluk_max_kayip_pct": 0.05, # %5 günlük limit
}

# ==================== STRATEJİ PARAMETRELERİ ====================
STRATEJI_PARAMS = {
    "bollinger_period": 20,
    "bollinger_std": 2.0,
    "rsi_period": 14,
    "atr_period": 14,
    "atr_stop_mult": 1.5,
    "min_stop_pct": 0.008,
    "max_stop_pct": 0.030,
    "risk_reward": 2.0,
    "hacim_min": 0.5,
}

# ==================== DURUMLAR ====================
BOT_CALISIYOR_MU = True
GUN_BASI_KASA = None
GUN_BASI_TARIH = None
AKTIF_POZISYONLAR = {}
COIN_COOLDOWNLAR = {}  # {coin: timestamp}
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
    }
    try:
        r = supabase.table("bot_hafiza").select("*").eq("id", 30).execute()
        if r.data:
            v = r.data[0]
            return {
                "aktif_pozisyonlar": v.get("aktif_pozisyonlar", {}),
                "cooldownlar": v.get("cooldownlar", {}),
                "analitik": v.get("analitik", ANALITIK.copy()),
            }
    except Exception:
        pass
    try:
        supabase.table("bot_hafiza").upsert({"id": 30, **varsayilan}).execute()
    except Exception:
        pass
    return varsayilan

def hafizayi_kaydet():
    try:
        supabase.table("bot_hafiza").upsert({
            "id": 30,
            "aktif_pozisyonlar": AKTIF_POZISYONLAR,
            "cooldownlar": COIN_COOLDOWNLAR,
            "analitik": ANALITIK,
        }).execute()
    except Exception:
        pass

kalici = hafizayi_yukle()
AKTIF_POZISYONLAR = kalici["aktif_pozisyonlar"]
COIN_COOLDOWNLAR = kalici["cooldownlar"]
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

def bollinger_hesapla(close, period=20, std=2.0):
    sma = close.rolling(period).mean()
    std_dev = close.rolling(period).std()
    return sma, sma + (std_dev * std), sma - (std_dev * std)

def hacim_orani(df, period=20):
    try:
        ort = df['volume'].rolling(period).mean().iloc[-1]
        son = df['volume'].iloc[-1]
        return float(son / ort) if ort > 0 else 1.0
    except Exception:
        return 1.0

# ==================== SİNYAL ÜRETİCİ ====================
def sinyal_uret(df):
    """
    Sabit kurgu:
    - LONG_SHORT: hem long hem short
    - mean_reversion: aşırı uçlardan dönüş
    - bollinger: Bollinger bandı giriş
    - fixed_tp: sabit TP, sabit stop
    """
    if len(df) < 60:
        return None
    
    close = df['close']
    open_ = df['open']
    fiyat = close.iloc[-1]
    son_open = open_.iloc[-1]
    
    # Bollinger
    sma, ust_bb, alt_bb = bollinger_hesapla(close, STRATEJI_PARAMS['bollinger_period'], STRATEJI_PARAMS['bollinger_std'])
    son_ust = ust_bb.iloc[-1]
    son_alt = alt_bb.iloc[-1]
    
    # RSI (mean reversion teyidi)
    rsi = rsi_hesapla(close, STRATEJI_PARAMS['rsi_period']).iloc[-1]
    
    # ATR (stop hesabı)
    atr = atr_hesapla(df, STRATEJI_PARAMS['atr_period']).iloc[-1]
    
    # Hacim
    h_orani = hacim_orani(df, 20)
    
    if pd.isna(son_ust) or pd.isna(son_alt) or pd.isna(rsi) or pd.isna(atr) or atr == 0:
        return None
    
    # ATR aralık kontrolü
    atr_pct = (atr / fiyat) * 100
    if not (0.3 <= atr_pct <= 6.0):
        return None
    
    # Hacim kontrolü
    if h_orani < STRATEJI_PARAMS['hacim_min']:
        return None
    
    # Stop/TP
    stop_pct = max(STRATEJI_PARAMS['min_stop_pct'],
                   min(STRATEJI_PARAMS['max_stop_pct'],
                       (atr_pct * STRATEJI_PARAMS['atr_stop_mult']) / 100.0))
    tp_pct = stop_pct * STRATEJI_PARAMS['risk_reward']
    
    # LONG sinyal: Fiyat alt banda değdi + RSI düşük + yeşil mum
    if fiyat <= son_alt and rsi < 35 and fiyat > son_open:
        return {
            "yon": "LONG", "giris": float(fiyat),
            "stop_pct": stop_pct, "tp_pct": tp_pct,
            "rsi": float(rsi), "atr_pct": atr_pct
        }
    
    # SHORT sinyal: Fiyat üst banda değdi + RSI yüksek + kırmızı mum
    if fiyat >= son_ust and rsi > 65 and fiyat < son_open:
        return {
            "yon": "SHORT", "giris": float(fiyat),
            "stop_pct": stop_pct, "tp_pct": tp_pct,
            "rsi": float(rsi), "atr_pct": atr_pct
        }
    
    return None

# ==================== POZİSYON AÇMA ====================
def pozisyon_ac(symbol, sig):
    """Pozisyon aç."""
    try:
        bal = exchange.fetch_balance()
        kasa = float(bal['total'].get('USDT', 0))
        if kasa < 10:
            return False
        if not set_leverage_and_margin_safely(symbol, RISK['kaldirac']):
            return False

        risk_usdt = kasa * RISK['islem_riski_pct']
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
            stop = giris * (1 - stop_pct)
            tp = giris * (1 + tp_pct)
            kapat_yon = 'sell'
        else:
            stop = giris * (1 + stop_pct)
            tp = giris * (1 - tp_pct)
            kapat_yon = 'buy'

        stop = float(exchange.price_to_precision(symbol, stop))
        tp = float(exchange.price_to_precision(symbol, tp))

        # Stop-loss emri
        try:
            exchange.create_order(symbol, 'stop', kapat_yon, miktar, stop,
                {'stopPrice': stop, 'triggerPrice': stop, 'reduceOnly': True})
        except Exception:
            try:
                exchange.create_order(symbol, 'stop_market', kapat_yon, miktar, stop,
                    {'stopPrice': stop, 'triggerPrice': stop, 'reduceOnly': True})
            except Exception: pass

        # TP emri
        try:
            exchange.create_order(symbol, 'limit', kapat_yon, miktar, tp, {'reduceOnly': True})
        except Exception: pass

        AKTIF_POZISYONLAR[symbol] = {
            "yon": yon, "giris": giris, "stop": stop, "tp": tp,
            "miktar": miktar,
            "giris_zaman": int(time.time()*1000)
        }
        COIN_COOLDOWNLAR[symbol] = time.time() + RISK['cooldown_dk'] * 60
        hafizayi_kaydet()

        telegram_mesaj_gonder(
            f"🎯 *İŞLEM AÇILDI*\n"
            f"📌 `{symbol[:12]}` | *{yon}*\n"
            f"💰 Giriş: `{giris}` | SL: `{stop}` | TP: `{tp}`\n"
            f"📊 RSI: `{sig['rsi']:.1f}` | Risk: `{risk_usdt:.2f}` USDT"
        )
        return True
    except Exception as e:
        print(f"❌ Pozisyon açma: {e}", flush=True)
        return False

# ==================== FLASK ====================
@app.route('/')
def home():
    return f"Kurgu Bot | Kurgu: {KURGU_ADI} | Poz: {len(AKTIF_POZISYONLAR)}"

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

        detay = ""
        if poslari:
            detay = "\n📋 *Aktif Pozisyonlar:*\n"
            for p in poslari:
                detay += f"• `{p.get('symbol')[:12]}` | {str(p.get('side','')).upper()} | `{float(p.get('unrealizedPnl',0)):+.2f}`\n"

        mesaj = (
            f"🎯 *SABİT KURGU BOTU*\n\n"
            f"🏗️ Kurgu: `{KURGU_ADI}`\n\n"
            f"💰 Toplam: `{total:.2f}` USDT (Serbest: `{free:.2f}`)\n"
            f"📈 PnL: `{pnl:+.2f}` USDT\n"
            f"📌 Pozisyon: `{len(poslari)}/{RISK['maks_pozisyon']}`\n\n"
            f"✅ TP: `{bs}` | ❌ Stop: `{bz}` | Başarı: `%{oran:.1f}`\n"
            f"{detay}"
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
    global AKTIF_POZISYONLAR, COIN_COOLDOWNLAR
    AKTIF_POZISYONLAR = {}
    COIN_COOLDOWNLAR = {}
    hafizayi_kaydet()
    await update.message.reply_text("🗑️ *Temizlendi.*", parse_mode='Markdown')

async def kurgu_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mesaj = (
        f"🏗️ *SABİT KURGU*\n\n"
        f"📛 Kurgu Adı: `{KURGU_ADI}`\n\n"
        f"*Kurgu Parçaları:*\n"
        f"• Yön: `{KURGU['yon']}` (hem LONG hem SHORT)\n"
        f"• Strateji: `{KURGU['strateji']}` (aşırı uç dönüşü)\n"
        f"• Gösterge: `{KURGU['gosterge']}` (Bollinger bandı)\n"
        f"• Çıkış: `{KURGU['cikis']}` (sabit TP)\n"
        f"• Zaman: `{KURGU['zaman']}` (1 saatlik)\n"
        f"• Coin: {len(TAKIP_EDILENLER)} adet\n\n"
        f"*Backtest Sonucu (bulunurken):*\n"
        f"• PF: 3.276\n"
        f"• Win: 57.74%\n"
        f"• DD: -2.37%\n\n"
        f"*Nasıl Çalışır:*\n"
        f"• Fiyat Bollinger alt bandına değer + RSI < 35 + yeşil mum → LONG\n"
        f"• Fiyat Bollinger üst bandına değer + RSI > 65 + kırmızı mum → SHORT\n"
        f"• Stop: ATR × 1.5 (min %0.8, max %3)\n"
        f"• TP: Stop × 2\n"
        f"• Cooldown: 30 dk"
    )
    await update.message.reply_text(mesaj, parse_mode='Markdown')

async def test_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Aktif kurguyu 6 coinde test et - hızlı backtest."""
    await update.message.reply_text("⏳ *6 coinde test ediliyor...* (30 sn)", parse_mode='Markdown')
    
    def run():
        try:
            satirlar = [f"📊 *KURGU 6 COİNDE TEST*\n`{KURGU_ADI}`\n```"]
            satirlar.append(f"{'COIN':<8} {'İŞL':>4} {'WIN%':>6} {'PF':>6} {'TOT%':>7}")
            satirlar.append("-" * 40)
            
            for coin in TAKIP_EDILENLER:
                df_raw = fetch_ohlcv_guvenli(coin, KURGU['zaman'], limit=1000)
                if df_raw is None or len(df_raw) < 200:
                    satirlar.append(f"{coin.split('/')[0]:<8} HATA")
                    continue
                df = pd.DataFrame(df_raw, columns=['timestamp','open','high','low','close','volume'])
                
                # Backtest
                trades = []
                poz = None
                for i in range(60, len(df)):
                    bar = df.iloc[i]
                    high = bar['high']; low = bar['low']
                    
                    if poz is not None:
                        if poz['yon'] == 'LONG':
                            if low <= poz['stop']:
                                trades.append({**poz, 'cikis': poz['stop']}); poz = None
                            elif high >= poz['tp']:
                                trades.append({**poz, 'cikis': poz['tp']}); poz = None
                        else:
                            if high >= poz['stop']:
                                trades.append({**poz, 'cikis': poz['stop']}); poz = None
                            elif low <= poz['tp']:
                                trades.append({**poz, 'cikis': poz['tp']}); poz = None
                        continue
                    
                    df_slice = df.iloc[max(0, i-100):i+1].reset_index(drop=True)
                    try:
                        sig = sinyal_uret(df_slice)
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
                    satirlar.append(f"{coin.split('/')[0]:<8} {'0':>4}")
                    continue
                
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
                tot = (equity[-1] - 1) * 100 if len(equity) else 0
                
                satirlar.append(
                    f"{coin.split('/')[0]:<8} {len(trades):>4} {win:>6.1f} {pf:>6.2f} {tot:>7.2f}"
                )
            
            satirlar.append("```")
            telegram_mesaj_gonder("\n".join(satirlar))
        except Exception as e:
            telegram_mesaj_gonder(f"❌ Test hatası: {e}")
    
    threading.Thread(target=run, daemon=True).start()

# ==================== ANA DÖNGÜ ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, ANALITIK

    print(f"🎯 [SABİT KURGU BOTU] Başladı", flush=True)
    print(f"🏗️ Kurgu: {KURGU_ADI}", flush=True)
    print(f"💰 Coin: {len(TAKIP_EDILENLER)} adet", flush=True)

    try:
        exchange.load_markets()
    except Exception:
        pass

    dongu_sayaci = 0
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5)
                continue

            # Günlük limit kontrolü
            gunluk = gunluk_kontrol()
            if gunluk is not None:
                if gunluk <= -RISK['gunluk_max_kayip_pct']:
                    telegram_mesaj_gonder(f"🛑 *Günlük zarar limiti!* (%{gunluk*100:.1f})")
                    BOT_CALISIYOR_MU = False
                    continue

            # Açık pozisyon kontrolü
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
                    except Exception:
                        pass

                    if basarili:
                        ANALITIK["basarili_islem_sayisi"] = ANALITIK.get("basarili_islem_sayisi", 0) + 1
                        telegram_mesaj_gonder(f"🎉 *Kâr* → `{sym[:12]}` 🟢")
                    else:
                        ANALITIK["basarisiz_islem_sayisi"] = ANALITIK.get("basarisiz_islem_sayisi", 0) + 1
                        telegram_mesaj_gonder(f"❌ *Stop* → `{sym[:12]}` 🔴")
                    hafizayi_kaydet()

            # Yeni sinyal kontrolü
            su_an = time.time()
            sinyaller = []
            debug = []
            
            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU:
                    break
                if symbol in aktif_borsa:
                    continue
                if len(aktif_borsa) >= RISK['maks_pozisyon']:
                    break
                if su_an < COIN_COOLDOWNLAR.get(symbol, 0):
                    continue
                
                try:
                    df_raw = fetch_ohlcv_guvenli(symbol, KURGU['zaman'], limit=200)
                    if df_raw is None or len(df_raw) < 100:
                        continue
                    df = pd.DataFrame(df_raw, columns=['timestamp','open','high','low','close','volume'])
                    sig = sinyal_uret(df)
                    if sig:
                        sinyaller.append({"symbol": symbol, **sig})
                        debug.append(f"{symbol.split('/')[0]}:✅{sig['yon']}")
                    else:
                        debug.append(f"{symbol.split('/')[0]}:yok")
                except Exception:
                    continue

            # İşlem aç
            for s in sinyaller:
                if not BOT_CALISIYOR_MU:
                    break
                if len(aktif_borsa) >= RISK['maks_pozisyon']:
                    break
                if pozisyon_ac(s['symbol'], s):
                    aktif_borsa[s['symbol']] = True

            dongu_sayaci += 1
            if dongu_sayaci % 40 == 0:
                ozet = " | ".join(debug[:6])
                print(f"🔍 #{dongu_sayaci} | Poz: {len(AKTIF_POZISYONLAR)} | {ozet}", flush=True)

        except Exception as e:
            print(f"⚠️ Döngü hatası: {e}", flush=True)

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
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))
    app_tg.add_handler(CommandHandler("temizle", temizle_komutu))
    app_tg.add_handler(CommandHandler("kurgu", kurgu_komutu))
    app_tg.add_handler(CommandHandler("test", test_komutu))

    app_tg.run_polling(drop_pending_updates=True)
