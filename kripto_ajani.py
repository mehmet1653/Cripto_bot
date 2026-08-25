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

# BNB çıkarıldı; BTC, ETH, SOL ve XRP ile optimize edildi
TAKIP_EDILENLER = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT', 'XRP/USDT:USDT']
AKTIF_GRID_SISTEMLERI = {}
BOT_CALISIYOR_MU = True

ANALitik_HAFIZA = {
    "basarisiz_analizler": []
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
    return f"Optimizasyonlu Analitik Bot | Durum: {durum_str}"

def set_isolated_leverage_safely(symbol, leverage):
    try:
        exchange.set_margin_mode('isolated', symbol, {'leverage': leverage})
    except Exception:
        pass
        
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
            
        summary = f"🧠 *ANALİTİK BOT - KASA DURUMU (% ve $)*\n\n"
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
                margin_tutari = float(p.get('initialMargin', 0)) or (float(size) * entry_p / KALDIRAC)
                
                roe_yuzde = (pnl / margin_tutari * 100) if margin_tutari > 0 else 0.0
                pnl_isaret = "+" if pnl >= 0 else ""
                
                summary += f"🔹 **{sym}** ({side} | {lev}x İzole)\n"
                summary += f"   Giriş: `{entry_p}` | Kontrat: `{size}` | Marjin: `{margin_tutari:.2f}$`\n"
                summary += f"   K/Z: `{pnl_isaret}{pnl:.2f} USDT` (`{pnl_isaret}{roe_yuzde:.2f}%`)\n"
        
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

async def kapat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global AKTIF_GRID_SISTEMLERI
    await update.message.reply_text("🔄 *Tüm açık pozisyonlar kapatılıyor...*", parse_mode='Markdown')
    
    try:
        positions = exchange.fetch_positions()
        active_positions = [p for p in positions if float(p.get('contracts', 0)) > 0]
        
        if not active_positions:
            await update.message.reply_text("ℹ️ Zaten açık hiçbir pozisyon bulunmuyor.", parse_mode='Markdown')
            return

        kapatilanlar = 0
        for p in active_positions:
            sym = p['symbol']
            yon = p['side'].upper()
            kontrat = float(p['contracts'])
            
            mesaj = f"🛑 *MANUEL KAPATMA (/kapat)* - `{sym}`"
            pozisyonu_garantili_kapat(sym, yon, kontrat, mesaj)
            kapatilanlar += 1

        AKTIF_GRID_SISTEMLERI.clear()
        await update.message.reply_text(f"✅ İşlem tamamlandı. Toplam `{kapatilanlar}` pozisyon kapatıldı.", parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Pozisyonlar kapatılırken hata oluştu: `{str(e)}`", parse_mode='Markdown')

# ==================== ANA STRATEJİ DÖNGÜSÜ ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, ANALitik_HAFIZA
    print("🔄 Optimize edilmiş arkaplan tarayıcı aktif.")
    
    try:
        exchange.load_markets()
    except Exception as e:
        print(f"Piyasalar yüklenemedi: {e}")
    
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5)
                continue
                
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
                                "ilk_hedef_alindi": False,
                                "giris_rsi": 50.0
                            }
            except Exception:
                pass

            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU:
                    break
                    
                try:
                    ticker = exchange.fetch_ticker(symbol)
                    guncel_fiyat = ticker['last']
                except Exception:
                    continue

                # 2. AÇIK POZİSYON KONTROLÜ
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
                             telegram_mesaj_gonder(f"🎯 *1. KADEME KÂR* - `{symbol}` (`+{kaldiracli_yuzde:.2f}%`)")
                        except Exception:
                            pass

                    if kaldiracli_yuzde >= FINAL_HEDEF_YUZDE:
                        mesaj = f"🚀 *FİNAL HEDEF (BAŞARILI)* - `{symbol}` (`+{kaldiracli_yuzde:.2f}%`)"
                        pozisyonu_garantili_kapat(symbol, yon, sistem['miktar'], mesaj)
                        if symbol in AKTIF_GRID_SISTEMLERI:
                            del AKTIF_GRID_SISTEMLERI[symbol]
                        
                    elif kaldiracli_yuzde <= -ZARAR_KES_YUZDE:
                        mesaj = f"🛑 *ZARAR KES & ANALİTİK DERS* - `{symbol}` (`{kaldiracli_yuzde:.2f}%`)"
                        pozisyonu_garantili_kapat(symbol, yon, sistem['miktar'], mesaj)
                        
                        analitik_hata_notu = {
                            "symbol": symbol,
                            "yanlis_yon": yon,
                            "giris_fiyati": merkez,
                            "zarar_orani": kaldiracli_yuzde,
                            "zaman": time.strftime('%H:%M:%S')
                        }
                        ANALitik_HAFIZA["basarisiz_analizler"].append(analitik_hata_notu)
                        
                        telegram_mesaj_gonder(
                            f"🧬 *Hafıza Güncellendi (Analitik Ders):*\n"
                            f"• Parite: `{symbol}`\n"
                            f"• Yanılan Yön: `{yon}`\n"
                            f"• Çıkarılan Ders: Hata not edilerek sonraki döngüye aktarıldı."
                        )
                        
                        if symbol in AKTIF_GRID_SISTEMLERI:
                            del AKTIF_GRID_SISTEMLERI[symbol]
                    continue

                # 3. YENİ POZİSYON AÇMA TARAMASI
                try:
                    balance = exchange.fetch_balance()
                    toplam_bakiye = float(balance['total'].get('USDT', 0))
                except Exception:
                    continue

                hedef_marjin = toplam_bakiye / 4.0
                if toplam_bakiye < hedef_marjin:
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

                son_hatalar = [h for h in ANALitik_HAFIZA["basarisiz_analizler"] if h["symbol"] == symbol]
                if son_hatalar:
                    son_hata = son_hatalar[-1]
                    if son_hata["yanlis_yon"] == grid_yonu and abs(rsi - 50) < 5:
                        continue

                set_isolated_leverage_safely(symbol, KALDIRAC)
                
                hedef_pozisyon_usdt = hedef_marjin * KALDIRAC
                ham_miktar = hedef_pozisyon_usdt / guncel_fiyat

                try:
                    market_info = exchange.market(symbol)
                    contract_size = float(market_info.get('contractSize', 1.0))
                    min_amount = float(market_info['limits']['amount']['min'] or 1.0)
                    
                    gercek_ham_miktar = ham_miktar / contract_size
                    if gercek_ham_miktar < min_amount:
                        gercek_ham_miktar = min_amount
                        
                    miktar = float(exchange.amount_to_precision(symbol, gercek_ham_miktar))
                except Exception:
                    miktar = float(exchange.amount_to_precision(symbol, ham_miktar))

                emir_yonu = 'buy' if grid_yonu == 'LONG' else 'sell'
                
                try:
                    exchange.create_market_order(symbol, emir_yonu, miktar)
                    hesaplanan_marjin = (miktar * contract_size * guncel_fiyat) / KALDIRAC if 'contract_size' in locals() else hedef_marjin
                    
                    AKTIF_GRID_SISTEMLERI[symbol] = {
                        "yon": grid_yonu,
                        "merkez_fiyat": guncel_fiyat,
                        "marjin": hesaplanan_marjin,
                        "miktar": miktar,
                        "ilk_hedef_alindi": False,
                        "giris_rsi": rsi
                    }
                    telegram_mesaj_gonder(
                        f"🚀 *İŞLEM AÇILDI ({KALDIRAC}x İZOLE - OPTİMİZE)*\n"
                        f"• Parite: `{symbol}` ({grid_yonu})\n"
                        f"• Marjin: `~{hesaplanan_marjin:.2f} USDT`\n"
                        f"• Kontrat: `{miktar}`\n"
                        f"• Giriş RSI: `{rsi:.1f}`"
                    )
                    time.sleep(20)
                except Exception as order_err:
                    print(f"Emir hatası ({symbol}): {order_err}")

        except Exception as loop_err:
            print(f"Döngü hatası: {loop_err}")
        
        time.sleep(15)

if __name__ == "__main__":
    print(f"🚀 Optimize Edilmiş Analitik Bot Başlatılıyor...")
    
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
        app_tg.run_polling(drop_pending_updates=True)
    except Exception as e:
        print(f"Telegram polling hatası: {e}")
                    
