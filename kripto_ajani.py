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
    'options': {'defaultType': 'swap'} # Gerçekçi futures/swap verisi için
})

TAKIP_EDILENLER = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT', 'BNB/USDT:USDT']
ACIK_GRID_POZISYONLARI = {}
BOT_CALISIYOR_MU = False

KASA = {
    "baslangic": 100.0,
    "guncel": 100.0, 
    "toplam_islem": 0,
    "basarili_islem": 0,
    "zararli_islem": 0,
    "gunluk_kar_zarar": 0.0,
    "toplam_odenen_komisyon": 0.0,
    "toplam_odenmis_funding": 0.0,
    "ogrenilen_dersler": []
}

KALDIRAC = 5
RISK_ORANI = 0.20           
GRID_YUZDE_GENISLIK = 0.08  

# Gate.io Futures Standart Komisyon Oranları (Taker/Maker ortalama binde 0.5 - 0.75 arası)
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
    return f"Gate.io Akıllı Grid Bot | Durum: {durum_str}"

# ==========================================
# 🔄 4 COİN İÇİN AKILLI ARKA PLAN & FUNDING TARAYICI
# ==========================================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU
    son_funding_zamani = time.time()
    
    while True:
        try:
            if BOT_CALISIYOR_MU:
                grid_tarama_ve_yonetim()
                
                # Her 4 saatte bir (veya test için periyodik) açık pozisyonlara Fonlama Ücreti yansıt
                su_an = time.time()
                if su_an - son_funding_zamani >= 14400: # 4 Saat = 14400 saniye
                    fonlama_ucretlerini_uygula()
                    son_funding_zamani = su_an
                    
        except Exception as e:
            print(f"Tarama Hatası: {e}")
        time.sleep(30)

def fonlama_ucretlerini_uygula():
    global KASA
    if not ACIK_GRID_POZISYONLARI:
        return
        
    toplam_kesinti = 0.0
    for symbol, poz in ACIK_GRID_POZISYONLARI.items():
        try:
            # Borsadan güncel funding oranını çekmeye çalış, başarısızsa ortalama binde 0.01 al
            funding_orani = 0.0001 
            try:
                fr_data = exchange.fetch_funding_rate(symbol)
                if 'fundingRate' in fr_data and fr_data['fundingRate'] is not None:
                    funding_orani = float(fr_data['fundingRate'])
            except:
                pass
                
            pozisyon_buyuklugu = poz['marjin'] * KALDIRAC
            # Long pozisyonsa ve oran pozitifse funding öder; short'sa tersi
            kesinti = pozisyon_buyuklugu * funding_orani
            
            if poz['yon'] == "LONG_GRID":
                net_kesinti = kesinti
            else:
                net_kesinti = -kesinti # Short için durum tersine dönebilir
                
            KASA["guncel"] -= net_kesinti
            KASA["toplam_odenmis_funding"] += net_kesinti
            toplam_kesinti += net_kesinti
        except Exception as e:
            print(f"Funding hata ({symbol}): {e}")
            
    if abs(toplam_kesinti) > 0.001:
        telegram_mesaj_gonder(f"💸 *4 Saatlik Fonlama (Funding) İşlendi*\n• Toplam Kesinti/Gelir: `{toplam_kesinti:+.4f} USD`\n• Güncel Kasa: `{KASA['guncel']:.2f} USD`")

def grid_tarama_ve_yonetim():
    global KASA
    try:
        for symbol in TAKIP_EDILENLER:
            if not BOT_CALISIYOR_MU:
                break
                
            ticker = exchange.fetch_ticker(symbol)
            guncel_fiyat = ticker['last']

            # 1. MEVCUT KANALLARI KONTROL ET (Stop / TP)
            if symbol in ACIK_GRID_POZISYONLARI:
                grid_veri = ACIK_GRID_POZISYONLARI[symbol]
                alt_sinir = grid_veri['alt_sinir']
                ust_sinir = grid_veri['ust_sinir']
                marjin = grid_veri['marjin']
                yon = grid_veri['yon']
                
                kapat_nedeni = ""
                brut_kar = 0.0

                if yon == "LONG_GRID":
                    if guncel_fiyat <= alt_sinir:
                        kapat_nedeni = "🛑 ALT KANAL KIRILDI (STOP)"
                        brut_kar = - (marjin * 0.04)
                    elif guncel_fiyat >= ust_sinir:
                        kapat_nedeni = "🎯 ÜST KANAL HEDEFE ULAŞTI (TP)"
                        brut_kar = marjin * 0.04
                elif yon == "SHORT_GRID":
                    if guncel_fiyat >= ust_sinir:
                        kapat_nedeni = "🛑 ÜST KANAL KIRILDI (STOP)"
                        brut_kar = - (marjin * 0.04)
                    elif guncel_fiyat <= alt_sinir:
                        kapat_nedeni = "🎯 ALT KANAL HEDEFE ULAŞTI (TP)"
                        brut_kar = marjin * 0.04

                if kapat_nedeni:
                    toplam_deger = marjin * KALDIRAC
                    # İşlem kapatırken de komisyon kesilir (Giriş + Çıkış maliyeti)
                    komisyon = toplam_deger * KOMISYON_ORANI * 2 
                    net_kz = brut_kar - komisyon
                    
                    KASA["guncel"] = KASA["guncel"] + marjin + net_kz
                    KASA["gunluk_kar_zarar"] += net_kz
                    KASA["toplam_odenen_komisyon"] += komisyon
                    KASA["toplam_islem"] += 1
                    
                    if net_kz > 0:
                        KASA["basarili_islem"] += 1
                    else:
                        KASA["zararli_islem"] += 1

                    telegram_mesaj_gonder(
                        f"{kapat_nedeni} - *{symbol} (5x Grid)*\n"
                        f"• Komisyon Dahil Net K/Z: `{net_kz:+.2f} USD`\n"
                        f"• Güncel Kasa: `{KASA['guncel']:.2f} USD`"
                    )
                    del ACIK_GRID_POZISYONLARI[symbol]
                continue

            # 2. YENİ KANAL AÇMA (KOMİSYON DÜŞÜLEREK)
            islem_butcesi = KASA["guncel"] * RISK_ORANI
            if symbol not in ACIK_GRID_POZISYONLARI and KASA["guncel"] >= islem_butcesi:
                ohlcv_4h = exchange.fetch_ohlcv(symbol, timeframe='4h', limit=50)
                df_4h = pd.DataFrame(ohlcv_4h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                
                ema50 = ta.trend.ema_indicator(df_4h['close'], window=30).iloc[-1]
                ema200 = ta.trend.ema_indicator(df_4h['close'], window=min(len(df_4h), 45)).iloc[-1]
                
                atr = ta.volatility.average_true_range(df_4h['high'], df_4h['low'], df_4h['close'], window=14).iloc[-1]
                fiyat_orani_atr = atr / guncel_fiyat
                
                if fiyat_orani_atr < 0.002: 
                    continue 

                grid_turu = "LONG_GRID" if ema50 >= ema200 else "SHORT_GRID"
                alt_sinir = guncel_fiyat * (1 - GRID_YUZDE_GENISLIK)
                ust_sinir = guncel_fiyat * (1 + GRID_YUZDE_GENISLIK)
                
                KASA["guncel"] -= islem_butcesi
                ACIK_GRID_POZISYONLARI[symbol] = {
                    "yon": grid_turu,
                    "giris": guncel_fiyat,
                    "alt_sinir": alt_sinir,
                    "ust_sinir": ust_sinir,
                    "marjin": islem_butcesi
                }
                
                telegram_mesaj_gonder(
                    f"📐 *GERÇEKÇİ KANAL AÇILDI*\n"
                    f"• Parite: `{symbol}` ({grid_turu})\n"
                    f"• Merkez Fiyat: `{guncel_fiyat:.2f}`\n"
                    f"• Alt Sınır: `{alt_sinir:.2f}` | Üst Sınır: `{ust_sinir:.2f}`\n"
                    f"• Marjin: `{islem_butcesi:.2f} USD` (5x)\n"
                    f"• Kalan Nakit: `{KASA['guncel']:.2f} USD`"
                )

    except Exception as e:
        print(f"Analiz Hata: {e}")

# ==========================================
# 📥 TELEGRAM KOMUTLARI
# ==========================================
def telegram_komutlari_dinle():
    global KASA, ACIK_GRID_POZISYONLARI, BOT_CALISIYOR_MU
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
                            telegram_mesaj_gonder("🚀 *Bot Başlatıldı!* Komisyon ve fonlama kesintileri dahil gerçekçi simülasyon devrede.")

                        elif metin == "/kapat":
                            BOT_CALISIYOR_MU = False
                            if not ACIK_GRID_POZISYONLARI:
                                telegram_mesaj_gonder("⏸️ Bot durduruldu. Açık kanal yoktu.")
                            else:
                                iade = sum([p['marjin'] for p in ACIK_GRID_POZISYONLARI.values()])
                                KASA["guncel"] += iade
                                ACIK_GRID_POZISYONLARI.clear()
                                telegram_mesaj_gonder(f"🚨 Bot durduruldu, tüm kanallar kapatıldı! Nakit Kasa: `{KASA['guncel']:.2f} USD`")

                        elif metin == "/durum":
                            bagli = sum([p['marjin'] for p in ACIK_GRID_POZISYONLARI.values()])
                            toplam_varlik = KASA['guncel'] + bagli
                            durum_str = "Çalışıyor 🟢" if BOT_CALISIYOR_MU else "Beklemede ⏸️"
                            
                            durum = (
                                f"📊 *GERÇEKÇİ 4-COİN KANAL RAPORU*\n"
                                f"• Bot Durumu: `{durum_str}`\n"
                                f"• Nakit Kasa: `{KASA['guncel']:.2f} USD`\n"
                                f"• Kanallarda Bağlı: `{bagli:.2f} USD`\n"
                                f"💰 *Toplam Portföy: `{toplam_varlik:.2f} USD`*\n"
                                f"• Ödenen Toplam Komisyon: `{KASA['toplam_odenen_komisyon']:.4f} USD`\n"
                                f"• Ödenen Toplam Funding: `{KASA['toplam_odenmis_funding']:.4f} USD`\n"
                                f"• Aktif Kanal Sayısı: `{len(ACIK_GRID_POZISYONLARI)}`\n"
                                f"• Günlük Net K/Z: `{KASA['gunluk_kar_zarar']:+.2f} USD`"
                            )
                            telegram_mesaj_gonder(durum)

                        elif metin == "/pozisyonlar":
                            if not ACIK_GRID_POZISYONLARI:
                                telegram_mesaj_gonder("📭 Şu an aktif kanal bulunmuyor.")
                            else:
                                poz_mesaji = "📈 *ANLIK KANALLAR (KOMİSYON DAHİL)*\n\n"
                                for sym, poz in ACIK_GRID_POZISYONLARI.items():
                                    try:
                                        ticker = exchange.fetch_ticker(sym)
                                        anlik_fiyat = ticker['last']
                                        giris = poz['giris']
                                        marjin = poz['marjin']
                                        
                                        if poz['yon'] == "LONG_GRID":
                                            fark_yuzdesi = ((anlik_fiyat - giris) / giris) * 100
                                        else:
                                            fark_yuzdesi = ((giris - anlik_fiyat) / giris) * 100
                                            
                                        kaldiracli_yuzde = fark_yuzdesi * KALDIRAC
                                        tahmini_kar = marjin * (kaldiracli_yuzde / 100)
                                        ikon = "🟢" if tahmini_kar >= 0 else "🔴"
                                        
                                        poz_mesaji += (
                                            f"{ikon} *{sym}* ({poz['yon']} 5x)\n"
                                            f"• Giriş: `{giris:.2f}` | Anlık: `{anlik_fiyat:.2f}`\n"
                                            f"• K/Z: `%{kaldiracli_yuzde:+.2f}` (`{tahmini_kar:+.2f} USD`)\n"
                                            f"-----------------------------------\n"
                                        )
                                    except Exception as ex:
                                        poz_mesaji += f"⚠️ *{sym}* hata: {ex}\n"
                                telegram_mesaj_gonder(poz_mesaji)

        except Exception as e:
            print(f"Telegram dinleme hata: {e}")
        time.sleep(2)

if __name__ == "__main__":
    print("🚀 Gerçekçi Komisyon & Funding Destekli Bot Başlatıldı...")
    threading.Thread(target=telegram_komutlari_dinle, daemon=True).start()
    threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True).start()
    telegram_mesaj_gonder("⏸️ Bot güncellendi! Komisyon ve fonlama oranları aktif. Başlatmak için `/baslat` yazabilirsin.")
    
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, use_reloader=False)
                            
