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
    'timeout': 15000,
    'options': {
        'defaultType': 'swap'
    }
})

exchange.set_sandbox_mode(True)

TAKIP_EDILENLER = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT', 'BNB/USDT:USDT']
AKTIF_GRID_SISTEMLERI = {}
BOT_CALISIYOR_MU = False

HAFIZA_KAYITLARI = {
    "zararli_islemler": [],
    "yasakli_yonler": {}
}

KALDIRAC = 10
ILK_HEDEF_YUZDE = 1.5       
FINAL_HEDEF_YUZDE = 2.5     
ZARAR_KES_YUZDE = 1.5       

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
    try:
        balance = exchange.fetch_balance()
        return float(balance['total'].get('USDT', 0))
    except Exception as e:
        hata_mesaji = f"🚨 *Bakiye Okuma Hatası!*\n`{str(e)}`"
        print(f"[HATA] {e}")
        telegram_mesaj_gonder(hata_mesaji)
        return 0.0

@app.route('/')
def home():
    durum_str = "AKTİF 🟢" if BOT_CALISIYOR_MU else "BEKLEMEDE ⏸️"
    return f"Gate.io Testnet Canlı Raporlu Bot | Durum: {durum_str}"

def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU
    print("🔄 Arka plan tarayıcı aktif.")
    
    while True:
        try:
            if BOT_CALISIYOR_MU:
                coklu_grid_yonetimi()
        except Exception as e:
            hata_mesaji = f"🚨 *Genel Döngü Kritik Hata!*\n`{str(e)}`"
            print(f"[KRİTİK HATA] {e}")
            telegram_mesaj_gonder(hata_mesaji)
        time.sleep(15)

def kaldirac_ve_marjin_ayarla(symbol):
    try:
        exchange.set_margin_mode('isolated', symbol, {'leverage': KALDIRAC})
    except Exception as e:
        print(f"Kaldıraç ayarlama uyarısı ({symbol}): {e}")

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
                
            try:
                ticker = exchange.fetch_ticker(symbol)
                guncel_fiyat = ticker['last']
            except Exception as e:
                hata_metni = f"⚠️ *Fiyat Alma Hatası* (`{symbol}`):\n`{str(e)}`"
                print(f"[HATA] {symbol} fiyat çekilemedi: {e}")
                telegram_mesaj_gonder(hata_metni)
                continue

            # 1. AÇIK POZİSYON KONTROLÜ
            if symbol in AKTIF_GRID_SISTEMLERI:
                sistem = AKTIF_GRID_SISTEMLERI[symbol]
                merkez = sistem['merkez_fiyat']
                yon = sistem['yon']
                
                fark_orani = (guncel_fiyat - merkez) / merkez
                if yon == "SHORT": 
                    fark_orani = -fark_orani
                    
                kaldiracli_yuzde = fark_orani * KALDIRAC * 100
                
                if kaldiracli_yuzde >= ILK_HEDEF_YUZDE and not sistem.get("ilk_hedef_alindi", False):
                    sistem["ilk_hedef_alindi"] = True
                    try:
                         miktar_cinsi = sistem['miktar'] / 2
                         kapatma_yonu = 'sell' if yon == 'LONG' else 'buy'
                         exchange.create_market_order(symbol, kapatma_yonu, miktar_cinsi, {'reduce_only': True})
                         telegram_mesaj_gonder(f"🎯 *1. KADEME KÂR* - `{symbol}` (`%{kaldiracli_yuzde:.2f}`)")
                    except Exception as e:
                        telegram_mesaj_gonder(f"🚨 *1. Hedef Emir Hatası* (`{symbol}`):\n`{str(e)}`")

                if kaldiracli_yuzde >= FINAL_HEDEF_YUZDE:
                    try:
                        kapatma_yonu = 'sell' if yon == 'LONG' else 'buy'
                        exchange.create_market_order(symbol, kapatma_yonu, sistem['miktar'], {'reduce_only': True})
                        telegram_mesaj_gonder(f"🚀 *FİNAL HEDEF* - `{symbol}` (`%{kaldiracli_yuzde:.2f}`)")
                        del AKTIF_GRID_SISTEMLERI[symbol]
                    except Exception as e:
                        telegram_mesaj_gonder(f"🚨 *Final Hedef Emir Hatası* (`{symbol}`):\n`{str(e)}`")
                    
                elif kaldiracli_yuzde <= -ZARAR_KES_YUZDE:
                    try:
                        kapatma_yonu = 'sell' if yon == 'LONG' else 'buy'
                        exchange.create_market_order(symbol, kapatma_yonu, sistem['miktar'], {'reduce_only': True})
                        HAFIZA_KAYITLARI["yasakli_yonler"][symbol] = {"yon": yon, "bitis_zamani": su_an + 1200}
                        telegram_mesaj_gonder(f"🛑 *ZARAR KES (STOP)* - `{symbol}` (`%{kaldiracli_yuzde:.2f}`)")
                        del AKTIF_GRID_SISTEMLERI[symbol]
                    except Exception as e:
                        telegram_mesaj_gonder(f"🚨 *Stop Loss Emir Hatası* (`{symbol}`):\n`{str(e)}`")
                continue

            # 2. YENİ POZİSYON TARAMA
            toplam_bakiye = bakiye_al()
            bagli_marjin = sum([p['marjin'] for p in AKTIF_GRID_SISTEMLERI.values()])
            anlik_portfoy = toplam_bakiye + bagli_marjin
            sabit_islem_butcesi = anlik_portfoy / 4.0

            if toplam_bakiye < sabit_islem_butcesi:
                print(f"[BİLGİ] {symbol}: Bakiye yetersiz ({toplam_bakiye} < {sabit_islem_butcesi})")
                continue

            try:
                ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
            except Exception as e:
                telegram_mesaj_gonder(f"⚠️ *OHLCV Veri Hatası* (`{symbol}`):\n`{str(e)}`")
                continue

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
            bant_genisligi = (bb_upper - bb_lower) / bb_mavg * 100

            grid_yonu = None
            piyasa_durumu = "NORMAL"

            if bant_genisligi < 1.2 or fiyat_atr_orani < 0.15:
                piyasa_durumu = "TESTERE (YATAY)"
                if guncel_fiyat >= bb_upper * 0.998 and rsi15m > 62:
                    grid_yonu = "SHORT"
                elif guncel_fiyat <= bb_lower * 1.002 and rsi15m < 38:
                    grid_yonu = "LONG"
                else:
                    # YENİ: Şart sağlanmadığında Telegram'a anlık bilgi geçecek
                    telegram_mesaj_gonder(f"🔍 *TARAMA RAPORU* (`{symbol}`)\n• Mod: `{piyasa_durumu}`\n• Fiyat: `{guncel_fiyat}` | RSI: `{rsi15m:.1f}`\n• Bant Genişliği: `%{bant_genisligi:.2f}`\n👉 *Durum:* Şartlar sağlanmadı, bekleniyor.")
                    continue
            else:
                piyasa_durumu = "TREND"
                if ema7 > ema21 and rsi15m > 50 and rsi15m < 75:
                    grid_yonu = "LONG"
                elif ema7 < ema21 and rsi15m < 50 and rsi15m > 25:
                    grid_yonu = "SHORT"
                else:
                    # YENİ: Şart sağlanmadığında Telegram'a anlık bilgi geçecek
                    telegram_mesaj_gonder(f"🔍 *TARAMA RAPORU* (`{symbol}`)\n• Mod: `{piyasa_durumu}`\n• Fiyat: `{guncel_fiyat}` | RSI: `{rsi15m:.1f}`\n• EMA7/21: `{ema7:.2f}` / `{ema21:.2f}`\n👉 *Durum:* Şartlar sağlanmadı, bekleniyor.")
                    continue

            if symbol in HAFIZA_KAYITLARI["yasakli_yonler"]:
                if HAFIZA_KAYITLARI["yasakli_yonler"][symbol]["yon"] == grid_yonu:
                    print(f"[TARAMA] {symbol}: Bu yön yasaklı listede.")
                    continue

            # EMİR AŞAMASI
            kaldirac_ve_marjin_ayarla(symbol)
            toplam_pozisyon_usdt = sabit_islem_butcesi * KALDIRAC
            miktar = toplam_pozisyon_usdt / guncel_fiyat
            miktar = exchange.amount_to_precision(symbol, miktar)
            emir_yonu = 'buy' if grid_yonu == 'LONG' else 'sell'
            
            try:
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
                    f"• Marjin: `{sabit_islem_butcesi:.2f} USD`\n"
                    f"• Fiyat: `{guncel_fiyat:.2f}`"
                )
            except Exception as e:
                hata_detayi = f"🚨 *EMİR AÇILAMADI / TAKILDI!* (`{symbol}`)\n• Yön: `{emir_yonu}`\n• Hata: `{str(e)}`"
                print(f"[EMİR HATASI] {symbol}: {e}")
                telegram_mesaj_gonder(hata_detayi)

    except Exception as e:
        print(f"[HATA] Grid Yönetim Döngü Hatası: {e}")

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
                            telegram_mesaj_gonder("🚀 *Anlık Raporlu Bot Aktif Edildi!* Artık her adımı bildireceğim.")

                        elif metin == "/kapat":
                            BOT_CALISIYOR_MU = False
                            for sym, p in list(AKTIF_GRID_SISTEMLERI.items()):
                                try:
                                    kapatma_yonu = 'sell' if p['yon'] == 'LONG' else 'buy'
                                    exchange.create_market_order(sym, kapatma_yonu, p['miktar'], {'reduce_only': True})
                                except Exception as e:
                                    print(f"Kapatma hatası: {e}")
                            AKTIF_GRID_SISTEMLERI.clear()
                            telegram_mesaj_gonder("🚨 *Tüm Pozisyonlar Kapatıldı!*")

                        elif metin == "/durum":
                            bakiye = bakiye_al()
                            durum_str = "Çalışıyor 🟢" if BOT_CALISIYOR_MU else "Beklemede ⏸️"
                            telegram_mesaj_gonder(f"📊 *DURUM*\n• Durum: `{durum_str}`\n• Cüzdan: `{bakiye:.2f} USDT`\n• Aktif: `{len(AKTIF_GRID_SISTEMLERI)}`")

                        elif metin == "/pozisyonlar":
                            if not AKTIF_GRID_SISTEMLERI:
                                telegram_mesaj_gonder("📭 Aktif pozisyon yok.")
                            else:
                                msg = f"⚡ *AKTİF POZİSYONLAR*\n\n"
                                for sym, p in AKTIF_GRID_SISTEMLERI.items():
                                    try:
                                        t = exchange.fetch_ticker(sym)
                                        curr = t['last']
                                        fark = (curr - p['merkez_fiyat']) / p['merkez_fiyat']
                                        if p['yon'] == "SHORT": fark = -fark
                                        msg += f"• *{sym}* ({p['yon']}) | K/Z: `%{fark * KALDIRAC * 100:+.2f}`\n"
                                    except:
                                        pass
                                telegram_mesaj_gonder(msg)
        except Exception as e:
            print(f"Telegram dinleme hata: {e}")
        time.sleep(2)

if __name__ == "__main__":
    print(f"🚀 Canlı Raporlu Bot Başlatılıyor ({KALDIRAC}x)...")
    threading.Thread(target=telegram_komutlari_dinle, daemon=True).start()
    threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True).start()
    telegram_mesaj_gonder(f"⚡ *Canlı Raporlu Bot Devrede ({KALDIRAC}x)! /baslat komutunu verip tarama raporlarını takip edebilirsin.*")
    
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, use_reloader=False)
                        
