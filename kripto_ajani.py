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

TAKIP_EDILENLER = [
    'SOL/USDT:USDT', 'AVAX/USDT:USDT', 'XRP/USDT:USDT', 'DOGE/USDT:USDT', 
    'SUI/USDT:USDT', 'HYPE/USDT:USDT', 'NEAR/USDT:USDT', 'RENDER/USDT:USDT', 'INJ/USDT:USDT'
]
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
MAKSIMUM_TOPLAM_POZISYON = 3
MIN_ADX_GUCU = 20.0

# **YENİ EKLENEN PARAMETRELER (OTURUM HEDEFİ VE DİNLENME)**
OTURUM_HEDEF_KAR_USDT = 5.0  # Toplam kâr bu rakama ulaşınca her şey kapatılıp dinlenilecek
DINLENME_SURESI_SANAIYE = 1800  # 30 dakika dinlenme (1800 saniye)
dinlenme_modunda_mi = False
dinlenme_bitis_zamanı = 0

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
    global dinlenme_modunda_mi
    durum_str = "DİNLENİYOR ☕" if dinlenme_modunda_mi else ("AKTİF 🟢" if BOT_CALISIYOR_MU else "BEKLEMEDE ⏸️")
    return f"Oturum Hedefli & Dinlenmeli Bot | Durum: {durum_str}"

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
    global dinlenme_modunda_mi, dinlenme_bitis_zamanı
    try:
        balance = exchange.fetch_balance()
        total_usdt = float(balance['total'].get('USDT', 0))
        free_usdt = float(balance['free'].get('USDT', 0))
        
        try:
            borsa_pozisyonlari = exchange.fetch_positions()
        except Exception:
            borsa_pozisyonlari = []

        aktif_ozet_listesi = []
        toplam_anlik_pnl = 0.0

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

                toplam_anlik_pnl += pnl
                aktif_ozet_listesi.append({
                    'symbol': sym, 'side': side, 'leverage': int(pos.get('leverage', KALDIRAC)),
                    'pnl': pnl, 'roe': roe, 'contracts': contracts
                })

        gunluk_pnl = ANALitik_HAFIZA['gunluk_net_kar_usd']
        
        summary = f"🚀 *OTURUM HEDEFİ & DİNLENME BOT RAPORU*\n\n"
        if dinlenme_modunda_mi:
            kalan_dakika = max(0, int((dinlenme_bitis_zamanı - time.time()) / 60))
            summary += f"☕ **Durum:** DİNLENME MODUNDA (Kalan: `{kalan_dakika} dk`)\n\n"
        else:
            summary += f"🟢 **Durum:** AKTİF TARAMA\n\n"

        summary += f"💰 **Toplam Kasa:** `{total_usdt:.2f} USDT`\n"
        summary += f"📊 **Aktif Anlık K/Z:** `{toplam_anlik_pnl:+.2f} USDT`\n"
        summary += f"📅 **Günlük Net Kâr:** `{gunluk_pnl:+.2f} USDT`\n"
        summary += f"🎯 **Oturum Hedef Kârı:** `{OTURUM_HEDEF_KAR_USDT} USDT`\n"
        
        return summary
    except Exception as e:
        return f"Kasa durumu alınırken hata oluştu: {str(e)}"

# ==================== TELEGRAM KOMUTLARI ====================
async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(get_account_status_summary(), parse_mode='Markdown')

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU, dinlenme_modunda_mi
    BOT_CALISIYOR_MU = True
    dinlenme_modunda_mi = False
    await update.message.reply_text("🟢 *Bot manuel olarak aktifleştirildi ve dinlenme sıfırlandı!*", parse_mode='Markdown')

async def durdur_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ *Bot durduruldu.*", parse_mode='Markdown')

async def kapat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 *Tüm aktif işlemler kapatılıyor...*", parse_mode='Markdown')
    try:
        for pos in exchange.fetch_positions():
            if float(pos.get('contracts', 0)) > 0:
                sym = pos.get('symbol')
                side = "LONG" if float(pos.get('notional', 0)) > 0 else "SHORT"
                pozisyonu_garantili_kapat(sym, side, float(pos['contracts']), f"🛑 *MANUEL KAPATMA* - `{sym}`")
        AKTIF_GRID_SISTEMLERI.clear()
        hafizayi_kaydet()
        await update.message.reply_text("✅ Temizlendi.", parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}", parse_mode='Markdown')

# ==================== ANA DÖNGÜ (HEDEF KÂR & DİNLENME) ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, ANALitik_HAFIZA, dinlenme_modunda_mi, dinlenme_bitis_zamanı
    print("🚀 Oturum Hedefli & Dinlenmeli Tarayıcı Devrede.")
    
    try:
        exchange.load_markets()
    except Exception as e:
        print(f"Piyasalar yüklenemedi: {e}")
    
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(3)
                continue

            # --- DİNLENME SÜRESİ KONTROLÜ ---
            if dinlenme_modunda_mi:
                if time.time() >= dinlenme_bitis_zamanı:
                    dinlenme_modunda_mi = False
                    telegram_mesaj_gonder("☕ *Dinlenme süresi bitti!* Bot yeniden piyasayı taramaya ve işlem açmaya başlıyor. 🚀")
                else:
                    time.sleep(10)
                    continue

            try:
                borsa_pozisyonlari = exchange.fetch_positions()
                aktif_borsa_map = {p['symbol']: p for p in borsa_pozisyonlari if float(p.get('contracts', 0)) > 0}
            except Exception:
                aktif_borsa_map = {}

            for sym in list(AKTIF_GRID_SISTEMLERI.keys()):
                if sym not in aktif_borsa_map:
                    del AKTIF_GRID_SISTEMLERI[sym]
                    hafizayi_kaydet()

            # --- TOPLAM ANLIK KÂR KONTROLÜ (HEDEF 5 DOLAR İSE DİNLENMEYE GEÇ) ---
            toplam_anlik_pnl = 0.0
            for symbol, pos in aktif_borsa_map.items():
                pnl = float(pos.get('unrealizedPnl', 0))
                toplam_anlik_pnl += pnl

            if aktif_borsa_map and toplam_anlik_pnl >= OTURUM_HEDEF_KAR_USDT:
                telegram_mesaj_gonder(
                    f"🎯 *OTURUM HEDEF KÂRINA ULAŞILDI!* (`+{toplam_anlik_pnl:.2f} USDT`)\n\n"
                    f"☕ Kârı cebe koymak ve piyasa gürültüsünden kaçmak için tüm pozisyonlar kapatılıyor. Bot 30 dakika dinlenmeye alınıyor..."
                )
                
                # Tüm açık pozisyonları kapat
                for symbol, pos in list(aktif_borsa_map.items()):
                    side = "LONG" if float(pos.get('notional', 0)) > 0 else "SHORT"
                    pozisyonu_garantili_kapat(symbol, side, float(pos['contracts']), "")

                ANALitik_HAFIZA["gunluk_net_kar_usd"] += toplam_anlik_pnl
                ANALitik_HAFIZA["basarili_islem_sayisi"] += len(aktif_borsa_map)
                AKTIF_GRID_SISTEMLERI.clear()
                hafizayi_kaydet()

                # Dinlenmeyi başlat
                dinlenme_modunda_mi = True
                dinlenme_bitis_zamanı = time.time() + DINLENME_SURESI_SANAIYE
                continue

            # --- NORMAL POZİSYON YÖNETİMİ (TRAILING VE SL) ---
            for symbol, pos in aktif_borsa_map.items():
                try:
                    guncel_fiyat = exchange.fetch_ticker(symbol)['last']
                except Exception:
                    continue

                side = str(pos.get('side', '')).upper()
                if not side:
                    side = "LONG" if float(pos.get('notional', 0)) > 0 else "SHORT"
                
                entry_price = float(pos.get('entryPrice', 0))
                pnl = float(pos.get('unrealizedPnl', 0))
                
                fark = (guncel_fiyat - entry_price) / entry_price if side == "LONG" else (entry_price - guncel_fiyat) / entry_price
                roe = fark * 100 * KALDIRAC

                kayitli = AKTIF_GRID_SISTEMLERI.get(symbol, {})
                en_yuksek_roe = kayitli.get("en_yuksek_roe", roe)

                if roe > en_yuksek_roe:
                    en_yuksek_roe = roe
                    kayitli["en_yuksek_roe"] = en_yuksek_roe
                    hafizayi_kaydet()

                if en_yuksek_roe >= 12.0 and roe <= (en_yuksek_roe - 4.0):
                    tahmini_kar = abs(pnl) if pnl > 0 else 2.0
                    ANALitik_HAFIZA["gunluk_net_kar_usd"] += tahmini_kar
                    ANALitik_HAFIZA["basarili_islem_sayisi"] += 1
                    hafizayi_kaydet()

                    pozisyonu_garantili_kapat(
                        symbol, side, float(pos['contracts']),
                        f"🎯 *KÂR SÖRFÜ TAMAMLANDI (TRAILING)*\n• Coin: `{symbol}`\n• Kâr: `+{pnl:.2f} USDT`"
                    )
                    continue

                if roe <= -10.0:
                    tahmini_zarar = abs(pnl) if pnl < 0 else 1.5
                    ANALitik_HAFIZA["gunluk_net_kar_usd"] -= tahmini_zarar
                    ANALitik_HAFIZA["basarisiz_islem_sayisi"] += 1
                    hafizayi_kaydet()

                    pozisyonu_garantili_kapat(
                        symbol, side, float(pos['contracts']),
                        f"🛑 *ZARAR KESİLDİ (SL)*\n• Coin: `{symbol}`\n• Zarar: `{pnl:.2f} USDT`"
                    )
                    continue

            # --- YENİ İŞLEM AÇMA TARAMASI ---
            if len(aktif_borsa_map) < MAKSIMUM_TOPLAM_POZISYON:
                taranan_sinyaller = []

                for symbol in TAKIP_EDILENLER:
                    if symbol in aktif_borsa_map:
                        continue

                    try:
                        ticker = exchange.fetch_ticker(symbol)
                        guncel_fiyat = ticker['last']
                        
                        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
                        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                        
                        ema7 = ta.trend.ema_indicator(df['close'], window=7).iloc[-1]
                        ema21 = ta.trend.ema_indicator(df['close'], window=21).iloc[-1]
                        rsi = ta.momentum.rsi(df['close'], window=14).iloc[-1]
                        adx = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14).adx().iloc[-1]
                    except Exception:
                        continue

                    if adx < MIN_ADX_GUCU:
                        continue

                    if ema7 > ema21 and 45 <= rsi <= 65:
                        yon = "LONG"
                        puan = adx + (rsi - 45)
                    elif ema7 < ema21 and 35 <= rsi <= 55:
                        yon = "SHORT"
                        puan = adx + (55 - rsi)
                    else:
                        continue

                    taranan_sinyaller.append({
                        "symbol": symbol, "puan": puan, "yon": yon, "rsi": rsi, "adx": adx, "fiyat": guncel_fiyat
                    })

                taranan_sinyaller.sort(key=lambda x: x["puan"], reverse=True)

                for sinyal in taranan_sinyaller:
                    if len(aktif_borsa_map) >= MAKSIMUM_TOPLAM_POZISYON:
                        break

                    symbol = sinyal["symbol"]
                    yon = sinyal["yon"]
                    puan = sinyal["puan"]
                    guncel_fiyat = sinyal["fiyat"]

                    try:
                        balance = exchange.fetch_balance()
                        toplam_bakiye = float(balance['total'].get('USDT', 0))
                    except Exception:
                        continue

                    hedef_marjin = toplam_bakiye / 4.0
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

                    emir_yonu = 'buy' if yon == 'LONG' else 'sell'
                    
                    try:
                        exchange.create_market_order(symbol, emir_yonu, miktar)
                        AKTIF_GRID_SISTEMLERI[symbol] = {
                            "yon": yon, "en_yuksek_roe": 0.0, "giris_fiyati": guncel_fiyat
                        }
                        hafizayi_kaydet()
                        
                        telegram_mesaj_gonder(
                            f"⭐ *İŞLEM AÇILDI ({KALDIRAC}x)*\n"
                            f"• Parite: `{symbol}` ({yon})\n"
                            f"• Skor: `{puan:.1f}`"
                        )
                        aktif_borsa_map[symbol] = {'symbol': symbol, 'side': yon, 'contracts': miktar}
                        time.sleep(3)
                    except Exception as order_err:
                        print(f"Emir hatası ({symbol}): {order_err}")

        except Exception as loop_err:
            print(f"Döngü hatası: {loop_err}")
            
        time.sleep(10)

def flask_web_server():
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

if __name__ == '__main__':
    threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True).start()
    threading.Thread(target=flask_web_server, daemon=True).start()
    
    app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app_tg.add_handler(CommandHandler("durum", durum_komutu))
    app_tg.add_handler(CommandHandler("pozisyonlar", durum_komutu))
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))
    
    app_tg.run_polling(drop_pending_updates=True)
