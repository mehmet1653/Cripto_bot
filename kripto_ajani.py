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

# Dolar bütçemize ve min lot sınırımıza uygun mantıklı pariteler
TAKIP_EDILENLER = ['SOL/USDT:USDT', 'XRP/USDT:USDT', 'BNB/USDT:USDT']
AKTIF_GRID_SISTEMLERI = {}
BOT_CALISIYOR_MU = True

# ÖĞRENEN HAFIZA SİSTEMİ
HAFIZA_KAYITLARI = {
    "zarar_gecmisi": {},
    "ogrenilen_yasaklar": {}
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

@app.route('/')
def home():
    durum_str = "AKTİF 🟢" if BOT_CALISIYOR_MU else "BEKLEMEDE ⏸️"
    return f"Gate.io Dolar Bazlı Bot | Durum: {durum_str}"

def set_leverage_safely(symbol, leverage):
    try:
        exchange.set_margin_mode('cross', symbol, {'leverage': leverage})
        exchange.set_leverage(leverage, symbol)
        return True
    except Exception:
        try:
            exchange.set_leverage(leverage, symbol)
            return True
        except Exception:
            return False

def pozisyonu_garantili_kapat(symbol, yon, miktar, sebep_mesaji):
    kapatma_yonu = 'sell' if yon == 'LONG' else 'buy'
    basarili = False
    
    try:
        exchange.create_market_order(symbol, kapatma_yonu, miktar, {'reduce_only': True})
        basarili = True
    except Exception:
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
            
        summary = f"🧠 *DOLAR BAZLI BOT - KASA DURUMU*\n\n"
        summary += f"💰 **Toplam Kasa:** `{total_usdt:.2f} USDT`\n"
        summary += f"💵 **Kullanılabilir:** `{free_usdt:.2f} USDT`\n"
        
        if toplam_anlik_pnl >= 0:
            summary += f"📈 **Toplam PnL:** `+{toplam_anlik_pnl:.2f} USDT` 🟢\n"
        else:
            summary += f"📉 **Toplam PnL:** `{toplam_anlik_pnl:.2f} USDT` 🔴\n"
            
        summary += f"📊 **Aktif Pozisyon:** `{len(active_positions)}`\n"
        
        if active_positions:
            summary += f"\n-----------------------------------\n"
            for p in active_positions:
                sym = p['symbol']
                side = p['side'].upper()
                lev = p.get('leverage', 'Bilinmiyor')
                pnl = float(p.get('unrealizedPnl', 0))
                size = p.get('contracts', 0)
                entry_p = float(p.get('entryPrice', 0))
                
                summary += f"🔹 **{sym}** ({side} | {lev}x)\n"
                summary += f"   Giriş: `{entry_p}` | Boyut: `{size}` | K/Z: `{pnl:.2f} USDT`\n"
        
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
    await update.message.reply_text("🟢 *Bot aktif edildi!*", parse_mode='Markdown')

async def durdur_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ *Bot durduruldu.*", parse_mode='Markdown')

# ==================== ANA STRATEJİ DÖNGÜSÜ ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, HAFIZA_KAYITLARI
    print("🔄 Dolar bazlı arkaplan tarayıcı aktif.")
    
    try:
        exchange.load_markets()
    except Exception as e:
        print(f"Piyasalar yüklenemedi: {e}")
    
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5)
                continue
                
            su_an = time.time()
            
            # 1. Pozisyon senkronizasyonu
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
            except Exception:
                pass

            # 2. Hafıza cezalarını temizle
            for sym in list(HAFIZA_KAYITLARI["ogrenilen_yasaklar"].keys()):
                if su_an > HAFIZA_KAYITLARI["ogrenilen_yasaklar"][sym]["bitis_zamani"]:
                    del HAFIZA_KAYITLARI["ogrenilen_yasaklar"][sym]

            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU:
                    break
                    
                try:
                    ticker = exchange.fetch_ticker(symbol)
                    guncel_fiyat = ticker['last']
                except Exception:
                    continue

                # 3. AÇIK POZİSYON KONTROLÜ
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
                        except Exception:
                            pass

                    if kaldiracli_yuzde >= FINAL_HEDEF_YUZDE:
                        mesaj = f"🚀 *FİNAL HEDEF (BAŞARILI)* - `{symbol}` (`%{kaldiracli_yuzde:.2f}`)"
                        pozisyonu_garantili_kapat(symbol, yon, sistem['miktar'], mesaj)
                        if symbol in HAFIZA_KAYITLARI["zarar_gecmisi"]:
                            HAFIZA_KAYITLARI["zarar_gecmisi"][symbol] = max(0, HAFIZA_KAYITLARI["zarar_gecmisi"][symbol] - 1)
                        if symbol in AKTIF_GRID_SISTEMLERI:
                            del AKTIF_GRID_SISTEMLERI[symbol]
                        
                    elif kaldiracli_yuzde <= -ZARAR_KES_YUZDE:
                        mesaj = f"🛑 *ZARAR KES (HAFIZA KAYDI)* - `{symbol}` (`%{kaldiracli_yuzde:.2f}`)"
                        pozisyonu_garantili_kapat(symbol, yon, sistem['miktar'], mesaj)
                        
                        if symbol not in HAFIZA_KAYITLARI["zarar_gecmisi"]:
                            HAFIZA_KAYITLARI["zarar_gecmisi"][symbol] = 0
                        HAFIZA_KAYITLARI["zarar_gecmisi"][symbol] += 1
                        
                        ceza_suresi = 900 * HAFIZA_KAYITLARI["zarar_gecmisi"][symbol]
                        HAFIZA_KAYITLARI["ogrenilen_yasaklar"][symbol] = {
                            "yon": yon, 
                            "bitis_zamani": su_an + ceza_suresi
                        }
                        telegram_mesaj_gonder(f"🧬 *Hafıza:* `{symbol}` `{yon}` yönünde yanıldı. {ceza_suresi/60:.0f} dk yasak.")
                        
                        if symbol in AKTIF_GRID_SISTEMLERI:
                            del AKTIF_GRID_SISTEMLERI[symbol]
                    continue

                # 4. YENİ POZİSYON AÇMA TARAMASI
                try:
                    balance = exchange.fetch_balance()
                    toplam_bakiye = float(balance['total'].get('USDT', 0))
                except Exception:
                    continue

                sabit_islem_butcesi = toplam_bakiye / 4.0
                if toplam_bakiye < sabit_islem_butcesi:
                    continue

                try:
                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=30)
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    
                    ema7 = ta.trend.ema_indicator(df['close'], window=7).iloc[-1]
                    ema21 = ta.trend.ema_indicator(df['close'], window=21).iloc[-1]
                    rsi = ta.momentum.rsi(df['close'], window=14).iloc[-1]
                except Exception:
                    continue

                grid_yonu = "LONG" if ema7 > ema21 else "SHORT"

                # Hafıza engeli kontrolü
                if symbol in HAFIZA_KAYITLARI["ogrenilen_yasaklar"]:
                    if HAFIZA_KAYITLARI["ogrenilen_yasaklar"][symbol]["yon"] == grid_yonu:
                        continue

                set_leverage_safely(symbol, KALDIRAC)
                toplam_pozisyon_usdt = sabit_islem_butcesi * KALDIRAC
                ham_miktar = toplam_pozisyon_usdt / guncel_fiyat

                # Kesin Min Miktar ve Bütçe Koruma Filtresi
                try:
                    market_info = exchange.market(symbol)
                    min_amount = market_info['limits']['amount']['min'] or 1.0
                except Exception:
                    min_amount = 1.0

                # Eğer bizim hesapladığımız miktar, borsanın zorunlu kıldığı min limitten küçükse 
                # ve bu durum devasa bütçe aşımına yol açacaksa o pariteyi es gec (Yanlışlıkla 1 BTC açmasın)
                if ham_miktar < min_amount:
                    # Sadece min miktar tam bizim bütçemize uygunsa min_amount yap, değilse atla
                    gereken_dolar = min_amount * guncel_fiyat / KALDIRAC
                    if gereken_dolar > sabit_islem_butcesi * 1.5:
                        continue # Bütçemizi aşıyor, bu pariteyi pas geç
                    miktar = min_amount
                else:
                    miktar = float(exchange.amount_to_precision(symbol, ham_miktar))

                emir_yonu = 'buy' if grid_yonu == 'LONG' else 'sell'
                
                try:
                    exchange.create_market_order(symbol, emir_yonu, float(miktar))
                    AKTIF_GRID_SISTEMLERI[symbol] = {
                        "yon": grid_yonu,
                        "merkez_fiyat": guncel_fiyat,
                        "marjin": sabit_islem_butcesi,
                        "miktar": float(miktar),
                        "ilk_hedef_alindi": False
                    }
                    telegram_mesaj_gonder(
                        f"🚀 *İŞLEM AÇILDI ({KALDIRAC}x)*\n"
                        f"• Parite: `{symbol}` ({grid_yonu})\n"
                        f"• Miktar: `{miktar}`"
                    )
                    time.sleep(20)
                except Exception as order_err:
                    print(f"Emir hatası ({symbol}): {order_err}")

        except Exception as loop_err:
            print(f"Döngü hatası: {loop_err}")
        
        time.sleep(15)

if __name__ == "__main__":
    print(f"🚀 Dolar Bazlı Hassas Bot Başlatılıyor...")
    
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
    
