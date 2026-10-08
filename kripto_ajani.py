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
TAKIP_EDILENLER = [
    'XRP/USDT:USDT', 
    'DOGE/USDT:USDT', 
    'LTC/USDT:USDT', 
    'LINK/USDT:USDT'
]

BOT_CALISIYOR_MU = True
state_lock = threading.Lock()

# ✅ Risk yönetimi (v12.2)
KALDIRAC = 5
POZISYON_MARJ = 0.10            # ✅ 0.05 -> 0.10 (kâr 2x)
MAKS_POZISYON = 2
SL_ATR = 2.5
TP_ATR = 4.0
TOPLAM_ZARAR_LIMIT = 1.5
COOLDOWN_SANIYE = 30 * 60

# ✅ Order Flow eşikleri
MFI_ESIK_AL = 55
MFI_ESIK_SAT = 45
CMF_ESIK = 0.05
HACIM_ESIK = 1.2
EMA_TREND_ESIK = 0.1

# ✅ v12.2: MFI AŞIRI UÇ FİLTRESİ
MFI_ASIRI_SATIM = 25            # MFI < 25 → SHORT açma (dip)
MFI_ASIRI_ALIM = 75             # MFI > 75 → LONG açma (tepe)

# ✅ Kill-switch
GUNLUK_BASLANGIC_BAKIYE = None
GUNLUK_ZARAR_LIMIT = 0.05
KILL_SWITCH_AKTIF = False

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

# ==================== ORDER FLOW ANALİZİ (v12.2) ====================
def order_flow_analiz(symbol):
    try:
        ohlcv_1h = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=50)
        if len(ohlcv_1h) < 30:
            return None, {}
        
        df = pd.DataFrame(ohlcv_1h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        mfi = ta.volume.money_flow_index(
            high=df['high'], low=df['low'], close=df['close'], volume=df['volume'],
            window=14
        ).iloc[-1]
        
        cmf = ta.volume.chaikin_money_flow(
            high=df['high'], low=df['low'], close=df['close'], volume=df['volume'],
            window=20
        ).iloc[-1]
        
        son3_hacim = df['volume'].iloc[-3:].mean()
        ort20_hacim = df['volume'].iloc[-20:].mean()
        hacim_oran = son3_hacim / ort20_hacim if ort20_hacim > 0 else 0
        
        ema20 = ta.trend.ema_indicator(df['close'], window=20).iloc[-1]
        ema20_onceki = ta.trend.ema_indicator(df['close'], window=20).iloc[-5]
        ema_slope = ((ema20 - ema20_onceki) / ema20_onceki) * 100
        
        ema50 = ta.trend.ema_indicator(df['close'], window=50).iloc[-1]
        anlik_fiyat = df['close'].iloc[-1]
        
        detay = {
            "mfi": round(mfi, 1),
            "cmf": round(cmf, 3),
            "hacim_oran": round(hacim_oran, 2),
            "ema_slope": round(ema_slope, 3),
            "fiyat": anlik_fiyat,
            "ema20": round(ema20, 4),
            "ema50": round(ema50, 4)
        }
        
        # ✅ v12.2: MFI AŞIRI UÇ FİLTRESİ
        if mfi < MFI_ASIRI_SATIM:
            return None, {**detay, "sebep": f"MFI AŞIRI SATIM ({mfi:.0f}<{MFI_ASIRI_SATIM}) - SHORT açılmıyor, dönüş riski"}
        
        if mfi > MFI_ASIRI_ALIM:
            return None, {**detay, "sebep": f"MFI AŞIRI ALIM ({mfi:.0f}>{MFI_ASIRI_ALIM}) - LONG açılmıyor, düşüş riski"}
        
        # Hacim zayıfsa bekle
        if hacim_oran < HACIM_ESIK:
            return None, {**detay, "sebep": f"HACIM ZAYIF ({hacim_oran:.2f}x)"}
        
        long_kosullar = [
            mfi > MFI_ESIK_AL,
            cmf > CMF_ESIK,
            hacim_oran > HACIM_ESIK,
            ema_slope > EMA_TREND_ESIK,
            anlik_fiyat > ema50
        ]
        
        short_kosullar = [
            mfi < MFI_ESIK_SAT,
            cmf < -CMF_ESIK,
            hacim_oran > HACIM_ESIK,
            ema_slope < -EMA_TREND_ESIK,
            anlik_fiyat < ema50
        ]
        
        if all(long_kosullar):
            return "LONG", {**detay, "sebep": f"MFI:{mfi:.0f} CMF:{cmf:.2f} Hacim:{hacim_oran:.2f}x EMA:{ema_slope:+.2f}%"}
        
        if all(short_kosullar):
            return "SHORT", {**detay, "sebep": f"MFI:{mfi:.0f} CMF:{cmf:.2f} Hacim:{hacim_oran:.2f}x EMA:{ema_slope:+.2f}%"}
        
        long_say = sum(long_kosullar)
        short_say = sum(short_kosullar)
        
        if long_say >= 3 and mfi > 55:
            return None, {**detay, "sebep": f"KISMİ LONG ({long_say}/5)"}
        if short_say >= 3 and mfi < 45:
            return None, {**detay, "sebep": f"KISMİ SHORT ({short_say}/5)"}
        
        return None, {**detay, "sebep": f"SİNYAL YOK (L:{long_say}/5 S:{short_say}/5)"}
        
    except Exception as e:
        return None, {"hata": str(e)}

# ==================== YARDIMCI ====================
def telegram_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": mesaj, "parse_mode": "Markdown"}, timeout=5)
    except: pass

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
    print("\n🧹 [BAŞLANGIÇ TEMİZLİĞİ] Eski pozisyonlar kontrol ediliyor...", flush=True)
    try:
        raw_pos = exchange.fetch_positions()
        eski_pozisyonlar = []
        for p in raw_pos:
            k = float(p.get('contracts', 0) or p.get('size', 0) or 0)
            if k > 0:
                eski_pozisyonlar.append(p['symbol'])
        
        if eski_pozisyonlar:
            print(f"⚠️ Eski pozisyonlar bulundu: {eski_pozisyonlar}", flush=True)
            telegram_gonder(f"⚠️ *BAŞLANGIÇ TEMİZLİĞİ*\nEski pozisyonlar: `{eski_pozisyonlar}`")
            
            for p in raw_pos:
                k = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                if k > 0:
                    sym = p['symbol']
                    yon = str(p.get('side', '')).upper() or "LONG"
                    try: exchange.cancel_all_orders(sym)
                    except: pass
                    market_kapat(sym, k, yon)
            
            with state_lock:
                AKTIF_SISTEMLER.clear()
            hafizayi_kaydet()
            print(f"✅ Eski pozisyonlar kapatıldı.", flush=True)
        else:
            print("✅ Eski pozisyon yok, temiz başlangıç.", flush=True)
    except Exception as e:
        print(f"⚠️ Başlangıç temizliği: {e}", flush=True)

# ==================== İŞLEM AÇMA ====================
def pozisyon_ac(symbol, yon, anlik_fiyat, atr, sebep):
    global AKTIF_SISTEMLER
    
    try:
        bakiye = exchange.fetch_balance()
        toplam_bakiye = float(bakiye['total'].get('USDT', 0))
        serbest_bakiye = float(bakiye.get('free', {}).get('USDT', 0) or 0)
        
        exchange.set_leverage(KALDIRAC, symbol)
        market = exchange.market(symbol)
        contract_size = float(market.get('contractSize', 1.0))
        
        hedef_marj = toplam_bakiye * POZISYON_MARJ
        marj = min(hedef_marj, serbest_bakiye)
        if marj < 1.0:
            print(f"  ⚠️ {symbol}: Yetersiz bakiye ({marj:.2f})", flush=True)
            return False
        
        hesaplanan_miktar = (marj * KALDIRAC) / anlik_fiyat / contract_size
        min_miktar = float(market['limits']['amount']['min'] or 1.0)
        
        if hesaplanan_miktar < min_miktar:
            zorlanan_marj = (min_miktar * anlik_fiyat * contract_size) / KALDIRAC
            limit_marj = hedef_marj * 1.5
            
            if zorlanan_marj > limit_marj:
                print(f"  ⏭️ {symbol}: Min miktar zorlaması çok büyük ({zorlanan_marj:.2f} > limit {limit_marj:.2f}), atlanıyor", flush=True)
                return False
        
        miktar = float(exchange.amount_to_precision(
            symbol,
            max(hesaplanan_miktar, min_miktar)
        ))
        
        gercek_marj = (miktar * anlik_fiyat * contract_size) / KALDIRAC
        limit_marj = hedef_marj * 1.5
        
        if gercek_marj > limit_marj:
            print(f"  ⏭️ {symbol}: Marj çok yüksek ({gercek_marj:.2f} > {limit_marj:.2f}), atlanıyor", flush=True)
            return False
        
        if yon == "LONG":
            tp = anlik_fiyat + (atr * TP_ATR)
            sl = anlik_fiyat - (atr * SL_ATR)
            kapat_yon = 'sell'
        else:
            tp = anlik_fiyat - (atr * TP_ATR)
            sl = anlik_fiyat + (atr * SL_ATR)
            kapat_yon = 'buy'
        
        print(f"  📊 {symbol}: Marj={gercek_marj:.2f} | Miktar={miktar} | Pozisyon={gercek_marj*KALDIRAC:.2f}", flush=True)
        
        islem_y = 'buy' if yon == 'LONG' else 'sell'
        exchange.create_order(symbol, 'market', islem_y, miktar)
        time.sleep(1.0)
        
        sl_ok = False
        try:
            exchange.create_order(symbol, 'limit', kapat_yon, miktar, tp, {'reduceOnly': True})
        except: pass
        try:
            exchange.create_order(symbol, 'stop', kapat_yon, miktar, sl, {'stopPrice': sl, 'reduceOnly': True})
            sl_ok = True
        except Exception as e:
            print(f"  🚨 SL KOYULAMADI: {e}", flush=True)
        
        if not sl_ok:
            market_kapat(symbol, miktar, yon)
            return False
        
        with state_lock:
            AKTIF_SISTEMLER[symbol] = {
                "giris_fiyati": anlik_fiyat,
                "yon": yon,
                "giris_zamani": time.time(),
                "sebep": sebep,
                "tp": tp,
                "sl": sl,
                "marj": gercek_marj
            }
        
        hafizayi_kaydet()
        print(f"  ✅ [AÇILDI] {symbol} {yon} @ {anlik_fiyat}", flush=True)
        telegram_gonder(
            f"🎯 *ORDER FLOW SİNYAL*\n"
            f"📌 `{symbol}` | *{yon}*\n"
            f"📊 {sebep}\n"
            f"🎯 Giriş: `{anlik_fiyat}`\n"
            f"💰 TP: `{tp:.4f}` | 🛑 SL: `{sl:.4f}`\n"
            f"💵 Marj: `{gercek_marj:.2f}` USDT | Kaldıraç: `{KALDIRAC}x`"
        )
        return True
    except Exception as e:
        print(f"  ⚠️ Pozisyon açma {symbol}: {e}", flush=True)
        return False

# ==================== KAPANAN POZİSYON ====================
def kapanan_pozisyonlari_kontrol(aktif_semboller_seti):
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
                
                karli = False
                try:
                    t = exchange.fetch_ticker(eski)
                    cikis = float(t['last'])
                    if y == "LONG":
                        karli = abs(cikis - tp) < abs(cikis - sl)
                    else:
                        karli = abs(cikis - tp) < abs(cikis - sl)
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
                tip = "✅ *KÂRLA KAPANDI*" if karli else "❌ *ZARARLA KAPANDI*"
                print(f"  {tip} {eski}", flush=True)
                telegram_gonder(f"{tip}\n📌 `{eski}`")
    except Exception as e:
        print(f"⚠️ Kapanış kontrolü: {e}", flush=True)

# ==================== ANA DÖNGÜ ====================
def ana_dongu():
    global GUNLUK_BASLANGIC_BAKIYE, KILL_SWITCH_AKTIF
    
    print("🚀 [BAŞLANGIÇ] v12.2 - MFI Filtreli Order Flow", flush=True)
    print(f"⚙️ Kaldıraç: {KALDIRAC}x | Marj: %{POZISYON_MARJ*100:.0f} | Max Poz: {MAKS_POZISYON}", flush=True)
    print(f"⚙️ MFI Filtre: <{MFI_ASIRI_SATIM} SHORT açma, >{MFI_ASIRI_ALIM} LONG açma", flush=True)
    
    try:
        exchange.load_markets()
        b = exchange.fetch_balance()
        GUNLUK_BASLANGIC_BAKIYE = float(b['total'].get('USDT', 0))
        print(f"💰 Başlangıç kasası: {GUNLUK_BASLANGIC_BAKIYE:.2f} USDT", flush=True)
    except: pass
    
    baslangic_temizligi()
    
    dongu = 0
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5)
                continue
            
            dongu += 1
            print(f"\n{'='*60}", flush=True)
            print(f"🔄 [DÖNGÜ #{dongu}] {datetime.now().strftime('%H:%M:%S')}", flush=True)
            print(f"{'='*60}", flush=True)
            
            try:
                b = exchange.fetch_balance()
                su_an = float(b['total'].get('USDT', 0))
                if GUNLUK_BASLANGIC_BAKIYE and GUNLUK_BASLANGIC_BAKIYE > 0:
                    gk = (su_an - GUNLUK_BASLANGIC_BAKIYE) / GUNLUK_BASLANGIC_BAKIYE
                    print(f"💰 Kasa: {su_an:.2f} | Günlük: %{gk*100:+.2f}", flush=True)
                    if gk <= -GUNLUK_ZARAR_LIMIT:
                        print(f"🛑 KILL-SWITCH!", flush=True)
                        telegram_gonder(f"🛑 *KILL-SWITCH*\nGünlük: %{gk*100:.2f}")
                        time.sleep(3600)
                        GUNLUK_BASLANGIC_BAKIYE = su_an
                        continue
            except: pass
            
            try:
                raw_pos_check = exchange.fetch_positions()
                toplam_acik_zarar = sum(float(p.get('unrealizedPnl', 0)) for p in raw_pos_check 
                                       if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0)
                
                if toplam_acik_zarar < -TOPLAM_ZARAR_LIMIT:
                    print(f"🛑 [TOPLAM ZARAR] {toplam_acik_zarar:.2f} USDT!", flush=True)
                    telegram_gonder(f"🛑 *TOPLAM ZARAR LİMİTİ*\n💵 {toplam_acik_zarar:.2f}")
                    
                    for p in raw_pos_check:
                        k = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                        if k > 0:
                            y = str(p.get('side', '')).upper() or "LONG"
                            market_kapat(p['symbol'], k, y)
                    
                    with state_lock:
                        AKTIF_SISTEMLER.clear()
                    hafizayi_kaydet()
                    time.sleep(600)
                    continue
            except: pass
            
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
            
            kapanan_pozisyonlari_kontrol(aktif_semboller_seti)
            
            if len(aktif_semboller_seti) >= MAKS_POZISYON:
                print(f"  ⛔ Limit dolu ({len(aktif_semboller_seti)}/{MAKS_POZISYON})", flush=True)
                time.sleep(10)
                continue
            
            print(f"\n🔍 ORDER FLOW ANALİZİ:", flush=True)
            for symbol in TAKIP_EDILENLER:
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
                    print(f"  ⚠️ {symbol.replace('/USDT:USDT','')}: Hata - {detay['hata']}", flush=True)
                    continue
                
                sym_kisa = symbol.replace('/USDT:USDT', '')
                mfi = detay.get('mfi', 0)
                cmf = detay.get('cmf', 0)
                hacim = detay.get('hacim_oran', 0)
                ema_slope = detay.get('ema_slope', 0)
                
                mfi_emoji = "🟢" if mfi > 55 else ("🔴" if mfi < 45 else "⚪")
                cmf_emoji = "🟢" if cmf > 0.05 else ("🔴" if cmf < -0.05 else "⚪")
                hacim_emoji = "🟢" if hacim > 1.2 else ("🔴" if hacim < 0.7 else "⚪")
                ema_emoji = "🟢" if ema_slope > 0.1 else ("🔴" if ema_slope < -0.1 else "⚪")
                
                print(f"  {sym_kisa}: {mfi_emoji}MFI:{mfi:.0f} {cmf_emoji}CMF:{cmf:.2f} {hacim_emoji}Hacim:{hacim:.2f}x {ema_emoji}EMA:{ema_slope:+.2f}% | {detay.get('sebep', '')}", flush=True)
                
                if karar is None:
                    continue
                
                print(f"  🎯 [SİNYAL] {sym_kisa}: {karar}", flush=True)
                
                try:
                    ohlcv_atr = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=30)
                    df_atr = pd.DataFrame(ohlcv_atr, columns=['t', 'o', 'h', 'l', 'c', 'v'])
                    atr = ta.volatility.AverageTrueRange(df_atr['h'], df_atr['l'], df_atr['c'], window=14).average_true_range().iloc[-1]
                except:
                    continue
                
                ticker = exchange.fetch_ticker(symbol)
                anlik_fiyat = float(ticker['last'])
                
                sonuc = pozisyon_ac(symbol, karar, anlik_fiyat, atr, detay.get('sebep', ''))
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
                
                sebep = ""
                if p['symbol'] in AKTIF_SISTEMLER:
                    sebep = f"\n  📊 {AKTIF_SISTEMLER[p['symbol']].get('sebep', '')}"
                
                pos_detay += (
                    f"\n{emoji} `{sym}` | *{yon}* ({kaldirac_p}x){sebep}\n"
                    f"  Giriş: `{giris}` → Anlık: `{guncel}`\n"
                    f"  Marj: `{marj:.2f}` USDT | ROE: `%{roe:+.2f}` | PnL: `{float(p.get('unrealizedPnl', 0)):+.3f}`"
                )
        else:
            pos_detay = "\n\n📌 Açık pozisyon yok."
        
        mesaj = (
            f"📊 *ORDER FLOW BOT (v12.2)*\n\n"
            f"💰 Kasa: `{total:.2f} USDT`\n"
            f"💵 Toplam PnL: `{pnl:+.2f} USDT`\n"
            f"📌 Açık: `{len(pos)}` / `{MAKS_POZISYON}`"
            f"{pos_detay}\n\n"
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
    await update.message.reply_text("🟢 Order Flow Bot (v12.2) aktif!")

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
