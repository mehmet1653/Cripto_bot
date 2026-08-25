import time
import threading
import requests
import ccxt
import pandas as pd
import ta
import os
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler

app = Flask(__name__)

# ==================== AYARLAR VE ANAHTARLAR ====================
TELEGRAM_TOKEN = "8870934003:AAGIpiwdgpnQVW7nbJIRcR0dOLOzj-MOZsA"
CHAT_ID = "6929517567"

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
BOT_CALISIYOR_MU = True

# ÖĞRENEN HAFIZA SİSTEMİ (Hataları ve cezaları burada tutar)
HAFIZA_KAYITLARI = {
    "zarar_gecmisi": {},  # Hangi parite hangi yönde kaç kez patladı
    "ogrenilen_yasaklar": {} # Hatalardan dolayı geçici yasaklanan yönler
}

KALDIRAC = 10
ILK_HEDEF_YUZDE = 1.5       
FINAL_HEDEF_YUZDE = 2.5     
ZARAR_KES_YUZDE = 1.5       
# ==========================================================

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
        return 0.0

@app.route('/')
def home():
    durum_str = "AKTİF 🟢" if BOT_CALISIYOR_MU else "BEKLEMEDE ⏸️"
    return f"Gate.io Öğrenen Bot | Durum: {durum_str}"

def set_leverage_safely(symbol, leverage):
    try:
        exchange.set_margin_mode('cross', symbol, {'leverage': leverage})
        exchange.set_leverage(leverage, symbol)
        return True
    except Exception as e:
        try:
            exchange.set_leverage(leverage, symbol)
            return True
        except Exception as e2:
            return False

def pozisyonu_garantili_kapat(symbol, yon, miktar, sebep_mesaji):
    kapatma_yonu = 'sell' if yon == 'LONG' else 'buy'
    basarili = False
    
    try:
        exchange.create_market_order(symbol, kapatma_yonu, miktar, {'reduce_only': True})
        basarili = True
    except Exception as e1:
        try:
            time.sleep(0.5)
            ticker = exchange.fetch_ticker(symbol)
            guvenli_fiyat = ticker['ask'] if kapatma_yonu == 'buy' else ticker['bid']
            exchange.create_order(symbol, 'limit', kapatma_yonu, miktar, guvenli_fiyat, {'timeInForce': 'IOC'})
            basarili = True
        except Exception as e2:
            telegram_mesaj_gonder(f"🚨 *POZİSYON KAPATILAMADI!* (`{symbol}`)\nHata: `{str(e2)}`")

    if basarili:
        telegram_mesaj_gonder(sebep_mesaji)

def get_account_status_summary():
    try:
        balance = exchange.fetch_balance()
        total_usdt = float(balance['total'].get('USDT', 0))
        free_usdt = float(balance['free'].get('USDT', 0))
        
        positions = exchange.fetch_positions()
        active_positions = [p for p in positions if float(p.get('contracts', 0)) > 0]
        
        toplam_anlik_pnl = 0.0
        for p in active_positions:
            toplam_anlik_pnl += float(p.get('unrealizedPnl', 0))
            
        summary = f"🧠 *ÖĞRENEN BOT - KASA DURUMU*\n\n"
        summary += f"💰 **Toplam Kasa:** `{total_usdt:.2f} USDT`\n"
        summary += f"💵 **Kullanılabilir:** `{free_usdt:.2f} USDT`\n"
        
        if toplam_anlik_pnl >= 0:
            summary += f"📈 **Toplam PnL:** `+{toplam_anlik_pnl:.2f} USDT` 🟢\n"
        else:
            summary += f"📉 **Toplam PnL:** `{toplam_anlik_pnl:.2f} USDT` 🔴\n"
            
        summary += f"📊 **Aktif Pozisyon:** `{len(active_positions)}`\n"
        summary += f"🧬 **Hafızadaki Cezalı Pariteler:** `{len(HAFIZA_KAYITLARI['ogrenilen_yasaklar'])}`\n"
        
        if active_positions:
            summary += f"\n-----------------------------------\n"
            for p in active_positions:
                sym = p['symbol']
                side = p['side'].upper()
                lev = p.get('leverage', 'Bilinmiyor')
                pnl = float(p.get('unrealizedPnl', 0))
                size = p.get('contracts', 0)
                entry_p = float(p.get('entryPrice', 0))
                
                pnl_ikon = "🟢" if pnl >= 0 else "🔴"
                summary += f"🔹 **{sym}** ({side} | {lev}x)\n"
                summary += f"   Giriş: `{entry_p}` | Boyut: `{size}`\n"
                summary += f"   Anlık K/Z: `{pnl:.2f} USDT` {pnl_ikon}\n"
        
        return summary
    except Exception as e:
        return f"Kasa durumu alınırken hata oluştu: {str(e)}"

# ==================== TELEGRAM KOMUTLARI ====================
async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_text = get_account_status_summary()
    await update.message.reply_text(status_text, parse_mode='Markdown')

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = True
    await update.message.reply_text("🟢 *Bot aktif edildi, öğrenen motor devrede!*", parse_mode='Markdown')

async def durdur_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ *Bot durduruldu.*", parse_mode='Markdown')

# ==================== ANA STRATEJİ DÖNGÜSÜ ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, HAFIZA_KAYITLARI
    print("🔄 Öğrenen arkaplan tarayıcı aktif.")
    
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5)
                continue
                
            su_an = time.time()
            
            # 1. Borsa Senkronizasyonu
            try:
                b_positions = exchange.fetch_positions()
                for bp in b_positions:
                    kontrat = float(bp.get('contracts', 0))
                    sym = bp['symbol']
                    if kontrat > 0:
                        b_yon = bp['side'].upper()
                        b_giris = float(bp.get('entryPrice', 0))
                        if sym not in AKTIF_GRID_SISTEMLERI:
                            AKTIF_GRID_SISTEMLERI[sym] = {
                                "yon": b_yon,
                                "merkez_fiyat": b_giris,
                                "marjin": kontrat * b_giris / KALDIRAC,
                                "miktar": kontrat,
                                "ilk_hedef_alindi": False
                            }
            except Exception as sync_err:
                pass

            # 2. Öğrenilen Yasak Sürelerinin Kontrolü ve Esnetilmesi
            for sym in list(HAFIZA_KAYITLARI["ogrenilen_yasaklar"].keys()):
                if su_an > HAFIZA_KAYITLARI["ogrenilen_yasaklar"][sym]["bitis_zamani"]:
                    del HAFIZA_KAYITLARI["ogrenilen_yasaklar"][sym]

            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU:
                    break
                    
                try:
                    ticker = exchange.fetch_ticker(symbol)
                    guncel_fiyat = ticker['last']
                except Exception as e:
                    continue

                # 3. AÇIK POZİSYON KONTROLÜ VE ÖĞRENME KAYDI
                if symbol in AKTIF_GRID_SISTEMLERI:
                    sistem = AKTIF_GRID_SISTEMLERI[symbol]
                    merkez = sistem['merkez_fiyat']
                    yon = sistem['yon']
                    
                    fark_orani = (guncel_fiyat - merkez) / merkez
                    if yon == "SHORT": 
                        fark_orani = -fark_orani
                        
                    kaldiracli_yuzde = fark_orani * KALDIRAC * 100
                    
                    if kaldiracli_yuzde >= ILK_HEDEF_YUZDE and not sistem.get("ilk_hedef_alindi", False):
                        sistem.update({"ilk_hedef_alindi": True})
                        try:
                             miktar_cinsi = sistem['miktar'] / 2
                             kapatma_yonu = 'sell' if yon == 'LONG' else 'buy'
                             exchange.create_market_order(symbol, kapatma_yonu, miktar_cinsi, {'reduce_only': True})
                             telegram_mesaj_gonder(f"🎯 *1. KADEME KÂR* - `{symbol}` (`%{kaldiracli_yuzde:.2f}`)")
                        except Exception as e:
                            pass

                    if kaldiracli_yuzde >= FINAL_HEDEF_YUZDE:
                        mesaj = f"🚀 *FİNAL HEDEF (BAŞARILI)* - `{symbol}` (`%{kaldiracli_yuzde:.2f}`)"
                        pozisyonu_garantili_kapat(symbol, yon, sistem['miktar'], mesaj)
                        # Başarılı olunca hafızadaki ceza puanını hafiflet
                        if symbol in HAFIZA_KAYITLARI["zarar_gecmisi"]:
                            HAFIZA_KAYITLARI["zarar_gecmisi"][symbol] = max(0, HAFIZA_KAYITLARI["zarar_gecmisi"][symbol] - 1)
                        if symbol in AKTIF_GRID_SISTEMLERI:
                            del AKTIF_GRID_SISTEMLERI[symbol]
                        
                    elif kaldiracli_yuzde <= -ZARAR_KES_YUZDE:
                        mesaj = f"🛑 *ZARAR KES (HATA ÖĞRENİldİ)* - `{symbol}` (`%{kaldiracli_yuzde:.2f}`)"
                        pozisyonu_garantili_kapat(symbol, yon, sistem['miktar'], mesaj)
                        
                        # --- HATA KAYDETME VE ÖĞRENME MEKANİZMASI ---
                        if symbol not in HAFIZA_KAYITLARI["zarar_gecmisi"]:
                            HAFIZA_KAYITLARI["zarar_gecmisi"][symbol] = 0
                        HAFIZA_KAYITLARI["zarar_gecmisi"][symbol] += 1
                        
                        # Zarar tekrar ettikçe cezalı kalma süresini katlayarak artır (Akıllı öğrenme)
                        ceza_suresi = 900 * HAFIZA_KAYITLARI["zarar_gecmisi"][symbol] # 15dk * hata sayısı
                        HAFIZA_KAYITLARI["ogrenilen_yasaklar"][symbol] = {
                            "yon": yon, 
                            "bitis_zamani": su_an + ceza_suresi
                        }
                        telegram_mesaj_gonder(f"🧬 *Hafıza Güncellendi:* `{symbol}` `{yon}` yönünde yanıldı. Bu yönde {ceza_suresi/60:.0f} dakika boyunca işlem açılmayacak.")
                        
                        if symbol in AKTIF_GRID_SISTEMLERI:
                            del AKTIF_GRID_SISTEMLERI[symbol]
                    continue

                # 4. YENİ POZİSYON TARAMASI (Hafıza Filtreli)
                toplam_bakiye = bakiye_al()
                bagli_marjin = sum([p['marjin'] for p in AKTIF_GRID_SISTEMLERI.values()])
                anlik_portfoy = toplam_bakiye + bagli_marjin
                sabit_islem_butcesi = anlik_portfoy / 4.0

                if toplam_bakiye < sabit_islem_butcesi:
                    continue

                try:
                    ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
                except Exception as e:
                    continue

                df_15m = pd.DataFrame(ohlcv_15m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                
                ema7 = ta.trend.ema_indicator(df_15m['close'], window=7).iloc[-1]
                ema21 = ta.trend.ema_indicator(df_15m['close'], window=21).iloc[-1]
                rsi15m = ta.momentum.rsi(df_15m['close'], window=14).iloc[-1]

                grid_yonu = None

                if ema7 > ema21 and rsi15m > 45:
                    grid_yonu = "LONG"
                elif ema7 < ema21 and rsi15m < 55:
                    grid_yonu = "SHORT"

                if not grid_yonu:
                    continue

                # Hafıza Kontrolü: Bot daha önce burada bu yönde zarar ettiyse bu sinyali reddeder!
                if symbol in HAFIZA_KAYITLARI["ogrenilen_yasaklar"]:
                    yasak_bilgi = HAFIZA_KAYITLARI["ogrenilen_yasaklar"][symbol]
                    if yasak_bilgi["yon"] == grid_yonu:
                        continue # Hafıza engelledi, bu yönde işlem açmıyor

                set_leverage_safely(symbol, KALDIRAC)
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
                        f"🧠 *ÖĞRENEN BOT İŞLEM AÇTI ({KALDIRAC}x)*\n"
                        f"• Parite: `{symbol}` ({grid_yonu})\n"
                        f"• Marjin: `{sabit_islem_butcesi:.2f} USD`\n"
                        f"• Fiyat: `{guncel_fiyat:.2f}`"
                    )
                except Exception as e:
                    print(f"[EMİR HATASI]: {e}")

        except Exception as e:
            print(f"[DÖNGÜ HATASI]: {e}")
        
        time.sleep(15)

if __name__ == "__main__":
    print(f"🚀 Öğrenen Bot Başlatılıyor...")
    
    threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True).start()
    
    port = int(os.environ.get("PORT", 5000))
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=port, use_reloader=False), daemon=True).start()
    
    app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app_tg.add_handler(CommandHandler("durum", durum_komutu))
    app_tg.add_handler(CommandHandler("pozisyonlar", durum_komutu))
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    
    print("Telegram komut dinleyicisi aktif...")
    app_tg.run_polling()
    
