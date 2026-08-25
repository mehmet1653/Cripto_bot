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
    return f"Gate.io Şeffaf Bot | Durum: {durum_str}"

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
            
        summary = f"🧠 *ŞEFFAF BOT - KASA DURUMU*\n\n"
        summary += f"💰 **Toplam Kasa:** `{total_usdt:.2f} USDT`\n"
        summary += f"💵 **Kullanılabilir:** `{free_usdt:.2f} USDT`\n"
        
        if toplam_anlik_pnl >= 0:
            summary += f"📈 **Toplam PnL:** `+{toplam_anlik_pnl:.2f} USDT` 🟢\n"
        else:
            summary += f"📉 **Toplam PnL:** `{toplam_anlik_pnl:.2f} USDT` 🔴\n"
            
        summary += f"📊 **Aktif Pozisyon Sayısı:** `{len(active_positions)}`\n"
        
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
    global BOT_CALISIYOR_MU
    print("🔄 Arka plan motoru başlatıldı, piyasa taranıyor...")
    
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5)
                continue
                
            # 1. Bakiye kontrolü
            try:
                balance = exchange.fetch_balance()
                toplam_bakiye = float(balance['total'].get('USDT', 0))
            except Exception as e:
                print(f"[KRİTİK HATA] Bakiye çekilemedi: {e}")
                time.sleep(10)
                continue

            sabit_islem_butcesi = toplam_bakiye / 4.0
            print(f"ℹ️ Mevcut Kasa: {toplam_bakiye} USDT | İşlem Bütçesi: {sabit_islem_butcesi} USDT")

            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU:
                    break
                    
                print(f"🔍 İncelenen Parite: {symbol}")
                
                try:
                    ticker = exchange.fetch_ticker(symbol)
                    guncel_fiyat = ticker['last']
                except Exception as e:
                    print(f"   -> Fiyat alınamadı ({symbol}): {e}")
                    continue

                # OHLCV verisi çekme
                try:
                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=30)
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                except Exception as e:
                    print(f"   -> Mum verisi alınamadı ({symbol}): {e}")
                    continue

                # Göstergeler
                try:
                    ema7 = ta.trend.ema_indicator(df['close'], window=7).iloc[-1]
                    ema21 = ta.trend.ema_indicator(df['close'], window=21).iloc[-1]
                    rsi = ta.momentum.rsi(df['close'], window=14).iloc[-1]
                except Exception as e:
                    print(f"   -> Gösterge hesaplama hatası ({symbol}): {e}")
                    continue

                print(f"   -> EMA7: {ema7:.2f} | EMA21: {ema21:.2f} | RSI: {rsi:.2f}")

                # Çok esnek işlem şartı (Hemen test için)
                grid_yonu = None
                if ema7 > ema21:
                    grid_yonu = "LONG"
                else:
                    grid_yonu = "SHORT"

                print(f"   -> Karar verilen yön: {grid_yonu}. Emir gönderiliyor...")

                # Kaldıraç ve Emir Gönderimi
                try:
                    exchange.set_margin_mode('cross', symbol, {'leverage': KALDIRAC})
                    exchange.set_leverage(KALDIRAC, symbol)
                except Exception as lev_err:
                    print(f"   -> Kaldıraç ayarlama uyarısı (Devam ediliyor): {lev_err}")

                toplam_pozisyon_usdt = sabit_islem_butcesi * KALDIRAC
                miktar = toplam_pozisyon_usdt / guncel_fiyat
                miktar = exchange.amount_to_precision(symbol, miktar)
                emir_yonu = 'buy' if grid_yonu == 'LONG' else 'sell'
                
                try:
                    order = exchange.create_market_order(symbol, emir_yonu, float(miktar))
                    print(f"✅ BAŞARILI! İşlem açıldı: {symbol} {grid_yonu}")
                    telegram_mesaj_gonder(f"🚀 *TEST İŞLEMİ AÇILDI*\n• Parite: `{symbol}` ({grid_yonu})")
                    time.sleep(30) # İlk işlemi açtıktan sonra biraz soluklan
                except Exception as order_err:
                    print(f"❌ EMİR REDDEDİLDİ ({symbol}): {order_err}")
                    telegram_mesaj_gonder(f"🚨 *EMİR HATASI* (`{symbol}`): `{order_err}`")

        except Exception as loop_err:
            print(f"🔥 ANA DÖNGÜ KRİTİK HATA: {loop_err}")
        
        time.sleep(15)

if __name__ == "__main__":
    print(f"🚀 Şeffaf Bot Başlatılıyor...")
    
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
    
