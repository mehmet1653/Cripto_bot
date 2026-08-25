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

TELEGRAM_TOKEN = "8870934003:AAGIpiwdgpnQVW7nbJIRcR0dOLOzj-MOZsA"
CHAT_ID = "6929517567"

exchange = ccxt.gate({
    'apiKey': '82cca880898a88d1a31e86d8eb474c57',
    'secret': '1ac479b9df5e6f2e89560b0d238a250694719b6fcae20da00ebc54ad6aeb8898',
    'enableRateLimit': True,
    'timeout': 15000,
    'options': {'defaultType': 'swap'}
})

exchange.set_sandbox_mode(True)

TAKIP_EDILENLER = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT', 'XRP/USDT:USDT']
BOT_CALISIYOR_MU = True
HAFIZA_DOSYASI = "bot_kalici_hafiza.json"

def hafizayi_yukle():
    if os.path.exists(HAFIZA_DOSYASI):
        try:
            with open(HAFIZA_DOSYASI, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "aktif_sistemler": {},
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
            "aktif_sistemler": AKTIF_GRID_SISTEMLERI,
            "analitik": ANALitik_HAFIZA
        }
        with open(HAFIZA_DOSYASI, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"Hafıza kaydetme hatası: {e}")

kalici_veri = hafizayi_yukle()
AKTIF_GRID_SISTEMLERI = kalici_veri.get("aktif_sistemler", {})
ANALitik_HAFIZA = kalici_veri.get("analitik", {
    "basarisiz_analizler": [],
    "basarili_islem_sayisi": 0,
    "basarisiz_islem_sayisi": 0,
    "gunluk_net_kar_usd": 0.0
})

KALDIRAC = 10
HEDEF_ROESINI_ISTENEN = 20.0         
ZARAR_KES_ROESINI_ISTENEN = 10.0     
MIN_ADX_GUCU = 20.0                  
AKILLI_BEKLEME_SANIYESI = 7200        
AKILLI_PORTFOY_KAR_YUZDESI = 3.0     

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
    durum_str = "AKTİF 🟢" if BOT_CALISIYOR_MU else "KİLİTLENDİ / BEKLEMEDE ⏸️"
    return f"Testnet Akıllı Kasa Koruma Botu | Durum: {durum_str}"

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
    try:
        open_orders = exchange.fetch_open_orders(symbol)
        for ord_item in open_orders:
            exchange.cancel_order(ord_item['id'], symbol)
    except Exception:
        pass

    try:
        market_info = exchange.market(symbol)
        min_amount = float(market_info['limits']['amount']['min'] or 1.0)
        if miktar < min_amount:
            miktar = min_amount
        miktar = float(exchange.amount_to_precision(symbol, miktar))
        exchange.create_market_order(symbol, kapatma_yonu, miktar, {'reduce_only': True})
    except Exception:
        try:
            time.sleep(0.2)
            ticker = exchange.fetch_ticker(symbol)
            guvenli_fiyat = ticker['ask'] if kapatma_yonu == 'buy' else ticker['bid']
            exchange.create_order(symbol, 'limit', kapatma_yonu, miktar, guvenli_fiyat, {'timeInForce': 'IOC', 'reduce_only': True})
        except Exception as e2:
            print(f"Pozisyon kapatma hatası: {e2}")

    if symbol in AKTIF_GRID_SISTEMLERI:
        del AKTIF_GRID_SISTEMLERI[symbol]
        hafizayi_kaydet()

    if sebep_mesaji:
        telegram_mesaj_gonder(sebep_mesaji)

def get_account_status_summary():
    try:
        balance = exchange.fetch_balance()
        total_usdt = float(balance['total'].get('USDT', 0))
        free_usdt = float(balance['free'].get('USDT', 0))
        try:
            borsa_pozisyonlari = exchange.fetch_positions()
        except Exception:
            borsa_pozisyonlari = []

        toplam_anlik_pnl = 0.0
        aktif_ozet_listesi = []

        for pos in borsa_pozisyonlari:
            contracts = float(pos.get('contracts', 0))
            if contracts > 0:
                sym = pos.get('symbol')
                side = str(pos.get('side', '')).upper()
                if not side:
                    side = "LONG" if float(pos.get('notional', 0)) > 0 else "SHORT"
                entry_price = float(pos.get('entryPrice', 0))
                pnl = float(pos.get('unrealizedPnl', 0))
                roe = float(pos.get('percentage', 0))
                
                if roe == 0 and entry_price > 0:
                    mark_price = float(pos.get('markPrice', entry_price))
                    fark = (mark_price - entry_price) / entry_price
                    if side == 'SHORT':
                        fark = -fark
                    roe = fark * 100 * KALDIRAC

                margin = float(pos.get('initialMargin', 0))
                if margin == 0:
                    notional = float(pos.get('notional', 0))
                    if notional > 0:
                        margin = notional / KALDIRAC

                toplam_anlik_pnl += pnl
                aktif_ozet_listesi.append({
                    'symbol': sym, 'side': side, 'leverage': int(pos.get('leverage', KALDIRAC)),
                    'pnl': pnl, 'roe': roe, 'contracts': contracts, 'entryPrice': entry_price, 'margin': margin
                })

        gunluk_pnl = ANALitik_HAFIZA['gunluk_net_kar_usd']
        bot_durum_str = "AKTİF 🟢" if BOT_CALISIYOR_MU else "KİLİTLENDİ / BEKLEMEDE ⏸️"
        
        summary = f"🧠 *AKILLI KASA KORUMA RAPORU*\n• **Bot Durumu:** `{bot_durum_str}`\n\n"
        summary += f"💰 **Toplam Kasa:** `{total_usdt:.2f} USDT`\n💵 **Kullanılabilir:** `{free_usdt:.2f} USDT`\n"
        summary += f"📅 **Günlük Net Kâr:** `{gunluk_pnl:.2f} USDT`\n"
        summary += f"📈 **Anlık Açık PnL:** `{toplam_anlik_pnl:.2f} USDT`\n"
        summary += f"📊 **Aktif Pozisyon:** `{len(aktif_ozet_listesi)}`\n"
        
        if aktif_ozet_listesi:
            summary += f"\n-----------------------------------\n"
            for p in aktif_ozet_listesi:
                summary += f"🔹 **{p['symbol']}** ({p['side']} | {p['leverage']}x)\n   K/Z: `{p['pnl']:.2f} USDT` (`{p['roe']:.2f}%`)\n"
        return summary
    except Exception as e:
        return f"Kasa durumu alınırken hata: {str(e)}"

async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(get_account_status_summary(), parse_mode='Markdown')

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = True
    await update.message.reply_text("🟢 *Bot yeniden aktifleştirildi!*", parse_mode='Markdown')

async def durdur_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ *Bot durduruldu.*", parse_mode='Markdown')

async def kapat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False  
    await update.message.reply_text("🔄 *Pozisyonlar kapatılıyor...*", parse_mode='Markdown')
    try:
        for pos in exchange.fetch_positions():
            if float(pos.get('contracts', 0)) > 0:
                pozisyonu_garantili_kapat(pos.get('symbol'), str(pos.get('side', 'LONG')).upper(), float(pos['contracts']), "Kapatıldı")
        AKTIF_GRID_SISTEMLERI.clear()
        hafizayi_kaydet()
        await update.message.reply_text("✅ Tüm pozisyonlar kapatıldı.", parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Hata: `{str(e)}`", parse_mode='Markdown')

def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, ANALitik_HAFIZA
    try:
        exchange.load_markets()
    except Exception:
        pass
    
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(3)
                continue
            
            try:
                toplam_bakiye = float(exchange.fetch_balance()['total'].get('USDT', 0))
            except Exception:
                toplam_bakiye = 1000.0

            try:
                borsa_pozisyonlari = exchange.fetch_positions()
            except Exception:
                borsa_pozisyonlari = []

            aktif_borsa_map = {}
            for pos in borsa_pozisyonlari:
                if float(pos.get('contracts', 0)) > 0:
                    aktif_borsa_map[pos.get('symbol')] = {
                        "side": str(pos.get('side', 'LONG')).upper(),
                        "contracts": float(pos['contracts']),
                        "entryPrice": float(pos.get('entryPrice', 0)),
                        "unrealizedPnl": float(pos.get('unrealizedPnl', 0)),
                        "percentage": float(pos.get('percentage', 0))
                    }

            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU:
                    break
                try:
                    guncel_fiyat = exchange.fetch_ticker(symbol)['last']
                except Exception:
                    continue

                if symbol in aktif_borsa_map:
                    p = aktif_borsa_map[symbol]
                    yon = p["side"]
                    fark = (guncel_fiyat - p["entryPrice"]) / p["entryPrice"]
                    if yon == "SHORT": fark = -fark
                    roe = fark * 100 * KALDIRAC

                    if roe >= HEDEF_ROESINI_ISTENEN or p["percentage"] >= HEDEF_ROESINI_ISTENEN:
                        ANALitik_HAFIZA["gunluk_net_kar_usd"] += abs(p["unrealizedPnl"]) or 2.0
                        ANALitik_HAFIZA["basarili_islem_sayisi"] += 1
                        hafizayi_kaydet()
                        pozisyonu_garantili_kapat(symbol, yon, p["contracts"], f"🚀 *KÂR AL* - `{symbol}` (`+{roe:.2f}%`)")
                    elif roe <= -ZARAR_KES_ROESINI_ISTENEN or p["percentage"] <= -ZARAR_KES_ROESINI_ISTENEN:
                        ANALitik_HAFIZA["gunluk_net_kar_usd"] -= abs(p["unrealizedPnl"]) or 1.0
                        ANALitik_HAFIZA["basarisiz_islem_sayisi"] += 1
                        hafizayi_kaydet()
                        pozisyonu_garantili_kapat(symbol, yon, p["contracts"], f"🛑 *ZARAR KES* - `{symbol}` (`{roe:.2f}%`)")
                    continue

                hedef_marjin = toplam_bakiye / 4.0
                if toplam_bakiye < hedef_marjin:
                    continue

                try:
                    df = pd.DataFrame(exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50), columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    ema7 = ta.trend.ema_indicator(df['close'], window=7).iloc[-1]
                    ema21 = ta.trend.ema_indicator(df['close'], window=21).iloc[-1]
                    rsi = ta.momentum.rsi(df['close'], window=14).iloc[-1]
                    adx = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14).adx().iloc[-1]
                except Exception:
                    continue

                if adx < MIN_ADX_GUCU:
                    continue

                grid_yonu = "LONG" if ema7 > ema21 else "SHORT"
                if grid_yonu == "SHORT" and rsi < 42: continue 
                if grid_yonu == "LONG" and rsi > 58: continue 

                set_isolated_leverage_safely(symbol, KALDIRAC)
                ham_miktar = (hedef_marjin * KALDIRAC) / guncel_fiyat

                try:
                    m_info = exchange.market(symbol)
                    c_size = float(m_info.get('contractSize', 1.0))
                    min_amt = float(m_info['limits']['amount']['min'] or 1.0)
                    gercek_miktar = max(ham_miktar / c_size, min_amt)
                    miktar = float(exchange.amount_to_precision(symbol, gercek_miktar))
                except Exception:
                    c_size = 1.0
                    miktar = float(exchange.amount_to_precision(symbol, ham_miktar))

                try:
                    exchange.create_market_order(symbol, 'buy' if grid_yonu == 'LONG' else 'sell', miktar)
                    hesaplanan_marjin = (miktar * c_size * guncel_fiyat) / KALDIRAC
                    
                    # Tek satırda güvenli sözlük tanımı
                    AKTIF_GRID_SISTEMLERI[symbol] = {"yon": grid_yonu, "merkez_fiyat": guncel_fiyat, "marjin": hesaplanan_marjin, "miktar": miktar, "giris_rsi": rsi, "acilis_zamani": time.time()}
                    hafizayi_kaydet()
                    
                    telegram_mesaj_gonder(f"🧠 *İŞLEM AÇILDI* - `{symbol}` ({grid_yonu} {KALDIRAC}x)")
                    time.sleep(10)
                except Exception as order_err:
                    print(f"Emir hatası: {order_err}")

        except Exception as loop_err:
            print(f"Döngü hatası: {loop_err}")
        time.sleep(2)

if __name__ == "__main__":
    threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=port, use_reloader=False), daemon=True).start()
    
    app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app_tg.add_handler(CommandHandler("durum", durum_komutu))
    app_tg.add_handler(CommandHandler("pozisyonlar", durum_komutu))
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))
    
    app_tg.run_polling(drop_pending_updates=True)
                    
