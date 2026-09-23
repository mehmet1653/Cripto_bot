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
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler
from sklearn.ensemble import RandomForestClassifier
from supabase import create_client, Client

# ==================== AYARLAR VE ANAHTARLAR ====================
TELEGRAM_TOKEN = "8870934003:AAGOzmO_VwYnj0Wz2hehI176rKiOkEaV0b0"
CHAT_ID = "6929517567"

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://rllpcylzhptqwzmzehnv.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "Sb_secret_ln9y67Ep_zCtOQ9Q2NE8KQ_nf0gKkmO")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

exchange = ccxt.gate({
    'apiKey': '82cca880898a88d1a31e86d8eb474c57',
    'secret': '1ac479b9df5e6f2e89560b0d238a250694719b6fcae20da00ebc54ad6aeb8898',
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

GLOBAL_COOLDOWN_BITIS = 0.0
SON_BTC_YONU = "LONG"

def hafizayi_yukle():
    print("💾 Hafıza Supabase'den yükleniyor...", flush=True)
    try:
        response = supabase.table("bot_hafiza").select("*").eq("id", 1).execute()
        if response.data and len(response.data) > 0:
            veri = response.data[0]
            print("💾 Hafıza başarıyla yüklendi.", flush=True)
            return {
                "aktif_sistemler": veri.get("aktif_sistemler", {}),
                "analitik": veri.get("analitik", {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0, "egitim_verileri": []}),
                "cooldownlar": veri.get("cooldownlar", {})
            }
    except Exception as e:
        print(f"⚠️ Hafıza yüklenirken hata: {e}", flush=True)
    
    return {
        "aktif_sistemler": {},
        "analitik": {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0, "egitim_verileri": []},
        "cooldownlar": {}
    }

def hafizayi_kaydet():
    with state_lock:
        try:
            payload_analitik = {
                "basarili_islem_sayisi": int(ANALitik_HAFIZA.get("basarili_islem_sayisi", 0)),
                "basarisiz_islem_sayisi": int(ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0)),
                "egitim_verileri": ANALitik_HAFIZA.get("egitim_verileri", [])
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
            print(f"⚠️ Hafıza kaydedilemedi: {e}", flush=True)

kalici_veri = hafizayi_yukle()
AKTIF_GRID_SISTEMLERI = kalici_veri.get("aktif_sistemler", {})
ANALitik_HAFIZA = kalici_veri.get("analitik", {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0, "egitim_verileri": []})
COIN_COOLDOWNLAR = kalici_veri.get("cooldownlar", {})

MAKSIMUM_TOPLAM_POZISYON = 2
COOLDOWN_SURESI_SANIYE = 10 * 60

def piyasa_rejimini_tespit_et():
    global SON_BTC_YONU
    print("🌐 [PİYASA] BTC rejimi ve yönü analiz ediliyor...", flush=True)
    try:
        ohlcv_btc = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='1h', limit=30)
        df_btc = pd.DataFrame(ohlcv_btc, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        adx_1h = ta.trend.ADXIndicator(df_btc['high'], df_btc['low'], df_btc['close'], window=14).adx().iloc[-1]
        
        ema9 = ta.trend.ema_indicator(df_btc['close'], window=9).iloc[-1]
        ema21 = ta.trend.ema_indicator(df_btc['close'], window=21).iloc[-1]
        
        fark_yuzdesi = (abs(ema9 - ema21) / ema21) * 100
        esik_degeri = 0.1
        
        if fark_yuzdesi > esik_degeri:
            trend_yonu = "LONG" if ema9 > ema21 else "SHORT"
            SON_BTC_YONU = trend_yonu
        else:
            trend_yonu = SON_BTC_YONU
            
        # ADX eşiği: 25 altı YATAY (Testere), 25 üstü TREND kabul edilir
        if adx_1h < 25.0:
            rejim = "YATAY"
        else:
            rejim = "TREND"
            
        print(f"🌐 [PİYASA SONUÇ] Rejim: {rejim} | BTC Yön: {trend_yonu} (Fark: %{fark_yuzdesi:.3f}) | ADX: {adx_1h:.2f}", flush=True)
        return rejim, trend_yonu
    except Exception as e:
        print(f"⚠️ [PİYASA HATA] Rejim tespit edilemedi: {e}. Varsayılan YATAY/LONG", flush=True)
        return "YATAY", SON_BTC_YONU

def emir_defteri_ve_seviye_analizi(symbol, anlik_fiyat, ticker_data):
    try:
        high_24h = float(ticker_data.get('high') or anlik_fiyat * 1.02)
        low_24h = float(ticker_data.get('low') or anlik_fiyat * 0.98)
        
        tepeye_yakin_mi = anlik_fiyat >= (high_24h * 0.994)
        dipe_yakin_mi = anlik_fiyat <= (low_24h * 1.006)

        order_book = exchange.fetch_order_book(symbol, limit=20)
        bids = order_book.get('bids', [])
        asks = order_book.get('asks', [])

        toplam_alis_hacmi = sum([b[1] for b in bids]) if bids else 1.0
        toplam_satis_hacmi = sum([a[1] for a in asks]) if asks else 1.0
        toplam_hacim = toplam_alis_hacmi + toplam_satis_hacmi

        alis_orani = (toplam_alis_hacmi / toplam_hacim) * 100
        satis_orani = (toplam_satis_hacmi / toplam_hacim) * 100
        
        return {
            "tepeye_yakin": tepeye_yakin_mi,
            "dipe_yakin": dipe_yakin_mi,
            "alis_orani": alis_orani,
            "satis_orani": satis_orani
        }
    except Exception as e:
        return {"tepeye_yakin": False, "dipe_yakin": False, "alis_orani": 50.0, "satis_orani": 50.0}

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

def makine_ogrenmesi_filtresi(df, rsi):
    try:
        if len(df) < 20: return True
        X = []
        y = []
        closes = df['close'].values
        highs = df['high'].values
        lows = df['low'].values
        
        for i in range(14, len(df) - 1):
            sub_close = closes[:i+1]
            sub_high = highs[:i+1]
            sub_low = lows[:i+1]
            sub_rsi = ta.momentum.rsi(pd.Series(sub_close), window=14).iloc[-1]
            sub_atr = ta.volatility.AverageTrueRange(pd.Series(sub_high), pd.Series(sub_low), pd.Series(sub_close), window=14).average_true_range().iloc[-1]
            
            future_return = (closes[i+1] - closes[i]) / closes[i]
            label = 1 if future_return > 0 else 0
            
            X.append([sub_rsi, sub_atr])
            y.append(label)
            
        if len(X) < 10: return True
        clf = RandomForestClassifier(n_estimators=20, random_state=42, max_depth=3)
        clf.fit(X, y)
        
        current_atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range().iloc[-1]
        pred = clf.predict([[rsi, current_atr]])[0]
        return bool(pred == 1)
    except Exception:
        return True

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
            f"📊 **BOT DURUM RAPORU (Hibrit Mod - 5x)**\n\n"
            f"🌐 Piyasa Rejimi: `{rejim}` (BTC Yön: `{btc_yon}`)\n"
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
    await update.message.reply_text("🟢 Hibrit Bot (5x) aktif edildi!")

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
    print("🚀 [BAŞLANGIÇ] Hibrit Bot Aktif (Testerede Tersi, Trendde Normal İşlem)...", flush=True)
    try:
        exchange.load_markets()
    except Exception: pass
    
    while True:
        try:
            if not BOT_CALISIYOR_MU:
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
            except Exception:
                aktif_borsa_map = {}
                aktif_semboller_listesi = []

            try:
                anlik_aktif_semboller = [p['symbol'] for p in raw_positions if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
                for eski_sym in list(AKTIF_GRID_SISTEMLERI.keys()):
                    if eski_sym not in anlik_aktif_semboller:
                        sistem_bilgisi = AKTIF_GRID_SISTEMLERI[eski_sym]
                        giris_fiyati = sistem_bilgisi.get("giris_fiyati", 0) if isinstance(sistem_bilgisi, dict) else 0
                        yon = sistem_bilgisi.get("yon", "LONG") if isinstance(sistem_bilgisi, dict) else "LONG"
                        
                        islem_karli_mi = False
                        try:
                            ticker = exchange.fetch_ticker(eski_sym)
                            cikis_fiyati = float(ticker['last'])
                            if yon == "LONG": islem_karli_mi = cikis_fiyati > giris_fiyati
                            else: islem_karli_mi = cikis_fiyati < giris_fiyati
                        except Exception:
                            islem_karli_mi = True

                        with state_lock:
                            bas_sayi = int(ANALitik_HAFIZA.get("basarili_islem_sayisi", 0))
                            basarisiz_sayi = int(ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0))
                            
                            if islem_karli_mi:
                                bas_sayi += 1
                                sonuc_mesaj_tipi = "✅ *İŞLEM KÂRLA KAPANDI (TP)*"
                            else:
                                basarisiz_sayi += 1
                                sonuc_mesaj_tipi = "❌ *İŞLEM ZARARLA KAPANDI (SL)*"
                                
                            ANALitik_HAFIZA["basarili_islem_sayisi"] = bas_sayi
                            ANALitik_HAFIZA["basarisiz_islem_sayisi"] = basarisiz_sayi
                            COIN_COOLDOWNLAR[eski_sym] = {"zaman": float(time.time() + COOLDOWN_SURESI_SANIYE), "son_yon": yon}
                            if eski_sym in AKTIF_GRID_SISTEMLERI: del AKTIF_GRID_SISTEMLERI[eski_sym]
                                
                        hafizayi_kaydet()
                        telegram_mesaj_gonder(f"{sonuc_mesaj_tipi}\n📌 `{eski_sym}`")
            except Exception: pass

            taranan_sinyaller = []

            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU: break
                
                with state_lock:
                    cooldown_veri = COIN_COOLDOWNLAR.get(symbol)
                    if cooldown_veri:
                        zaman_kontrol = cooldown_veri.get("zaman", 0) if isinstance(cooldown_veri, dict) else float(cooldown_veri)
                        if zaman_kontrol - time.time() > 0: continue

                try:
                    ticker = exchange.fetch_ticker(symbol)
                    anlik_fiyat = float(ticker['last'])
                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    rsi = ta.momentum.rsi(df['close'], window=14).iloc[-1]

                    emir_analizi = emir_defteri_ve_seviye_analizi(symbol, anlik_fiyat, ticker)

                    # HİBRİT MANTIK KARAR MEKANİZMASI
                    if piyasa_rejimi == "YATAY":
                        # 1. TESTERE PİYASASI: TERS MANTIK (Contrarian)
                        if rsi < 35: ham_yon = "LONG"
                        elif rsi > 65: ham_yon = "SHORT"
                        else: continue
                        
                        islem_yonu = "SHORT" if ham_yon == "LONG" else "LONG"
                        mod_adi = "TERS MOD (Testere)"
                    else:
                        # 2. TREND PİYASASI: NORMAL MANTIK (Trend Following - BTC Yönüne Uygun)
                        if btc_yonu == "LONG" and rsi < 55:
                            islem_yonu = "LONG"
                        elif btc_yonu == "SHORT" and rsi > 45:
                            islem_yonu = "SHORT"
                        else:
                            continue
                        mod_adi = "NORMAL TREND MODU"

                    print(f"🔄 [{mod_adi}] {symbol} için karar verildi 👉 {islem_yonu} | Rejim: {piyasa_rejimi} (RSI: {rsi:.2f})", flush=True)

                    ml_onay = makine_ogrenmesi_filtresi(df, rsi)
                    if not ml_onay: continue

                    taranan_sinyaller.append({
                        "symbol": symbol, "yon": islem_yonu, "rsi": rsi, "fiyat": anlik_fiyat, "df": df, "mod": mod_adi
                    })
                except Exception: continue

            for sinyal in taranan_sinyaller:
                if not BOT_CALISIYOR_MU: break
                if sinyal["symbol"] in aktif_semboller_listesi: continue
                if len(aktif_borsa_map) >= MAKSIMUM_TOPLAM_POZISYON: break

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
                    
                    exchange.create_order(sinyal["symbol"], 'market', islem_yonu, miktar)
                    time.sleep(0.5)
                    try:
                        exchange.create_order(sinyal["symbol"], 'limit', kapat_yon, miktar, tp_fiyat, {'reduceOnly': True})
                        exchange.create_order(sinyal["symbol"], 'stop', kapat_yon, miktar, sl_fiyat, {'stopPrice': sl_fiyat, 'reduceOnly': True})
                    except Exception: pass

                    with state_lock:
                        AKTIF_GRID_SISTEMLERI[sinyal["symbol"]] = {
                            "giris_fiyati": giris_fiyati, "yon": sinyal["yon"],
                            "giris_rsi": float(sinyal["rsi"]), "giris_zamani": time.time()
                        }
                        aktif_semboller_listesi.append(sinyal["symbol"])
                    hafizayi_kaydet()
                    
                    telegram_mesaj_gonder(
                        f"🎯 *İŞLEM GİRİŞİ ({sinyal['mod']} - 5x)*\n"
                        f"📌 `{sinyal['symbol']}` | Yön: `{sinyal['yon']}`\n"
                        f"🎯 Giriş: `{giris_fiyati}`\n"
                        f"💰 Hedef TP: `{tp_fiyat}` (Hedef ROE: `%{hedef_roe:.1f}`)\n"
                        f"🛑 Stop-Loss: `{sl_fiyat}`"
                    )
                    break
                except Exception: pass

        except Exception: pass
        
        time.sleep(5)

async def main():
    app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    try:
        requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook?drop_pending_updates=True", timeout=5)
    except Exception: pass

    app_tg.add_handler(CommandHandler("durum", durum_komutu))
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))
    
    await app_tg.initialize()
    await app_tg.start()
    await app_tg.updater.start_polling(drop_pending_updates=True)

    tarayici_thread = threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True)
    tarayici_thread.start()

    stop_event = asyncio.Event()
    await stop_event.wait()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
