import os
import sys

os.environ['PYTHONUNBUFFERED'] = '1'
try:
    sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)
except Exception:
    sys.stdout.reconfigure(line_buffering=True)

import time
import threading
import asyncio
import requests
import ccxt
import pandas as pd
import ta
import numpy as np
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler
from supabase import create_client, Client
from flask import Flask

# ==================== FLASK ====================
app = Flask(__name__)

@app.route("/")
def home():
    return "Kripto Bot Aktif!", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# ==================== ENV ====================
if os.path.exists('/etc/secrets/.env'):
    load_dotenv('/etc/secrets/.env', override=True)
else:
    load_dotenv(override=True)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌ SUPABASE env eksik!", flush=True)
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

exchange = ccxt.gate({
    'apiKey': os.environ.get("GATE_API_KEY", "").strip(),
    'secret': os.environ.get("GATE_SECRET", "").strip(),
    'enableRateLimit': True,
    'timeout': 30000,
    'options': {'defaultType': 'swap'}
})
exchange.set_sandbox_mode(True)

TAKIP_EDILENLER = [
    'SOL/USDT:USDT',
    'XRP/USDT:USDT',
    'DOGE/USDT:USDT',
    'LTC/USDT:USDT',
    'LINK/USDT:USDT'
]

# ==================== GLOBAL DURUM ====================
BOT_CALISIYOR_MU = True
state_lock = threading.RLock()
KALDIRAC = 5
MAKSIMUM_TOPLAM_POZISYON = 2
COOLDOWN_SURESI_SANIYE = 15 * 60
TARAMA_ARALIGI = 10
HACIM_ESIGI = 0.3
MIN_TP_YUZDE = 0.005

SON_BTC_YONU = "YATAY (Testere)"
SON_REJIM = "YATAY"
SON_BTC_ADX = 0.0
BTC_REJIM_GECISI_UYARI = False

DURUM_CACHE = {
    "kasa": 0.0,
    "toplam_pnl": 0.0,
    "acik_pozisyon_sayisi": 0,
    "pozisyon_detaylari": "",
    "son_guncelleme": 0
}

# ==================== SUPABASE HAFIZA ====================
def hafizayi_yukle():
    print("💾 Hafıza yükleniyor...", flush=True)
    try:
        response = supabase.table("bot_hafiza").select("*").eq("id", 1).execute()
        if response.data and len(response.data) > 0:
            veri = response.data[0]
            print("💾 Hafıza yüklendi.", flush=True)
            return {
                "aktif_sistemler": veri.get("aktif_sistemler", {}),
                "analitik": veri.get("analitik", {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0, "egitim_verileri": []}),
                "cooldownlar": veri.get("cooldownlar", {})
            }
    except Exception as e:
        print(f"⚠️ Hafıza yükleme hatası: {e}", flush=True)
    return {
        "aktif_sistemler": {},
        "analitik": {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0, "egitim_verileri": []},
        "cooldownlar": {}
    }

def hafizayi_kaydet():
    with state_lock:
        try:
            payload_analitik = {
                "basarili_islem_sayisi": int(ANALITIK_HAFIZA.get("basarili_islem_sayisi", 0)),
                "basarisiz_islem_sayisi": int(ANALITIK_HAFIZA.get("basarisiz_islem_sayisi", 0)),
                "egitim_verileri": ANALITIK_HAFIZA.get("egitim_verileri", [])[-50:]
            }
            clean_cooldowns = {}
            for k, v in COIN_COOLDOWNLAR.items():
                if isinstance(v, dict):
                    clean_cooldowns[k] = {"zaman": float(v.get("zaman", 0)), "son_yon": str(v.get("son_yon", ""))}
                else:
                    clean_cooldowns[k] = {"zaman": float(v), "son_yon": ""}

            supabase.table("bot_hafiza").upsert({
                "id": 1,
                "aktif_sistemler": AKTIF_GRID_SISTEMLERI,
                "analitik": payload_analitik,
                "cooldownlar": clean_cooldowns
            }).execute()
        except Exception as e:
            print(f"⚠️ Hafıza kaydetme hatası: {e}", flush=True)

kalici_veri = hafizayi_yukle()
AKTIF_GRID_SISTEMLERI = kalici_veri.get("aktif_sistemler", {})
ANALITIK_HAFIZA = kalici_veri.get("analitik", {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0, "egitim_verileri": []})
COIN_COOLDOWNLAR = kalici_veri.get("cooldownlar", {})

# ==================== PİYASA REJİMİ ====================
def piyasa_rejimini_tespit_et():
    global SON_BTC_YONU, SON_REJIM, SON_BTC_ADX, BTC_REJIM_GECISI_UYARI
    try:
        ohlcv_btc = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='1h', limit=60)
        df_btc = pd.DataFrame(ohlcv_btc, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

        adx_1h = float(ta.trend.ADXIndicator(df_btc['high'], df_btc['low'], df_btc['close'], window=14).adx().iloc[-1])

        bb = ta.volatility.BollingerBands(close=df_btc['close'], window=20, window_dev=2)
        bb_high = float(bb.bollinger_hband().iloc[-1])
        bb_low = float(bb.bollinger_lband().iloc[-1])
        bb_mid = float(bb.bollinger_mavg().iloc[-1])
        bb_bandwidth = (bb_high - bb_low) / bb_mid if bb_mid > 0 else 0

        ema9 = float(ta.trend.ema_indicator(df_btc['close'], window=9).iloc[-1])
        ema21 = float(ta.trend.ema_indicator(df_btc['close'], window=21).iloc[-1])
        fark_yuzdesi = (abs(ema9 - ema21) / ema21) * 100 if ema21 > 0 else 0

        trend_mi = (adx_1h >= 25.0) and (bb_bandwidth >= 0.04) and (fark_yuzdesi >= 0.3)

        trend_gecis_sinyali = (
            (20 <= adx_1h < 25) or 
            (0.03 <= bb_bandwidth < 0.04) or 
            (0.2 <= fark_yuzdesi < 0.3)
        )

        if trend_mi:
            rejim = "TREND"
            trend_yonu = "LONG" if ema9 > ema21 else "SHORT"
            SON_BTC_YONU = trend_yonu
            BTC_REJIM_GECISI_UYARI = False
        else:
            rejim = "YATAY"
            trend_yonu = "YATAY (Testere)"
            
            if trend_gecis_sinyali:
                BTC_REJIM_GECISI_UYARI = True
                print(f"⚠️ [REJİM GEÇİŞ UYARISI] YATAY → TREND sinyali yükseliyor! ADX:{adx_1h:.1f} BBW:{bb_bandwidth:.4f} EMA%:{fark_yuzdesi:.2f}", flush=True)
            else:
                BTC_REJIM_GECISI_UYARI = False

        SON_REJIM = rejim
        SON_BTC_ADX = adx_1h

        print(f"📊 [BTC REJİM] Rejim: {rejim} | Yön: {trend_yonu} | ADX: {adx_1h:.1f} | BBW: {bb_bandwidth:.4f} | EMA%: {fark_yuzdesi:.2f} | GeçişUyarı: {BTC_REJIM_GECISI_UYARI}", flush=True)
        return rejim, trend_yonu
    except Exception as e:
        print(f"⚠️ [BTC REJİM HATA] {e}", flush=True)
        return "YATAY", "YATAY (Testere)"

# ==================== EMİR DEFTERİ + DİNAMİK TP/SL ====================
def emir_defteri_duvar_analizi(symbol, anlik_fiyat, yon, df, mod="TREND"):
    try:
        atr = float(ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range().iloc[-1])
        
        # ============================================
        # ✅ TESTERE MODU: ESKİ KOD MANTIĞI (ATR × 2.0 / 1.5)
        # ============================================
        if "TERS MOD" in mod or "Testere" in mod:
            print(f"   🎯 [TESTERE MODU - ESKİ KOD] Sabit ATR × 2.0 / 1.5", flush=True)
            
            if yon == 'LONG':
                giris_fiyati = anlik_fiyat
                tp_fiyat = anlik_fiyat + (atr * 2.0)
                sl_fiyat = anlik_fiyat - (atr * 1.5)
                kapat_yon = 'sell'
            else:
                giris_fiyati = anlik_fiyat
                tp_fiyat = anlik_fiyat - (atr * 2.0)
                sl_fiyat = anlik_fiyat + (atr * 1.5)
                kapat_yon = 'buy'
            
            hedef_roe = abs((tp_fiyat - giris_fiyati) / giris_fiyati) * 100 * KALDIRAC
            sl_roe = abs((sl_fiyat - giris_fiyati) / giris_fiyati) * 100 * KALDIRAC
            print(f"🧱 [TESTERE TP/SL] {symbol} | {yon} | Giriş:{giris_fiyati:.4f} TP:{tp_fiyat:.4f} SL:{sl_fiyat:.4f} | TP ROE:%{hedef_roe:.1f} SL ROE:-%{sl_roe:.1f}", flush=True)
            return float(tp_fiyat), float(sl_fiyat), kapat_yon, float(hedef_roe), float(giris_fiyati)
        
        # ============================================
        # ✅ TREND MODU: Emir defteri duvar analizi
        # ============================================
        order_book = exchange.fetch_order_book(symbol, limit=25)
        bids = order_book.get('bids', [])
        asks = order_book.get('asks', [])

        if not bids or not asks:
            raise Exception("Order book boş")

        bid_miktarlari = [b[1] for b in bids]
        ask_miktarlari = [a[1] for a in asks]
        ortalama_bid = sum(bid_miktarlari) / len(bid_miktarlari) if bid_miktarlari else 1
        ortalama_ask = sum(ask_miktarlari) / len(ask_miktarlari) if ask_miktarlari else 1

        en_iyi_alis_duvari = max(bids, key=lambda x: x[1])
        en_iyi_satis_duvari = max(asks, key=lambda x: x[1])

        duvar_bilgisi = "ATR Yedekli"
        min_tp_mesafe = anlik_fiyat * MIN_TP_YUZDE

        if yon == 'LONG':
            duvar_fiyat_alis = en_iyi_alis_duvari[0]
            duvar_fiyat_satis = en_iyi_satis_duvari[0]

            if duvar_fiyat_alis < anlik_fiyat and en_iyi_alis_duvari[1] > (ortalama_bid * 1.5):
                giris_fiyati = duvar_fiyat_alis + (atr * 0.1)
                duvar_bilgisi = f"Alış Duvarı: {duvar_fiyat_alis}"
            else:
                giris_fiyati = anlik_fiyat

            if duvar_fiyat_satis > anlik_fiyat and en_iyi_satis_duvari[1] > (ortalama_ask * 1.5):
                tp_fiyat = duvar_fiyat_satis - (atr * 0.15)
            else:
                tp_fiyat = anlik_fiyat + (atr * 1.8)

            if (tp_fiyat - anlik_fiyat) < min_tp_mesafe:
                tp_fiyat = anlik_fiyat + (atr * 1.8)
                duvar_bilgisi += " (ATR-TP)"

            sl_fiyat = giris_fiyati - (atr * 1.3)
            kapat_yon = 'sell'
        else:
            duvar_fiyat_alis = en_iyi_alis_duvari[0]
            duvar_fiyat_satis = en_iyi_satis_duvari[0]

            if duvar_fiyat_satis > anlik_fiyat and en_iyi_satis_duvari[1] > (ortalama_ask * 1.5):
                giris_fiyati = duvar_fiyat_satis - (atr * 0.1)
                duvar_bilgisi = f"Satış Duvarı: {duvar_fiyat_satis}"
            else:
                giris_fiyati = anlik_fiyat

            if duvar_fiyat_alis < anlik_fiyat and en_iyi_alis_duvari[1] > (ortalama_bid * 1.5):
                tp_fiyat = duvar_fiyat_alis + (atr * 0.15)
            else:
                tp_fiyat = anlik_fiyat - (atr * 1.8)

            if (anlik_fiyat - tp_fiyat) < min_tp_mesafe:
                tp_fiyat = anlik_fiyat - (atr * 1.8)
                duvar_bilgisi += " (ATR-TP)"

            sl_fiyat = giris_fiyati + (atr * 1.3)
            kapat_yon = 'buy'

        hedef_roe = abs((tp_fiyat - giris_fiyati) / giris_fiyati) * 100 * KALDIRAC
        print(f"🧱 [DUVAR] {symbol} | {yon} | {duvar_bilgisi} | Giriş:{giris_fiyati:.4f} TP:{tp_fiyat:.4f} SL:{sl_fiyat:.4f} | Hedef ROE:%{hedef_roe:.1f}", flush=True)
        return float(tp_fiyat), float(sl_fiyat), kapat_yon, float(hedef_roe), float(giris_fiyati)

    except Exception as e:
        print(f"⚠️ Duvar analizi hata ({symbol}), ATR bazlı: {e}", flush=True)
        try:
            atr = float(ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range().iloc[-1])
        except Exception:
            atr = anlik_fiyat * 0.01
        if yon == 'LONG':
            tp_fiyat = anlik_fiyat + (atr * 1.5)
            sl_fiyat = anlik_fiyat - (atr * 1.0)
            kapat_yon = 'sell'
        else:
            tp_fiyat = anlik_fiyat - (atr * 1.5)
            sl_fiyat = anlik_fiyat + (atr * 1.0)
            kapat_yon = 'buy'
        hedef_roe = abs((tp_fiyat - anlik_fiyat) / anlik_fiyat) * 100 * KALDIRAC
        return float(tp_fiyat), float(sl_fiyat), kapat_yon, float(hedef_roe), float(anlik_fiyat)

# ==================== TELEGRAM ====================
def telegram_mesaj_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": mesaj, "parse_mode": "Markdown"},
            timeout=5
        )
    except Exception as e:
        print(f"⚠️ Telegram gönderim hatası: {e}", flush=True)

async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if str(update.effective_chat.id) != str(CHAT_ID):
            return

        with state_lock:
            basarili = int(ANALITIK_HAFIZA.get("basarili_islem_sayisi", 0))
            basarisiz = int(ANALITIK_HAFIZA.get("basarisiz_islem_sayisi", 0))
            aktif_kopya = dict(AKTIF_GRID_SISTEMLERI)

        toplam_islem = basarili + basarisiz
        basari_orani = (basarili / toplam_islem * 100) if toplam_islem > 0 else 0.0
        pos_detaylari = DURUM_CACHE.get("pozisyon_detaylari", "") or "  (henüz taranmadı)"

        gecis_uyari = " ⚠️ GEÇİŞ" if BTC_REJIM_GECISI_UYARI else ""

        mesaj = (
            f"📊 *BOT DURUM RAPORU*\n\n"
            f"🌐 Rejim: `{SON_REJIM}`{gecis_uyari} | BTC: `{SON_BTC_YONU}` | ADX: `{SON_BTC_ADX:.1f}`\n"
            f"💰 Kasa: `{DURUM_CACHE['kasa']:.2f} USDT` | PnL: `{DURUM_CACHE['toplam_pnl']:+.2f} USDT`\n"
            f"📌 Açık Pozisyon: `{len(aktif_kopya)} / {MAKSIMUM_TOPLAM_POZISYON}`\n"
            f"{pos_detaylari}\n\n"
            f"✅ TP: `{basarili}` | ❌ SL: `{basarisiz}`\n"
            f"📈 Başarı: `%{basari_orani:.1f}`"
        )
        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        print(f"⚠️ durum hatası: {e}", flush=True)

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    if str(update.effective_chat.id) != str(CHAT_ID): return
    BOT_CALISIYOR_MU = True
    await update.message.reply_text("🟢 Bot aktif! (Eski testere + Duvar analizi + Ani kırılım kontrolü)")

async def durdur_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    if str(update.effective_chat.id) != str(CHAT_ID): return
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ Bot durduruldu.")

async def kapat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(CHAT_ID): return
    try:
        positions = exchange.fetch_positions()
        kapatilan_coinler = []
        for pos in positions:
            kontrat = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
            if kontrat > 0:
                sym = pos['symbol']
                yon = str(pos.get('side', '')).upper() or "LONG"
                kapatma = 'sell' if yon == 'LONG' else 'buy'
                try: exchange.cancel_all_orders(sym)
                except Exception: pass
                exchange.create_order(sym, 'market', kapatma, kontrat, None, {'reduceOnly': True})
                kapatilan_coinler.append(sym)
                
                with state_lock:
                    COIN_COOLDOWNLAR[sym] = {
                        "zaman": float(time.time() + COOLDOWN_SURESI_SANIYE),
                        "son_yon": yon
                    }
                    if sym in AKTIF_GRID_SISTEMLERI:
                        del AKTIF_GRID_SISTEMLERI[sym]
                
                print(f"🔒 [MANUEL KAPAT] {sym} | {yon} | Cooldown {COOLDOWN_SURESI_SANIYE//60} dk", flush=True)
        
        hafizayi_kaydet()
        
        if kapatilan_coinler:
            liste = "\n".join([f"• `{s}`" for s in kapatilan_coinler])
            await update.message.reply_text(
                f"✅ *{len(kapatilan_coinler)} pozisyon kapatıldı*\n"
                f"{liste}\n\n"
                f"⏳ Her biri için {COOLDOWN_SURESI_SANIYE//60} dakika cooldown aktif.",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text("ℹ️ Açık pozisyon yok.")
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

def telegram_bot_thread_fonksiyonu():
    try:
        requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook?drop_pending_updates=True", timeout=10)
    except Exception: pass

    async def _run():
        app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
        app_tg.add_handler(CommandHandler("durum", durum_komutu))
        app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
        app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
        app_tg.add_handler(CommandHandler("kapat", kapat_komutu))

        await app_tg.initialize()
        await app_tg.start()
        await app_tg.updater.start_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
        print("🤖 Telegram bot polling aktif", flush=True)

        while True:
            await asyncio.sleep(3600)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_run())

# ==================== DURUM CACHE GÜNCELLE ====================
def durum_cache_guncelle():
    try:
        balance = exchange.fetch_balance()
        total = float(balance['total'].get('USDT', 0))
        positions = exchange.fetch_positions()
        acik = [p for p in positions if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
        toplam_pnl = sum(float(p.get('unrealizedPnl', 0) or 0) for p in acik)

        detay = ""
        with state_lock:
            aktif_kopya = dict(AKTIF_GRID_SISTEMLERI)

        for p in acik:
            sym = p['symbol']
            yon = str(p.get('side', '')).upper() or "LONG"
            giris = float(p.get('entryPrice', 0) or 0)
            lev = int(p.get('leverage', KALDIRAC) or KALDIRAC)
            try:
                ticker = exchange.fetch_ticker(sym)
                guncel = float(ticker['last'])
                fark = (guncel - giris) / giris if yon == "LONG" else (giris - guncel) / giris
                roe = fark * 100 * lev
            except Exception:
                roe = 0.0

            kayit = aktif_kopya.get(sym, {})
            tp_fiyat = float(kayit.get("tp_fiyat", 0) or 0)
            sl_fiyat = float(kayit.get("sl_fiyat", 0) or 0)

            if tp_fiyat > 0:
                hedef_roe = abs((tp_fiyat - giris) / giris) * 100 * lev
                sl_roe = abs((sl_fiyat - giris) / giris) * 100 * lev if sl_fiyat > 0 else 0
                detay += (
                    f"\n• `{sym}` | {yon} | Giriş: `{giris}`\n"
                    f"  Anlık ROE: `%{roe:+.2f}`\n"
                    f"  🎯 Hedef TP: `{tp_fiyat}` (ROE: `+%{hedef_roe:.1f}`)\n"
                    f"  🛑 SL: `{sl_fiyat}` (ROE: `-%{sl_roe:.1f}`)"
                )
            else:
                detay += f"\n• `{sym}` | {yon} | Giriş: `{giris}`\n  Anlık ROE: `%{roe:+.2f}`"

        with state_lock:
            DURUM_CACHE["kasa"] = total
            DURUM_CACHE["toplam_pnl"] = toplam_pnl
            DURUM_CACHE["acik_pozisyon_sayisi"] = len(acik)
            DURUM_CACHE["pozisyon_detaylari"] = detay
            DURUM_CACHE["son_guncelleme"] = time.time()
    except Exception as e:
        print(f"⚠️ durum_cache hatası: {e}", flush=True)

# ==================== ANA DÖNGÜ ====================
def otomatik_arkaplan_tarayici():
    print("🚀 [BAŞLANGIÇ] Bot devrede. Eski testere + Duvar + Ani kırılım aktif.", flush=True)
    try:
        exchange.load_markets()
    except Exception as e:
        print(f"⚠️ load_markets: {e}", flush=True)

    dongu_sayaci = 0

    while True:
        dongu_baslangic = time.time()
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5)
                continue

            dongu_sayaci += 1
            print(f"\n{'='*50}", flush=True)
            print(f"🔄 [DÖNGÜ #{dongu_sayaci}] {time.strftime('%H:%M:%S')}", flush=True)

            piyasa_rejimi, btc_yonu = piyasa_rejimini_tespit_et()

            # 2) Borsa senkronizasyonu
            try:
                borsa_pozlar = exchange.fetch_positions()
                borsa_acik = {}
                for p in borsa_pozlar:
                    kontrat = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                    if kontrat > 0:
                        borsa_acik[p['symbol']] = p

                with state_lock:
                    for sym, p in borsa_acik.items():
                        if sym not in AKTIF_GRID_SISTEMLERI:
                            print(f"➕ [SENKRON] {sym} borsada var, hafızaya eklendi", flush=True)
                            AKTIF_GRID_SISTEMLERI[sym] = {
                                "order_id": p.get('id', ''),
                                "durum": "DOLDURULDU",
                                "giris_fiyati": float(p.get('entryPrice', 0) or 0),
                                "tp_fiyat": 0,
                                "sl_fiyat": 0,
                                "kapat_yon": 'sell' if str(p.get('side', '')).upper() == 'LONG' else 'buy',
                                "miktar": kontrat,
                                "yon": str(p.get('side', '')).upper() or "LONG",
                                "giris_rsi": 0,
                                "giris_zamani": time.time(),
                                "mod": "SENKRON"
                            }

                    silinecek = [s for s in AKTIF_GRID_SISTEMLERI.keys() if s not in borsa_acik]
                    for s in silinecek:
                        kayit = AKTIF_GRID_SISTEMLERI[s]
                        yon = kayit.get("yon", "LONG")
                        giris_f = float(kayit.get("giris_fiyati", 0))
                        tp_f = float(kayit.get("tp_fiyat", 0))
                        sl_f = float(kayit.get("sl_fiyat", 0))

                        try:
                            son_islemler = exchange.fetch_my_trades(s, limit=5)
                            cikis = float(son_islemler[-1]['price']) if son_islemler else giris_f
                        except Exception:
                            try:
                                t = exchange.fetch_ticker(s)
                                cikis = float(t['last'])
                            except Exception:
                                cikis = giris_f

                        tp_mesafe = abs(cikis - tp_f) if tp_f > 0 else float('inf')
                        sl_mesafe = abs(cikis - sl_f) if sl_f > 0 else float('inf')

                        if tp_mesafe < sl_mesafe and tp_f > 0:
                            karlimi = True
                        elif sl_mesafe < tp_mesafe and sl_f > 0:
                            karlimi = False
                        else:
                            karlimi = (cikis > giris_f) if yon == "LONG" else (cikis < giris_f)

                        if yon == "LONG":
                            pnl_yuzde = (cikis - giris_f) / giris_f * 100 * KALDIRAC
                        else:
                            pnl_yuzde = (giris_f - cikis) / giris_f * 100 * KALDIRAC

                        if karlimi:
                            ANALITIK_HAFIZA["basarili_islem_sayisi"] = int(ANALITIK_HAFIZA.get("basarili_islem_sayisi", 0)) + 1
                            tip = "✅ *KÂR İLE KAPANDI (TP)*"
                        else:
                            ANALITIK_HAFIZA["basarisiz_islem_sayisi"] = int(ANALITIK_HAFIZA.get("basarisiz_islem_sayisi", 0)) + 1
                            tip = "❌ *ZARAR İLE KAPANDI (SL)*"

                        COIN_COOLDOWNLAR[s] = {
                            "zaman": float(time.time() + COOLDOWN_SURESI_SANIYE),
                            "son_yon": yon
                        }
                        del AKTIF_GRID_SISTEMLERI[s]

                        print(f"💰 [KAPANIŞ] {s} | {tip} | ROE:%{pnl_yuzde:+.2f}", flush=True)
                        telegram_mesaj_gonder(
                            f"{tip}\n"
                            f"📌 `{s}` | {yon}\n"
                            f"📍 Giriş: `{giris_f}` → Çıkış: `{cikis}`\n"
                            f"📊 Sonuç ROE: `%{pnl_yuzde:+.2f}`\n"
                            f"⏳ Cooldown: {COOLDOWN_SURESI_SANIYE // 60} dakika"
                        )

                hafizayi_kaydet()
            except Exception as e:
                print(f"⚠️ senkron hata: {e}", flush=True)

            # 2.5) YAZILIMSAL SL/TP + ANİ TREND KIRILIM KONTROLÜ
            with state_lock:
                kontrol_listesi = list(AKTIF_GRID_SISTEMLERI.items())

            for sym, kayit in kontrol_listesi:
                try:
                    sl_f = float(kayit.get("sl_fiyat", 0) or 0)
                    tp_f = float(kayit.get("tp_fiyat", 0) or 0)
                    if sl_f <= 0 and tp_f <= 0:
                        continue

                    yon = kayit.get("yon", "LONG")
                    giris = float(kayit.get("giris_fiyati", 0))

                    try:
                        t = exchange.fetch_ticker(sym)
                        anlik = float(t['last'])
                    except Exception:
                        continue

                    # ============================================
                    # ✅ ANİ TREND KIRILIM KONTROLÜ (Sadece testere pozisyonları için)
                    # ============================================
                    mod_bilgisi = str(kayit.get("mod", ""))
                    if "TERS MOD" in mod_bilgisi or "Testere" in mod_bilgisi:
                        try:
                            ohlcv_k = exchange.fetch_ohlcv(sym, timeframe='15m', limit=20)
                            df_k = pd.DataFrame(ohlcv_k, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                            
                            # 1) Bollinger Bandı kırılımı
                            bb_k = ta.volatility.BollingerBands(close=df_k['close'], window=20, window_dev=2)
                            bb_ust = float(bb_k.bollinger_hband().iloc[-1])
                            bb_alt = float(bb_k.bollinger_lband().iloc[-1])
                            
                            bb_kirildi = False
                            bb_yon = ""
                            if anlik > bb_ust:
                                bb_kirildi = True
                                bb_yon = "yukarı"
                            elif anlik < bb_alt:
                                bb_kirildi = True
                                bb_yon = "aşağı"
                            
                            # 2) Hacim patlaması (son mum > ortalama × 2.0)
                            son_hacim = float(df_k['volume'].iloc[-1])
                            ort_hacim = float(df_k['volume'].iloc[-21:-1].mean()) if len(df_k) >= 21 else float(df_k['volume'].mean())
                            hacim_patlama = (son_hacim > ort_hacim * 2.0) if ort_hacim > 0 else False
                            
                            # 3) Son 3 mum aynı yönde
                            son_3_kapanis = df_k['close'].iloc[-3:].values
                            artan = all(son_3_kapanis[i] < son_3_kapanis[i+1] for i in range(len(son_3_kapanis)-1))
                            azalan = all(son_3_kapanis[i] > son_3_kapanis[i+1] for i in range(len(son_3_kapanis)-1))
                            mum_kirilim = artan or azalan
                            mum_yon = "yukarı" if artan else ("aşağı" if azalan else "")
                            
                            kirilim_sinyalleri = sum([bb_kirildi, hacim_patlama, mum_kirilim])
                            
                            # 2/3+ sinyal + yön ters ise KAPAT
                            if kirilim_sinyalleri >= 2:
                                karsi_yon = False
                                if yon == "LONG":
                                    if bb_kirildi and bb_yon == "aşağı":
                                        karsi_yon = True
                                    if hacim_patlama and azalan:
                                        karsi_yon = True
                                elif yon == "SHORT":
                                    if bb_kirildi and bb_yon == "yukarı":
                                        karsi_yon = True
                                    if hacim_patlama and artan:
                                        karsi_yon = True
                                
                                if karsi_yon:
                                    sinyal_metni = f"BB:{'✓' if bb_kirildi else '✗'} Hacim:{'✓' if hacim_patlama else '✗'} Mum:{'✓' if mum_kirilim else '✗'}"
                                    print(f"🚨 [ANİ TREND KIRILIMI] {sym} | {yon} | Sinyal:{kirilim_sinyalleri}/3 ({sinyal_metni}) | {mum_yon or bb_yon} kırılım → KAPAT", flush=True)
                                    
                                    try:
                                        try:
                                            exchange.cancel_all_orders(sym)
                                        except Exception:
                                            pass
                                        positions = exchange.fetch_positions([sym])
                                        for p in positions:
                                            kontrat = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                                            if kontrat > 0:
                                                kapat_yon = 'sell' if yon == 'LONG' else 'buy'
                                                exchange.create_order(sym, 'market', kapat_yon, kontrat, None, {'reduceOnly': True})
                                                print(f"   ✅ {sym} ani kırılım ile kapatıldı", flush=True)
                                    except Exception as e:
                                        print(f"   ⚠️ Kapatma hatası: {e}", flush=True)
                                    
                                    if yon == "LONG":
                                        pnl_yuzde = (anlik - giris) / giris * 100 * KALDIRAC
                                    else:
                                        pnl_yuzde = (giris - anlik) / giris * 100 * KALDIRAC
                                    
                                    with state_lock:
                                        if pnl_yuzde > 0:
                                            ANALITIK_HAFIZA["basarili_islem_sayisi"] = int(ANALITIK_HAFIZA.get("basarili_islem_sayisi", 0)) + 1
                                        else:
                                            ANALITIK_HAFIZA["basarisiz_islem_sayisi"] = int(ANALITIK_HAFIZA.get("basarisiz_islem_sayisi", 0)) + 1
                                        COIN_COOLDOWNLAR[sym] = {
                                            "zaman": float(time.time() + COOLDOWN_SURESI_SANIYE),
                                            "son_yon": yon
                                        }
                                        if sym in AKTIF_GRID_SISTEMLERI:
                                            del AKTIF_GRID_SISTEMLERI[sym]
                                    
                                    hafizayi_kaydet()
                                    telegram_mesaj_gonder(
                                        f"🚨 *ANİ TREND KIRILIMI*\n"
                                        f"📌 `{sym}` | {yon}\n"
                                        f"📍 Giriş: `{giris}` → Çıkış: `{anlik}`\n"
                                        f"📊 Sonuç: `%{pnl_yuzde:+.2f}`\n"
                                        f"⚡ Sinyal: {kirilim_sinyalleri}/3 ({sinyal_metni})\n"
                                        f"⏳ Cooldown: {COOLDOWN_SURESI_SANIYE // 60} dakika"
                                    )
                                    continue
                        except Exception as e:
                            print(f"   ⚠️ Ani kırılım kontrol hatası ({sym}): {e}", flush=True)

                    # Normal SL/TP kontrolü
                    sl_vurdu = False
                    tp_vurdu = False

                    if yon == "LONG":
                        if sl_f > 0 and anlik <= sl_f:
                            sl_vurdu = True
                        if tp_f > 0 and anlik >= tp_f:
                            tp_vurdu = True
                    else:
                        if sl_f > 0 and anlik >= sl_f:
                            sl_vurdu = True
                        if tp_f > 0 and anlik <= tp_f:
                            tp_vurdu = True

                    if sl_vurdu or tp_vurdu:
                        tip = "🛑 *SL VURDU (Yazılımsal)*" if sl_vurdu else "🎯 *TP VURDU (Yazılımsal)*"
                        print(f"⚡ [MANUEL KAPAT] {sym} | {tip} | Anlık:{anlik} | SL:{sl_f} | TP:{tp_f}", flush=True)

                        try:
                            try:
                                exchange.cancel_all_orders(sym)
                            except Exception:
                                pass
                            positions = exchange.fetch_positions([sym])
                            for p in positions:
                                kontrat = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                                if kontrat > 0:
                                    kapatma_yon = 'sell' if yon == 'LONG' else 'buy'
                                    exchange.create_order(sym, 'market', kapatma_yon, kontrat, None, {'reduceOnly': True})
                                    print(f"   ✅ {sym} manuel kapatıldı ({kontrat} kontrat)", flush=True)
                        except Exception as e:
                            print(f"   ⚠️ Manuel kapatma hatası: {e}", flush=True)

                        if yon == "LONG":
                            pnl_yuzde = (anlik - giris) / giris * 100 * KALDIRAC
                        else:
                            pnl_yuzde = (giris - anlik) / giris * 100 * KALDIRAC

                        with state_lock:
                            if sl_vurdu:
                                ANALITIK_HAFIZA["basarisiz_islem_sayisi"] = int(ANALITIK_HAFIZA.get("basarisiz_islem_sayisi", 0)) + 1
                            else:
                                ANALITIK_HAFIZA["basarili_islem_sayisi"] = int(ANALITIK_HAFIZA.get("basarili_islem_sayisi", 0)) + 1

                            COIN_COOLDOWNLAR[sym] = {
                                "zaman": float(time.time() + COOLDOWN_SURESI_SANIYE),
                                "son_yon": yon
                            }
                            if sym in AKTIF_GRID_SISTEMLERI:
                                del AKTIF_GRID_SISTEMLERI[sym]

                        hafizayi_kaydet()

                        telegram_mesaj_gonder(
                            f"{tip}\n"
                            f"📌 `{sym}` | {yon}\n"
                            f"📍 Giriş: `{giris}` → Çıkış: `{anlik}`\n"
                            f"📊 Sonuç ROE: `%{pnl_yuzde:+.2f}`\n"
                            f"⏳ Cooldown: {COOLDOWN_SURESI_SANIYE // 60} dakika"
                        )
                except Exception as e:
                    print(f"⚠️ SL/TP kontrol ({sym}): {e}", flush=True)

            # REJİM TREND'E DÖNDÜYSE TERS POZİSYONLARI KAPAT
            if piyasa_rejimi == "TREND":
                with state_lock:
                    ters_pozisyonlar = []
                    for sym, kayit in AKTIF_GRID_SISTEMLERI.items():
                        if "TERS MOD" in str(kayit.get("mod", "")):
                            yon = kayit.get("yon", "LONG")
                            if (yon == "LONG" and btc_yonu == "SHORT") or (yon == "SHORT" and btc_yonu == "LONG"):
                                ters_pozisyonlar.append(sym)

                for sym in ters_pozisyonlar:
                    with state_lock:
                        kayit = AKTIF_GRID_SISTEMLERI.get(sym)
                    if not kayit:
                        continue

                    yon = kayit.get("yon", "LONG")
                    giris = float(kayit.get("giris_fiyati", 0))

                    try:
                        t = exchange.fetch_ticker(sym)
                        anlik = float(t['last'])
                    except Exception:
                        continue

                    print(f"🚨 [REJİM DEĞİŞTİ] {sym} | Testere pozisyonu trend yönüne ters, KAPATILIYOR", flush=True)

                    try:
                        try:
                            exchange.cancel_all_orders(sym)
                        except Exception:
                            pass
                        positions = exchange.fetch_positions([sym])
                        for p in positions:
                            kontrat = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                            if kontrat > 0:
                                kapat_yon = 'sell' if yon == 'LONG' else 'buy'
                                exchange.create_order(sym, 'market', kapat_yon, kontrat, None, {'reduceOnly': True})
                    except Exception as e:
                        print(f"   ⚠️ Kapatma hatası: {e}", flush=True)

                    if yon == "LONG":
                        pnl = (anlik - giris) / giris * 100 * KALDIRAC
                    else:
                        pnl = (giris - anlik) / giris * 100 * KALDIRAC

                    with state_lock:
                        if pnl > 0:
                            ANALITIK_HAFIZA["basarili_islem_sayisi"] = int(ANALITIK_HAFIZA.get("basarili_islem_sayisi", 0)) + 1
                        else:
                            ANALITIK_HAFIZA["basarisiz_islem_sayisi"] = int(ANALITIK_HAFIZA.get("basarisiz_islem_sayisi", 0)) + 1
                        COIN_COOLDOWNLAR[sym] = {
                            "zaman": float(time.time() + COOLDOWN_SURESI_SANIYE),
                            "son_yon": yon
                        }
                        if sym in AKTIF_GRID_SISTEMLERI:
                            del AKTIF_GRID_SISTEMLERI[sym]

                    hafizayi_kaydet()
                    telegram_mesaj_gonder(
                        f"🚨 *REJİM DEĞİŞTİ — POZİSYON KAPATILDI*\n"
                        f"📌 `{sym}` | {yon}\n"
                        f"📍 Giriş: `{giris}` → Çıkış: `{anlik}`\n"
                        f"📊 Sonuç: `%{pnl:+.2f}`\n"
                        f"⚡ YATAY→TREND geçişi, testere pozisyonu trend yönüne ters"
                    )

            # 3) Yeni tarama
            with state_lock:
                toplam_aktif = len(AKTIF_GRID_SISTEMLERI)

            if toplam_aktif >= MAKSIMUM_TOPLAM_POZISYON:
                print(f"📌 [LİMİT] Max pozisyon ({toplam_aktif}/{MAKSIMUM_TOPLAM_POZISYON})", flush=True)
            else:
                taranan = []
                for symbol in TAKIP_EDILENLER:
                    if not BOT_CALISIYOR_MU:
                        break

                    with state_lock:
                        if symbol in AKTIF_GRID_SISTEMLERI:
                            continue
                        cd = COIN_COOLDOWNLAR.get(symbol)
                        if cd:
                            z = cd.get("zaman", 0) if isinstance(cd, dict) else float(cd)
                            kalan = int(z - time.time())
                            if kalan > 0:
                                print(f"⏳ [{symbol}] Cooldown: {kalan}s", flush=True)
                                continue

                    try:
                        ticker = exchange.fetch_ticker(symbol)
                        anlik = float(ticker['last'])

                        try:
                            oo = exchange.fetch_open_orders(symbol)
                            if oo:
                                exchange.cancel_all_orders(symbol)
                        except Exception:
                            pass

                        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=60)
                        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                        rsi = float(ta.momentum.rsi(df['close'], window=14).iloc[-1])

                        son_hacim = float(df['volume'].iloc[-2])
                        ort_hacim = float(df['volume'].iloc[-21:-1].mean())
                        hacim_orani = son_hacim / ort_hacim if ort_hacim > 0 else 1.0

                        atr = float(ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range().iloc[-1])

                        high_24h = float(ticker.get('high') or anlik * 1.02)
                        low_24h = float(ticker.get('low') or anlik * 0.98)
                        tepeye_yakin = anlik >= (high_24h * 0.994)
                        dipe_yakin = anlik <= (low_24h * 1.006)

                        if piyasa_rejimi == "YATAY":
                            if rsi < 35:
                                islem_yonu = "SHORT"
                                sebep = f"Testere dibi (RSI {rsi:.1f}<35) → TERS SHORT"
                            elif rsi > 65:
                                islem_yonu = "LONG"
                                sebep = f"Testere tepesi (RSI {rsi:.1f}>65) → TERS LONG"
                            else:
                                print(
                                    f"🔍 [{symbol}] Fiyat:{anlik:.4f} | RSI:{rsi:.1f} | "
                                    f"Hacim x{hacim_orani:.2f} | ATR:{atr:.4f} | "
                                    f"⛔ RSI nötr ({rsi:.1f}) — testere modu beklemede",
                                    flush=True
                                )
                                continue
                            mod_adi = "TERS MOD (Testere)"
                        else:
                            if btc_yonu == "LONG" and rsi < 55 and not tepeye_yakin:
                                islem_yonu = "LONG"
                                sebep = f"BTC trendi LONG (RSI {rsi:.1f}, tepeden uzak)"
                            elif btc_yonu == "SHORT" and rsi > 45 and not dipe_yakin:
                                islem_yonu = "SHORT"
                                sebep = f"BTC trendi SHORT (RSI {rsi:.1f}, dipten uzak)"
                            else:
                                print(
                                    f"🔍 [{symbol}] Fiyat:{anlik:.4f} | RSI:{rsi:.1f} | "
                                    f"BTC:{btc_yonu} | Tepe:{tepeye_yakin} Dip:{dipe_yakin} | "
                                    f"⛔ Trend koşulları uygun değil",
                                    flush=True
                                )
                                continue
                            mod_adi = "TREND"

                        print(
                            f"🔍 [{symbol}] Fiyat:{anlik:.4f} | RSI:{rsi:.1f} | "
                            f"Hacim x{hacim_orani:.2f} | ATR:{atr:.4f} | "
                            f"→ {islem_yonu} ({mod_adi}) [{sebep}]",
                            flush=True
                        )

                        if hacim_orani < HACIM_ESIGI:
                            print(f"   ⛔ Hacim çok düşük (x{hacim_orani:.2f})", flush=True)
                            continue

                        taranan.append({
                            "symbol": symbol, "yon": islem_yonu, "rsi": rsi,
                            "fiyat": anlik, "df": df, "mod": mod_adi, "sebep": sebep
                        })
                    except Exception as e:
                        print(f"⚠️ tarama ({symbol}): {e}", flush=True)

                # 4) Emir aç
                for sinyal in taranan:
                    if not BOT_CALISIYOR_MU:
                        break

                    with state_lock:
                        if sinyal["symbol"] in AKTIF_GRID_SISTEMLERI:
                            print(f"   ⛔ {sinyal['symbol']} zaten açık, atlanıyor", flush=True)
                            continue
                        cd = COIN_COOLDOWNLAR.get(sinyal["symbol"])
                        if cd:
                            z = cd.get("zaman", 0) if isinstance(cd, dict) else float(cd)
                            kalan = int(z - time.time())
                            if kalan > 0:
                                print(f"   ⛔ {sinyal['symbol']} cooldown ({kalan}s), atlanıyor", flush=True)
                                continue
                        if len(AKTIF_GRID_SISTEMLERI) >= MAKSIMUM_TOPLAM_POZISYON:
                            break

                    try:
                        bakiye = exchange.fetch_balance()
                        toplam_b = float(bakiye['total'].get('USDT', 0))
                        serbest_b = float(bakiye.get('free', {}).get('USDT', 0) or 0)

                        if serbest_b < 5.0:
                            print(f"⚠️ Serbest bakiye düşük: {serbest_b}", flush=True)
                            continue

                        exchange.set_leverage(KALDIRAC, sinyal["symbol"])
                        market = exchange.market(sinyal["symbol"])

                        kullan = min(toplam_b * 0.4, serbest_b)
                        if kullan < 1.0:
                            continue

                        tp, sl, kapat_yon, hedef_roe, ideal_giris = emir_defteri_duvar_analizi(
                            sinyal["symbol"], sinyal["fiyat"], sinyal["yon"], sinyal["df"], sinyal["mod"]
                        )

                        contract_size = float(market.get('contractSize', 1.0) or 1.0)
                        min_amt = float(market['limits']['amount']['min'] or 1.0)
                        ham = (kullan * KALDIRAC) / ideal_giris / contract_size
                        miktar = float(exchange.amount_to_precision(sinyal["symbol"], max(ham, min_amt)))

                        emir_yonu = 'buy' if sinyal["yon"] == 'LONG' else 'sell'

                        print(f"🚀 [EMİR] {sinyal['symbol']} {sinyal['yon']} miktar:{miktar}", flush=True)
                        giris_emir = exchange.create_order(sinyal["symbol"], 'market', emir_yonu, miktar)
                        gerceklesen = float(giris_emir.get('average', 0) or giris_emir.get('price', 0) or ideal_giris)

                        # TP emri
                        try:
                            tp_emir = exchange.create_order(
                                sinyal["symbol"], 'limit', kapat_yon, miktar, tp,
                                {'reduceOnly': True}
                            )
                            print(f"   ✅ TP emri gönderildi: {tp} (id:{tp_emir.get('id','?')})", flush=True)
                        except Exception as e:
                            print(f"   ❌ TP emri HATA: {e}", flush=True)

                        # SL emri
                        try:
                            sl_emir = exchange.create_order(
                                sinyal["symbol"], 'stop', kapat_yon, miktar, sl,
                                {'stopPrice': sl, 'reduceOnly': True}
                            )
                            print(f"   ✅ SL emri gönderildi: {sl} (id:{sl_emir.get('id','?')})", flush=True)
                        except Exception as e:
                            print(f"   ❌ SL emri HATA: {e}", flush=True)
                            telegram_mesaj_gonder(
                                f"⚠️ *SL EMRİ GÖNDERİLEMEDİ*\n"
                                f"📌 `{sinyal['symbol']}` | Yön: `{sinyal['yon']}`\n"
                                f"🛑 Yazılımsal SL: `{sl}`\n"
                                f"⏱️ Bot her 10 sn kontrol edip manuel kapatacak."
                            )

                        with state_lock:
                            AKTIF_GRID_SISTEMLERI[sinyal["symbol"]] = {
                                "order_id": giris_emir['id'],
                                "durum": "DOLDURULDU",
                                "giris_fiyati": gerceklesen,
                                "tp_fiyat": tp,
                                "sl_fiyat": sl,
                                "kapat_yon": kapat_yon,
                                "miktar": miktar,
                                "yon": sinyal["yon"],
                                "giris_rsi": float(sinyal["rsi"]),
                                "giris_zamani": time.time(),
                                "mod": sinyal["mod"]
                            }
                        hafizayi_kaydet()

                        print(f"✅ [AÇILDI] {sinyal['symbol']} {sinyal['yon']} @ {gerceklesen} ({sinyal['mod']})", flush=True)
                        telegram_mesaj_gonder(
                            f"🚀 *İŞLEM AÇILDI* ({sinyal['mod']} - 5x)\n"
                            f"📌 `{sinyal['symbol']}` | Yön: `{sinyal['yon']}`\n"
                            f"📍 Giriş: `{gerceklesen}`\n"
                            f"💰 TP: `{tp}` (ROE: `+%{hedef_roe:.1f}`)\n"
                            f"🛑 SL: `{sl}`\n"
                            f"🔎 Sebep: {sinyal['sebep']}"
                        )
                    except Exception as e:
                        print(f"⚠️ emir hatası: {e}", flush=True)

            durum_cache_guncelle()

        except Exception as e:
            print(f"⚠️ ANA DÖNGÜ HATA: {e}", flush=True)
            import traceback
            traceback.print_exc()

        gecen = time.time() - dongu_baslangic
        bekle = max(0, TARAMA_ARALIGI - gecen)
        time.sleep(bekle)

# ==================== BAŞLAT ====================
if __name__ == '__main__':
    threading.Thread(target=run_flask, daemon=True).start()
    print("🌐 Flask başlatıldı", flush=True)

    threading.Thread(target=telegram_bot_thread_fonksiyonu, daemon=True).start()
    print("🤖 Telegram thread başlatıldı", flush=True)

    otomatik_arkaplan_tarayici()
