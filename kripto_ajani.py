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
from supabase import create_client, Client

app = Flask(__name__)

# ==================== AYARLAR VE ANAHTARLAR ====================
TELEGRAM_TOKEN = "8870934003:AAGIpiwdgpnQVW7nbJIRcR0dOLOzj-MOZsA"
CHAT_ID = "6929517567"

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://rllpcylzhptqwzmzehnv.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "Sb_secret_ln9y67Ep_zCtOQ9Q2NE8KQ_nf0gKkmO")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

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

# ==================== SUPABASE HAFIZA ====================
def hafizayi_yukle():
    try:
        response = supabase.table("bot_hafiza").select("*").eq("id", 1).execute()
        if response.data and len(response.data) > 0:
            veri = response.data[0]
            return {
                "aktif_sistemler": veri.get("aktif_sistemler", {}),
                "analitik": veri.get("analitik", {
                    "basarili_islem_sayisi": 0,
                    "basarisiz_islem_sayisi": 0,
                    "gunluk_net_kar_usd": 0.0
                })
            }
    except Exception as e:
        print(f"Hafıza yükleme hatası: {e}")
        
    varsayilan = {
        "aktif_sistemler": {},
        "analitik": {
            "basarili_islem_sayisi": 0,
            "basarisiz_islem_sayisi": 0,
            "gunluk_net_kar_usd": 0.0
        }
    }
    supabase.table("bot_hafiza").upsert({"id": 1, **varsayilan}).execute()
    return varsayilan

def hafizayi_kaydet():
    try:
        supabase.table("bot_hafiza").upsert({
            "id": 1,
            "aktif_sistemler": AKTIF_SISTEMLER,
            "analitik": ANALITIK
        }).execute()
    except Exception as e:
        print(f"Hafıza kaydetme hatası: {e}")

kalici_veri = hafizayi_yukle()
AKTIF_SISTEMLER = kalici_veri.get("aktif_sistemler", {})
ANALITIK = kalici_veri.get("analitik", {
    "basarili_islem_sayisi": 0,
    "basarisiz_islem_sayisi": 0,
    "gunluk_net_kar_usd": 0.0
})

MAKSIMUM_TOPLAM_POZISYON = 3

def telegram_mesaj_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": mesaj, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"Telegram Gönderme Hatası: {e}")

@app.route('/')
def home():
    return f"Kazanma Odaklı Bot | Aktif Pozisyon: {len(AKTIF_SISTEMLER)}"

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

def pozisyonu_garantili_kapat(symbol, yon, miktar, sebep_mesaji, basarili=True):
    kapatma_yonu = 'sell' if yon == 'LONG' else 'buy'
    try:
        for ord_item in exchange.fetch_open_orders(symbol):
            exchange.cancel_order(ord_item['id'], symbol)
    except Exception:
        pass

    try:
        market_info = exchange.market(symbol)
        min_amount = float(market_info['limits']['amount']['min'] or 1.0)
        if miktar < min_amount:
            miktar = min_amount
        miktar = float(exchange.amount_to_precision(symbol, miktar))
        exchange.create_order(symbol, 'market', kapatma_yonu, miktar, None, {'reduce_only': True})
    except Exception as e:
        print(f"Kapatma API hatası: {e}")

    if basarili:
        ANALITIK["basarili_islem_sayisi"] += 1
    else:
        ANALITIK["basarisiz_islem_sayisi"] += 1

    if symbol in AKTIF_SISTEMLER:
        del AKTIF_SISTEMLER[symbol]
        hafizayi_kaydet()

    if sebep_mesaji:
        telegram_mesaj_gonder(sebep_mesaji)

# ==================== TELEGRAM KOMUTLARI ====================
async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        balance = exchange.fetch_balance()
        total = float(balance['total'].get('USDT', 0))
        
        try:
            raw_positions = exchange.fetch_positions()
            borsa_poslari = [p for p in raw_positions if float(p.get('contracts', 0)) > 0]
        except Exception:
            borsa_poslari = []

        toplam_pnl = sum(float(p.get('unrealizedPnl', 0)) for p in borsa_poslari)
        pnl_ikon = "🟢" if toplam_pnl >= 0 else "🔴"

        b_sayi = ANALITIK.get("basarili_islem_sayisi", 0)
        m_sayi = ANALITIK.get("basarisiz_islem_sayisi", 0)
        toplam = b_sayi + m_sayi
        basari_orani = (b_sayi / toplam * 100) if toplam > 0 else 0.0

        mesaj = (
            f"💰 *KAZANMA ODAKLI BOT DURUMU*\n\n"
            f"💵 Toplam Kasa: `{total:.2f} USDT`\n"
            f"{pnl_ikon} Anlık Kâr/Zarar: `{toplam_pnl:+.2f} USDT`\n"
            f"📌 Açık Pozisyon: `{len(borsa_poslari)} / {MAKSIMUM_TOPLAM_POZISYON}`\n\n"
            f"🎯 Başarılı TP: `{b_sayi}` | 🛑 Stop SL: `{m_sayi}`\n"
            f"📈 Başarı Oranı: `%{basari_orani:.1f}`\n\n"
        )
        
        if borsa_poslari:
            mesaj += "📋 *Aktif Pozisyonlar (Trailing Aktif):*\n"
            for pos in borsa_poslari:
                sym = pos.get('symbol')
                yon = str(pos.get('side', '')).upper()
                kaldirac = pos.get('leverage', 10)
                pnl = float(pos.get('unrealizedPnl', 0))
                yuzde = float(pos.get('percentage', 0))
                isaret = "🟢" if pnl >= 0 else "🔴"
                mesaj += f"🔹 *{sym}* (`{yon}` {kaldirac}x)\n   {isaret} `{pnl:+.2f} USDT` (`%{yuzde:+.2f}`)\n"
        else:
            mesaj += "ℹ️ Açık pozisyon yok, fırsat kollanıyor."

        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = True
    await update.message.reply_text("🚀 *Kazanma Odaklı Bot Başlatıldı!*", parse_mode='Markdown')

async def durdur_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ *Bot durduruldu.*", parse_mode='Markdown')

async def kapat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 *Tüm pozisyonlar kapatılıyor...*", parse_mode='Markdown')
    try:
        for pos in exchange.fetch_positions():
            if float(pos.get('contracts', 0)) > 0:
                pozisyonu_garantili_kapat(pos['symbol'], str(pos.get('side', '')).upper(), float(pos['contracts']), f"🛑 *MANUEL KAPATMA* - `{pos['symbol']}`", basarili=False)
        AKTIF_SISTEMLER.clear()
        hafizayi_kaydet()
        await update.message.reply_text("✅ Temizlendi.", parse_mode='Markdown')
    except Exception as e:
        AKTIF_SISTEMLER.clear()
        hafizayi_kaydet()
        await update.message.reply_text(f"✅ Hafıza sıfırlandı. ({e})", parse_mode='Markdown')

# ==================== ANA KAZANÇ DÖNGÜSÜ & TRAILING STOP ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU
    print("🚀 Kazanma Odaklı Sörf Tarayıcısı Devrede.")
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
                raw_positions = exchange.fetch_positions()
                aktif_borsa_map = {p['symbol']: p for p in raw_positions if float(p.get('contracts', 0)) > 0}
            except Exception:
                aktif_borsa_map = {}

            for sym in list(AKTIF_SISTEMLER.keys()):
                if sym not in aktif_borsa_map:
                    del AKTIF_SISTEMLER[sym]
                    hafizayi_kaydet()

            # --- 1. TRAILING STOP VE KÂR BÜYÜTME KONTROLÜ ---
            for symbol, pos in aktif_borsa_map.items():
                try:
                    guncel_fiyat = exchange.fetch_ticker(symbol)['last']
                except Exception:
                    continue

                yon = str(pos.get('side', '')).upper()
                merkez = float(pos.get('entryPrice', 0))
                kaldirac = int(pos.get('leverage', 10))
                
                fark = (guncel_fiyat - merkez) / merkez if yon == "LONG" else (merkez - guncel_fiyat) / merkez
                roe = fark * 100 * kaldirac
                pnl = float(pos.get('unrealizedPnl', 0))

                kayitli = AKTIF_SISTEMLER.get(symbol, {})
                en_yuksek_roe = kayitli.get("en_yuksek_roe", roe)

                # Yeni zirve görüldüyse kaydet
                if roe > en_yuksek_roe:
                    en_yuksek_roe = roe
                    kayitli["en_yuksek_roe"] = en_yuksek_roe
                    hafizayi_kaydet()

                # TRAILING STOP MEKANİZMASI:
                # Eğer kâr %10'un üzerine çıktıysa, arkadan stopu sıkılaştırıp kârı garantiliyoruz.
                # Zirveden %4 geri çekilirse kârı cebimize atıp kaçıyoruz!
                if en_yuksek_roe >= 10.0 and roe <= (en_yuksek_roe - 4.0):
                    pozisyonu_garantili_kapat(
                        symbol, yon, float(pos['contracts']),
                        f"🎯 *KÂR SÖRFÜ TAMAMLANDI (TRAILING TP)*\n\n📌 *Coin:* `{symbol}`\n📊 *Yön:* `{yon}`\n💰 *Alınan Kâr:* `+{pnl:.2f} USDT` (`%{roe:.2f}` / Zirve: `%{en_yuksek_roe:.2f}`)",
                        basarili=True
                    )
                    continue

                # Standart Sabit TP (%15 net kârda direkt cebine at)
                if roe >= 15.0:
                    pozisyonu_garantili_kapat(
                        symbol, yon, float(pos['contracts']),
                        f"🎯 *HEDEF 15% VURuldu (TP)*\n\n📌 *Coin:* `{symbol}`\n📊 *Yön:* `{yon}`\n💰 *Kâr:* `+{pnl:.2f} USDT` (`%{roe:.2f}`)",
                        basarili=True
                    )
                    continue

                # Sabit Stop Loss (Güvenli %8 - parayı ezdirmez ama nefes aldırır)
                if roe <= -8.0:
                    pozisyonu_garantili_kapat(
                        symbol, yon, float(pos['contracts']),
                        f"🛑 *ZARAR KESİLDİ (SL)*\n\n📌 *Coin:* `{symbol}`\n📊 *Yön:* `{yon}`\n📉 *Zarar:* `{pnl:.2f} USDT` (`%{roe:.2f}`)",
                        basarili=False
                    )
                    continue

            # --- 2. GÜÇLÜ TREND YAKALAMA (ALIM FIRSATLARI) ---
            taranan_sinyaller = []

            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU:
                    break
                if symbol in aktif_borsa_map:
                    continue

                try:
                    guncel_fiyat = exchange.fetch_ticker(symbol)['last']
                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=40)
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    
                    ema7 = ta.trend.ema_indicator(df['close'], window=7).iloc[-1]
                    ema21 = ta.trend.ema_indicator(df['close'], window=21).iloc[-1]
                    rsi = ta.momentum.rsi(df['close'], window=14).iloc[-1]
                    adx = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14).adx().iloc[-1]
                except Exception:
                    continue

                # Sadece net trend olan veya güçlü hacimli yerlere gireceğiz (Testereden kaçış)
                if adx < 22:
                    continue

                if ema7 > ema21 and rsi > 48 and rsi < 72:
                    yon = "LONG"
                    puan = adx + (rsi - 50)
                elif ema7 < ema21 and rsi < 52 and rsi > 28:
                    yon = "SHORT"
                    puan = adx + (50 - rsi)
                else:
                    continue

                taranan_sinyaller.append({
                    "symbol": symbol,
                    "puan": puan,
                    "yon": yon,
                    "rsi": rsi,
                    "adx": adx,
                    "fiyat": guncel_fiyat
                })

            taranan_sinyaller.sort(key=lambda x: x["puan"], reverse=True)

            # --- 3. 10x KALDIRAÇLA POZİSYON AÇMA ---
            for sinyal in taranan_sinyaller:
                if not BOT_CALISIYOR_MU:
                    break
                if len(aktif_borsa_map) >= MAKSIMUM_TOPLAM_POZISYON:
                    break

                symbol = sinyal["symbol"]
                yon = sinyal["yon"]
                puan = sinyal["puan"]
                rsi = sinyal["rsi"]
                adx = sinyal["adx"]
                guncel_fiyat = sinyal["fiyat"]

                kaldirac = 10  # Ne çok ölü ne çok riskli, kazandıracak ideal kaldıraç
                kasa_orani = 0.12 # Kasanın %12'si ile işlem

                try:
                    balance = exchange.fetch_balance()
                    toplam_bakiye = float(balance['total'].get('USDT', 0))
                except Exception:
                    continue

                set_isolated_leverage_safely(symbol, kaldirac)
                
                hedef_marjin = toplam_bakiye * kasa_orani
                hedef_pozisyon_usdt = hedef_marjin * kaldirac
                ham_miktar = hedef_pozisyon_usdt / guncel_fiyat

                try:
                    market_info = exchange.market(symbol)
                    contract_size = float(market_info.get('contractSize', 1.0))
                    min_amount = float(market_info['limits']['amount']['min'] or 1.0)
                    
                    gercek_ham_miktar = max(ham_miktar / contract_size, min_amount)
                    miktar = float(exchange.amount_to_precision(symbol, gercek_ham_miktar))
                    
                    if miktar < min_amount:
                        miktar = min_amount

                    emir_yonu = 'buy' if yon == 'LONG' else 'sell'
                    exchange.create_order(symbol, 'market', emir_yonu, miktar)

                    AKTIF_SISTEMLER[symbol] = {
                        "en_yuksek_roe": 0.0
                    }
                    hafizayi_kaydet()
                    
                    telegram_mesaj_gonder(
                        f"🚀 *KAZANMA ODAKLI İŞLEM AÇILDI*\n\n"
                        f"📌 *Coin:* `{symbol}`\n"
                        f"📊 *Yön:* `{yon}` | ⭐ *Güç Puanı:* `{puan:.1f}`\n"
                        f"⚙️ *Kaldıraç:* `{kaldirac}x` | 📈 *RSI:* `{rsi:.1f}` | *ADX:* `{adx:.1f}`\n"
                        f"🎯 *Hedef:* Trailing Stop (Kârı Sörfletecek)"
                    )
                    
                    aktif_borsa_map[symbol] = {'symbol': symbol, 'side': yon, 'contracts': miktar}
                except Exception as e:
                    print(f"Emir açma hatası ({symbol}): {e}")

        except Exception as e:
            print(f"Tarayıcı hata: {e}")
            
        time.sleep(10)

def flask_web_server():
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))

if __name__ == '__main__':
    threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True).start()
    threading.Thread(target=flask_web_server, daemon=True).start()
    
    app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app_tg.add_handler(CommandHandler("durum", durum_komutu))
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))
    
    app_tg.run_polling()
