import time
import threading
import requests
import ccxt
import pandas as pd
import ta
import os
from flask import Flask

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# --- GATE.IO TESTNET (DEMO) BAĞLANTISI ---
exchange = ccxt.gate({
    'apiKey': '82cca880898a88d1a31e86d8eb474c57',
    'secret': '1ac479b9df5e6f2e89560b0d238a250694719b6fcae20da00ebc54ad6aeb8898',
    'enableRateLimit': True,
    'options': {
        'defaultType': 'swap'  # Vadeli işlemler (Perpetual Swap)
    }
})

# Testnet / Sandbox modunu aktif et
exchange.set_sandbox_mode(True)

TAKIP_EDILENLER = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT', 'BNB/USDT:USDT']
AKTIF_GRID_SISTEMLERI = {}
BOT_CALISIYOR_MU = False

HAFIZA_KAYITLARI = {
    "zararli_islemler": [],
    "yasakli_yonler": {}
}

KALDIRAC = 10  # İstediğin gibi 10x
ILK_HEDEF_YUZDE = 1.5       
FINAL_HEDEF_YUZDE = 2.5     
ZARAR_KES_YUZDE = 1.5       
KOMISYON_ORANI = 0.0005 

def telegram_mesaj_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print(f"[TEST EKRANI] -> {mesaj}")
        return
        
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": mesaj, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram Gönderme Hatası: {e}")

def bakiye_al():
    """Gate.io Testnet hesabından güncel USDT bakiyesini çeker"""
    try:
        balance = exchange.fetch_balance()
        # Vadeli (swap) cüzdanındaki serbest USDT bakiyesi
        return float(balance['total'].get('USDT', 0))
    except Exception as e:
        print(f"Bakiye okuma hatası: {e}")
        return 0.0

@app.route('/')
def home():
    durum_str = "AKTİF 🟢" if BOT_CALISIYOR_MU else "BEKLEMEDE ⏸️"
    return f"Gate.io Testnet Gerçek Emir Botu | Durum: {durum_str}"

def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU
    son_funding_zamani = time.time()
    
    while True:
        try:
            if BOT_CALISIYOR_MU:
                coklu_grid_yonetimi()
                
                su_an = time.time()
                if su_an - son_funding_zamani >= 14400:
                    son_funding_zamani = su_an
                    
        except Exception as e:
            print(f"Tarama Hatası: {e}")
        time.sleep(10)

def kaldirac_ve_marjin_ayarla(symbol):
    """Gate.io borsasında parite için kaldıracı ve izole modu ayarlar"""
    try:
        # İzole mod ayarı (Gate.io'da cross: false = izole)
        exchange.set_margin_mode('isolated', symbol, {'leverage': KALDIRAC})
    except Exception as e:
        # Zaten ayarlıysa hata verebilir, yoksayabiliriz
        pass

def coklu_grid_yonetimi():
    global HAFIZA_KAYITLARI
    try:
        su_an = time.time()
        
        for sym in list(HAFIZA_KAYITLARI["yasakli_yonler"].keys()):
            if su_an > HAFIZA_KAYITLARI["yasakli_yonler"][sym]["bitis_zamani"]:
                del HAFIZA_KAYITLARI["yasakli_yonler"][sym]

        for symbol in TAKIP_EDILENLER:
            if not BOT_CALISIYOR_MU:
                break
                
            ticker = exchange.fetch_ticker(symbol)
            guncel_fiyat = ticker['last']

            # 1. ZATEN AÇIK BİR POZİSYON VARSA HEDEFLERİ KONTROL ET
            if symbol in AKTIF_GRID_SISTEMLERI:
                sistem = AKTIF_GRID_SISTEMLERI[symbol]
                merkez = sistem['merkez_fiyat']
                yon = sistem['yon']
                
                fark_orani = (guncel_fiyat - merkez) / merkez
                if yon == "SHORT": 
                    fark_orani = -fark_orani
                    
                kaldiracli_yuzde = fark_orani * KALDIRAC * 100
                
                # İLK HEDEF (%1.5 kâr -> Yarısını Kapat)
                if kaldiracli_yuzde >= ILK_HEDEF_YUZDE and not sistem.get("ilk_hedef_alindi", False):
                    sistem["ilk_hedef_alindi"] = True
                    try:
                         miktar_cinsi = sistem['miktar'] / 2
                         kapatma_yonu = 'sell' if yon == 'LONG' else 'buy'
                         exchange.create_market_order(symbol, kapatma_yonu, miktar_cinsi, {'reduce_only': True})
                         
                         telegram_mesaj_gonder(
                             f"🎯 *1. KADEME KÂR (Yarısı Kapandı)* - `{symbol}`\n"
                             f"• Hedef: `%{kaldiracli_yuzde:.2f}`"
                         )
                    except Exception as e:
                        print(f"1. Hedef emir hatası: {e}")

                # FİNAL HEDEF (%2.5 kâr -> Hepsini Kapat)
                if kaldiracli_yuzde >= FINAL_HEDEF_YUZDE:
                    try:
                        kapatma_yonu = 'sell' if yon == 'LONG' else 'buy'
                        exchange.create_market_order(symbol, kapatma_yonu, sistem['miktar'], {'reduce_only': True})
                        
                        telegram_mesaj_gonder(
                            f"🚀 *FİNAL HEDEF TAMAMLANDI (Pozisyon Kapandı)* - `{symbol}`\n"
                            f"• Kâr Oranı: `%{kaldiracli_yuzde:.2f}`"
                        )
                        del AKTIF_GRID_SISTEMLERI[symbol]
                    except Exception as e:
                        print(f"Final hedef emir hatası: {e}")
                    
                # ZARAR KES (STOP LOSS - %1.5 zarar)
                elif kaldiracli_yuzde <= -ZARAR_KES_YUZDE:
                    try:
                        kapatma_yonu = 'sell' if yon == 'LONG' else 'buy'
                        exchange.create_market_order(symbol, kapatma_yonu, sistem['miktar'], {'reduce_only': True})
                        
                        HAFIZA_KAYITLARI["yasakli_yonler"][symbol] = {
                            "yon": yon,
                            "bitis_zamani": su_an + 1200 
                        }

                        telegram_mesaj_gonder(
                            f"🛑 *KONTROLLÜ STOP (Zarar Kes)* - `{symbol}`\n"
                            f"• Oran: `%{kaldiracli_yuzde:.2f}`"
                        )
                        del AKTIF_GRID_SISTEMLERI[symbol]
                    except Exception as e:
                        print(f"Stop loss emir hatası: {e}")
                continue

            # 2. YENİ POZİSYON AÇMA MANTIĞI (Borsaya Gerçek Emir)
            toplam_bakiye = bakiye_al()
            bagli_marjin = sum([p['marjin'] for p in AKTIF_GRID_SISTEMLERI.values()])
            anlik_portfoy = toplam_bakiye + bagli_marjin
            sabit_islem_butcesi = anlik_portfoy / 4.0

            if symbol not in AKTIF_GRID_SISTEMLERI and toplam_bakiye >= sabit_islem_butcesi:
                ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
                df_15m = pd.DataFrame(ohlcv_15m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                
                ema7 = ta.trend.ema_indicator(df_15m['close'], window=7).iloc[-1]
                ema21 = ta.trend.ema_indicator(df_15m['close'], window=21).iloc[-1]
                rsi15m = ta.momentum.rsi(df_15m['close'], window=14).iloc[-1]
                
                bollinger = ta.volatility.BollingerBands(df_15m['close'], window=20, window_dev=2)
                bb_upper = bollinger.bollinger_hband().iloc[-1]
                bb_lower = bollinger.bollinger_lband().iloc[-1]
                bb_mavg = bollinger.bollinger_mavg().iloc[-1]
                
                atr = ta.volatility.AverageTrueRange(df_15m['high'], df_15m['low'], df_15m['close'], window=14).average_true_range().iloc[-1]
                fiyat_atr_orani = atr / guncel_fiyat * 100

                piyasa_durumu = "NORMAL"
                grid_yonu = None

                bant_genisligi = (bb_upper - bb_lower) / bb_mavg * 100
                if bant_genisligi < 1.2 or fiyat_atr_orani < 0.15:
                    piyasa_durumu = "TESTERE (YATAY)"
                    if guncel_fiyat >= bb_upper * 0.998 and rsi15m > 62:
                        grid_yonu = "SHORT"
                    elif guncel_fiyat <= bb_lower * 1.002 and rsi15m < 38:
                        grid_yonu = "LONG"
                    else:
                        continue
                else:
                    piyasa_durumu = "TREND"
                    if ema7 > ema21 and rsi15m > 50 and rsi15m < 75:
                        grid_yonu = "LONG"
                    elif ema7 < ema21 and rsi15m < 50 and rsi15m > 25:
                        grid_yonu = "SHORT"
                    else:
                        continue

                if not grid_yonu:
                    continue

                if symbol in HAFIZA_KAYITLARI["yasakli_yonler"]:
                    if HAFIZA_KAYITLARI["yasakli_yonler"][symbol]["yon"] == grid_yonu:
                        continue

                # BORSADA GERÇEK İŞLEM AÇMA ADIMI
                kaldirac_ve_marjin_ayarla(symbol)
                
                # Pozisyon büyüklüğü hesaplama (Marjin * Kaldıraç / Fiyat)
                toplam_pozisyon_usdt = sabit_islem_butcesi * KALDIRAC
                miktar = toplam_pozisyon_usdt / guncel_fiyat
                
                # Gate.io kontrat birimine göre miktarı yuvarla / ayarla
                market_info = exchange.market(symbol)
                miktar = exchange.amount_to_precision(symbol, miktar)
                
                emir_yonu = 'buy' if grid_yonu == 'LONG' else 'sell'
                
                try:
                    # Gate.io'ya gerçek piyasa emri gönder
                    order = exchange.create_market_order(symbol, emir_yonu, float(miktar))
                    
                    AKTIF_GRID_SISTEMLERI[symbol] = {
                        "yon": grid_yonu,
                        "merkez_fiyat": guncel_fiyat,
                        "marjin": sabit_islem_butcesi,
                        "miktar": float(miktar),
                        "ilk_hedef_alindi": False
                    }
                    
                    telegram_mesaj_gonder(
                        f"🧠 *BORSADA GERÇEK İŞLEM AÇILDI ({KALDIRAC}x)*\n"
                        f"• Parite: `{symbol}` ({grid_yonu})\n"
                        f"• İzole Marjin: `{sabit_islem_butcesi:.2f} USD` (1/4)\n"
                        f"• Fiyat: `{guncel_fiyat:.2f}`"
                    )
                except Exception as e:
                    print(f"Emir gönderme hatası ({symbol}): {e}")

    except Exception as e:
        print(f"Grid Yönetim Hata: {e}")

def telegram_komutlari_dinle():
    global AKTIF_GRID_SISTEMLERI, BOT_CALISIYOR_MU
    son_id = 0
    while True:
        try:
            if not TELEGRAM_TOKEN:
                time.sleep(5)
                continue
                
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={son_id + 1}&timeout=20"
            res = requests.get(url, timeout=25).json()

            if "result" in res:
                for veri in res["result"]:
                    son_id = veri["update_id"]
                    if "message" in veri and "text" in veri["message"]:
                        metin = veri["message"]["text"].strip()
                        
                        if metin == "/baslat":
                            BOT_CALISIYOR_MU = True
                            telegram_mesaj_gonder("🚀 *Testnet Canlı Emir Botu Aktif Edildi!*")

                        elif metin == "/kapat":
                            BOT_CALISIYOR_MU = False
                            # Açık tüm pozisyonları borsada piyasa fiyatından kapat
                            for sym, p in list(AKTIF_GRID_SISTEMLERI.items()):
                                try:
                                    kapatma_yonu = 'sell' if p['yon'] == 'LONG' else 'buy'
                                    exchange.create_market_order(sym, kapatma_yonu, p['miktar'], {'reduce_only': True})
                                except Exception as e:
                                    print(f"Kapatma hatası {sym}: {e}")
                            AKTIF_GRID_SISTEMLERI.clear()
                            telegram_mesaj_gonder("🚨 *Tüm Pozisyonlar Borsada Kapatıldı!*")

                        elif metin == "/durum":
                            bakiye = bakiye_al()
                            durum_str = "Çalışıyor 🟢" if BOT_CALISIYOR_MU else "Beklemede ⏸️"
                            durum = (
                                f"📊 *GATE.IO TESTNET DURUM*\n"
                                f"• Durum: `{durum_str}`\n"
                                f"• Cüzdan Bakiyesi: `{bakiye:.2f} USDT`\n"
                                f"• Aktif Pozisyon Sayısı: `{len(AKTIF_GRID_SISTEMLERI)}`"
                            )
                            telegram_mesaj_gonder(durum)

                        elif metin == "/pozisyonlar":
                            if not AKTIF_GRID_SISTEMLERI:
                                telegram_mesaj_gonder("📭 Borsada aktif pozisyon bulunmuyor.")
                            else:
                                msg = f"⚡ *BORSADAKİ AKTİF POZİSYONLAR ({KALDIRAC}x)*\n\n"
                                for sym, p in AKTIF_GRID_SISTEMLERI.items():
                                    try:
                                        t = exchange.fetch_ticker(sym)
                                        curr = t['last']
                                        fark = (curr - p['merkez_fiyat']) / p['merkez_fiyat']
                                        if p['yon'] == "SHORT": fark = -fark
                                        kaldiracli_yuzde = fark * KALDIRAC * 100
                                        msg += f"• *{sym}* ({p['yon']}) | Merkez: `{p['merkez_fiyat']:.2f}` | K/Z: `%{kaldiracli_yuzde:+.2f}`\n"
                                    except:
                                        pass
                                telegram_mesaj_gonder(msg)

        except Exception as e:
            print(f"Telegram dinleme hata: {e}")
        time.sleep(2)

if __name__ == "__main__":
    print(f"🚀 Gate.io Canlı Emir Botu Başlatılıyor ({KALDIRAC}x)...")
    threading.Thread(target=telegram_komutlari_dinle, daemon=True).start()
    threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True).start()
    telegram_mesaj_gonder(f"⚡ *Gate.io Gerçek Emir Botu Başlatıldı ({KALDIRAC}x)!*")
    
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, use_reloader=False)
                                        
