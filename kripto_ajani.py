import os
import time
import threading
import sys
import requests
import ccxt
import pandas as pd
from datetime import datetime, timezone
from flask import Flask, request as flask_request
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

# ==================== SORUNSUZ COİNLER ====================
TAKIP_EDILENLER = [
    'ETH/USDT:USDT', 'SOL/USDT:USDT', 'XRP/USDT:USDT',
    'ADA/USDT:USDT', 'DOGE/USDT:USDT', 'AVAX/USDT:USDT', 'LINK/USDT:USDT'
]

ZAMAN_DILIMI = "1h"
EMA_PERIYOT = 50

MODLAR = {
    "trend_grid": {
        "grid_sayisi": 2,          
        "grid_aralik_pct": 0.02,   
        "kaldirac": 1,              
        "maks_aktif_grid": 3,      
        "bakiye_orani": 0.30,      # Kasayı güvenli bölmek için her seferinde mevcut serbest bakiyenin %30'u baz alınır
    }
}
AKTIF_MOD = "trend_grid"

BOT_CALISIYOR_MU = True
AKTIF_GRIDLER = {} 
COIN_COOLDOWNLAR = {}

def mod_al():
    return MODLAR[AKTIF_MOD]

# ==================== SUPABASE ====================
def hafizayi_yukle():
    varsayilan = {"aktif_gridler": {}, "cooldownlar": {}}
    try:
        r = supabase.table("grid_bot_hafiza").select("*").eq("id", 50).execute()
        if r.data:
            v = r.data[0]
            return {"aktif_gridler": v.get("aktif_gridler", {}), "cooldownlar": v.get("cooldownlar", {})}
    except Exception:
        pass
    try:
        supabase.table("grid_bot_hafiza").upsert({"id": 50, **varsayilan}).execute()
    except Exception:
        pass
    return varsayilan

def hafizayi_kaydet():
    try:
        supabase.table("grid_bot_hafiza").upsert({
            "id": 50,
            "aktif_gridler": AKTIF_GRIDLER,
            "cooldownlar": COIN_COOLDOWNLAR,
        }).execute()
    except Exception:
        pass

kalici = hafizayi_yukle()
AKTIF_GRIDLER = kalici["aktif_gridler"]
COIN_COOLDOWNLAR = kalici["cooldownlar"]

# ==================== YARDIMCI ====================
def telegram_mesaj_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": mesaj}, timeout=10)
    except Exception: pass

def tum_emirleri_iptal_et(symbol):
    try:
        for e in exchange.fetch_open_orders(symbol):
            try: exchange.cancel_order(e['id'], symbol)
            except Exception: pass
    except Exception: pass
    try: exchange.cancel_all_orders(symbol)
    except Exception: pass

def set_leverage_safely(symbol, leverage):
    try:
        exchange.set_leverage(leverage, symbol)
        try: exchange.set_margin_mode('isolated', symbol)
        except Exception: pass
        return True
    except Exception as e:
        print(f"⚠️ Kaldıraç Hatası ({symbol}): {e}", flush=True)
        return False

def fetch_ohlcv_guvenli(symbol, timeframe, limit=100):
    for attempt in range(2):
        try:
            data = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            if data and len(data) > 0:
                return data
        except Exception as e:
            print(f"⚠️ OHLCV Hatası ({symbol}): {e}", flush=True)
            time.sleep(1)
    return None

# ==================== TREND VE SİNYAL ====================
def ema_hesapla(close, period=50):
    return close.ewm(span=period, adjust=False).mean()

def piyasa_analiz_et(df):
    close = df['close']
    son_fiyat = close.iloc[-1]
    ema_val = ema_hesapla(close, EMA_PERIYOT).iloc[-1]
    
    print(f"📊 Analiz -> Fiyat: {son_fiyat} | EMA{EMA_PERIYOT}: {ema_val:.2f}", flush=True)
    if son_fiyat >= ema_val:
        return "LONG_GRID", f"Fiyat ({son_fiyat}) >= EMA{EMA_PERIYOT} ({ema_val:.2f})"
    else:
        return "SHORT_GRID", f"Fiyat ({son_fiyat}) < EMA{EMA_PERIYOT} ({ema_val:.2f})"

# ==================== GRID KURULUMU ====================
def grid_kur(symbol):
    mod = mod_al()
    print(f"🔍 {symbol} taranıyor...", flush=True)
    df_raw = fetch_ohlcv_guvenli(symbol, ZAMAN_DILIMI, limit=100)
    if df_raw is None or len(df_raw) < EMA_PERIYOT:
        print(f"❌ {symbol} için veri yetersiz", flush=True)
        return False, "Veri yetersiz"
    
    df = pd.DataFrame(df_raw, columns=['timestamp','open','high','low','close','volume'])
    yon_tipi, neden = piyasa_analiz_et(df)

    try:
        bal = exchange.fetch_balance()
        kasa = float(bal['free'].get('USDT', 0))
        
        if kasa < 0.5: 
            print(f"❌ Yetersiz Serbest Bakiye: {kasa} USDT", flush=True)
            return False, "Bakiye yetersiz"
        
        if not set_leverage_safely(symbol, mod['kaldirac']):
            return False, "Kaldıraç hatası"

        # Bakiyeyi tam orana böl ve exchange limitlerini dikkate alarak güvenli bütçe hesapla
        tahsis_usdt = kasa * mod['bakiye_orani']
        fiyat = float(df['close'].iloc[-1])
        grid_sayisi = mod['grid_sayisi']
        aralik = mod['grid_aralik_pct']

        tum_emirleri_iptal_et(symbol)
        
        market_info = exchange.market(symbol)
        min_amount = float(market_info['limits']['amount']['min'] or 1.0)
        
        # Her bir grid kademesine düşen bütçeyi kesinlikle anlık serbest bakiyeyi aşmayacak şekilde sınırla
        tekil_butce = max(tahsis_usdt / grid_sayisi, min_amount * fiyat / mod['kaldirac'])
        if tekil_butce * grid_sayisi > kasa * 0.95:
            tekil_butce = (kasa * 0.95) / grid_sayisi

        kademeler = []
        
        if yon_tipi == "LONG_GRID":
            for i in range(grid_sayisi):
                kademe_fiyat = fiyat * (1 - (i + 1) * aralik)
                kademe_fiyat = float(exchange.price_to_precision(symbol, kademe_fiyat))
                
                ham_miktar = tekil_butce / kademe_fiyat
                miktar = max(ham_miktar, min_amount)
                miktar = float(exchange.amount_to_precision(symbol, miktar))
                
                emir = exchange.create_order(symbol, 'limit', 'buy', miktar, kademe_fiyat)
                kademeler.append({"id": emir['id'], "fiyat": kademe_fiyat, "miktar": miktar, "tip": "buy"})
        else:
            for i in range(grid_sayisi):
                kademe_fiyat = fiyat * (1 + (i + 1) * aralik)
                kademe_fiyat = float(exchange.price_to_precision(symbol, kademe_fiyat))
                
                ham_miktar = tekil_butce / kademe_fiyat
                miktar = max(ham_miktar, min_amount)
                miktar = float(exchange.amount_to_precision(symbol, miktar))
                
                emir = exchange.create_order(symbol, 'limit', 'sell', miktar, kademe_fiyat)
                kademeler.append({"id": emir['id'], "fiyat": kademe_fiyat, "miktar": miktar, "tip": "sell"})

        AKTIF_GRIDLER[symbol] = {
            "yon": yon_tipi,
            "ana_fiyat": fiyat,
            "kademeler": kademeler,
            "zaman": int(time.time() * 1000)
        }
        hafizayi_kaydet()

        print(f"✅ BAŞARILI: {symbol} -> {yon_tipi} kuruldu.", flush=True)
        telegram_mesaj_gonder(f"🟢 GRID KURULDU ({yon_tipi})\nSembol: {symbol}\nSebep: {neden}")
        return True, "Başarılı"
    except Exception as e:
        print(f"❌ Emir Oluşturma Hatası ({symbol}): {e}", flush=True)
        return False, str(e)

# ==================== FLASK WEB SERVER ====================
@app.route('/')
def home():
    balance = {}
    try:
        balance = exchange.fetch_balance()
    except Exception:
        pass
    total = float(balance.get('total', {}).get('USDT', 0))
    free = float(balance.get('free', {}).get('USDT', 0))
    return f"Trend Grid Bot Çalışıyor! | Kasa: {total:.2f} USDT (Serbest: {free:.2f}) | Aktif Grid: {len(AKTIF_GRIDLER)}"

@app.route('/tetikle', methods=['GET'])
def manuel_tetikle():
    global BOT_CALISIYOR_MU
    if not BOT_CALISIYOR_MU:
        return "Bot durdurulmuş durumda.", 400
    
    kurulan = 0
    for symbol in TAKIP_EDILENLER:
        if symbol in AKTIF_GRIDLER: continue
        if len(AKTIF_GRIDLER) >= MODLAR[AKTIF_MOD]['maks_aktif_grid']: break
        basarili, _ = grid_kur(symbol)
        if basarili:
            kurulan += 1
            time.sleep(1)
    return f"Tarama tamamlandı. Kurulan yeni grid sayısı: {kurulan}"

@app.route('/kapat', methods=['GET'])
def tumunu_kapat():
    for sym in list(AKTIF_GRIDLER.keys()): 
        tum_emirleri_iptal_et(sym)
    AKTIF_GRIDLER.clear()
    hafizayi_kaydet()
    return "Tüm aktif gridler kapatıldı ve emirler iptal edildi."

# ==================== ANA DÖNGÜ ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU
    try: exchange.load_markets()
    except Exception: pass

    while True:
        try:
            if BOT_CALISIYOR_MU and len(AKTIF_GRIDLER) < MODLAR[AKTIF_MOD]['maks_aktif_grid']:
                for symbol in TAKIP_EDILENLER:
                    if symbol in AKTIF_GRIDLER: continue
                    if len(AKTIF_GRIDLER) >= MODLAR[AKTIF_MOD]['maks_aktif_grid']: break
                    
                    basarili, _ = grid_kur(symbol)
                    if basarili:
                        time.sleep(2)
        except Exception as e:
            print(f"Hata: {e}", flush=True)
        time.sleep(60)

if __name__ == '__main__':
    threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
