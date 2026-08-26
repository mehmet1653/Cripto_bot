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
    'apiKey': '82cca880898a88d1a31e86d8eb474c57',
    'secret': '1ac479b9df5e6f2e89560b0d238a250694719b6fcae20da00ebc54ad6aeb8898',
    'enableRateLimit': True,
    'timeout': 15000,
    'options': {
        'defaultType': 'swap'
    }
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

# ==================== KASA KORUMA & RİSK YÖNETİMİ ====================
HEDEF_ROESINI_ISTENEN = 20.0     # Pozisyonda hedeflenen net getiri (%20 Kâr ROI -> Fiyat %2 lehimize)
ZARAR_KES_ROESINI_ISTENEN = 10.0 # Pozisyonda göze alınan net zarar (%10 Stop ROI -> Fiyat %1 aleyhimize)
MIN_ADX_GUCU = 20.0              # Yatay piyasayı filtrelemek için minimum trend gücü
# ======================================================================

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
    return f"Testnet Kasa Koruma Modlu Bot | Durum: {durum_str}"

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
    except Exception as e:
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
                    'symbol': sym,
                    'side': side,
                    'leverage': int(pos.get('leverage', KALDIRAC)),
                    'pnl': pnl,
                    'roe': roe,
                    'contracts': contracts,
                    'entryPrice': entry_price,
                    'margin': margin
                })

        gunluk_pnl = ANALitik_HAFIZA['gunluk_net_kar_usd']
        
        summary = f"🛡️ *KASA KORUMA & POZİSYON RAPORU*\n\n"
        summary += f"💰 **Toplam Kasa:** `{total_usdt:.2f} USDT`\n"
        summary += f"💵 **Kullanılabilir:** `{free_usdt:.2f} USDT`\n"
        
        if gunluk_pnl >= 0:
            summary += f"📅 **Günlük Net Kâr:** `+{gunluk_pnl:.2f} USDT` 🟢\n"
        else:
            summary += f"📅 **Günlük Net Zarar:** `{gunluk_pnl:.2f} USDT` 🔴\n"
            
        if toplam_anlik_pnl >= 0:
            summary += f"📈 **Anlık Açık PnL:** `+{toplam_anlik_pnl:.2f} USDT` 🟢\n"
        else:
            summary += f"📉 **Anlık Açık PnL:** `{toplam_anlik_pnl:.2f} USDT` 🔴\n"
            
        summary += f"📊 **Aktif Pozisyon:** `{len(aktif_ozet_listesi)}`\n"
        summary += f"🎯 **Başarılı İşlem:** `{ANALitik_HAFIZA['basarili_islem_sayisi']}` adet\n"
        summary += f"🛑 **Başarısız (Zarar Kes):** `{ANALitik_HAFIZA['basarisiz_islem_sayisi']}` adet\n"
        
        if aktif_ozet_listesi:
            summary += f"\n-----------------------------------\n"
            for p in aktif_ozet_listesi:
                pnl_isaret = "+" if p['pnl'] >= 0 else ""
                summary += f"🔹 **{p['symbol']}** ({p['side']} | {p['leverage']}x İzole)\n"
                summary += f"   Giriş: `{p['entryPrice']}` | Kontrat: `{p['contracts']}` | Marjin: `{p['margin']:.2f}$`\n"
                summary += f"   K/Z: `{pnl_isaret}{p['pnl']:.2f} USDT` (`{pnl_isaret}{p['roe']:.2f}%`)\n"
        
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
    await update.message.reply_text("🟢 *Kasa koruma botu aktif edildi!*", parse_mode='Markdown')

async def durdur_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ *Bot durduruldu.*", parse_mode='Markdown')

async def kapat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 *Tüm aktif işlemler kapatılıyor...*", parse_mode='Markdown')
    
    try:
        borsa_pozisyonlari = exchange.fetch_positions()
        kapatilanlar = 0
        for pos in borsa_pozisyonlari:
            if float(pos.get('contracts', 0)) > 0:
                sym = pos.get('symbol')
                side = str(pos.get('side', '')).upper()
                if not side:
                    side = "LONG" if float(pos.get('notional', 0)) > 0 else "SHORT"
                pozisyonu_garantili_kapat(sym, side, float(pos['contracts']), f"🛑 *MANUEL KAPATMA (/kapat)* - `{sym}`")
                kapatilanlar += 1

        AKTIF_GRID_SISTEMLERI.clear()
        hafizayi_kaydet()
        await update.message.reply_text(f"✅ İşlem tamamlandı. Toplam `{kapatilanlar}` pozisyon kapatıldı.", parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Kapatma sırasında hata: `{str(e)}`", parse_mode='Markdown')

# ==================== ANA STRATEJİ DÖNGÜSÜ ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, ANALitik_HAFIZA
    print("🔄 Kasa Koruma Modlu Akıllı Tarayıcı aktif.")
    
    try:
        exchange.load_markets()
    except Exception as e:
        print(f"Piyasalar yüklenemedi: {e}")
    
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(3)
                continue

            try:
                borsa_pozisyonlari = exchange.fetch_positions()
            except Exception:
                borsa_pozisyonlari = []

            aktif_borsa_map = {}
            for pos in borsa_pozisyonlari:
                if float(pos.get('contracts', 0)) > 0:
                    sym = pos.get('symbol')
                    side = str(pos.get('side', '')).upper()
                    if not side:
                        side = "LONG" if float(pos.get('notional', 0)) > 0 else "SHORT"
                    aktif_borsa_map[sym] = {
                        "side": side,
                        "contracts": float(pos['contracts']),
                        "entryPrice": float(pos.get('entryPrice', 0)),
                        "unrealizedPnl": float(pos.get('unrealizedPnl', 0)),
                        "percentage": float(pos.get('percentage', 0))
                    }

            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU:
                    break
                    
                try:
                    ticker = exchange.fetch_ticker(symbol)
                    guncel_fiyat = ticker['last']
                except Exception:
                    continue

                if symbol in aktif_borsa_map:
                    pos_bilgi = aktif_borsa_map[symbol]
                    yon = pos_bilgi["side"]
                    merkez = pos_bilgi["entryPrice"]
                    
                    fark_orani = (guncel_fiyat - merkez) / merkez
                    if yon == "SHORT": 
                        fark_orani = -fark_orani
                        
                    net_kar_zarar_yuzdesi = fark_orani * 100 * KALDIRAC
                    
                    if net_kar_zarar_yuzdesi >= HEDEF_ROESINI_ISTENEN or pos_bilgi["percentage"] >= HEDEF_ROESINI_ISTENEN:
                        tahmini_kar_usd = abs(pos_bilgi["unrealizedPnl"]) if pos_bilgi["unrealizedPnl"] > 0 else 2.0
                        ANALitik_HAFIZA["gunluk_net_kar_usd"] += tahmini_kar_usd
                        ANALitik_HAFIZA["basarili_islem_sayisi"] += 1
                        hafizayi_kaydet()
                        
                        mesaj = f"🚀 *HEDEF BAŞARILI (KÂR ALINDI)* - `{symbol}` (`+{net_kar_zarar_yuzdesi:.2f}%`)"
                        pozisyonu_garantili_kapat(symbol, yon, pos_bilgi["contracts"], mesaj)
                        
                    elif net_kar_zarar_yuzdesi <= -ZARAR_KES_ROESINI_ISTENEN or pos_bilgi["percentage"] <= -ZARAR_KES_ROESINI_ISTENEN:
                        tahmini_zarar_usd = abs(pos_bilgi["unrealizedPnl"]) if pos_bilgi["unrealizedPnl"] < 0 else 1.0
                        ANALitik_HAFIZA["gunluk_net_kar_usd"] -= tahmini_zarar_usd
                        ANALitik_HAFIZA["basarisiz_islem_sayisi"] += 1
                        
                        analitik_hata_notu = {
                            "symbol": symbol,
                            "yanlis_yon": yon,
                            "giris_fiyati": merkez,
                            "zarar_orani": net_kar_zarar_yuzdesi,
                            "zarar_usd": tahmini_zarar_usd,
                            "zaman": time.strftime('%H:%M:%S')
                        }
                        ANALitik_HAFIZA["basarisiz_analizler"].append(analitik_hata_notu)
                        hafizayi_kaydet()
                        
                        mesaj = f"🛑 *ZARAR KES (KASA KORUMA STOP) & ANALİZE EKLENDİ* - `{symbol}` (`{net_kar_zarar_yuzdesi:.2f}%`)"
                        pozisyonu_garantili_kapat(symbol, yon, pos_bilgi["contracts"], mesaj)
                        
                    continue

                if symbol in aktif_borsa_map:
                    continue

                try:
                    balance = exchange.fetch_balance()
                    toplam_bakiye = float(balance['total'].get('USDT', 0))
                except Exception:
                    continue

                hedef_marjin = toplam_bakiye / 4.0
                if toplam_bakiye < hedef_marjin:
                    continue

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
                        f"🛡️ *İŞLEM AÇILDI (KASA KORUMALI) - {KALDIRAC}x*\n"
                        f"• Parite: `{symbol}` ({grid_yonu})\n"
                        f"• Giriş Fiyatı: `{guncel_fiyat}`\n"
                        f"• Hedef Kâr: `~{hedef_fiyati:.4f}` (`+{HEDEF_ROESINI_ISTENEN}% ROI`)\n"
                        f"• Zarar Kes: `~{stop_fiyati:.4f}` (`-{ZARAR_KES_ROESINI_ISTENEN}% ROI`)\n"
                        f"• Trend Gücü (ADX): `{adx_degeri:.1f}`\n"
                        f"• Marjin: `~{hesaplanan_marjin:.2f} USDT`\n"
                        f"• Giriş RSI: `{rsi:.1f}`"
                    )
                    time.sleep(10)
                except Exception as order_err:
                    print(f"Emir hatası ({symbol}): {order_err}")

        except Exception as loop_err:
            print(f"Döngü hatası: {loop_err}")
        
        time.sleep(2)

if __name__ == "__main__":
    print(f"🛡️ Kasa Koruma Modlu Bot Başlatılıyor...")
    
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
                    
