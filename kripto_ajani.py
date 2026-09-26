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

# ==================== FLASK WEB SUNUCUSU (Render Port Desteği İçin) ====================
app = Flask(__name__)

@app.route("/")
def home():
    return "Kripto Bot Aktif ve Çalışıyor!", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# ==================== ORTAM DEĞİŞKENLERİ VE GÜVENLİK ====================
if os.path.exists('/etc/secrets/.env'):
    load_dotenv('/etc/secrets/.env', override=True)
else:
    load_dotenv(override=True)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌ KRİTİK HATA: SUPABASE_URL veya SUPABASE_KEY tanımlı değil!", flush=True)
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

exchange = ccxt.gate({
    'apiKey': os.environ.get("GATE_API_KEY", "").strip(),
    'secret': os.environ.get("GATE_SECRET", "").strip(),
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
SON_BTC_YONU = "YATAY (Testere)"

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
    try:
        ohlcv_btc = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='1h', limit=40)
        df_btc = pd.DataFrame(ohlcv_btc, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        adx_1h = ta.trend.ADXIndicator(df_btc['high'], df_btc['low'], df_btc['close'], window=14).adx().iloc[-1]
        
        indicator_bb = ta.volatility.BollingerBands(close=df_btc['close'], window=20, window_dev=2)
        bb_high = indicator_bb.bollinger_hband().iloc[-1]
        bb_low = indicator_bb.bollinger_lband().iloc[-1]
        bb_mid = indicator_bb.bollinger_mavg().iloc[-1]
        bb_bandwidth = (bb_high - bb_low) / bb_mid
        
        ema9 = ta.trend.ema_indicator(df_btc['close'], window=9).iloc[-1]
        ema21 = ta.trend.ema_indicator(df_btc['close'], window=21).iloc[-1]
        fark_yuzdesi = (abs(ema9 - ema21) / ema21) * 100
        
        if adx_1h < 35.0 or bb_bandwidth < 0.04 or fark_yuzdesi < 0.3:
            rejim = "YATAY"
            trend_yonu = "YATAY (Testere)"
        else:
            rejim = "TREND"
            trend_yonu = "LONG" if ema9 > ema21 else "SHORT"
            SON_BTC_YONU = trend_yonu
            
        print(f"📊 [BTC REJİM ANALİZİ] Rejim: {rejim} | BTC Yönü/Durumu: {trend_yonu} | ADX: {adx_1h:.1f}", flush=True)
        return rejim, trend_yonu
    except Exception as e:
        print(f"⚠️ [BTC REJİM HATA] {e}", flush=True)
        return "YATAY", "YATAY (Testere)"

def emir_defteri_duvar_analizi(symbol, anlik_fiyat, yon, df):
    try:
        order_book = exchange.fetch_order_book(symbol, limit=25)
        bids = order_book.get('bids', []) 
        asks = order_book.get('asks', []) 
        
        atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range().iloc[-1]

        if not bids or not asks:
            raise Exception("Order book boş döndü")

        bid_miktarlari = [b[1] for b in bids]
        ask_miktarlari = [a[1] for a in asks]
        
        ortalama_bid = sum(bid_miktarlari) / len(bid_miktarlari) if bid_miktarlari else 1
        ortalama_ask = sum(ask_miktarlari) / len(ask_miktarlari) if ask_miktarlari else 1

        en_iyi_alis_duvari = max(bids, key=lambda x: x[1]) if bids else [anlik_fiyat - atr, 0]
        en_iyi_satis_duvari = max(asks, key=lambda x: x[1]) if asks else [anlik_fiyat + atr, 0]

        duvar_bilgisi = "ATR Yedekli"

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

            sl_fiyat = giris_fiyati + (atr * 1.3)
            kapat_yon = 'buy'

        hedef_roe = abs((tp_fiyat - giris_fiyati) / giris_fiyati) * 100 * KALDIRAC
        print(f"🧱 [DUVAR ANALİZİ] {symbol} | {yon} | Strateji: {duvar_bilgisi} | Giriş: {giris_fiyati:.4f} | TP: {tp_fiyat:.4f} | SL: {sl_fiyat:.4f}", flush=True)
        return float(tp_fiyat), float(sl_fiyat), kapat_yon, float(hedef_roe), float(giris_fiyati)

    except Exception as e:
        print(f"⚠️ Duvar analizi hata ({symbol}), ATR bazlı klasik hesaplamaya geçiliyor: {e}", flush=True)
        atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range().iloc[-1]
        if yon == 'LONG':
            tp_fiyat = anlik_fiyat + (atr * 1.8)
            sl_fiyat = anlik_fiyat - (atr * 1.3)
            kapat_yon = 'sell'
        else:
            tp_fiyat = anlik_fiyat - (atr * 1.8)
            sl_fiyat = anlik_fiyat + (atr * 1.3)
            kapat_yon = 'buy'
        hedef_roe = abs((tp_fiyat - anlik_fiyat) / anlik_fiyat) * 100 * KALDIRAC
        return float(tp_fiyat), float(sl_fiyat), kapat_yon, float(hedef_roe), float(anlik_fiyat)

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
        with state_lock:
            aktif_sistemler_kopya = dict(AKTIF_GRID_SISTEMLERI)

        for p in borsa_poslari:
            sym = p['symbol']
            yon = str(p.get('side', '')).upper() or "LONG"
            giris = float(p.get('entryPrice', 0))
            kaldirac_val = int(p.get('leverage', KALDIRAC))
            ticker_data = await asyncio.to_thread(exchange.fetch_ticker, sym)
            guncel_fiyat = float(ticker_data['last'])
            fark = (guncel_fiyat - giris) / giris if yon == "LONG" else (giris - guncel_fiyat) / giris
            roe = fark * 100 * kaldirac_val
            
            kayit_bilgi = aktif_sistemler_kopya.get(sym, {})
            tp_fiyat = kayit_bilgi.get("tp_fiyat", 0)
            if tp_fiyat > 0:
                hedef_fark = abs((tp_fiyat - giris) / giris) * 100 * kaldirac_val
                pos_detaylari += f"\n• `{sym}` | {yon} | Giriş: `{giris}`\n  Anlık ROE: `%{roe:+.2f}` | Hedef TP: `{tp_fiyat}` (`%{hedef_fark:.1f}`)"
            else:
                pos_detaylari += f"\n• `{sym}` | {yon} | Giriş: `{giris}`\n  Anlık ROE: `%{roe:+.2f}`"

        mesaj = (
            f"📊 **BOT DURUM RAPORU (Emir Defteri Duvar Analizli - 5x)**\n\n"
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
    await update.message.reply_text("🟢 Duvar Analizli Kripto Botu (5x) aktif edildi!")

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
        
        with state_lock:
            AKTIF_GRID_SISTEMLERI.clear()
        hafizayi_kaydet()
        await update.message.reply_text("✅ Tüm pozisyonlar ve kayıtlar temizlendi.")
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

def otomatik_arkaplan_tarayici():
    print("🚀 [BAŞLANGIÇ] Emir Defteri Duvar Analizli Bot Devrede ve Canlı Log Modunda...", flush=True)
    try:
        exchange.load_markets()
    except Exception: pass
    
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                print("⏸️ [DURDURULDU] Bot pasif durumda, bekleniyor...", flush=True)
                time.sleep(5)
                continue

            print("\n--------------------------------------------------", flush=True)
            print("🔄 [YENİ DÖNGÜ] Piyasa ve coinler duvar analiziyle taranıyor...", flush=True)
            piyasa_rejimi, btc_yonu = piyasa_rejimini_tespit_et()

            try:
                borsa_gercek_pozlar = exchange.fetch_positions()
                acik_semboller = [p['symbol'] for p in borsa_gercek_pozlar if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
                
                with state_lock:
                    hafiza_keys = list(AKTIF_GRID_SISTEMLERI.keys())
                    for h_sym in hafiza_keys:
                        if h_sym not in acik_semboller:
                            del AKTIF_GRID_SISTEMLERI[h_sym]
                            hafizayi_kaydet()
            except Exception as sync_err:
                print(f"⚠️ Senkronizasyon kontrolü sırasında hata: {sync_err}", flush=True)

            with state_lock:
                aktif_keys = list(AKTIF_GRID_SISTEMLERI.keys())

            for eski_sym in aktif_keys:
                try:
                    kayit = AKTIF_GRID_SISTEMLERI.get(eski_sym)
                    if not kayit: continue
                    
                    durum = kayit.get("durum")

                    if durum == "DOLDURULDU":
                        raw_positions = exchange.fetch_positions([eski_sym])
                        pozisyon_var_mi = False
                        for p in raw_positions:
                            if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0:
                                pozisyon_var_mi = True
                                break

                        if not pozisyon_var_mi:
                            giris_fiyati = kayit.get("giris_fiyati", 0)
                            yon = kayit.get("yon", "LONG")
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
                except Exception as e:
                    print(f"⚠️ Takip hata ({eski_sym}): {e}", flush=True)

            toplam_aktif_islem_sayisi = len(AKTIF_GRID_SISTEMLERI)
            if toplam_aktif_islem_sayisi >= MAKSIMUM_TOPLAM_POZISYON:
                print(f"📌 [LİMİT DOLU] Maksimum pozisyon sayısına ulaşıldı ({toplam_aktif_islem_sayisi}/{MAKSIMUM_TOPLAM_POZISYON}). Pozisyonlar izleniyor...", flush=True)
                time.sleep(5)
                continue

            taranan_sinyaller = []

            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU: break
                if symbol in AKTIF_GRID_SISTEMLERI:
                    print(f"🔒 [{symbol}] Zaten açık pozisyon var, pas geçiliyor.", flush=True)
                    continue
                
                with state_lock:
                    cooldown_veri = COIN_COOLDOWNLAR.get(symbol)
                    if cooldown_veri:
                        zaman_kontrol = cooldown_veri.get("zaman", 0) if isinstance(cooldown_veri, dict) else float(cooldown_veri)
                        kalan_sure = int(zaman_kontrol - time.time())
                        if kalan_sure > 0:
                            print(f"⏳ [{symbol}] Cooldown süresinde (Kalan: {kalan_sure} sn), incelenmiyor.", flush=True)
                            continue

                try:
                    ticker = exchange.fetch_ticker(symbol)
                    anlik_fiyat = float(ticker['last'])
                    
                    open_orders = exchange.fetch_open_orders(symbol)
                    if len(open_orders) > 0:
                        try: exchange.cancel_all_orders(symbol)
                        except Exception: pass

                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    rsi = ta.momentum.rsi(df['close'], window=14).iloc[-1]

                    indicator_bb = ta.volatility.BollingerBands(close=df['close'], window=20, window_dev=2)
                    bb_mid = indicator_bb.bollinger_mavg().iloc[-1]

                    if piyasa_rejimi == "YATAY":
                        if anlik_fiyat < bb_mid:
                            islem_yonu = "LONG"
                        else:
                            islem_yonu = "SHORT"
                        mod_adi = "ESNEK YATAY BANT"
                    else:
                        if btc_yonu == "LONG":
                            islem_yonu = "LONG"
                        else:
                            islem_yonu = "SHORT"
                        mod_adi = "NORMAL TREND"

                    print(f"🔍 [COİN İNCELENDİ] {symbol} | Fiyat: {anlik_fiyat} | RSI: {rsi:.2f} | Karar: {islem_yonu} ({mod_adi})", flush=True)

                    taranan_sinyaller.append({
                        "symbol": symbol, "yon": islem_yonu, "rsi": rsi, "fiyat": anlik_fiyat, "df": df, "mod": mod_adi
                    })
                except Exception as e:
                    print(f"⚠️ [TARAMA HATA] {symbol}: {e}", flush=True)
                    continue

            for sinyal in taranan_sinyaller:
                if not BOT_CALISIYOR_MU: break
                if sinyal["symbol"] in AKTIF_GRID_SISTEMLERI: continue
                if len(AKTIF_GRID_SISTEMLERI) >= MAKSIMUM_TOPLAM_POZISYON: break

                try:
                    bakiye_bilgisi = exchange.fetch_balance()
                    toplam_bakiye = float(bakiye_bilgisi['total'].get('USDT', 0))
                    serbest_bakiye = float(bakiye_bilgisi.get('free', {}).get('USDT', 0) or 0)

                    exchange.set_leverage(KALDIRAC, sinyal["symbol"])
                    market = exchange.market(sinyal["symbol"])
                    
                    kullanilacak_tutar = min(toplam_bakiye * 0.4, serbest_bakiye)
                    if kullanilacak_tutar < 1.0: 
                        print(f"⚠️ [BAKİYE YETERSİZ] Kullanılabilir bakiye çok düşük: {kullanilacak_tutar} USDT", flush=True)
                        continue

                    tp_fiyat, sl_fiyat, kapat_yon, hedef_roe, ideal_giris = emir_defteri_duvar_analizi(
                        sinyal["symbol"], sinyal["fiyat"], sinyal["yon"], sinyal["df"]
                    )

                    miktar = float(exchange.amount_to_precision(
                        sinyal["symbol"], 
                        max((kullanilacak_tutar * KALDIRAC) / ideal_giris / float(market.get('contractSize', 1.0)), 
                        float(market['limits']['amount']['min'] or 1.0))
                    ))
                    
                    emir_yonu = 'buy' if sinyal["yon"] == 'LONG' else 'sell'
                    
                    print(f"🚀 [EMİR İLETİLİYOR] {sinyal['symbol']} için {sinyal['yon']} piyasa emri gönderiliyor...", flush=True)
                    giris_emir = exchange.create_order(sinyal["symbol"], 'market', emir_yonu, miktar)
                    gerceklesen_giris = float(giris_emir.get('average', 0) or giris_emir.get('price', 0) or ideal_giris)

                    exchange.create_order(sinyal["symbol"], 'limit', kapat_yon, miktar, tp_fiyat, {'reduceOnly': True})
                    exchange.create_order(sinyal["symbol"], 'stop', kapat_yon, miktar, sl_fiyat, {'stopPrice': sl_fiyat, 'reduceOnly': True})

                    with state_lock:
                        AKTIF_GRID_SISTEMLERI[sinyal["symbol"]] = {
                            "order_id": giris_emir['id'],
                            "durum": "DOLDURULDU",
                            "giris_fiyati": gerceklesen_giris,
                            "tp_fiyat": tp_fiyat,
                            "sl_fiyat": sl_fiyat,
                            "kapat_yon": kapat_yon,
                            "miktar": miktar,
                            "yon": sinyal["yon"],
                            "giris_rsi": float(sinyal["rsi"]), 
                            "giris_zamani": time.time()
                        }
                    hafizayi_kaydet()
                    
                    print(f"✅ [İŞLEM BAŞARILI] {sinyal['symbol']} {sinyal['yon']} açıldı! Giriş: {gerceklesen_giris}", flush=True)
                    telegram_mesaj_gonder(
                        f"🚀 *DUVAR ANALİZLİ İŞLEM AÇILDI ({sinyal['mod']} - 5x)*\n"
                        f"📌 `{sinyal['symbol']}` | Yön: `{sinyal['yon']}`\n"
                        f"📍 Giriş Fiyatı: `{gerceklesen_giris}`\n"
                        f"💰 Hedef TP: `{tp_fiyat}` (ROE: `%{hedef_roe:.1f}`)\n"
                        f"🛑 Stop-Loss: `{sl_fiyat}`"
                    )
                    break
                except Exception as e:
                    print(f"⚠️ Emir oluşturulurken hata: {e}", flush=True)

        except Exception as e:
            print(f"⚠️ Ana döngü hata: {e}", flush=True)
        
        print("💤 Döngü tamamlandı, 5 saniye sonra tekrar taranacak...", flush=True)
        time.sleep(5)

async def main():
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook?drop_pending_updates=True"
        await asyncio.to_thread(requests.get, url, timeout=10)
        await asyncio.sleep(2)
    except Exception as e:
        print(f"⚠️ Webhook silinirken hata: {e}", flush=True)

    app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app_tg.add_handler(CommandHandler("durum", durum_komutu))
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))
    
    await app_tg.initialize()
    await app_tg.start()
    
    while True:
        try:
            await app_tg.updater.start_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
            break
        except Exception as e:
            print(f"⚠️ Polling çakışması: {e}. Tekrar deneniyor...", flush=True)
            await asyncio.sleep(5)

    tarayici_text_thread = threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True)
    tarayici_text_thread.start()

    stop_event = asyncio.Event()
    await stop_event.wait()

if __name__ == '__main__':
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    print("🌐 Flask web sunucusu arka planda başlatıldı...", flush=True)

    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
