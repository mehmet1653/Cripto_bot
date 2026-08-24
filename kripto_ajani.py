import time
import threading
import requests
import ccxt
import pandas as pd
import ta
import os
from datetime import datetime
from flask import Flask

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Gate.io entegrasyonu
exchange = ccxt.gate({
    'enableRateLimit': True,
    'options': {'defaultType': 'spot'}
})

TAKIP_EDILENLER = ['BTC/USDT', 'ETH/USDT', 'BNB/USDT', 'SOL/USDT']
ACIK_POZISYONLAR = {}

KASA = {
    "baslangic": 100.0,
    "guncel": 100.0, 
    "toplam_islem": 0,
    "basarili_islem": 0,
    "zararli_islem": 0,
    "gunluk_kar_zarar": 0.0,
    "toplam_ odenen_komisyon": 0.0,
    "ogrenilen_dersler": []
}

HEDEF_YUZDESI = 0.03
STOP_YUZDESI = 0.015
RISK_ORANI = 0.25 
KALDIRAC = 5
KOMISYON_ORANI = 0.0008 

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

# ==========================================
# 🌐 WEB SUNUCUSU (UptimeRobot Buraya Ping Atacak)
# ==========================================
@app.route('/')
def home():
    return "🟢 Pozisyon Takip Özellikli Gate.io Kripto Ajanı Aktif!"

@app.route('/tara')
def disaridan_tarama_tetikle():
    return "⚡ Manuel tetikleme noktası.", 200

# ==========================================
# 🔄 ARKA PLANDA SÜREKLİ DÖNEN TARAMA MOTORU
# ==========================================
def otomatik_arkaplan_tarayici():
    print("🔄 Arka plan tarama döngüsü başlatıldı...")
    while True:
        try:
            disaridan_tarama_tetikle_internal()
        except Exception as e:
            print(f"Arka plan tarama hatası: {e}")
        time.sleep(180) # Her 3 dakikada bir tarar

def disaridan_tarama_tetikle_internal():
    global KASA
    try:
        for symbol in TAKIP_EDILENLER:
            ohlcv_4h = exchange.fetch_ohlcv(symbol, timeframe='4h', limit=50)
            df_4h = pd.DataFrame(ohlcv_4h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            ema50 = ta.trend.ema_indicator(df_4h['close'], window=50).iloc[-1]
            ema200 = ta.trend.ema_indicator(df_4h['close'], window=200).iloc[-1]
            ana_trend = "LONG" if ema50 > ema200 else "SHORT"

            ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=30)
            df_15m = pd.DataFrame(ohlcv_15m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            guncel_fiyat = df_15m['close'].iloc[-1]
            rsi_15m = ta.momentum.rsi(df_15m['close'], window=14).iloc[-1]
            
            # AÇIK POZİSYON TAKİBİ
            if symbol in ACIK_POZISYONLAR:
                poz = ACIK_POZISYONLAR[symbol]
                marjin = poz['marjin']
                toplam_pozisyon_degeri = marjin * KALDIRAC
                
                kapatilacak_mi = False
                brut_kar_zarar = 0.0
                durum_notu = ""

                if poz['yon'] == "LONG":
                    if guncel_fiyat >= poz['tp']:
                        brut_kar_zarar = marjin * HEDEF_YUZDESI * KALDIRAC
                        durum_notu = "🎯 HEDEFE ULAŞILDI (TP)"
                        kapatilacak_mi = True
                    elif guncel_fiyat <= poz['sl']:
                        brut_kar_zarar = - (marjin * STOP_YUZDESI * KALDIRAC)
                        durum_notu = "🛑 STOP OLDU (SL)"
                        kapatilacak_mi = True
                    elif ana_trend == "SHORT":
                        fark_yuzdesi = (guncel_fiyat - poz['giris']) / poz['giris']
                        brut_kar_zarar = marjin * fark_yuzdesi * KALDIRAC
                        durum_notu = "⚠️ RÜZGAR DÖNDÜ"
                        kapatilacak_mi = True

                elif poz['yon'] == "SHORT":
                    if guncel_fiyat <= poz['tp']:
                        brut_kar_zarar = marjin * HEDEF_YUZDESI * KALDIRAC
                        durum_notu = "🎯 HEDEFE ULAŞILDI (TP)"
                        kapatilacak_mi = True
                    elif guncel_fiyat >= poz['sl']:
                        brut_kar_zarar = - (marjin * STOP_YUZDESI * KALDIRAC)
                        durum_notu = "🛑 STOP OLDU (SL)"
                        kapatilacak_mi = True
                    elif ana_trend == "LONG":
                        fark_yuzdesi = (poz['giris'] - guncel_fiyat) / poz['giris']
                        brut_kar_zarar = marjin * fark_yuzdesi * KALDIRAC
                        durum_notu = "⚠️ RÜZGAR DÖNDÜ"
                        kapatilacak_mi = True

                if kapatilacak_mi:
                    islem_komisyonu = toplam_pozisyon_degeri * KOMISYON_ORANI
                    net_kar_zarar = brut_kar_zarar - islem_komisyonu
                    
                    KASA["guncel"] = KASA["guncel"] + marjin + net_kar_zarar
                    KASA["gunluk_kar_zarar"] += net_kar_zarar
                    KASA["toplam_ odenen_komisyon"] += islem_komisyonu
                    KASA["toplam_islem"] += 1
                    
                    if net_kar_zarar > 0:
                        KASA["basarili_islem"] += 1
                    else:
                        yeni_ders = f"{symbol} paritesinde RSI {poz.get('giris_rsi', 0):.1f} ile açılan {poz['yon']} işlemde komisyon sonrası zarar yazıldı."
                        if yeni_ders not in KASA["ogrenilen_dersler"]:
                            KASA["ogrenilen_dersler"].append(yeni_ders)
                        KASA["zararli_islem"] += 1

                    telegram_mesaj_gonder(
                        f"{durum_notu} - *{symbol}*\n"
                        f"• Brüt K/Z: `{brut_kar_zarar:+.2f} USD`\n"
                        f"• Kesilen Komisyon: `-{islem_komisyonu:.2f} USD`\n"
                        f"• *Net K/Z:* `{net_kar_zarar:+.2f} USD`\n"
                        f"• Güncel Cüzdan Kasası: `{KASA['guncel']:.2f} USD`"
                    )
                    del ACIK_POZISYONLAR[symbol]
                continue

            # YENİ SİNYAL ÜRETİMİ
            islem_butcesi = KASA["guncel"] * RISK_ORANI
            
            if symbol not in ACIK_POZISYONLAR and KASA["guncel"] >= islem_butcesi:
                if ana_trend == "LONG" and rsi_15m < 45:
                    KASA["guncel"] -= islem_butcesi
                    tp = guncel_fiyat * (1 + HEDEF_YUZDESI)
                    sl = guncel_fiyat * (1 - STOP_YUZDESI)
                    ACIK_POZISYONLAR[symbol] = {"yon": "LONG", "giris": guncel_fiyat, "tp": tp, "sl": sl, "marjin": islem_butcesi, "giris_rsi": rsi_15m}
                    telegram_mesaj_gonder(f"🚀 *YENİ LONG SİNYALİ*\n• Parite: `{symbol}`\n• Fiyat: `{guncel_fiyat:.2f}`\n• Marjin: `{islem_butcesi:.2f} USD` (5x)\n• Kalan Kasa: `{KASA['guncel']:.2f} USD`")
                
                elif ana_trend == "SHORT" and rsi_15m > 55:
                    KASA["guncel"] -= islem_butcesi
                    tp = guncel_fiyat * (1 - HEDEF_YUZDESI)
                    sl = guncel_fiyat * (1 + STOP_YUZDESI)
                    ACIK_POZISYONLAR[symbol] = {"yon": "SHORT", "giris": guncel_fiyat, "tp": tp, "sl": sl, "marjin": islem_butcesi, "giris_rsi": rsi_15m}
                    telegram_mesaj_gonder(f"🩸 *YENİ SHORT SİNYALİ*\n• Parite: `{symbol}`\n• Fiyat: `{guncel_fiyat:.2f}`\n• Marjin: `{islem_butcesi:.2f} USD` (5x)\n• Kalan Kasa: `{KASA['guncel']:.2f} USD`")

    except Exception as e:
        print(f"❌ Tarama Hatası: {e}")

# ==========================================
# 📥 TELEGRAM KOMUTLARI DİNLEYİCİSİ
# ==========================================
def telegram_komutlari_dinle():
    global KASA, ACIK_POZISYONLAR
    son_guncelleme_id = 0
    while True:
        try:
            if not TELEGRAM_TOKEN:
                time.sleep(5)
                continue
                
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={son_guncelleme_id + 1}&timeout=20"
            response = requests.get(url, timeout=25).json()

            if "result" in response:
                for veri in response["result"]:
                    son_guncelleme_id = veri["update_id"]
                    
                    if "message" in veri and "text" in veri["message"]:
                        mesaj_metni = veri["message"]["text"].strip()
                        
                        if mesaj_metni.startswith("/kasa"):
                            parcalar = mesaj_metni.split()
                            if len(parcalar) > 1:
                                try:
                                    yeni_bakiye = float(parcalar[1])
                                    KASA["baslangic"] = yeni_bakiye
                                    KASA["guncel"] = yeni_bakiye
                                    KASA["gunluk_kar_zarar"] = 0.0
                                    KASA["toplam_ odenen_komisyon"] = 0.0
                                    ACIK_POZISYONLAR.clear()
                                    telegram_mesaj_gonder(f"✅ *Kasa Sıfırlandı ve Güncellendi!*\n• Yeni Bakiye: `{yeni_bakiye} USD`")
                                except ValueError:
                                    telegram_mesaj_gonder("⚠️ Örnek kullanım: `/kasa 500`")
                        
                        elif mesaj_metni == "/durum":
                            dersler_str = "\n".join([f"• {d}" for d in KASA["ogrenilen_dersler"][-3:]]) if KASA["ogrenilen_dersler"] else "• Henüz ders yok."
                            durum_mesaj = (
                                f"📊 *ANLIK İZOLE MARJİN RAPORU*\n"
                                f"• Cüzdan Nakit Kasa: `{KASA['guncel']:.2f} USD`\n"
                                f"• Açık Pozisyon: `{len(ACIK_POZISYONLAR)}`\n"
                                f"• Ödenen Komisyon: `{KASA['toplam_ odenen_komisyon']:.2f} USD`\n"
                                f"• Günlük Net K/Z: `{KASA['gunluk_kar_zarar']:+.2f} USD`\n\n"
                                f"🧠 *Son Öğrenilenler:*\n{dersler_str}"
                            )
                            telegram_mesaj_gonder(durum_mesaj)

                        elif mesaj_metni == "/pozisyonlar":
                            if not ACIK_POZISYONLAR:
                                telegram_mesaj_gonder("📭 Şu an açık aktif pozisyon bulunmuyor.")
                            else:
                                poz_mesaji = "📈 *ANLIK AÇIK POZİSYONLAR TAKİBİ*\n\n"
                                for sym, poz in ACIK_POZISYONLAR.items():
                                    try:
                                        # Anlık fiyatı borsadan çekelim
                                        ticker = exchange.fetch_ticker(sym)
                                        anlik_fiyat = ticker['last']
                                        
                                        giris = poz['giris']
                                        marjin = poz['marjin']
                                        
                                        if poz['yon'] == "LONG":
                                            fark_yuzdesi = (anlik_fiyat - giris) / giris
                                        else:
                                            fark_yuzdesi = (giris - anlik_fiyat) / giris
                                            
                                        kaldiracli_kar_yuzdesi = fark_yuzdesi * KALDIRAC * 100
                                        tahmini_kar_usd = marjin * (kaldiracli_kar_yuzdesi / 100)
                                        
                                        durum_ikonu = "🟢" if tahmini_kar_usd >= 0 else "🔴"
                                        
                                        poz_mesaji += (
                                            f"{durum_ikonu} *{sym}* ({poz['yon']} 5x)\n"
                                            f"• Giriş: `{giris:.2f}` | Anlık: `{anlik_fiyat:.2f}`\n"
                                            f"• K/Z Yüzdesi: `%{kaldiracli_kar_yuzdesi:+.2f}`\n"
                                            f"• K/Z USD: `{tahmini_kar_usd:+.2f} USD`\n"
                                            f"-----------------------------------\n"
                                        )
                                    except Exception as ex:
                                        poz_mesaji += f"⚠️ *{sym}* fiyatı alınamadı: {ex}\n"
                                
                                telegram_mesaj_gonder(poz_mesaji)

        except Exception as e:
            print(f"❌ TELEGRAM DİNLEME HATASI: {e}")
        time.sleep(2)

if __name__ == "__main__":
    print("🚀 Pozisyon Takip Özellikli Kripto Ajanı Başlatılıyor...")
    
    t_komut = threading.Thread(target=telegram_komutlari_dinle, daemon=True)
    t_komut.start()
    
    t_tarama = threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True)
    t_tarama.start()
    
    telegram_mesaj_gonder("🟢 Bot Aktif! Artık Telegram'dan `/pozisyonlar` yazarak açık işlemlerin anlık kâr/zarar durumunu görebilirsin.")
    
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, use_reloader=False)
    
