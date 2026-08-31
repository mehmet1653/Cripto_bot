import os
import time
import hmac
import hashlib
import requests
import json
import numpy as np
import pandas as pd
from datetime import datetime

# ==========================================
# AYARLAR VE YAPILANDIRMA
# ==========================================
API_KEY = os.getenv("GATE_API_KEY", "")
API_SECRET = os.getenv("GATE_API_SECRET", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

TAKIP_EDILENLER = [
    'SOL_USDT', 'XRP_USDT', 'HYPE_USDT', 
    'SUI_USDT', 'DOGE_USDT', 'AVAX_USDT', 
    'ADA_USDT', 'LINK_USDT', 'NEAR_USDT', 
    'RENDER_USDT', 'FET_USDT', 'PEPE_USDT', 
    'APT_USDT', 'AR_USDT', 'TIA_USDT'
]

KOMISYON_ORANI = 0.0006
MAX_AKTIF_ISLEM = 3  # Şimdilik 3 işlem sınırı

OGRENME_HAFIZASI = {
    "LONG_BASARI_ORANI": 1.0, 
    "SHORT_BASARI_ORANI": 1.0,
    "TOPLAM_ISLEM": 0,
    "BASARILI_ISLEM": 0
}

# ==========================================
# GATE.IO İMZA VE API YÖNETİCİSİ
# ==========================================
def gate_istek_gonder(method, endpoint, query_params=None, payload_str=None):
    host = "https://fx-api.gateio.ws"
    t = str(int(time.time()))
    
    if query_params:
        query_string = "&".join([f"{k}={v}" for k, v in sorted(query_params.items())])
    else:
        query_string = ""
        
    payload_to_sign = payload_str if payload_str else ""
    hashed_payload = hashlib.sha512(payload_to_sign.encode('utf-8')).hexdigest()
    
    s = f"{method}\n/api/v4{endpoint}\n{query_string}\n{hashed_payload}\n{t}"
    sign = hmac.new(API_SECRET.encode('utf-8'), s.encode('utf-8'), hashlib.sha512).hexdigest()
    
    headers = {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'KEY': API_KEY,
        'Timestamp': t,
        'SIGN': sign
    }
    
    url = host + endpoint
    if query_string:
        url += "?" + query_string
        
    start_time = time.time()
    try:
        if method == "GET":
            response = requests.get(url, headers=headers, timeout=10)
        elif method == "POST":
            response = requests.post(url, headers=headers, data=payload_to_sign, timeout=10)
        
        gecikme_ms = int((time.time() - start_time) * 1000)
        print(f"[API GECİKME] {method} {endpoint} -> {gecikme_ms} ms")
        return response.json()
    except Exception as e:
        print(f"API İstek Hatası ({endpoint}): {e}")
        return None

def telegram_bildir(mesaj):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[TELEGRAM YOK] {mesaj}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mesaj, "parse_mode": "Markdown"}
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Telegram bildirim hatası: {e}")

# ==========================================
# BORSADAN VERİ ÇEKME VE İŞLEM YÖNETİMİ
# ==========================================
def mum_verisi_cek(symbol, interval="15m", limit=100):
    try:
        url = f"https://fx-api.gateio.ws/api/v4/futures/usdt/candlesticks?contract={symbol}&interval={interval}&limit={limit}"
        response = requests.get(url, timeout=10)
        data = response.json()
        if not isinstance(data, list) or len(data) == 0:
            return None
        df = pd.DataFrame(data, columns=["t", "v", "c", "h", "l", "o", "sum"])
        df["close"] = df["c"].astype(float)
        df["high"] = df["h"].astype(float)
        df["low"] = df["l"].astype(float)
        df["open"] = df["o"].astype(float)
        df["volume"] = df["v"].astype(float)
        return df
    except Exception as e:
        print(f"Mum verisi çekilemedi ({symbol}): {e}")
        return None

def aktif_pozisyonlari_say():
    pos = gate_istek_gonder("GET", "/futures/usdt/positions")
    if isinstance(pos, list):
        aktif = [p for p in pos if float(p.get("size", 0)) != 0]
        return len(aktif)
    return 0

def marj_ve_kaldirac_ayarla(symbol, kaldirac):
    # 1. İzole Marj Moduna Al (0 = İzole, 1 = Cross)
    margin_endpoint = f"/futures/usdt/positions/{symbol}/margin"
    margin_payload = json.dumps({"margin_type": 0})
    gate_istek_gonder("POST", margin_endpoint, payload_str=margin_payload)
    
    # 2. Kaldıraç Ayarla
    lev_endpoint = f"/futures/usdt/positions/{symbol}/leverage"
    lev_params = {"leverage": str(kaldirac)}
    gate_istek_gonder("POST", lev_endpoint, query_params=lev_params)

def gercek_emir_gonder(symbol, yon, boyut_orani, puan):
    # Sinyal puanına göre dinamik kaldıraç (5x - 20x)
    if puan >= 90:
        kaldirac = 20
    elif puan >= 75:
        kaldirac = 10
    else:
        kaldirac = 5
        
    # İzole Marj ve Kaldıraç Yapılandırması
    marj_ve_kaldirac_ayarla(symbol, kaldirac)
    
    # Gerçek bakiye çekme
    bakiye_data = gate_istek_gonder("GET", "/futures/usdt/accounts")
    if not bakiye_data or "available" not in bakiye_data:
        print("Bakiye okunamadı, emir iptal.")
        return False
        
    musait_bakiye = float(bakiye_data["available"])
    kullanilacak_butce = musait_bakiye * boyut_orani * kaldirac
    
    # Anlık fiyat
    ticker = gate_istek_gonder("GET", f"/futures/usdt/tickers?contract={symbol}")
    if not ticker or len(ticker) == 0:
        return False
    anlik_fiyat = float(ticker[0]["last"])
    
    adet = int(kullanilacak_butce / anlik_fiyat)
    if adet <= 0:
        print(f"Bakiye yetersiz: {symbol} için adet 0 çıktı.")
        return False
        
    if yon == "SHORT":
        adet = -adet
        
    payload = {
        "contract": symbol,
        "size": adet,
        "price": "0",  # Market emri
        "tif": "ioc"
    }
    
    res = gate_istek_gonder("POST", "/futures/usdt/orders", payload_str=json.dumps(payload))
    if res and "id" in res:
        telegram_bildir(
            f"🚨 *GERÇEK İZOLE İŞLEM AÇILDI!*\n\n"
            f"📌 *Coin:* `{symbol}`\n"
            f"📊 *Yön:* `{yon}`\n"
            f"⚙️ *Marj Modu:* `İzole (Isolated)`\n"
            f"🚀 *Kaldıraç:* `{kaldirac}x`\n"
            f"⭐ *Puan:* `{puan}/100`\n"
            f"💰 *Kullanılan Kasa Oranı:* `%{int(boyut_orani * 100)}`"
        )
        return True
    else:
        print(f"Emir Hatası ({symbol}): {res}")
        return False

# ==========================================
# İNDİKATÖR VE YAPAY ZEKA SKORLAMA
# ==========================================
def indikatorleri_hesapla(df):
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    delta = df["close"].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df["rsi"] = 100 - (100 / (1 + rs))
    df["body_size"] = (df["close"] - df["open"]).abs()
    df["avg_body"] = df["body_size"].rolling(window=10).mean()
    return df

def sinyal_puanla_ve_boyutlandir(symbol):
    df_15m = mum_verisi_cek(symbol, "15m", 50)
    df_1h = mum_verisi_cek(symbol, "1h", 50)
    df_4h = mum_verisi_cek(symbol, "4h", 50)
    
    if df_15m is None or df_1h is None or df_4h is None:
        return 0, 0, None

    df_15m = indikatorleri_hesapla(df_15m)
    df_1h = indikatorleri_hesapla(df_1h)
    df_4h = indikatorleri_hesapla(df_4h)
    
    puan = 0
    yon = None
    
    c_15m = df_15m.iloc[-1]
    c_1h = df_1h.iloc[-1]
    c_4h = df_4h.iloc[-1]
    
    if c_4h["close"] > c_4h["ema20"]:
        puan += 30
        yon = "LONG"
    elif c_4h["close"] < c_4h["ema20"]:
        puan += 30
        yon = "SHORT"
        
    if yon == "LONG" and c_1h["close"] > c_1h["ema20"]:
        puan += 20
    elif yon == "SHORT" and c_1h["close"] < c_1h["ema20"]:
        puan += 20
        
    if yon == "LONG" and 40 < c_15m["rsi"] < 65 and c_15m["close"] > c_15m["ema20"]:
        puan += 15
    elif yon == "SHORT" and 35 < c_15m["rsi"] < 60 and c_15m["close"] < c_15m["ema20"]:
        puan += 15

    if c_15m["body_size"] <= (c_15m["avg_body"] * 2.5):
        puan += 15

    if yon == "LONG" and OGRENME_HAFIZASI["LONG_BASARI_ORANI"] >= 1.0:
        puan += 20
    elif yon == "SHORT" and OGRENME_HAFIZASI["SHORT_BASARI_ORANI"] >= 1.0:
        puan += 20

    kasa_orani = 0.0
    if puan >= 85:
        kasa_orani = 0.40
    elif puan >= 70:
        kasa_orani = 0.25
    elif puan >= 55:
        kasa_orani = 0.10
    else:
        kasa_orani = 0.0

    # Komisyon Kontrolü
    toplam_komisyon = KOMISYON_ORANI * 2
    tahmini_brut_kar = 0.015
    if (tahmini_brut_kar - toplam_komisyon) <= 0.003:
        return 0, 0, None

    return puan, kasa_orani, yon

# ==========================================
# ANA ÇALIŞTIRMA DÖNGÜSÜ (10 SN TARAMA)
# ==========================================
def ana_dongu():
    telegram_bildir("🚀 *İzole Marj & Gerçek Emir Modlu AI Botu* 10 saniyelik tarama döngüsüyle aktifleşti!")
    
    while True:
        try:
            aktif_sayisi = aktif_pozisyonlari_say()
            if aktif_sayisi >= MAX_AKTIF_ISLEM:
                print(f"[INFO] 3 işlem sınırına ulaşıldı ({aktif_sayisi}/{MAX_AKTIF_ISLEM}). Yeni işlem açılmayacak, bekleniyor...")
                time.sleep(10)
                continue
                
            for coin in TAKIP_EDILENLER:
                if aktif_pozisyonlari_say() >= MAX_AKTIF_ISLEM:
                    break
                    
                puan, kasa_orani, yon = sinyal_puanla_ve_boyutlandir(coin)
                
                if puan >= 55 and kasa_orani > 0:
                    print(f"[ALARM] {coin} için {puan} puan yakalandı, izole emir iletiliyor...")
                    basarili = gercek_emir_gonder(coin, yon, kasa_orani, puan)
                    if basarili:
                        time.sleep(5) 
                        break
                
                # İstediğin gibi hızlı tarama akışı için kısa bekleme
                time.sleep(1) 
        except Exception as e:
            print(f"Ana döngü hatası: {e}")
            time.sleep(5)
            
        # Tam 10 saniyede bir ana sepet tarama döngüsü
        time.sleep(10)

if __name__ == "__main__":
    ana_dongu()
