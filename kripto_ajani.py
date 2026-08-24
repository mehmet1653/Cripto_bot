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

exchange = ccxt.gate({
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'}
})

TAKIP_EDILENLER = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT', 'BNB/USDT:USDT']
AKTIF_GRID_SISTEMLERI = {}
BOT_CALISIYOR_MU = False

HAFIZA_KAYITLARI = {
    "zararli_islemler": [],
    "yasakli_yonler": {}
}

KASA = {
    "baslangic": 100.0,
    "guncel": 100.0, 
    "toplam_islem": 0,
    "basarili_islem": 0,
    "zararli_islem": 0,
    "gunluk_kar_zarar": 0.0,
    "toplam_odenen_komisyon": 0.0,
    "toplam_odenmis_funding": 0.0
}

KALDIRAC = 10

# KADEMELİ HEDEFLER
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

@app.route('/')
def home():
    durum_str = "AKTİF 🟢" if BOT_CALISIYOR_MU else "BEKLEMEDE ⏸️"
    return f"Gate.io Sabit 4'e Bölünmüş Scalp Bot | Durum: {durum_str}"

def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU
    son_funding_zamani = time.time()
    
    while True:
        try:
            if BOT_CALISIYOR_MU:
                coklu_grid_yonetimi()
                
                su_an = time.time()
                if su_an - son_funding_zamani >= 14400:
                    fonlama_ucretlerini_uygula()
                    son_funding_zamani = su_an
                    
        except Exception as e:
            print(f"Tarama Hatası: {e}")
        time.sleep(10)

def fonlama_ucretlerini_uygula():
    global KASA
    if not AKTIF_GRID_SISTEMLERI:
        return
        
    toplam_kesinti = 0.0
    for symbol, sistem in AKTIF_GRID_SISTEMLERI.items():
        try:
            funding_orani = 0.0001 
            try:
                fr_data = exchange.fetch_funding_rate(symbol)
                if 'fundingRate' in fr_data and fr_data['fundingRate'] is not None:
                    funding_orani = float(fr_data['fundingRate'])
            except:
                pass
                
            pozisyon_buyuklugu = sistem['marjin'] * KALDIRAC
            kesinti = pozisyon_buyuklugu * funding_orani
            net_kesinti = kesinti if sistem['yon'] == "LONG" else -kesinti
                
            KASA["guncel"] -= net_kesinti
            KASA["toplam_odenmis_funding"] += net_kesinti
            toplam_kesinti += net_kesinti
        except Exception as e:
            print(f"Funding hata ({symbol}): {e}")
            
    if abs(toplam_kesinti) > 0.001:
        telegram_mesaj_gonder(f"💸 *4 Saatlik Fonlama İşlendi*\n• Toplam: `{toplam_kesinti:+.4f} USD`\n• Güncel Kasa: `{KASA['guncel']:.2f} USD`")

def coklu_grid_yonetimi():
    global KASA, HAFIZA_KAYITLARI
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

            if symbol in AKTIF_GRID_SISTEMLERI:
                sistem = AKTIF_GRID_SISTEMLERI[symbol]
                merkez = sistem['merkez_fiyat']
                marjin = sistem['marjin']
                yon = sistem['yon']
                
                fark_orani = (guncel_fiyat - merkez) / merkez
                if yon == "SHORT": 
                    fark_orani = -fark_orani
                    
                kaldiracli_yuzde = fark_orani * KALDIRAC * 100
                
                # 1. KADEMELİ KÂR AL (Yarısı Satılır)
                if kaldiracli_yuzde >= ILK_HEDEF_YUZDE and not sistem.get("ilk_hedef_alindi", False):
                    sistem["ilk_hedef_alindi"] = True
                    
                    yari_marjin = marjin / 2
                    toplam_deger = yari_marjin * KALDIRAC
                    komisyon = toplam_deger * KOMISYON_ORANI * 2
                    brut_kar = yari_marjin * (kaldiracli_yuzde / 100)
                    net_kz = brut_kar - komisyon
                    
                    KASA["guncel"] += yari_marjin + net_kz
                    KASA["gunluk_kar_zarar"] += net_kz
                    KASA["toplam_odenen_komisyon"] += komisyon
                    KASA["toplam_islem"] += 1
                    KASA["basarili_islem"] += 1
                    
                    sistem['marjin'] = yari_marjin

                    telegram_mesaj_gonder(
                        f"🎯 *1. KADEME KÂR CEBE ATILDI (Yarısı Kapandı)* - `{symbol}`\n"
                        f"• Net K/Z: `{net_kz:+.2f} USD` (`%{kaldiracli_yuzde:.2f}`)"
                    )

                # 2. FİNAL HEDEF
                if kaldiracli_yuzde >= FINAL_HEDEF_YUZDE:
                    toplam_deger = marjin * KALDIRAC
                    komisyon = toplam_deger * KOMISYON_ORANI * 2 
                    brut_kar = marjin * (kaldiracli_yuzde / 100)
                    net_kz = brut_kar - komisyon
                    
                    KASA["guncel"] = KASA["guncel"] + marjin + net_kz
                    KASA["gunluk_kar_zarar"] += net_kz
                    KASA["toplam_odenen_komisyon"] += komisyon
                    KASA["toplam_islem"] += 1
                    KASA["basarili_islem"] += 1

                    telegram_mesaj_gonder(
                        f"🚀 *FİNAL HEDEF TAMAMLANDI!* - `{symbol}`\n"
                        f"• Kalan Yarı Net K/Z: `{net_kz:+.2f} USD` (`%{kaldiracli_yuzde:.2f}`)\n"
                        f"• Güncel Kasa: `{KASA['guncel']:.2f} USD`"
                    )
                    del AKTIF_GRID_SISTEMLERI[symbol]
                    
                # ZARAR KES (STOP)
                elif kaldiracli_yuzde <= -ZARAR_KES_YUZDE:
                    toplam_deger = marjin * KALDIRAC
                    komisyon = toplam_deger * KOMISYON_ORANI * 2
                    zarar = marjin * (ZARAR_KES_YUZDE / 100)
                    net_kz = -zarar - komisyon
                    
                    KASA["guncel"] = KASA["guncel"] + marjin + net_kz
                    KASA["gunluk_kar_zarar"] += net_kz
                    KASA["toplam_odenen_komisyon"] += komisyon
                    KASA["toplam_islem"] += 1
                    KASA["zararli_islem"] += 1

                    HAFIZA_KAYITLARI["zararli_islemler"].append({"symbol": symbol, "yon": yon, "zaman": su_an})
                    HAFIZA_KAYITLARI["yasakli_yonler"][symbol] = {
                        "yon": yon,
                        "bitis_zamani": su_an + 1200 
                    }

                    telegram_mesaj_gonder(
                        f"🛑 *KONTROLLÜ STOP* - `{symbol}`\n"
                        f"• Zarar: `{net_kz:+.2f} USD`\n"
                        f"• Kesilen Komisyon: `{komisyon:.4f} USD`"
                    )
                    del AKTIF_GRID_SISTEMLERI[symbol]
                continue

            # 🔥 TOPLAM PORTFÖYÜ HESAPLA VE TAM 4 E BÖLEREK SABİT MARJİN BELİRLE
            bagli_marjin = sum([p['marjin'] for p in AKTIF_GRID_SISTEMLERI.values()])
            anlik_portfoy = KASA["guncel"] + bagli_marjin
            sabit_islem_butcesi = anlik_portfoy / 4.0  # Her zaman toplam kasanın tam 4'te 1'i (Örn: 100 dolar ise tam 25 dolar)

            if symbol not in AKTIF_GRID_SISTEMLERI and KASA["guncel"] >= sabit_islem_butcesi:
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
                    yasakli_bilgi = HAFIZA_KAYITLARI["yasakli_yonler"][symbol]
                    if yasakli_bilgi["yon"] == grid_yonu:
                        continue

                KASA["guncel"] -= sabit_islem_butcesi
                AKTIF_GRID_SISTEMLERI[symbol] = {
                    "yon": grid_yonu,
                    "merkez_fiyat": guncel_fiyat,
                    "marjin": sabit_islem_butcesi,
                    "ilk_hedef_alindi": False
                }
                
                telegram_mesaj_gonder(
                    f"🧠 *SABİT 4'E BÖLÜNME İLE AÇILDI*\n"
                    f"• Parite: `{symbol}` ({grid_yonu} 5x İzole)\n"
                    f"• Piyasa Modu: `{piyasa_durumu}`\n"
                    f"• İzole Marjin: `{sabit_islem_butcesi:.2f} USD` (Tam 1/4)\n"
                    f"• Fiyat: `{guncel_fiyat:.2f}` | RSI: `{rsi15m:.1f}`"
                )

    except Exception as e:
        print(f"Grid Yönetim Hata: {e}")

def telegram_komutlari_dinle():
    global KASA, AKTIF_GRID_SISTEMLERI, BOT_CALISIYOR_MU, HAFIZA_KAYITLARI
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
                            telegram_mesaj_gonder("🚀 *Sabit 4 Parçalı Bot Aktif! (Tam 1/4 Eşit Marjin Sistemi)*")

                        elif metin == "/kapat":
                            BOT_CALISIYOR_MU = False
                            toplam_gercek_iade = 0.0
                            
                            for sym, p in AKTIF_GRID_SISTEMLERI.items():
                                try:
                                    t = exchange.fetch_ticker(sym)
                                    curr = t['last']
                                    merkez = p['merkez_fiyat']
                                    marjin = p['marjin']
                                    
                                    fark_orani = (curr - merkez) / merkez
                                    if p['yon'] == "SHORT": fark_orani = -fark_orani
                                        
                                    kaldiracli_yuzde = fark_orani * KALDIRAC * 100
                                    brut_tutar = marjin * (kaldiracli_yuzde / 100)
                                    
                                    toplam_deger = marjin * KALDIRAC
                                    komisyon = toplam_deger * KOMISYON_ORANI * 2
                                    net_durum = brut_tutar - komisyon
                                    
                                    gercek_tutar = marjin + net_durum
                                    toplam_gercek_iade += max(0, gercek_tutar)
                                    
                                    KASA["gunluk_kar_zarar"] += net_durum
                                    KASA["toplam_odenen_komisyon"] += komisyon
                                    KASA["toplam_islem"] += 1
                                    if net_durum >= 0: KASA["basarili_islem"] += 1
                                    else: KASA["zararli_islem"] += 1
                                except:
                                    toplam_gercek_iade += p['marjin']
                                    
                            KASA["guncel"] += toplam_gercek_iade
                            AKTIF_GRID_SISTEMLERI.clear()
                            telegram_mesaj_gonder(f"🚨 *Tüm Kanallar Kapatıldı*\n• Nakit Kasa: `{KASA['guncel']:.2f} USD`")

                        elif metin == "/durum":
                            bagli = sum([p['marjin'] for p in AKTIF_GRID_SISTEMLERI.values()])
                            anlik_acik_kz = 0.0
                            for sym, p in AKTIF_GRID_SISTEMLERI.items():
                                try:
                                    t = exchange.fetch_ticker(sym)
                                    curr = t['last']
                                    fark = (curr - p['merkez_fiyat']) / p['merkez_fiyat']
                                    if p['yon'] == "SHORT": fark = -fark
                                    anlik_acik_kz += p['marjin'] * (fark * KALDIRAC)
                                except:
                                    pass

                            toplam_varlik = KASA['guncel'] + bagli + anlik_acik_kz
                            durum_str = "Çalışıyor 🟢" if BOT_CALISIYOR_MU else "Beklemede ⏸️"
                            
                            durum = (
                                f"📊 *SABİT 4'LÜ BÖLME RAPORU*\n"
                                f"• Durum: `{durum_str}`\n"
                                f"• Nakit Kasa: `{KASA['guncel']:.2f} USD`\n"
                                f"• Bağlı İzole Marjin: `{bagli:.2f} USD`\n"
                                f"• Anlık Açık K/Z: `{anlik_acik_kz:+.2f} USD`\n"
                                f"💰 *Toplam Portföy: `{toplam_varlik:.2f} USD`*\n"
                                f"• 💸 Komisyon: `{KASA['toplam_odenen_komisyon']:.4f} USD`\n"
                                f"• Günlük Net K/Z: `{KASA['gunluk_kar_zarar']:+.2f} USD`\n"
                                f"• Başarılı: `{KASA['basarili_islem']}` | Zararlı: `{KASA['zararli_islem']}`"
                            )
                            telegram_mesaj_gonder(durum)

                        elif metin == "/pozisyonlar":
                            if not AKTIF_GRID_SISTEMLERI:
                                telegram_mesaj_gonder("📭 Aktif pozisyon bulunmuyor.")
                            else:
                                msg = "⚡ *SABİT 4'LÜ AKTİF POZİSYONLAR*\n\n"
                                for sym, p in AKTIF_GRID_SISTEMLERI.items():
                                    try:
                                        t = exchange.fetch_ticker(sym)
                                        curr = t['last']
                                        fark_orani = (curr - p['merkez_fiyat']) / p['merkez_fiyat']
                                        if p['yon'] == "SHORT": fark_orani = -fark_orani
                                            
                                        kaldiracli_yuzde = fark_orani * KALDIRAC * 100
                                        tahmini_dolar = p['marjin'] * (kaldiracli_yuzde / 100)
                                        ikon = "🟢" if tahmini_dolar >= 0 else "🔴"
                                        asama = "2. Aşama (Final)" if p.get("ilk_hedef_alindi") else "1. Aşama (Yolcu)"
                                        
                                        msg += f"{ikon} *{sym}* ({p['yon']} 5x İzole) - `{asama}`\n• Marjin: `{p['marjin']:.2f} USD` | Merkez: `{p['merkez_fiyat']:.2f}`\n• K/Z: `%{kaldiracli_yuzde:+.2f}` (`{tahmini_dolar:+.2f} USD`)\n-------------------\n"
                                    except:
                                        pass
                                telegram_mesaj_gonder(msg)

        except Exception as e:
            print(f"Telegram dinleme hata: {e}")
        time.sleep(2)

if __name__ == "__main__":
    print("🚀 Sabit 4'e Bölünmüş Bot Aktif...")
    threading.Thread(target=telegram_komutlari_dinle, daemon=True).start()
    threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True).start()
    telegram_mesaj_gonder("⚡ Bot güncellendi! Artık toplam portföyü her zaman tam 4 eşit parçaya bölerek (örneğin 100 dolar için tam 25'er dolar) izole marjin açıyor.")
    
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, use_reloader=False)
