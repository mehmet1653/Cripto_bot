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
MIN_ADX_GUCU = 25.0  

OTURUM_HEDEF_KAR_USDT = 5.0  
DINLENME_SURESI_SANAIYE = 1800  
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
    return f"Multi-Indikatör Confluence Bot | Durum: {durum_str}"

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
        
        try:
            borsa_pozisyonlari = exchange.fetch_positions()
        except Exception:
            borsa_pozisyonlari = []

        toplam_anlik_pnl = sum(float(pos.get('unrealizedPnl', 0)) for pos in borsa_pozisyonlari if float(pos.get('contracts', 0)) > 0)
        gunluk_pnl = ANALitik_HAFIZA['gunluk_net_kar_usd']
        
        summary = f"🚀 *ÇOKLU İNDİKATÖR ONAYLI BOT RAPORU*\n\n"
        if dinlenme_modunda_mi:
            kalan_dakika = max(0, int((dinlenme_bitis_zamanı - time.time()) / 60))
            summary += f"☕ **Durum:** DİNLENME MODUNDA (Kalan: `{kalan_dakika} dk`)\n\n"
        else:
            summary += f"🟢 **Durum:** DERİN SÜZGEÇTE TARANIYOR\n\n"

        summary += f"💰 **Toplam Kasa:** `{total_usdt:.2f} USDT`\n"
        summary += f"📊 **Aktif Anlık K/Z:** `{toplam_anlik_pnl:+.2f} USDT`\n"
        summary += f"📅 **Günlük Net Kâr:** `{gunluk_pnl:+.2f} USDT`\n"
        
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
    await update.message.reply_text("🟢 *Bot manuel aktifleştirildi ve sıfırlandı!*", parse_mode='Markdown')

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

# ==================== ANA DÖNGÜ (MULTI-INDICATOR ONAYLI TARAMA) ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, ANALitik_HAFIZA, dinlenme_modunda_mi, dinlenme_bitis_zamanı
    print("🚀 Çoklu İndikatör Konfluans Botu Devrede.")
    
    try:
        exchange.load_markets()
    except Exception as e:
        print(f"Piyasalar yüklenemedi: {e}")
    
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(3)
                continue

            if dinlenme_modunda_mi:
                if time.time() >= dinlenme_bitis_zamanı:
                    dinlenme_modunda_mi = False
                    telegram_mesaj_gonder("☕ *Dinlenme süresi bitti!* Bot yeniden piyasayı derinlemesine süzmeye başlıyor. 🚀")
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

            # Oturum hedefi kontrolü (5 Dolar)
            toplam_anlik_pnl = sum(float(pos.get('unrealizedPnl', 0)) for pos in aktif_borsa_map.values())
            if aktif_borsa_map and toplam_anlik_pnl >= OTURUM_HEDEF_KAR_USDT:
                telegram_mesaj_gonder(
                    f"🎯 *OTURUM HEDEF KÂRINA ULAŞILDI!* (`+{toplam_anlik_pnl:.2f} USDT`)\n"
                    f"☕ Kâr cüzdana alındı, bot 30 dakika dinlenmeye çekiliyor..."
                )
                for symbol, pos in list(aktif_borsa_map.items()):
                    side = "LONG" if float(pos.get('notional', 0)) > 0 else "SHORT"
                    pozisyonu_garantili_kapat(symbol, side, float(pos['contracts']), "")

                ANALitik_HAFIZA["gunluk_net_kar_usd"] += toplam_anlik_pnl
                ANALitik_HAFIZA["basarili_islem_sayisi"] += len(aktif_borsa_map)
                AKTIF_GRID_SISTEMLERI.clear()
                hafizayi_kaydet()

                dinlenme_modunda_mi = True
                dinlenme_bitis_zamanı = time.time() + DINLENME_SURESI_SANAIYE
                continue

            # Aktif pozisyon yönetimi (Trailing ve SL)
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
                    ANALitik_HAFIZA["gunluk_net_kar_usd"] += max(pnl, 2.0)
                    ANALitik_HAFIZA["basarili_islem_sayisi"] += 1
                    hafizayi_kaydet()
                    pozisyonu_garantili_kapat(symbol, side, float(pos['contracts']), f"🎯 *TRAILING TP* -> `{symbol}` (+`{pnl:.2f} USDT`)")
                    continue

                if roe <= -10.0:
                    ANALitik_HAFIZA["gunluk_net_kar_usd"] -= abs(pnl) if pnl < 0 else 1.5
                    ANALitik_HAFIZA["basarisiz_islem_sayisi"] += 1
                    hafizayi_kaydet()
                    pozisyonu_garantili_kapat(symbol, side, float(pos['contracts']), f"🛑 *STOP LOSS* -> `{symbol}` (`{pnl:.2f} USDT`)")
                    continue

            # --- ÇOKLU İNDİKATÖR SÜZGEÇLİ TARAMA ---
            if len(aktif_borsa_map) < MAKSIMUM_TOPLAM_POZISYON:
                taranan_sinyaller = []

                for symbol in TAKIP_EDILENLER:
                    if symbol in aktif_borsa_map:
                        continue

                    try:
                        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=100)
                        if len(ohlcv) < 50:
                            continue
                        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                        
                        # 1. EMA'lar
                        df['ema7'] = ta.trend.ema_indicator(df['close'], window=7)
                        df['ema21'] = ta.trend.ema_indicator(df['close'], window=21)
                        
                        # 2. RSI
                        df['rsi'] = ta.momentum.rsi(df['close'], window=14)
                        
                        # 3. ADX
                        adx_ind = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14)
                        df['adx'] = adx_ind.adx()

                        # 4. MACD (Momentum Teyidi)
                        macd_ind = ta.trend.MACD(df['close'])
                        df['macd'] = macd_ind.macd()
                        df['macd_signal'] = macd_ind.macd_signal()

                        # 5. Bollinger Bands (Volatilite Sınırları)
                        bb_ind = ta.volatility.BollingerBands(df['close'], window=20, window_dev=2)
                        df['bb_high'] = bb_ind.bollinger_hband()
                        df['bb_low'] = df['bollinger_lband'] = bb_ind.bollinger_lband()

                        # 6. Hacim Ortalaması
                        df['vol_sma'] = df['volume'].rolling(window=20).mean()

                        # Son Değerler
                        son_kapanis = df['close'].iloc[-1]
                        bir_onceki_kapanis = df['close'].iloc[-2]
                        
                        ema7 = df['ema7'].iloc[-1]
                        ema21 = df['ema21'].iloc[-1]
                        rsi = df['rsi'].iloc[-1]
                        adx = df['adx'].iloc[-1]
                        macd = df['macd'].iloc[-1]
                        macd_sig = df['macd_signal'].iloc[-1]
                        bb_high = df['bb_high'].iloc[-1]
                        bb_low = df['bb_low'].iloc[-1]
                        hacim = df['volume'].iloc[-1]
                        hacim_ort = df['vol_sma'].iloc[-1]
                    except Exception:
                        continue

                    # Temel ADX ve Hacim Engeli (Hacimsiz ve zayıf trendleri direkt ele)
                    if adx < MIN_ADX_GUCU or hacim <= hacim_ort:
                        continue

                    yon = None
                    puan = 0

                    # --- LONG KONTROLÜ (Çoklu Teyit) ---
                    # Şartlar: EMA7 > EMA21, Son 2 mum yükseliş, RSI 50-68 arası, MACD > Sinyal ve Fiyat Bollinger Üst banda yapışmamış
                    if (ema7 > ema21 and 
                        son_kapanis > bir_onceki_kapanis and 
                        50 <= rsi <= 68 and 
                        macd > macd_sig and 
                        son_kapanis < bb_high):
                        
                        yon = "LONG"
                        puan = adx + (rsi - 50) + (macd - macd_sig) * 10

                    # --- SHORT KONTROLÜ (Çoklu Teyit) ---
                    # Şartlar: EMA7 < EMA21, Son 2 mum düşüş, RSI 32-50 arası, MACD < Sinyal ve Fiyat Bollinger Alt banda yapışmamış
                    elif (ema7 < ema21 and 
                          son_kapanis < bir_onceki_kapanis and 
                          32 <= rsi <= 50 and 
                          macd < macd_sig and 
                          son_kapanis > bb_low):
                        
                        yon = "SHORT"
                        puan = adx + (50 - rsi) + (macd_sig - macd) * 10
                    else:
                        continue

                    taranan_sinyaller.append({
                        "symbol": symbol, "puan": puan, "yon": yon, "rsi": rsi, "adx": adx, "fiyat": son_kapanis
                    })

                taranan_sinyaller.sort(key=lambda x: x["puan"], reverse=True)

                for sinyal in taranan_sinyaller:
                    if len(aktif_borsa_map) >= MAKSIMUM_TOPLAM_POZISYON:
                        break

                    symbol = sinyal["symbol"]
                    yon = sinyal["yon"]
                    puan = sinyal["puan"]
                    rsi = sinyal["rsi"]
                    adx = sinyal["adx"]
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
                            f"🧠🎯 *KUSURSUZ ONAYLI İŞLEM ({KALDIRAC}x)*\n"
                            f"• Parite: `{symbol}` ({yon})\n"
                            f"• Konfluans Skoru: `{puan:.1f}`\n"
                            f"• Filtreler: ADX: `{adx:.1f}` | RSI: `{rsi:.1f}` | Hacim & MACD: `OK`\n"
                            f"• Giriş Fiyatı: `{guncel_fiyat}`"
                        )
                        aktif_borsa_map[symbol] = {'symbol': symbol, 'side': yon, 'contracts': miktar}
                        time.sleep(3)
                    except Exception as order_err:
                        print(f"Emir hatası ({symbol}): {order_err}")

        except Exception as loop_err:
            print(f"Döngü hatası: {loop_err}")
            
        time.sleep(15)

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
