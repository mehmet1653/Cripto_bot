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
    'options': {'defaultType': 'spot'}
})

TAKIP_EDILENLER = ['BTC/USDT', 'ETH/USDT']
ACIK_GRID_POZISYONLARI = {}

KASA = {
    "baslangic": 100.0,
    "guncel": 100.0, 
    "toplam_islem": 0,
    "basarili_islem": 0,
    "zararli_islem": 0,
    "gunluk_kar_zarar": 0.0,
    "toplam_odenen_komisyon": 0.0,
    "ogrenilen_dersler": []
}

# GRID STRATEJİ PARAMETRELERİ
KALDIRAC = 5
RISK_ORANI = 0.30       # Kasanın %30'u ile ızgara alanı açılır
GRID_YUZDE_GENISLIK = 0.08  # Anlık fiyatın %8 altı ve üstü kanal aralığı
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

@app.route('/')
def home():
    return "🟢 Gate.io 5x Grid & Kanal Stratejisi Aktif!"

# ==========================================
# 🔄 5X GRID ARKA PLAN TARAYICI VE KANAL YÖNETİMİ
# ==========================================
def otomatik_arkaplan_tarayici():
    while True:
        try:
            grid_tarama_ve_yonetim()
        except Exception as e:
            print(f"Grid Tarama Hatası: {e}")
        time.sleep(25)

def grid_tarama_ve_yonetim():
    global KASA
    try:
        for symbol in TAKIP_EDILENLER:
            ticker = exchange.fetch_ticker(symbol)
            guncel_fiyat = ticker['last']

            # 1. MEVCUT GRID KANALINI KONTROL ET
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
                    komisyon = toplam_deger * KOMISYON_ORANI
                    net_kz = brut_kar - komisyon
                    
                    KASA["guncel"] = KASA["guncel"] + marjin + net_kz
                    KASA["gunluk_kar_zarar"] += net_kz
                    KASA["toplam_odenen_komisyon"] += komisyon
                    KASA["toplam_islem"] += 1
                    
                    if net_kz > 0:
                        KASA["basarili_islem"] += 1
                    else:
                        KASA["zararli_islem"] += 1
                        ders = f"{symbol} 5x Grid kanal dışına taştığı için kapandı. K/Z: {net_kz:+.2f}$"
                        if ders not in KASA["ogrenilen_dersler"]:
                            KASA["ogrenilen_dersler"].append(ders)

                    telegram_mesaj_gonder(
                        f"{kapat_nedeni} - *{symbol} (5x Grid)*\n"
                        f"• Net K/Z: `{net_kz:+.2f} USD`\n"
                        f"• Güncel Kasa: `{KASA['guncel']:.2f} USD`"
                    )
                    del ACIK_GRID_POZISYONLARI[symbol]
                continue

            # 2. YENİ GRID KANALI KURULUMU
            islem_butcesi = KASA["guncel"] * RISK_ORANI
            if symbol not in ACIK_GRID_POZISYONLARI and KASA["guncel"] >= islem_butcesi:
                ohlcv_4h = exchange.fetch_ohlcv(symbol, timeframe='4h', limit=30)
                df_4h = pd.DataFrame(ohlcv_4h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                ema50 = ta.trend.ema_indicator(df_4h['close'], window=30).iloc[-1]
                ema200 = ta.trend.ema_indicator(df_4h['close'], window=min(len(df_4h), 100)).iloc[-1]
                
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
                    f"📐 *YENİ 5X GRİD KANALI KURULDU*\n"
                    f"• Parite: `{symbol}` ({grid_turu})\n"
                    f"• Merkez Fiyat: `{guncel_fiyat:.2f}`\n"
                    f"• Alt Sınır: `{alt_sinir:.2f}` | Üst Sınır: `{ust_sinir:.2f}`\n"
                    f"• Ayrılan Marjin: `{islem_butcesi:.2f} USD` (5x)\n"
                    f"• Kalan Kasa: `{KASA['guncel']:.2f} USD`"
                )

    except Exception as e:
        print(f"Grid Hata: {e}")

# ==========================================
# 📥 TELEGRAM KOMUTLARI
# ==========================================
def telegram_komutlari_dinle():
    global KASA, ACIK_GRID_POZISYONLARI
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
                        
                        if metin.startswith("/kasa"):
                            parca = metin.split()
                            if len(parca) > 1:
                                yeni = float(parca[1])
                                KASA["baslangic"] = yeni
                                KASA["guncel"] = yeni
                                KASA["gunluk_kar_zarar"] = 0.0
                                KASA["toplam_odenen_komisyon"] = 0.0
                                ACIK_GRID_POZISYONLARI.clear()
                                telegram_mesaj_gonder(f"✅ Kasa sıfırlandı: `{yeni} USD`")

                        elif metin == "/kapat":
                            if not ACIK_GRID_POZISYONLARI:
                                telegram_mesaj_gonder("📭 Aktif grid kanalı yok.")
                            else:
                                iade = sum([p['marjin'] for p in ACIK_GRID_POZISYONLARI.values()])
                                KASA["guncel"] += iade
                                ACIK_GRID_POZISYONLARI.clear()
                                telegram_mesaj_gonder(f"🚨 Tüm ızgaralar acilen kapatıldı! Yeni Kasa: `{KASA['guncel']:.2f} USD`")

                        elif metin == "/durum":
                            bagli = sum([p['marjin'] for p in ACIK_GRID_POZISYONLARI.values()])
                            toplam_varlik = KASA['guncel'] + bagli
                            dersler = "\n".join([f"• {d}" for d in KASA["ogrenilen_dersler"][-3:]]) if KASA["ogrenilen_dersler"] else "• Yok"
                            durum = (
                                f"📊 *5X GRİD KANAL RAPORU*\n"
                                f"• Nakit Kasa: `{KASA['guncel']:.2f} USD`\n"
                                f"• Kanallarda Bağlı: `{bagli:.2f} USD`\n"
                                f"💰 *Toplam Portföy: `{toplam_varlik:.2f} USD`*\n"
                                f"• Aktif Kanal Sayısı: `{len(ACIK_GRID_POZISYONLARI)}`\n"
                                f"• Günlük Net K/Z: `{KASA['gunluk_kar_zarar']:+.2f} USD`\n\n"
                                f"🧠 *Dersler:*\n{dersler}"
                            )
                            telegram_mesaj_gonder(durum)

                        elif metin == "/pozisyonlar":
                            if not ACIK_GRID_POZISYONLARI:
                                telegram_mesaj_gonder("📭 Şu an açık aktif grid kanalı bulunmuyor.")
                            else:
                                poz_mesaji = "📈 *ANLIK 5X GRİD KANALLARI*\n\n"
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
                                            f"• Alt Sınır: `{poz['alt_sinir']:.2f}` | Üst Sınır: `{poz['ust_sinir']:.2f}`\n"
                                            f"• Kanal K/Z: `%{kaldiracli_yuzde:+.2f}` (`{tahmini_kar:+.2f} USD`)\n"
                                            f"-----------------------------------\n"
                                        )
                                    except Exception as ex:
                                        poz_mesaji += f"⚠️ *{sym}* veri alınamadı: {ex}\n"
                                telegram_mesaj_gonder(poz_mesaji)

        except Exception as e:
            print(f"Telegram dinleme hata: {e}")
        time.sleep(2)

if __name__ == "__main__":
    print("🚀 5x Grid Bot Başlatılıyor...")
    threading.Thread(target=telegram_komutlari_dinle, daemon=True).start()
    threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True).start()
    telegram_mesaj_gonder("🟢 Bot güncellendi! Artık `/pozisyonlar` komutu aktif.")
    
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, use_reloader=False)
                            
