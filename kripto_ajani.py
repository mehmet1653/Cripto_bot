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
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler
from supabase import create_client, Client

# ==================== RENDER WEB SUNUCUSU ====================
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot aktif ve calisiyor!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ==================== AYARLAR VE ANAHTARLAR (.ENV / RENDER) ====================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8870934003:AAGOzmO_VwYnj0Wz2hehI176rKiOkEaV0b0")
CHAT_ID = os.environ.get("CHAT_ID", "6929517567")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://rllpcylzhptqwzmzehnv.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "Sb_secret_ln9y67Ep_zCtOQ9Q2NE8KQ_nf0gKkmO")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

exchange = ccxt.gate({
    'apiKey': os.environ.get("GATE_API_KEY", "82cca880898a88d1a31e86d8eb474c57"),
    'secret': os.environ.get("GATE_SECRET", "1ac479b9df5e6f2e89560b0d238a250694719b6fcae20da00ebc54ad6aeb8898"),
    'enableRateLimit': True,
    'timeout': 30000,
    'options': {
        'defaultType': 'swap'
    }
})

exchange.set_sandbox_mode(True)

TAKIP_EDILENLER = [
    'SOL/USDT:USDT', 
    'XRP/USDT:USDT', 
    'DOGE/USDT:USDT', 
    'LTC/USDT:USDT', 
    'LINK/USDT:USDT'
]

BOT_CALISIYOR_MU = True
state_lock = threading.Lock()
KALDIRAC = 5

SON_BTC_YONU = "YATAY (Testere)"
COIN_COOLDOWN_SURELERI = {} 

def hafizayi_yukle():
    print("💾 Hafıza Supabase'den yükleniyor...", flush=True)
    try:
        response = supabase.table("bot_hafiza").select("*").eq("id", 1).execute()
        if response.data and len(response.data) > 0:
            veri = response.data[0]
            print("💾 Hafıza başarıyla yüklendi.", flush=True)
            return {
                "aktif_sistemler": veri.get("aktif_sistemler", {}),
                "analitik": veri.get("analitik", {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0}),
                "genel_cooldown_bitis": float(veri.get("genel_cooldown_bitis", 0.0))
            }
    except Exception as e:
        print(f"⚠️ Hafıza yüklenirken hata: {e}", flush=True)
    
    return {
        "aktif_sistemler": {},
        "analitik": {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0},
        "genel_cooldown_bitis": 0.0
    }

def hafizayi_kaydet():
    with state_lock:
        try:
            payload_analitik = {
                "basarili_islem_sayisi": int(ANALitik_HAFIZA.get("basarili_islem_sayisi", 0)),
                "basarisiz_islem_sayisi": int(ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0))
            }

            supabase.table("bot_hafiza").upsert({
                "id": 1,
                "aktif_sistemler": AKTIF_GRID_SISTEMLERI,
                "analitik": payload_analitik,
                "genel_cooldown_bitis": float(GENEL_COOLDOWN_BITIS)
            }).execute()
        except Exception as e:
            print(f"⚠️ Hafıza kaydedilemedi: {e}", flush=True)

kalici_veri = hafizayi_yukle()
AKTIF_GRID_SISTEMLERI = kalici_veri.get("aktif_sistemler", {})
ANALitik_HAFIZA = kalici_veri.get("analitik", {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0})
GENEL_COOLDOWN_BITIS = float(kalici_veri.get("genel_cooldown_bitis", 0.0))

MAKSIMUM_TOPLAM_POZISYON = 2
GENEL_DINLENME_SURESI_SANIYE = 2 * 60 * 60  
ANORMAL_SL_SURESI_SINIRI = 5 * 60           
COIN_COOLDOWN_SURESI = 15 * 60              # TP ve SL sonrasında 15 dk cooldown

def piyasa_rejimini_tespit_et():
    global SON_BTC_YONU
    print("🌐 [PİYASA] Rejim analizi yapılıyor...", flush=True)
    try:
        ohlcv_btc = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='1h', limit=50)
        df_btc = pd.DataFrame(ohlcv_btc, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        adx_1h = ta.trend.ADXIndicator(df_btc['high'], df_btc['low'], df_btc['close'], window=14).adx().iloc[-1]
        ema9 = ta.trend.ema_indicator(df_btc['close'], window=9).iloc[-1]
        ema21 = ta.trend.ema_indicator(df_btc['close'], window=21).iloc[-1]
        
        bollinger = ta.volatility.BollingerBands(df_btc['close'], window=20, window_dev=2)
        bb_width = (bollinger.bollinger_hband().iloc[-1] - bollinger.bollinger_lband().iloc[-1]) / bollinger.bollinger_mavg().iloc[-1]
        
        trend_onayi = (adx_1h > 28.0) and (bb_width > 0.012)
        
        if not trend_onayi:
            rejim = "YATAY"
            trend_yonu = "YATAY (Testere)"
        else:
            rejim = "TREND"
            trend_yonu = "LONG" if ema9 > ema21 else "SHORT"
            SON_BTC_YONU = trend_yonu
            
        print(f"🌐 [PİYASA SONUÇ] Rejim: {rejim} | BTC Yön: {trend_yonu}", flush=True)
        return rejim, trend_yonu
    except Exception as e:
        print(f"⚠️ Rejim tespit hatası: {e}", flush=True)
        return "YATAY", "YATAY (Testere)"

def akilli_seviye_hesapla(anlik_fiyat, yon, df):
    atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range().iloc[-1]
    
    if yon == 'LONG':
        tp_fiyat = anlik_fiyat + (atr * 2.0)
        sl_fiyat = anlik_fiyat - (atr * 1.5)
        kapat_yon = 'sell'
    else:
        tp_fiyat = anlik_fiyat - (atr * 2.0)
        sl_fiyat = anlik_fiyat + (atr * 1.5)
        kapat_yon = 'buy'
        
    hedef_roe = abs((tp_fiyat - anlik_fiyat) / anlik_fiyat) * 100 * KALDIRAC
    return float(tp_fiyat), float(sl_fiyat), kapat_yon, float(hedef_roe)

def telegram_mesaj_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": mesaj, "parse_mode": "Markdown"}, timeout=5)
    except Exception: pass

async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    try:
        balance = await asyncio.to_thread(exchange.fetch_balance)
        total = float(balance['total'].get('USDT', 0))
        borsa_poslari = [p for p in await asyncio.to_thread(exchange.fetch_positions) if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
        toplam_pnl = sum(float(p.get('unrealizedPnl', 0)) for p in borsa_poslari)
        rejim, btc_yon = piyasa_rejimini_tespit_et()
        basarili = int(ANALitik_HAFIZA.get("basarili_islem_sayisi", 0))
        basarisiz = int(ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0))
        toplam_islem = basarili + basarisiz
        basari_orani = (basarili / toplam_islem * 100) if toplam_islem > 0 else 0.0

        kalan_dinlenme = GENEL_COOLDOWN_BITIS - time.time()
        dinlenme_durumu = f"⚠️ Anormal Piyasa Koruması (Kalan: `{int(kalan_dinlenme // 60)} dk`)" if kalan_dinlenme > 0 else "🟢 Aktif (Normal Akış)"

        pos_detaylari = ""
        for p in borsa_poslari:
            sym = p['symbol']
            yon = str(p.get('side', '')).upper() or "LONG"
            giris = float(p.get('entryPrice', 0))
            kaldirac_val = int(p.get('leverage', KALDIRAC))
            ticker_data = await asyncio.to_thread(exchange.fetch_ticker, sym)
            guncel_fiyat = float(ticker_data['last'])
            fark = (guncel_fiyat - giris) / giris if yon == "LONG" else (giris - guncel_fiyat) / giris
            roe = fark * 100 * kaldirac_val
            pos_detaylari += f"\n• `{sym}` | {yon} | Giriş: `{giris}`\n  Anlık ROE: `%{roe:+.2f}`"

        mesaj = (
            f"📊 **BOT DURUM RAPORU**\n\n"
            f"🌐 Piyasa Rejimi: `{rejim}` (BTC Yön: `{btc_yon}`)\n"
            f"🛡️ Bot Koruma Durumu: `{dinlenme_durumu}`\n"
            f"💰 Kasa: `{total:.2f} USDT` | Toplam PnL: `{toplam_pnl:+.2f} USDT`\n"
            f"📌 Açık Pozisyon: `{len(borsa_poslari)} / {MAKSIMUM_TOPLAM_POZISYON}`"
            f"{pos_detaylari}\n\n"
            f"✅ Başarılı TP: `{basarili}` | ❌ Başarısız SL: `{basarisiz}`\n"
            f"📈 Başarı Oranı: `%{basari_orani:.1f}`"
        )
        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = True
    await update.message.reply_text("🟢 Bot aktif edildi!")

async def durdur_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ Bot durduruldu.")

async def kapat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    try:
        positions = await asyncio.to_thread(exchange.fetch_positions)
        for pos in positions:
            kontrat = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
            if kontrat > 0:
                yon = str(pos.get('side', '')).upper() or "LONG"
                kapatma_yonu = 'sell' if yon == 'LONG' else 'buy'
                try: exchange.cancel_all_orders(pos['symbol'])
                except Exception: pass
                exchange.create_order(pos['symbol'], 'market', kapatma_yonu, kontrat, None, {'reduceOnly': True})
        await update.message.reply_text("✅ Tüm pozisyonlar ve emirler kapatıldı.")
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

def otomatik_arkaplan_tarayici():
    global GENEL_COOLDOWN_BITIS
    print("🚀 [BAŞLANGIÇ] Arka plan tarayıcı döngüsü başlatıldı...", flush=True)
    try:
        exchange.load_markets()
        print("✅ [BAŞARILI] Borsa piyasa verileri yüklendi.", flush=True)
    except Exception as e:
        print(f"⚠️ Piyasa verileri yüklenirken hata: {e}", flush=True)
    
    while True:
        try:
            print("🔄 [DÖNGÜ] Yeni tarama turu başlıyor...", flush=True)
            if not BOT_CALISIYOR_MU:
                print("⏸️ Bot durdurulmuş durumda, bekleniyor...", flush=True)
                time.sleep(5)
                continue

            piyasa_rejimi, btc_yonu = piyasa_rejimini_tespit_et()

            try:
                raw_positions = exchange.fetch_positions()
                aktif_borsa_map = {}
                aktif_semboller_listesi = []
                for p in raw_positions:
                    kontrat_miktari = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                    if kontrat_miktari > 0:
                        sym = p['symbol']
                        aktif_borsa_map[sym] = p
                        aktif_semboller_listesi.append(sym)
                print(f"📌 [BORSA] Aktif Pozisyon Sayısı: {len(aktif_borsa_map)} / {MAKSIMUM_TOPLAM_POZISYON}", flush=True)
            except Exception as e:
                print(f"⚠️ Pozisyonlar çekilirken hata: {e}", flush=True)
                aktif_borsa_map = {}
                aktif_semboller_listesi = []

            # Pozisyon Kapanış Kontrolü ve Kâr/Zarar Raporlaması
            try:
                anlik_aktif_semboller = [p['symbol'] for p in raw_positions if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
                for eski_sym in list(AKTIF_GRID_SISTEMLERI.keys()):
                    if eski_sym not in anlik_aktif_semboller:
                        print(f"🔍 [KAPANIŞ TESPİTİ] {eski_sym} pozisyonu kapanmış görünüyor, analiz ediliyor...", flush=True)
                        sistem_bilgisi = AKTIF_GRID_SISTEMLERI[eski_sym]
                        giris_fiyati = sistem_bilgisi.get("giris_fiyati", 0) if isinstance(sistem_bilgisi, dict) else 0
                        yon = sistem_bilgisi.get("yon", "LONG") if isinstance(sistem_bilgisi, dict) else "LONG"
                        giris_zamani = sistem_bilgisi.get("giris_zamani", time.time()) if isinstance(sistem_bilgisi, dict) else time.time()
                        tp_fiyat = sistem_bilgisi.get("tp_fiyat", giris_fiyati) if isinstance(sistem_bilgisi, dict) else giris_fiyati
                        
                        islem_karli_mi = False
                        cikis_fiyati = giris_fiyati
                        try:
                            ticker = exchange.fetch_ticker(eski_sym)
                            cikis_fiyati = float(ticker['last'])
                            if yon == "LONG": 
                                islem_karli_mi = cikis_fiyati >= tp_fiyat or cikis_fiyati > giris_fiyati
                            else: 
                                islem_karli_mi = cikis_fiyati <= tp_fiyat or cikis_fiyati < giris_fiyati
                        except Exception:
                            islem_karli_mi = True

                        gecen_sure_saniye = time.time() - giris_zamani
                        
                        COIN_COOLDOWN_SURELERI[eski_sym] = time.time() + COIN_COOLDOWN_SURESI

                        fark_oran = ((cikis_fiyati - giris_fiyati) / giris_fiyati) * 100 if yon == "LONG" else ((giris_fiyati - cikis_fiyati) / giris_fiyati) * 100
                        tahmini_roe = fark_oran * KALDIRAC

                        with state_lock:
                            bas_sayi = int(ANALitik_HAFIZA.get("basarili_islem_sayisi", 0))
                            basarisiz_sayi = int(ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0))
                            
                            if islem_karli_mi:
                                bas_sayi += 1
                                sonuc_mesaj_tipi = f"✅ *İŞLEM KÂRLA KAPANDI (TP)*\n📌 `{eski_sym}` | Yön: `{yon}`\n🎯 Giriş: `{giris_fiyati}` | Çıkış: `{cikis_fiyati}`\n💰 Kâr ROE: `%{tahmini_roe:+.2f}`"
                            else:
                                basarisiz_sayi += 1
                                if gecen_sure_saniye < ANORMAL_SL_SURESI_SINIRI:
                                    GENEL_COOLDOWN_BITIS = float(time.time() + GENEL_DINLENME_SURESI_SANIYE)
                                    sonuc_mesaj_tipi = f"❌ *ANORMAL HIZLI SL ({int(gecen_sure_saniye)} sn)*\n📌 `{eski_sym}` | Zarar ROE: `%{tahmini_roe:+.2f}`\n🛡️ *Piyasa Şoku Nedeniyle Tüm Bot 2 Saat Dinlemeye Aldı!*"
                                else:
                                    sonuc_mesaj_tipi = f"❌ *İŞLEM ZARARLA KAPANDI (SL)*\n📌 `{eski_sym}` | Yön: `{yon}`\n🛑 Giriş: `{giris_fiyati}` | Çıkış: `{cikis_fiyati}`\n📉 Zarar ROE: `%{tahmini_roe:+.2f}`"
                                
                            ANALitik_HAFIZA["basarili_islem_sayisi"] = bas_sayi
                            ANALitik_HAFIZA["basarisiz_islem_sayisi"] = basarisiz_sayi
                            
                            if eski_sym in AKTIF_GRID_SISTEMLERI: 
                                del AKTIF_GRID_SISTEMLERI[eski_sym]
                                
                        hafizayi_kaydet()
                        telegram_mesaj_gonder(sonuc_mesaj_tipi)
                        print(f"📢 [BİLDİRİM] Kapanış Telegram'a gönderildi: {eski_sym}", flush=True)
            except Exception as e: 
                print(f"⚠️ Kapanış kontrol hatası: {e}", flush=True)

            if time.time() < GENEL_COOLDOWN_BITIS:
                print(f"⏳ [KORUMA] Bot genel dinlenme modunda. Kalan: {int((GENEL_COOLDOWN_BITIS - time.time()) // 60)} dk", flush=True)
                time.sleep(30)
                continue

            if len(aktif_borsa_map) >= MAKSIMUM_TOPLAM_POZISYON:
                print(f"🔒 [LİMİT] Maksimum pozisyon sınırına ({MAKSIMUM_TOPLAM_POZISYON}) ulaşıldı. Yeni işlem aranmıyor.", flush=True)
                time.sleep(10)
                continue

            taranan_sinyaller = []

            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU: break

                if symbol in aktif_semboller_listesi: continue
                if symbol in COIN_COOLDOWN_SURELERI and time.time() < COIN_COOLDOWN_SURELERI[symbol]: continue

                try:
                    ticker = exchange.fetch_ticker(symbol)
                    anlik_fiyat = float(ticker['last'])
                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    rsi = ta.momentum.rsi(df['close'], window=14).iloc[-1]

                    if piyasa_rejimi == "TREND":
                        islem_yonu = btc_yonu
                        mod_adi = f"TREND MODU (BTC: {btc_yonu})"
                    else:
                        if rsi < 38: ham_yon = "LONG"
                        elif rsi > 62: ham_yon = "SHORT"
                        else: continue
                        
                        islem_yonu = "SHORT" if ham_yon == "LONG" else "LONG"
                        mod_adi = "TESTERE MODU (Ters İşlem)"

                    taranan_sinyaller.append({
                        "symbol": symbol, "yon": islem_yonu, "rsi": rsi, "fiyat": anlik_fiyat, "df": df, "mod": mod_adi
                    })
                    print(f"🎯 [SİNYAL BULUNDU] {symbol} | Yön: {islem_yonu} | RSI: {rsi:.1f}", flush=True)
                except Exception as e:
                    print(f"⚠️ {symbol} taranırken hata: {e}", flush=True)
                    continue

            for sinyal in taranan_sinyaller:
                if not BOT_CALISIYOR_MU: break
                if len(aktif_borsa_map) >= MAKSIMUM_TOPLAM_POZISYON: break
                if sinyal["symbol"] in aktif_semboller_listesi: continue

                try:
                    bakiye_bilgisi = exchange.fetch_balance()
                    toplam_bakiye = float(bakiye_bilgisi['total'].get('USDT', 0))
                    serbest_bakiye = float(bakiye_bilgisi.get('free', {}).get('USDT', 0) or 0)

                    exchange.set_leverage(KALDIRAC, sinyal["symbol"])
                    market = exchange.market(sinyal["symbol"])
                    
                    kullanilacak_tutar = min(toplam_bakiye * 0.4, serbest_bakiye)
                    if kullanilacak_tutar < 1.0: continue

                    giris_fiyati = sinyal["fiyat"]
                    tp_fiyat, sl_fiyat, kapat_yon, hedef_roe = akilli_seviye_hesapla(
                        giris_fiyati, sinyal["yon"], sinyal["df"]
                    )

                    miktar = float(exchange.amount_to_precision(
                        sinyal["symbol"], 
                        max((kullanilacak_tutar * KALDIRAC) / giris_fiyati / float(market.get('contractSize', 1.0)), 
                        float(market['limits']['amount']['min'] or 1.0))
                    ))
                    
                    islem_yonu = 'buy' if sinyal["yon"] == 'LONG' else 'sell'
                    
                    print(f"🚀 [İŞLEM AÇILIYOR] {sinyal['symbol']} {sinyal['yon']} | Miktar: {miktar}", flush=True)
                    exchange.create_order(sinyal["symbol"], 'market', islem_yonu, miktar)
                    time.sleep(0.5)
                    try:
                        exchange.create_order(sinyal["symbol"], 'limit', kapat_yon, miktar, tp_fiyat, {'reduceOnly': True})
                        exchange.create_order(sinyal["symbol"], 'stop', kapat_yon, miktar, sl_fiyat, {'stopPrice': sl_fiyat, 'reduceOnly': True})
                    except Exception as e:
                        print(f"⚠️ TP/SL emirleri girilirken hata: {e}", flush=True)

                    with state_lock:
                        AKTIF_GRID_SISTEMLERI[sinyal["symbol"]] = {
                            "giris_fiyati": giris_fiyati, 
                            "yon": sinyal["yon"],
                            "tp_fiyat": tp_fiyat,
                            "sl_fiyat": sl_fiyat,
                            "giris_rsi": float(sinyal["rsi"]), 
                            "giris_zamani": time.time()
                        }
                    hafizayi_kaydet()
                    
                    telegram_mesaj_gonder(
                        f"🎯 *İŞLEM GİRİŞİ ({sinyal['mod']} - 5x)*\n"
                        f"📌 `{sinyal['symbol']}` | Yön: `{sinyal['yon']}`\n"
                        f"🎯 Giriş: `{giris_fiyati}`\n"
                        f"💰 Hedef TP: `{tp_fiyat}` (Hedef ROE: `%{hedef_roe:.1f}`)\n"
                        f"🛑 Stop-Loss: `{sl_fiyat}`"
                    )
                    break
                except Exception as e:
                    print(f"⚠️ İşlem açılış hatası: {e}", flush=True)
                    pass

        except Exception as e:
            print(f"⚠️ Ana döngü genel hata: {e}", flush=True)
        
        time.sleep(5)

async def main():
    web_thread = threading.Thread(target=run_web, daemon=True)
    web_thread.start()

    app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    # Çakışmaları önlemek için eski webhook/polling kuyruğunu tamamen temizle
    try:
        requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook?drop_pending_updates=True", timeout=5)
        print("🧹 [TELEGRAM] Eski webhook kalıntıları temizlendi.", flush=True)
    except Exception: pass

    app_tg.add_handler(CommandHandler("durum", durum_komutu))
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))
    
    await app_tg.initialize()
    await app_tg.start()
    await app_tg.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    print("🤖 [TELEGRAM] Bot polling dinlemeye başladı.", flush=True)

    tarayici_thread = threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True)
    tarayici_thread.start()

    stop_event = asyncio.Event()
    await stop_event.wait()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
