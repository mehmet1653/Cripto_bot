import time
import threading
import requests
import ccxt
import pandas as pd
import ta
import os
from datetime import datetime
from flask import Flask

# ==========================================
# 🌐 RENDER UYUMLU WEB SUNUCUSU
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return "🟢 Komisyon Hesaplamalı Kripto Ajanı Aktif!"

def run_web():
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
    
# ==========================================
# ⚙️ AYARLAR VE HAFIZA (KOMİSYON DAHİL)
# ==========================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

exchange = ccxt.binance({
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

HEDEF_YUZDESI = 0.03  # %3 Hedef
STOP_YUZDESI = 0.015  # %1.5 Stop-Loss
RISK_ORANI = 0.25     # Kasanın 4'te 1'i
KALDIRAC = 5          # 5x Kaldıraç

# Binance ortalama Futures/Spot alım-satım komisyonu (Giriş + Çıkış toplam ortalama %0.08)
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
# 📥 TELEGRAM KOMUTLARI DİNLEYİCİSİ (İŞÇİ 1)
# ==========================================
def telegram_komutlari_dinle():
    global KASA
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
                                    telegram_mesaj_gonder(f"✅ *Kasa Sıfırlandı ve Güncellendi!*\n• Yeni Başlangıç: `{yeni_bakiye} USD`")
                                except ValueError:
                                    telegram_mesaj_gonder("⚠️ Örnek kullanım: `/kasa 500`")
                        
                        elif mesaj_metni == "/durum":
                            dersler_str = "\n".join([f"• {d}" for d in KASA["ogrenilen_dersler"][-3:]]) if KASA["ogrenilen_dersler"] else "• Henüz ders yok."
                            durum_mesaj = (
                                f"📊 *ANLIK DURUM & KOMİSYON RAPORU*\n"
                                f"• Güncel Kasa: `{KASA['guncel']:.2f} USD`\n"
                                f"• Ödenen Komisyon: `{KASA['toplam_ odenen_komisyon']:.2f} USD`\n"
                                f"• Açık Pozisyon: `{len(ACIK_POZISYONLAR)}`\n"
                                f"• Toplam İşlem: `{KASA['toplam_islem']}`\n"
                                f"• Günlük Net K/Z: `{KASA['gunluk_kar_zarar']:+.2f} USD`\n\n"
                                f"🧠 *Son Öğrenilenler:*\n{dersler_str}"
                            )
                            telegram_mesaj_gonder(durum_mesaj)
        except Exception as e:
            print(f"❌ TELEGRAM DİNLEME HATASI: {e}")
        time.sleep(2)

# ==========================================
# 🧠 PİYASA TARAMA VE KOMİSYONLU HESAPLAMA (İŞÇİ 2)
# ==========================================
def piyasayi_tara_ve_takip_et():
    global KASA
    gunsonu_raporu_gonderildi = False
    
    while True:
        simdiki_zaman = datetime.now()
        
        # Saat 23:00 Gün Sonu Raporu
        if simdiki_zaman.hour == 23 and simdiki_zaman.minute == 0:
            if not gunsonu_raporu_gonderildi:
                rapor = (
                    f"🌙 *GÜN SONU BİLANÇOSU (23:00)* 🌙\n"
                    f"-----------------------------------\n"
                    f"• Başlangıç Kasası: `{KASA['baslangic']:.2f} USD`\n"
                    f"• Güncel Kasa: `{KASA['guncel']:.2f} USD`\n"
                    f"• Toplam Ödenen Komisyon: `{KASA['toplam_ odenen_komisyon']:.2f} USD`\n"
                    f"• Toplam İşlem: `{KASA['toplam_islem']}` (Başarılı: {KASA['basarili_islem']} | Zararlı: {KASA['zararli_islem']})\n"
                    f"• Net Günlük Kâr/Zarar: `{KASA['gunluk_kar_zarar']:+.2f} USD`\n"
                    f"-----------------------------------\n"
                    f"🤖 Bot komisyonları düşerek net hesaplama yaptı."
                )
                telegram_mesaj_gonder(rapor)
                gunsonu_raporu_gonderildi = True
        else:
            gunsonu_raporu_gonderildi = False

        for symbol in TAKIP_EDILENLER:
            try:
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
                    islem_boyutu = poz['boyut']
                    toplam_pozisyon_degeri = islem_boyutu * KALDIRAC
                    
                    kapatilacak_mi = False
                    brut_kar_zarar = 0.0
                    durum_notu = ""

                    if poz['yon'] == "LONG":
                        if guncel_fiyat >= poz['tp']:
                            brut_kar_zarar = islem_boyutu * HEDEF_YUZDESI * KALDIRAC
                            durum_notu = "🎯 HEDEFE ULAŞILDI (TP)"
                            kapatilacak_mi = True
                        elif guncel_fiyat <= poz['sl']:
                            brut_kar_zarar = - (islem_boyutu * STOP_YUZDESI * KALDIRAC)
                            durum_notu = "🛑 STOP OLDU (SL)"
                            kapatilacak_mi = True
                        elif ana_trend == "SHORT":
                            fark_yuzdesi = (guncel_fiyat - poz['giris']) / poz['giris']
                            brut_kar_zarar = islem_boyutu * fark_yuzdesi * KALDIRAC
                            durum_notu = "⚠️ RÜZGAR DÖNDÜ"
                            kapatilacak_mi = True

                    elif poz['yon'] == "SHORT":
                        if guncel_fiyat <= poz['tp']:
                            brut_kar_zarar = islem_boyutu * HEDEF_YUZDESI * KALDIRAC
                            durum_notu = "🎯 HEDEFE ULAŞILDI (TP)"
                            kapatilacak_mi = True
                        elif guncel_fiyat >= poz['sl']:
                            brut_kar_zarar = - (islem_boyutu * STOP_YUZDESI * KALDIRAC)
                            durum_notu = "🛑 STOP OLDU (SL)"
                            kapatilacak_mi = True
                        elif ana_trend == "LONG":
                            fark_yuzdesi = (poz['giris'] - guncel_fiyat) / poz['giris']
                            brut_kar_zarar = islem_boyutu * fark_yuzdesi * KALDIRAC
                            durum_notu = "⚠️ RÜZGAR DÖNDÜ"
                            kapatilacak_mi = True

                    if kapatilacak_mi:
                        islem_komisyonu = toplam_pozisyon_degeri * KOMISYON_ORANI
                        net_kar_zarar = brut_kar_zarar - islem_komisyonu
                        
                        KASA["guncel"] += net_kar_zarar
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
                            f"• Güncel Kasa: `{KASA['guncel']:.2f} USD`"
                        )
                        del ACIK_POZISYONLAR[symbol]
                    continue

                # YENİ SİNYAL ÜRETİMİ
                if len(ACIK_POZISYONLAR) < 4:
                    islem_butcesi = KASA["guncel"] * RISK_ORANI
                    
                    if ana_trend == "LONG" and rsi_15m < 45:
                        tp = guncel_fiyat * (1 + HEDEF_YUZDESI)
                        sl = guncel_fiyat * (1 - STOP_YUZDESI)
                        ACIK_POZISYONLAR[symbol] = {"yon": "LONG", "giris": guncel_fiyat, "tp": tp, "sl": sl, "boyut": islem_butcesi, "giris_rsi": rsi_15m}
                        telegram_mesaj_gonder(f"🚀 *YENİ LONG SİNYALİ*\n• Parite: `{symbol}`\n• Fiyat: `{guncel_fiyat:.2f}`\n• Marjin: `{islem_butcesi:.2f} USD` (5x)\n• RSI: `{rsi_15m:.1f}`")
                    
                    elif ana_trend == "SHORT" and rsi_15m > 55:
                        tp = guncel_fiyat * (1 - HEDEF_YUZDESI)
                        sl = guncel_fiyat * (1 + STOP_YUZDESI)
                        ACIK_POZISYONLAR[symbol] = {"yon": "SHORT", "giris": guncel_fiyat, "tp": tp, "sl": sl, "boyut": islem_butcesi, "giris_rsi": rsi_15m}
                        telegram_mesaj_gonder(f"🩸 *YENİ SHORT SİNYALİ*\n• Parite: `{symbol}`\n• Fiyat: `{guncel_fiyat:.2f}`\n• Marjin: `{islem_butcesi:.2f} USD` (5x)\n• RSI: `{rsi_15m:.1f}`")

            except Exception as e:
                print(f"❌ PİYASA TARAMA HATASI ({symbol}): {e}")
            
            time.sleep(2)
            
        time.sleep(180)

if __name__ == "__main__":
    print("🚀 Komisyonlu Kripto Ajanı başlatılıyor...")
    
    def web_sunucusunu_baslat():
        port = int(os.environ.get("PORT", 5000))
        app.run(host='0.0.0.0', port=port)
        
    t_web = threading.Thread(target=web_sunucusunu_baslat, daemon=True)
    t_web.start()
    
    t_komut = threading.Thread(target=telegram_komutlari_dinle, daemon=True)
    t_komut.start()
    
    telegram_mesaj_gonder("🟢 Komisyon Kesintili Kripto Ajanı Devrede ve Hata Takipli Modda Başladı!")
    
    piyasayi_tara_ve_takip_et()
    
