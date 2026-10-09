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
from datetime import datetime
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
DINAMIK_LISTE = []
SON_LISTE_GUNCELLEME = 0
LISTE_GUNCELLEME_SURESI = 30 * 60
LISTE_BOYUT = 8

KARA_LISTE = [
    'BTC/USDT:USDT', 'ETH/USDT:USDT', 'AVAX/USDT:USDT',
    'WBTC/USDT:USDT', 'WETH/USDT:USDT',
    'USDC/USDT:USDT', 'USDT/USDT:USDT', 'DAI/USDT:USDT',
    'FDUSD/USDT:USDT', 'TUSD/USDT:USDT', 'BUSD/USDT:USDT'
]

MIN_HACIM_24S = 10_000_000
MIN_DEGISIM_YUZDE = 1.0
MIN_FIYAT = 1.0
MAX_FIYAT = 300.0

BOT_CALISIYOR_MU = True
state_lock = threading.Lock()

# Risk yönetimi
KALDIRAC = 5
POZISYON_MARJ = 0.10
MAKS_POZISYON = 2
SL_YUZDE = 1.5
TP_YUZDE = 3.0
TOPLAM_ZARAR_LIMIT = 1.5
COOLDOWN_SANIYE = 30 * 60

# Gevşetilmiş Order Flow eşikleri
MFI_ESIK_AL = 52
MFI_ESIK_SAT = 48
CMF_ESIK = 0.03
HACIM_ESIK = 1.0
EMA_TREND_ESIK = 0.05
MFI_ASIRI_SATIM = 15
MFI_ASIRI_ALIM = 85
GEREKLI_SINYAL = 5

# Kill-switch
GUNLUK_BASLANGIC_BAKIYE = None
GUNLUK_ZARAR_LIMIT = 0.05

def hafizayi_yukle():
    print("💾 Hafıza yükleniyor...", flush=True)
    try:
        response = supabase.table("bot_hafiza").select("*").eq("id", 1).execute()
        if response.data and len(response.data) > 0:
            veri = response.data[0]
            print("💾 Hafıza yüklendi.", flush=True)
            return {
                "aktif_sistemler": veri.get("aktif_sistemler", {}),
                "analitik": veri.get("analitik", {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0, "egitim_verileri": []}),
                "cooldownlar": veri.get("cooldownlar", {})
            }
    except Exception as e:
        print(f"⚠️ Hafıza: {e}", flush=True)
    
    return {
        "aktif_sistemler": {},
        "analitik": {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0, "egitim_verileri": []},
        "cooldownlar": {}
    }

def hafizayi_kaydet():
    with state_lock:
        try:
            payload = {
                "basarili_islem_sayisi": int(ANALITIK.get("basarili_islem_sayisi", 0)),
                "basarisiz_islem_sayisi": int(ANALITIK.get("basarisiz_islem_sayisi", 0)),
                "egitim_verileri": ANALITIK.get("egitim_verileri", [])
            }
            clean_cd = {}
            for k, v in COIN_COOLDOWNLAR.items():
                if isinstance(v, dict):
                    clean_cd[k] = {"zaman": float(v.get("zaman", 0)), "son_yon": str(v.get("son_yon", ""))}
                else:
                    clean_cd[k] = {"zaman": float(v), "son_yon": ""}
            
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
ANALITIK = kalici.get("analitik", {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0, "egitim_verileri": []})
COIN_COOLDOWNLAR = kalici.get("cooldownlar", {})

# ==================== DİNAMİK COIN HAVUZU ====================
def dinamik_liste_guncelle(zorla=False):
    global DINAMIK_LISTE, SON_LISTE_GUNCELLEME
    
    if not zorla and (time.time() - SON_LISTE_GUNCELLEME < LISTE_GUNCELLEME_SURESI):
        return
    
    print(f"\n🔄 [DİNAMİK LİSTE] Güncelleniyor...", flush=True)
    SON_LISTE_GUNCELLEME = time.time()
    
    try:
        tickers = exchange.fetch_tickers()
        adaylar = []
        
        for sym, t in tickers.items():
            if ':USDT' not in sym: continue
            if sym in KARA_LISTE: continue
            
            try:
                hacim = float(t.get('quoteVolume', 0) or 0)
                degisim = abs(float(t.get('percentage', 0) or 0))
                fiyat = float(t.get('last', 0) or 0)
                
                if hacim < MIN_HACIM_24S: continue
                if degisim < MIN_DEGISIM_YUZDE: continue
                if fiyat < MIN_FIYAT: continue
                if fiyat > MAX_FIYAT: continue
                
                try:
                    market = exchange.market(sym)
                    min_miktar = float(market['limits']['amount']['min'] or 1.0)
                    contract_size = float(market.get('contractSize', 1.0))
                    min_marj = (min_miktar * fiyat * contract_size) / KALDIRAC
                    if min_marj > 15.0: continue
                except:
                    continue
                
                skor = (hacim / 1_000_000) * degisim
                adaylar.append({"symbol": sym, "skor": skor, "degisim": degisim, "hacim": hacim, "fiyat": fiyat})
            except:
                continue
        
        adaylar.sort(key=lambda x: x['skor'], reverse=True)
        yeni_liste = [a['symbol'] for a in adaylar[:LISTE_BOYUT]]
        
        if yeni_liste:
            DINAMIK_LISTE = yeni_liste
            print(f"✅ [DİNAMİK] {len(DINAMIK_LISTE)} coin:", flush=True)
            for a in adaylar[:LISTE_BOYUT]:
                print(f"   • {a['symbol'].replace('/USDT:USDT','')} | %{a['degisim']:.1f} | {a['hacim']/1_000_000:.0f}M", flush=True)
    except Exception as e:
        print(f"⚠️ [DİNAMİK] Hata: {e}", flush=True)

# ==================== ANALİZ FONKSİYONLARI ====================
def coklu_tf_trend(symbol):
    try:
        sonuclar = {}
        for tf, limit, key in [('15m', 30, 'tf15'), ('1h', 50, 'tf60'), ('4h', 40, 'tf240')]:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
            if len(ohlcv) < 20:
                sonuclar[key] = "YATAY"; continue
            df = pd.DataFrame(ohlcv, columns=['t', 'o', 'h', 'l', 'c', 'v'])
            ema9 = ta.trend.ema_indicator(df['c'], window=9).iloc[-1]
            ema21 = ta.trend.ema_indicator(df['c'], window=21).iloc[-1]
            fark = ((ema9 - ema21) / ema21) * 100
            if fark > 0.1: sonuclar[key] = "YUKARI"
            elif fark < -0.1: sonuclar[key] = "ASAGI"
            else: sonuclar[key] = "YATAY"
        return sonuclar
    except:
        return {'tf15': "YATAY", 'tf60': "YATAY", 'tf240': "YATAY"}

def btc_bias():
    try:
        ohlcv = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='1h', limit=50)
        df = pd.DataFrame(ohlcv, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        ema20 = ta.trend.ema_indicator(df['c'], window=20).iloc[-1]
        ema50 = ta.trend.ema_indicator(df['c'], window=50).iloc[-1]
        fiyat = df['c'].iloc[-1]
        if fiyat > ema20 > ema50: return "LONG"
        elif fiyat < ema20 < ema50: return "SHORT"
        else: return "KARISIK"
    except: return "KARISIK"

def momentum_ivmesi(symbol):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=20)
        df = pd.DataFrame(ohlcv, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        closes = df['c'].values
        son3 = abs(closes[-1] - closes[-4])
        onceki3 = abs(closes[-4] - closes[-7])
        if onceki3 == 0: return "SABIT"
        oran = son3 / onceki3
        if oran > 1.3: return "HIZLANIYOR"
        elif oran < 0.5: return "YAVASLIYOR"
        else: return "SABIT"
    except: return "SABIT"

def swing_analizi(symbol):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=30)
        df = pd.DataFrame(ohlcv, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        yuksekler = df['h'].values[-10:]
        dusukler = df['l'].values[-10:]
        son_dusuk = min(dusukler[-5:])
        onceki_dusuk = min(dusukler[:5])
        son_yuksek = max(yuksekler[-5:])
        onceki_yuksek = max(yuksekler[:5])
        yukselen_dip = son_dusuk > onceki_dusuk
        dusen_tepe = son_yuksek < onceki_yuksek
        yukselen_tepe = son_yuksek > onceki_yuksek
        dusen_dip = son_dusuk < onceki_dusuk
        if yukselen_dip and yukselen_tepe: return "YUKARI_TREND"
        elif dusen_dip and dusen_tepe: return "ASAGI_TREND"
        elif yukselen_dip and dusen_tepe: return "SIKISMA"
        else: return "KARISIK"
    except: return "KARISIK"

def destek_direnc_yakinlik(symbol, anlik):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=48)
        df = pd.DataFrame(ohlcv, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        destek = sorted(df['l'].values)[:5]
        direnc = sorted(df['h'].values)[-5:]
        ort_destek = sum(destek) / 5
        ort_direnc = sum(direnc) / 5
        destek_mesafe = ((anlik - ort_destek) / anlik) * 100
        direnc_mesafe = ((ort_direnc - anlik) / anlik) * 100
        return {
            "destek_mesafe": round(destek_mesafe, 2),
            "direnc_mesafe": round(direnc_mesafe, 2),
            "destege_yakin": destek_mesafe < 0.7,
            "direnge_yakin": direnc_mesafe < 0.7
        }
    except:
        return {"destek_mesafe": 999, "direnc_mesafe": 999, "destege_yakin": False, "direnge_yakin": False}

def order_flow_analiz(symbol):
    try:
        ohlcv_1h = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=50)
        if len(ohlcv_1h) < 30: return None, {}
        
        df = pd.DataFrame(ohlcv_1h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        anlik_fiyat = df['close'].iloc[-1]
        
        mfi = ta.volume.money_flow_index(high=df['high'], low=df['low'], close=df['close'], volume=df['volume'], window=14).iloc[-1]
        cmf = ta.volume.chaikin_money_flow(high=df['high'], low=df['low'], close=df['close'], volume=df['volume'], window=20).iloc[-1]
        
        son3_hacim = df['volume'].iloc[-3:].mean()
        ort20_hacim = df['volume'].iloc[-20:].mean()
        hacim_oran = son3_hacim / ort20_hacim if ort20_hacim > 0 else 0
        
        ema20 = ta.trend.ema_indicator(df['close'], window=20).iloc[-1]
        ema20_onceki = ta.trend.ema_indicator(df['close'], window=20).iloc[-5]
        ema_slope = ((ema20 - ema20_onceki) / ema20_onceki) * 100
        ema50 = ta.trend.ema_indicator(df['close'], window=50).iloc[-1]
        
        tf = coklu_tf_trend(symbol)
        tf15 = tf['tf15']; tf60 = tf['tf60']; tf240 = tf['tf240']
        btc_yon = btc_bias()
        momentum = momentum_ivmesi(symbol)
        swing = swing_analizi(symbol)
        dd = destek_direnc_yakinlik(symbol, anlik_fiyat)
        
        detay = {
            "mfi": round(mfi, 1), "cmf": round(cmf, 3), "hacim_oran": round(hacim_oran, 2),
            "ema_slope": round(ema_slope, 3), "fiyat": anlik_fiyat,
            "tf15": tf15, "tf60": tf60, "tf240": tf240, "btc": btc_yon,
            "momentum": momentum, "swing": swing,
            "destek_mesafe": dd['destek_mesafe'], "direnc_mesafe": dd['direnc_mesafe'],
            "destege_yakin": dd['destege_yakin'], "direnge_yakin": dd['direnge_yakin']
        }
        
        if mfi < MFI_ASIRI_SATIM: return None, {**detay, "sebep": f"MFI DİP ({mfi:.0f})"}
        if mfi > MFI_ASIRI_ALIM: return None, {**detay, "sebep": f"MFI TEPE ({mfi:.0f})"}
        if hacim_oran < HACIM_ESIK: return None, {**detay, "sebep": f"HACIM ZAYIF ({hacim_oran:.2f}x)"}
        
        long_kosullar = [
            mfi > MFI_ESIK_AL, cmf > CMF_ESIK, hacim_oran > HACIM_ESIK,
            ema_slope > EMA_TREND_ESIK, anlik_fiyat > ema50,
            tf15 == "YUKARI", tf60 == "YUKARI", tf240 != "ASAGI",
            btc_yon != "SHORT", momentum != "YAVASLIYOR",
            swing != "ASAGI_TREND", not dd['direnge_yakin']
        ]
        
        short_kosullar = [
            mfi < MFI_ESIK_SAT, cmf < -CMF_ESIK, hacim_oran > HACIM_ESIK,
            ema_slope < -EMA_TREND_ESIK, anlik_fiyat < ema50,
            tf15 == "ASAGI", tf60 == "ASAGI", tf240 != "YUKARI",
            btc_yon != "LONG", momentum != "YAVASLIYOR",
            swing != "YUKARI_TREND", not dd['destege_yakin']
        ]
        
        long_say = sum(long_kosullar)
        short_say = sum(short_kosullar)
        sebep = f"L:{long_say}/12 S:{short_say}/12 TF[{tf15}/{tf60}/{tf240}] BTC:{btc_yon} Swing:{swing}"
        
        if long_say >= GEREKLI_SINYAL: return "LONG", {**detay, "sebep": sebep}
        if short_say >= GEREKLI_SINYAL: return "SHORT", {**detay, "sebep": sebep}
        return None, {**detay, "sebep": sebep}
    except Exception as e:
        return None, {"hata": str(e)}

# ==================== YARDIMCI ====================
def telegram_gonder(mesaj, deneme=3):
    if not TELEGRAM_TOKEN or not CHAT_ID: return False
    for i in range(deneme):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": mesaj, "parse_mode": "Markdown"},
                timeout=10
            )
            if r.status_code == 200:
                return True
            else:
                print(f"⚠️ Telegram deneme {i+1} hata: {r.status_code}", flush=True)
        except Exception as e:
            print(f"⚠️ Telegram deneme {i+1}: {e}", flush=True)
        time.sleep(2)
    print(f"🚨 Telegram gönderilemedi: {mesaj[:50]}", flush=True)
    return False

def market_kapat(symbol, miktar, yon):
    kapat_yon = 'sell' if yon == 'LONG' else 'buy'
    try:
        exchange.create_order(symbol, 'market', kapat_yon, miktar, None, {'reduceOnly': True})
        return True
    except Exception as e:
        print(f"⚠️ Kapatma {symbol}: {e}", flush=True)
        return False

def tum_emirleri_iptal(symbol):
    try:
        orders = exchange.fetch_open_orders(symbol)
        for o in orders:
            try: exchange.cancel_order(o['id'], symbol)
            except: pass
    except: pass

def sayaci_artir(basarili_mi):
    with state_lock:
        if basarili_mi:
            ANALITIK["basarili_islem_sayisi"] = int(ANALITIK.get("basarili_islem_sayisi", 0)) + 1
        else:
            ANALITIK["basarisiz_islem_sayisi"] = int(ANALITIK.get("basarisiz_islem_sayisi", 0)) + 1
    hafizayi_kaydet()

def baslangic_temizligi():
    print("\n🧹 [BAŞLANGIÇ TEMİZLİĞİ]...", flush=True)
    try:
        raw_pos = exchange.fetch_positions()
        eski = [p['symbol'] for p in raw_pos if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
        
        if eski:
            print(f"⚠️ Eski pozisyonlar: {eski}", flush=True)
            telegram_gonder(f"⚠️ *TEMİZLİK*\n`{eski}`")
            for p in raw_pos:
                k = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                if k > 0:
                    sym = p['symbol']
                    yon = str(p.get('side', '')).upper() or "LONG"
                    try: exchange.cancel_all_orders(sym)
                    except: pass
                    market_kapat(sym, k, yon)
            with state_lock: AKTIF_SISTEMLER.clear()
            hafizayi_kaydet()
        else:
            print("✅ Temiz başlangıç.", flush=True)
    except Exception as e:
        print(f"⚠️ Temizlik: {e}", flush=True)

# ==================== POZİSYON AÇMA (TP+SL EMİRLİ) ====================
def pozisyon_ac(symbol, yon, anlik_fiyat, sebep):
    global AKTIF_SISTEMLER
    try:
        bakiye = exchange.fetch_balance()
        toplam_bakiye = float(bakiye['total'].get('USDT', 0))
        serbest = float(bakiye.get('free', {}).get('USDT', 0) or 0)
        
        exchange.set_leverage(KALDIRAC, symbol)
        market = exchange.market(symbol)
        contract_size = float(market.get('contractSize', 1.0))
        
        hedef_marj = toplam_bakiye * POZISYON_MARJ
        marj = min(hedef_marj, serbest)
        if marj < 1.0: return False
        
        hesaplanan = (marj * KALDIRAC) / anlik_fiyat / contract_size
        min_miktar = float(market['limits']['amount']['min'] or 1.0)
        
        if hesaplanan < min_miktar:
            zorlanan = (min_miktar * anlik_fiyat * contract_size) / KALDIRAC
            if zorlanan > hedef_marj * 1.5: return False
        
        miktar = float(exchange.amount_to_precision(symbol, max(hesaplanan, min_miktar)))
        gercek_marj = (miktar * anlik_fiyat * contract_size) / KALDIRAC
        if gercek_marj > hedef_marj * 1.5: return False
        
        if yon == "LONG":
            tp = anlik_fiyat * (1 + TP_YUZDE / 100)
            sl = anlik_fiyat * (1 - SL_YUZDE / 100)
            kapat_yon = 'sell'
        else:
            tp = anlik_fiyat * (1 - TP_YUZDE / 100)
            sl = anlik_fiyat * (1 + SL_YUZDE / 100)
            kapat_yon = 'buy'
        
        # Market emriyle pozisyon aç
        islem_y = 'buy' if yon == 'LONG' else 'sell'
        exchange.create_order(symbol, 'market', islem_y, miktar)
        time.sleep(1.0)
        
        # ✅ TP LIMIT order
        tp_ok = False
        try:
            exchange.create_order(
                symbol, 'limit', kapat_yon, miktar, tp,
                {'reduceOnly': True}
            )
            tp_ok = True
            print(f"  ✅ TP emri koyuldu: {tp:.4f}", flush=True)
        except Exception as e:
            print(f"  ⚠️ TP emri koyulamadı: {e}", flush=True)
        
        # ✅ SL STOP order
        sl_ok = False
        try:
            exchange.create_order(
                symbol, 'stop', kapat_yon, miktar, sl,
                {'stopPrice': sl, 'reduceOnly': True}
            )
            sl_ok = True
            print(f"  ✅ SL emri koyuldu: {sl:.4f}", flush=True)
        except Exception as e:
            print(f"  🚨 SL emri koyulamadı: {e}", flush=True)
        
        # SL emri koyulamadıysa → POZİSYONU KAPAT (risk yönetimi)
        if not sl_ok:
            print(f"  🚨 [ACİL] {symbol} SL emri koyulamadı, pozisyon kapatılıyor!", flush=True)
            try:
                market_kapat(symbol, miktar, yon)
            except: pass
            return False
        
        with state_lock:
            AKTIF_SISTEMLER[symbol] = {
                "giris_fiyati": anlik_fiyat, "yon": yon, "giris_zamani": time.time(),
                "sebep": sebep, "tp": tp, "sl": sl, "marj": gercek_marj,
                "miktar": miktar, "tp_order": tp_ok, "sl_order": sl_ok
            }
        
        hafizayi_kaydet()
        print(f"  ✅ [AÇILDI] {symbol} {yon} @ {anlik_fiyat} | TP: {tp:.4f} | SL: {sl:.4f} | Marj: {gercek_marj:.2f}", flush=True)
        
        telegram_gonder(
            f"🎯 *SİNYAL*\n"
            f"📌 `{symbol}` | *{yon}*\n"
            f"📊 {sebep}\n"
            f"🎯 Giriş: `{anlik_fiyat:.4f}`\n"
            f"💰 TP: `{tp:.4f}` | 🛑 SL: `{sl:.4f}`\n"
            f"💵 Marj: `{gercek_marj:.2f}` | Kaldıraç: `{KALDIRAC}x`\n"
            f"📋 TP: {'✅' if tp_ok else '⚠️'} | SL: {'✅' if sl_ok else '🚨'}"
        )
        return True
    except Exception as e:
        print(f"  ⚠️ Pozisyon {symbol}: {e}", flush=True)
        return False

# ==================== MANUEL SL/TP KONTROLÜ ====================
def manuel_sl_tp_kontrol(aktif_borsa_map):
    """Borsa emirleri tetiklenmezse manuel kontrol (yedek)"""
    global AKTIF_SISTEMLER
    
    with state_lock:
        kopya = list(AKTIF_SISTEMLER.items())
    
    for symbol, bilgi in kopya:
        if symbol not in aktif_borsa_map: continue
        
        try:
            ticker = exchange.fetch_ticker(symbol)
            anlik = float(ticker['last'])
        except: continue
        
        yon = bilgi.get("yon", "LONG")
        sl = float(bilgi.get("sl", 0))
        tp = float(bilgi.get("tp", 0))
        miktar = float(bilgi.get("miktar", 0))
        giris = float(bilgi.get("giris_fiyati", 0))
        
        sl_tetiklendi = False
        if yon == "LONG" and anlik <= sl: sl_tetiklendi = True
        elif yon == "SHORT" and anlik >= sl: sl_tetiklendi = True
        
        if sl_tetiklendi:
            # Borsa emri hala duruyorsa iptal et
            tum_emirleri_iptal(symbol)
            # İşaretli PnL
            if yon == "LONG":
                kar = (anlik - giris) * miktar
            else:
                kar = (giris - anlik) * miktar
            
            print(f"  🛑 [MANUEL SL] {symbol} {yon} | PnL: {kar:.4f} USDT", flush=True)
            if market_kapat(symbol, miktar, yon):
                with state_lock:
                    if symbol in AKTIF_SISTEMLER: del AKTIF_SISTEMLER[symbol]
                    COIN_COOLDOWNLAR[symbol] = {"zaman": float(time.time() + COOLDOWN_SANIYE), "son_yon": yon}
                sayaci_artir(False)
                hafizayi_kaydet()
                telegram_gonder(f"🛑 *SL*\n📌 `{symbol}` {yon}\n💵 PnL: `{kar:.4f}` USDT")
            continue
        
        if tp > 0:
            tp_tetiklendi = False
            if yon == "LONG" and anlik >= tp: tp_tetiklendi = True
            elif yon == "SHORT" and anlik <= tp: tp_tetiklendi = True
            
            if tp_tetiklendi:
                tum_emirleri_iptal(symbol)
                if yon == "LONG":
                    kar = (anlik - giris) * miktar
                else:
                    kar = (giris - anlik) * miktar
                
                print(f"  💰 [MANUEL TP] {symbol} {yon} | PnL: +{kar:.4f} USDT", flush=True)
                if market_kapat(symbol, miktar, yon):
                    with state_lock:
                        if symbol in AKTIF_SISTEMLER: del AKTIF_SISTEMLER[symbol]
                        COIN_COOLDOWNLAR[symbol] = {"zaman": float(time.time() + COOLDOWN_SANIYE), "son_yon": yon}
                    sayaci_artir(True)
                    hafizayi_kaydet()
                    telegram_gonder(f"💰 *TP*\n📌 `{symbol}` {yon}\n💵 PnL: `+{kar:.4f}` USDT")

# ==================== BORSA KAPANIŞ TESPİTİ ====================
def kapanan_pozisyonlari_kontrol(aktif_semboller_seti):
    """Borsa emri tetiklendiğinde (TP/SL) hafızadan sil"""
    global AKTIF_SISTEMLER
    
    try:
        anlik_aktif = list(aktif_semboller_seti)
        for eski in list(AKTIF_SISTEMLER.keys()):
            if eski not in anlik_aktif:
                bilgi = AKTIF_SISTEMLER[eski]
                g = float(bilgi.get("giris_fiyati", 0))
                y = bilgi.get("yon", "LONG")
                tp = float(bilgi.get("tp", 0))
                sl = float(bilgi.get("sl", 0))
                miktar = float(bilgi.get("miktar", 0))
                
                # Kâr mı zarar mı?
                karli = False
                pnl = 0
                try:
                    t = exchange.fetch_ticker(eski)
                    cikis = float(t['last'])
                    if y == "LONG":
                        pnl = (cikis - g) * miktar
                        karli = pnl > 0
                    else:
                        pnl = (g - cikis) * miktar
                        karli = pnl > 0
                except: karli = True
                
                sayaci_artir(karli)
                
                with state_lock:
                    COIN_COOLDOWNLAR[eski] = {
                        "zaman": float(time.time() + COOLDOWN_SANIYE),
                        "son_yon": y
                    }
                    if eski in AKTIF_SISTEMLER:
                        del AKTIF_SISTEMLER[eski]
                
                hafizayi_kaydet()
                
                if karli:
                    print(f"  ✅ [KAPANDI-TP] {eski} | +{pnl:.4f} USDT", flush=True)
                    telegram_gonder(f"✅ *KÂRLA KAPANDI*\n📌 `{eski}` {y}\n💵 +{pnl:.4f} USDT")
                else:
                    print(f"  ❌ [KAPANDI-SL] {eski} | {pnl:.4f} USDT", flush=True)
                    telegram_gonder(f"❌ *ZARARLA KAPANDI*\n📌 `{eski}` {y}\n💵 {pnl:.4f} USDT")
    except Exception as e:
        print(f"⚠️ Kapanış kontrolü: {e}", flush=True)

# ==================== ANA DÖNGÜ ====================
def ana_dongu():
    global GUNLUK_BASLANGIC_BAKIYE
    
    print("🚀 [BAŞLANGIÇ] v15.1 - Trend + Emirli Sistem", flush=True)
    print(f"⚙️ Kaldıraç: {KALDIRAC}x | Marj: %{POZISYON_MARJ*100:.0f} | Max Poz: {MAKS_POZISYON}", flush=True)
    print(f"⚙️ SL: %{SL_YUZDE} | TP: %{TP_YUZDE} | Gerekli Sinyal: {GEREKLI_SINYAL}/12", flush=True)
    print(f"⚙️ Eşikler: MFI {MFI_ESIK_SAT}-{MFI_ESIK_AL} | CMF ±{CMF_ESIK} | Hacim {HACIM_ESIK}x", flush=True)
    
    try:
        exchange.load_markets()
        b = exchange.fetch_balance()
        GUNLUK_BASLANGIC_BAKIYE = float(b['total'].get('USDT', 0))
        print(f"💰 Başlangıç: {GUNLUK_BASLANGIC_BAKIYE:.2f} USDT", flush=True)
    except: pass
    
    baslangic_temizligi()
    dinamik_liste_guncelle(zorla=True)
    
    dongu = 0
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5); continue
            
            dongu += 1
            print(f"\n{'='*60}", flush=True)
            print(f"🔄 [DÖNGÜ #{dongu}] {datetime.now().strftime('%H:%M:%S')}", flush=True)
            print(f"{'='*60}", flush=True)
            
            dinamik_liste_guncelle()
            
            # Kasa
            try:
                b = exchange.fetch_balance()
                su_an = float(b['total'].get('USDT', 0))
                if GUNLUK_BASLANGIC_BAKIYE and GUNLUK_BASLANGIC_BAKIYE > 0:
                    gk = (su_an - GUNLUK_BASLANGIC_BAKIYE) / GUNLUK_BASLANGIC_BAKIYE
                    print(f"💰 Kasa: {su_an:.2f} | Günlük: %{gk*100:+.2f}", flush=True)
                    if gk <= -GUNLUK_ZARAR_LIMIT:
                        telegram_gonder(f"🛑 *KILL-SWITCH*")
                        time.sleep(3600)
                        GUNLUK_BASLANGIC_BAKIYE = su_an
                        continue
            except: pass
            
            # Pozisyonlar
            try:
                raw_pos = exchange.fetch_positions()
                aktif_borsa_map = {}
                aktif_semboller_seti = set()
                for p in raw_pos:
                    k = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                    if k > 0:
                        aktif_borsa_map[p['symbol']] = p
                        aktif_semboller_seti.add(p['symbol'])
                print(f"📌 Açık: {list(aktif_semboller_seti) if aktif_semboller_seti else 'YOK'}", flush=True)
            except:
                aktif_borsa_map = {}
                aktif_semboller_seti = set()
            
            # ✅ Açık pozisyonları takip et (borsa emir tetiklenmesi + manuel SL/TP)
            if aktif_semboller_seti:
                manuel_sl_tp_kontrol(aktif_borsa_map)
                # Borsa emri tetiklendi mi?
                kapanan_pozisyonlari_kontrol(aktif_semboller_seti)
            
            # Toplam zarar
            try:
                toplam_acik_zarar = sum(float(p.get('unrealizedPnl', 0)) for p in raw_pos 
                                       if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0)
                if toplam_acik_zarar < -TOPLAM_ZARAR_LIMIT:
                    telegram_gonder(f"🛑 *TOPLAM ZARAR* {toplam_acik_zarar:.2f}")
                    for p in raw_pos:
                        k = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                        if k > 0:
                            y = str(p.get('side', '')).upper() or "LONG"
                            market_kapat(p['symbol'], k, y)
                    with state_lock: AKTIF_SISTEMLER.clear()
                    hafizayi_kaydet()
                    time.sleep(600)
                    continue
            except: pass
            
            # Limit kontrolü
            if len(aktif_semboller_seti) >= MAKS_POZISYON:
                print(f"  ⛔ Limit dolu ({len(aktif_semboller_seti)}/{MAKS_POZISYON})", flush=True)
                time.sleep(10)
                continue
            
            # ✅ Sürekli sinyal tara
            print(f"\n🔍 SİNYAL TARAMA ({len(DINAMIK_LISTE)} coin):", flush=True)
            for symbol in DINAMIK_LISTE:
                if not BOT_CALISIYOR_MU: break
                if symbol in aktif_semboller_seti: continue
                if len(aktif_semboller_seti) >= MAKS_POZISYON: break
                
                with state_lock:
                    cd = COIN_COOLDOWNLAR.get(symbol)
                    if cd:
                        z = cd.get("zaman", 0) if isinstance(cd, dict) else float(cd)
                        if z > time.time():
                            kalan = int((z - time.time()) / 60)
                            print(f"  ⏳ {symbol.replace('/USDT:USDT','')}: Cooldown ({kalan} dk)", flush=True)
                            continue
                
                karar, detay = order_flow_analiz(symbol)
                
                if "hata" in detay:
                    continue
                
                sym_kisa = symbol.replace('/USDT:USDT', '')
                mfi = detay.get('mfi', 0)
                cmf = detay.get('cmf', 0)
                hacim = detay.get('hacim_oran', 0)
                tf15 = detay.get('tf15', '?')
                tf60 = detay.get('tf60', '?')
                tf240 = detay.get('tf240', '?')
                btc = detay.get('btc', '?')
                
                print(f"  {sym_kisa}: MFI:{mfi:.0f} CMF:{cmf:.2f} Hacim:{hacim:.2f}x | TF[15m:{tf15} 1h:{tf60} 4h:{tf240}] | BTC:{btc} | {detay.get('sebep', '')}", flush=True)
                
                if karar is None:
                    continue
                
                print(f"  🎯 [SİNYAL] {sym_kisa}: {karar}", flush=True)
                
                ticker = exchange.fetch_ticker(symbol)
                anlik_fiyat = float(ticker['last'])
                
                sonuc = pozisyon_ac(symbol, karar, anlik_fiyat, detay.get('sebep', ''))
                if sonuc:
                    aktif_semboller_seti.add(symbol)
                    break
            
        except Exception as e:
            print(f"⚠️ Ana döngü: {e}", flush=True)
            import traceback
            traceback.print_exc()
        
        time.sleep(10)

# ==================== TELEGRAM ====================
async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    try:
        balance = await asyncio.to_thread(exchange.fetch_balance)
        total = float(balance['total'].get('USDT', 0))
        pos = [p for p in await asyncio.to_thread(exchange.fetch_positions) if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
        pnl = sum(float(p.get('unrealizedPnl', 0)) for p in pos)
        
        bas = int(ANALITIK.get("basarili_islem_sayisi", 0))
        basz = int(ANALITIK.get("basarisiz_islem_sayisi", 0))
        top = bas + basz
        oran = (bas / top * 100) if top > 0 else 0
        
        pos_detay = ""
        if pos:
            pos_detay = "\n\n📌 *AÇIK POZİSYONLAR:*"
            for p in pos:
                sym = p['symbol'].replace('/USDT:USDT', '')
                yon = str(p.get('side', '')).upper() or "LONG"
                giris = float(p.get('entryPrice', 0))
                marj = float(p.get('initialMargin', 0) or 0)
                
                try:
                    t = await asyncio.to_thread(exchange.fetch_ticker, p['symbol'])
                    guncel = float(t['last'])
                except: guncel = giris
                
                kaldirac_p = int(p.get('leverage', 1))
                if yon == "LONG":
                    roe = ((guncel - giris) / giris) * 100 * kaldirac_p
                else:
                    roe = ((giris - guncel) / giris) * 100 * kaldirac_p
                
                if roe >= 0.5: emoji = "🟢"
                elif roe >= -1: emoji = "🟡"
                else: emoji = "🔴"
                
                sl_bilgi = ""
                if p['symbol'] in AKTIF_SISTEMLER:
                    bilgi = AKTIF_SISTEMLER[p['symbol']]
                    sl_bilgi = f"\n  🛑 SL: `{bilgi.get('sl', 0):.4f}` | 💰 TP: `{bilgi.get('tp', 0):.4f}`"
                
                pos_detay += (
                    f"\n{emoji} `{sym}` | *{yon}* ({kaldirac_p}x){sl_bilgi}\n"
                    f"  Giriş: `{giris}` → Anlık: `{guncel}`\n"
                    f"  Marj: `{marj:.2f}` | ROE: `%{roe:+.2f}` | PnL: `{float(p.get('unrealizedPnl', 0)):+.3f}`"
                )
        else:
            pos_detay = "\n\n📌 Açık pozisyon yok."
        
        liste_str = "\n".join([f"• `{s.replace('/USDT:USDT','')}`" for s in DINAMIK_LISTE[:8]]) if DINAMIK_LISTE else "Boş"
        
        mesaj = (
            f"📊 *TREND BOT (v15.1)*\n\n"
            f"💰 Kasa: `{total:.2f} USDT`\n"
            f"💵 Toplam PnL: `{pnl:+.2f} USDT`\n"
            f"📌 Açık: `{len(pos)}` / `{MAKS_POZISYON}`"
            f"{pos_detay}\n\n"
            f"📋 *Coin Havuzu:*\n{liste_str}\n\n"
            f"✅ TP: `{bas}` | ❌ SL: `{basz}`\n"
            f"📈 Başarı: `%{oran:.1f}`"
        )
        
        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    global BOT_CALISIYOR_MU, GUNLUK_BASLANGIC_BAKIYE
    BOT_CALISIYOR_MU = True
    try:
        b = await asyncio.to_thread(exchange.fetch_balance)
        GUNLUK_BASLANGIC_BAKIYE = float(b['total'].get('USDT', 0))
    except: pass
    await update.message.reply_text("🟢 Trend Bot (v15.1) aktif!")

async def durdur_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ Durduruldu.")

async def kapat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    try:
        positions = await asyncio.to_thread(exchange.fetch_positions)
        kapatilan = 0
        for p in positions:
            k = float(p.get('contracts', 0) or p.get('size', 0) or 0)
            if k > 0:
                y = str(p.get('side', '')).upper() or "LONG"
                try: exchange.cancel_all_orders(p['symbol'])
                except: pass
                if market_kapat(p['symbol'], k, y):
                    kapatilan += 1
        
        with state_lock:
            AKTIF_SISTEMLER.clear()
        hafizayi_kaydet()
        
        await update.message.reply_text(f"✅ {kapatilan} pozisyon kapatıldı.")
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

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
    
    await app_tg.initialize()
    await app_tg.start()
    await app_tg.updater.start_polling(drop_pending_updates=True)
    
    t = threading.Thread(target=ana_dongu, daemon=True)
    t.start()
    
    stop = asyncio.Event()
    await stop.wait()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
