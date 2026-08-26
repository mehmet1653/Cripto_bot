import time
import threading
import requests
import ccxt
import pandas as pd
import ta
import os
import json
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler

app = Flask(__name__)

# ==================== AYARLAR VE ANAHTARLAR ====================
TELEGRAM_TOKEN = "8870934003:AAGIpiwdgpnQVW7nbJIRcR0dOLOzj-MOZsA"
CHAT_ID = "6929517567"

exchange = ccxt.gate({
    'enableRateLimit': True,
    'timeout': 15000,
    'options': {
        'defaultType': 'swap'
    }
})

TAKIP_EDILENLER = [
    'BTC/USDT:USDT', 
    'ETH/USDT:USDT', 
    'SOL/USDT:USDT', 
    'XRP/USDT:USDT'
]

BOT_CALISIYOR_MU = False

HAFIZA_DOSYASI = "paper_trading_hafiza.json"

def hafizayi_yukle():
    if os.path.exists(HAFIZA_DOSYASI):
        try:
            with open(HAFIZA_DOSYASI, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "bakiye": 100.0,  # Simülasyon başlangıç kasası (100 USDT)
        "aktif_pozisyonlar": {},
        "analitik": {
            "basarisiz_analizler": [],
            "basarili_islem_sayisi": 0,
            "basarisiz_islem_sayisi": 0,
            "gunluk_net_kar_usd": 0.0
        }
    }

def hafizayi_kaydet():
    try:
        data = {
            "bakiye": SANAL_BAKIYE,
            "aktif_pozisyonlar": SANAL_POZISYONLAR,
            "analitik": ANALitik_HAFIZA
        }
        with open(HAFIZA_DOSYASI, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"Hafıza kaydetme hatası: {e}")

kalici_veri = hafizayi_yukle()
SANAL_BAKIYE = kalici_veri.get("bakiye", 100.0)
SANAL_POZISYONLAR = kalici_veri.get("aktif_pozisyonlar", {})
ANALitik_HAFIZA = kalici_veri.get("analitik", {
    "basarisiz_analizler": [],
    "basarili_islem_sayisi": 0,
    "basarisiz_islem_sayisi": 0,
    "gunluk_net_kar_usd": 0.0
})

KALDIRAC = 10
HEDEF_ROESINI_ISTENEN = 5.0      # %5 Kâr (ROI)
ZARAR_KES_ROESINI_ISTENEN = 10.0 # %10 Zarar (ROI)
MIN_ADX_GUCU = 20.0              
KOMISYON_ORANI = 0.0005          # Gerçekçi ortalama işlem komisyonu (%0.05 toplam)

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
    durum_str = "AKTİF (PAPER TRADING) 🟢" if BOT_CALISIYOR_MU else "BEKLEMEDE ⏸️"
    return f"Sanal Simülasyon Botu | Kasa: {SANAL_BAKIYE:.2f} USDT | Durum: {durum_str}"

def pozisyonu_kapat_sanal(symbol, sebep_mesaji):
    global SANAL_BAKIYE
    if symbol not in SANAL_POZISYONLAR:
        return

    pos = SANAL_POZISYONLAR[symbol]
    yon = pos["yon"]
    giris = pos["giris_fiyati"]
    marjin = pos["margin"]
    
    try:
        ticker = exchange.fetch_ticker(symbol)
        cikis_fiyati = ticker['last']
    except Exception:
        cikis_fiyati = giris

    fark = (cikis_fiyati - giris) / giris
    if yon == "SHORT":
        fark = -fark

    roe = fark * 100 * KALDIRAC
    brut_pnl = marjin * (roe / 100.0)
    
    islem_hacmi = marjin * KALDIRAC
    komisyon_kesintisi = islem_hacmi * KOMISYON_ORANI * 2 
    
    net_pnl = brut_pnl - komisyon_kesintisi
    SANAL_BAKIYE += marjin + net_pnl

    ANALitik_HAFIZA["gunluk_net_kar_usd"] += net_pnl

    if net_pnl >= 0:
        ANALitik_HAFIZA["basarili_islem_sayisi"] += 1
    else:
        ANALitik_HAFIZA["basarisiz_islem_sayisi"] += 1
        ANALitik_HAFIZA["basarisiz_analizler"].append({
            "symbol": symbol,
            "yanlis_yon": yon,
            "giris_fiyati": giris,
            "zarar_orani": roe,
            "zarar_usd": net_pnl,
            "zaman": time.strftime('%H:%M:%S')
        })

    del SANAL_POZISYONLAR[symbol]
    hafizayi_kaydet()

    if sebep_mesaji:
        pnl_isaret = "+" if net_pnl >= 0 else ""
        tam_mesaj = f"{sebep_mesaji}\n   Net K/Z: `{pnl_isaret}{net_pnl:.2f} USDT` (`{pnl_isaret}{roe:.2f}% ROI` - Komisyon düşüldü)"
        telegram_mesaj_gonder(tam_mesaj)

def get_account_status_summary():
    toplam_anlik_pnl = 0.0
    toplam_marjin_degeri = 0.0
    aktif_ozet_listesi = []

    for sym, pos in SANAL_POZISYONLAR.items():
        toplam_marjin_degeri += pos["margin"]
        try:
            ticker = exchange.fetch_ticker(sym)
            guncel_fiyat = ticker['last']
        except Exception:
            guncel_fiyat = pos["giris_fiyati"]

        fark = (guncel_fiyat - pos["giris_fiyati"]) / pos["giris_fiyati"]
        if pos["yon"] == "SHORT":
            fark = -fark
        roe = fark * 100 * KALDIRAC
        pnl = pos["margin"] * (roe / 100.0)
        toplam_anlik_pnl += pnl

        aktif_ozet_listesi.append({
            'symbol': sym,
            'side': pos["yon"],
            'leverage': KALDIRAC,
            'pnl': pnl,
            'roe': roe,
            'entryPrice': pos["giris_fiyati"],
            'margin': pos["margin"]
        })

    anlik_toplam_kasa = SANAL_BAKIYE + toplam_marjin_degeri + toplam_anlik_pnl
    gunluk_pnl = ANALitik_HAFIZA['gunluk_net_kar_usd']
    
    summary = f"📊 *PAPER TRADING (SANAL) DURUM RAPORU*\n\n"
    summary += f"💰 **Toplam Güncel Kasa:** `{anlik_toplam_kasa:.2f} USDT`\n"
    summary += f"💵 **Kasadaki Nakit (Serbest):** `{SANAL_BAKIYE:.2f} USDT`\n"
    summary += f"🔒 **Dolaşımdaki Marjin (İşlemde):** `{toplam_marjin_degeri:.2f} USDT`\n"
    
    if toplam_anlik_pnl >= 0:
        summary += f"📈 **Anlık Açık PnL:** `+{toplam_anlik_pnl:.2f} USDT` 🟢\n"
    else:
        summary += f"📉 **Anlık Açık PnL:** `{toplam_anlik_pnl:.2f} USDT` 🔴\n"

    if gunluk_pnl >= 0:
        summary += f"📅 **Genel Toplam Kâr:** `+{gunluk_pnl:.2f} USDT` 🟢\n"
    else:
        summary += f"📅 **Genel Toplam Zarar:** `{gunluk_pnl:.2f} USDT` 🔴\n"
        
    summary += f"🎯 **Başarılı Pozisyon:** `{ANALitik_HAFIZA['basarili_islem_sayisi']}` adet\n"
    summary += f"🛑 **Başarısız Pozisyon:** `{ANALitik_HAFIZA['basarisiz_islem_sayisi']}` adet\n"
    
    if aktif_ozet_listesi:
        summary += f"\n-----------------------------------\n"
        for p in aktif_ozet_listesi:
            pnl_isaret = "+" if p['pnl'] >= 0 else ""
            summary += f"🔹 **{p['symbol']}** ({p['side']} | {p['leverage']}x İzole)\n"
            summary += f"   Giriş: `{p['entryPrice']}` | Marjin: `{p['margin']:.2f}$`\n"
            summary += f"   K/Z: `{pnl_isaret}{p['pnl']:.2f} USDT` (`{pnl_isaret}{p['roe']:.2f}%`)\n"
    
    return summary

# ==================== TELEGRAM KOMUTLARI ====================
async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(get_account_status_summary(), parse_mode='Markdown')

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = True
    await update.message.reply_text("🟢 *Sanal simülasyon botu aktif edildi! Taramalar başladı.*", parse_mode='Markdown')

async def durdur_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ *Bot durduruldu.*", parse_mode='Markdown')

async def kapat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 *Tüm aktif simülasyon pozisyonları kapatılıyor...*", parse_mode='Markdown')
    semboller = list(SANAL_POZISYONLAR.keys())
    for sym in semboller:
        pozisyonu_kapat_sanal(sym, f"🛑 *MANUEL KAPATMA (/kapat)* - `{sym}`")
    await update.message.reply_text("✅ Tüm sanal pozisyonlar kapatıldı ve kasaya eklendi.", parse_mode='Markdown')

# ==================== ANA STRATEJİ DÖNGÜSÜ ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, SANAL_BAKIYE
    print("🔄 Paper Trading Akıllı Tarayıcı hazırda bekliyor (Komut bekleniyor).")
    
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(2)
                continue

            # 1. Açık pozisyonların Kâr Al / Zarar Kes kontrolü
            aktif_semboller = list(SANAL_POZISYONLAR.keys())
            for symbol in aktif_semboller:
                pos = SANAL_POZISYONLAR[symbol]
                try:
                    ticker = exchange.fetch_ticker(symbol)
                    guncel_fiyat = ticker['last']
                except Exception:
                    continue

                fark = (guncel_fiyat - pos["giris_fiyati"]) / pos["giris_fiyati"]
                if pos["yon"] == "SHORT":
                    fark = -fark
                roe = fark * 100 * KALDIRAC

                if roe >= HEDEF_ROESINI_ISTENEN:
                    pozisyonu_kapat_sanal(symbol, f"🚀 *HEDEF BAŞARILI (KÂR ALINDI)* - `{symbol}` (`+{roe:.2f}% ROI`)")
                elif roe <= -ZARAR_KES_ROESINI_ISTENEN:
                    pozisyonu_kapat_sanal(symbol, f"🛑 *ZARAR KES (STOP) & ANALİZE EKLENDİ* - `{symbol}` (`{roe:.2f}% ROI`)")

            # 2. Yeni işlem arama (1/4 kasa kuralı)
            if len(SANAL_POZISYONLAR) == 0:
                for symbol in TAKIP_EDILENLER:
                    if not BOT_CALISIYOR_MU or len(SANAL_POZISYONLAR) > 0:
                        break
                        
                    try:
                        ticker = exchange.fetch_ticker(symbol)
                        guncel_fiyat = ticker['last']
                    except Exception:
                        continue

                    if SANAL_BAKIYE < 10.0:
                        continue

                    hedef_marjin = SANAL_BAKIYE / 4.0

                    try:
                        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
                        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                        
                        ema7 = ta.trend.ema_indicator(df['close'], window=7).iloc[-1]
                        ema21 = ta.trend.ema_indicator(df['close'], window=21).iloc[-1]
                        rsi = ta.momentum.rsi(df['close'], window=14).iloc[-1]
                        
                        adx_indicator = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14)
                        adx_degeri = adx_indicator.adx().iloc[-1]
                    except Exception:
                        continue

                    if adx_degeri < MIN_ADX_GUCU:
                        continue

                    grid_yonu = "LONG" if ema7 > ema21 else "SHORT"

                    if grid_yonu == "SHORT" and rsi < 42:
                        continue 
                    if grid_yonu == "LONG" and rsi > 58:
                        continue 

                    son_hatalar = [h for h in ANALitik_HAFIZA["basarisiz_analizler"] if h["symbol"] == symbol]
                    if son_hatalar:
                        son_hata = son_hatalar[-1]
                        if son_hata["yanlis_yon"] == grid_yonu and abs(rsi - 50) < 5:
                            continue

                    # Sanal pozisyon açılışı
                    SANAL_BAKIYE -= hedef_marjin
                    SANAL_POZISYONLAR[symbol] = {
                        "yon": grid_yonu,
                        "giris_fiyati": guncel_fiyat,
                        "margin": hedef_marjin,
                        "giris_rsi": rsi
                    }
                    hafizayi_kaydet()

                    fiyat_hedef_orani = HEDEF_ROESINI_ISTENEN / (100.0 * KALDIRAC)
                    fiyat_stop_orani = ZARAR_KES_ROESINI_ISTENEN / (100.0 * KALDIRAC)

                    if grid_yonu == 'LONG':
                        stop_fiyati = guncel_fiyat * (1.0 - fiyat_stop_orani)
                        hedef_fiyati = guncel_fiyat * (1.0 + fiyat_hedef_orani)
                    else:
                        stop_fiyati = guncel_fiyat * (1.0 + fiyat_stop_orani)
                        hedef_fiyati = guncel_fiyat * (1.0 - fiyat_hedef_orani)

                    telegram_mesaj_gonder(
                        f"🛡️ *SANAL İŞLEM AÇILDI - {KALDIRAC}x*\n"
                        f"• Parite: `{symbol}` ({grid_yonu})\n"
                        f"• Giriş Fiyatı: `{guncel_fiyat}`\n"
                        f"• Hedef Kâr: `~{hedef_fiyati:.4f}` (`+{HEDEF_ROESINI_ISTENEN}% ROI`)\n"
                        f"• Zarar Kes: `~{stop_fiyati:.4f}` (`-{ZARAR_KES_ROESINI_ISTENEN}% ROI`)\n"
                        f"• Trend Gücü (ADX): `{adx_degeri:.1f}`\n"
                        f"• Marjin (1/4 Kasa): `~{hedef_marjin:.2f} USDT`\n"
                        f"• Giriş RSI: `{rsi:.1f}`"
                    )
                    time.sleep(5)

        except Exception as loop_err:
            print(f"Simülasyon döngü hatası: {loop_err}")
        
        time.sleep(3)

if __name__ == "__main__":
    print(f"🛡️ Paper Trading (Sanal Simülasyon) Botu Başlatıldı. Telegram'dan /baslat komutu bekleniyor...")
    
    # Başlamadan önce eski webhook kalıntılarını ve takılan kilitleri temizle
    try:
        requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook?drop_pending_updates=true", timeout=5)
    except Exception:
        pass

    threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True).start()
    
    port = int(os.environ.get("PORT", 5000))
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=port, use_reloader=False), daemon=True).start()
    
    app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app_tg.add_handler(CommandHandler("durum", durum_komutu))
    app_tg.add_handler(CommandHandler("pozisyonlar", durum_komutu))
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))
    
    print("Telegram komut dinleyicisi aktif...")
    try:
        app_tg.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        print(f"Telegram polling hatası: {e}")
