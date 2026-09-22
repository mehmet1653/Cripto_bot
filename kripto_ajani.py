import os
import time
import threading
import sys
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

os.environ['PYTHONUNBUFFERED'] = '1'
sys.stdout.reconfigure(line_buffering=True)

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
SON_ZARAR_ZAMANLARI = []
COIN_OI_TAKIP = {} 
ANLIK_PIYASA_MODU = "TESTERE"

# ==================== SUPABASE HAFIZA ====================
def hafizayi_yukle():
    try:
        response = supabase.table("bot_hafiza").select("*").eq("id", 1).execute()
        if response.data and len(response.data) > 0:
            veri = response.data[0]
            print("💾 Hafıza Supabase'den başarıyla yüklendi.", flush=True)
            return {
                "aktif_sistemler": veri.get("aktif_sistemler", {}),
                "analitik": veri.get("analitik", {
                    "basarili_islem_sayisi": 0,
                    "basarisiz_islem_sayisi": 0,
                    "egitim_verileri": []
                }),
                "cooldownlar": veri.get("cooldownlar", {})
            }
    except Exception as e:
        print(f"⚠️ Hafıza yükleme hatası: {e}", flush=True)
        
    varsayilan = {
        "aktif_sistemler": {},
        "analitik": {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0, "egitim_verileri": []},
        "cooldownlar": {}
    }
    try:
        supabase.table("bot_hafiza").upsert({"id": 1, **varsayilan}).execute()
    except Exception as e:
        print(f"⚠️ Hafıza tablo hatası: {e}", flush=True)
    return varsayilan

def hafizayi_kaydet():
    with state_lock:
        try:
            payload_analitik = {
                "basarili_islem_sayisi": int(analitik_hafiza.get("basarili_islem_sayisi", 0)),
                "basarisiz_islem_sayisi": int(analitik_hafiza.get("basarisiz_islem_sayisi", 0)),
                "egitim_verileri": analitik_hafiza.get("egitim_verileri", [])
            }
            
            clean_cooldowns = {}
            for k, v in COIN_COOLDOWNLAR.items():
                if isinstance(v, dict):
                    clean_cooldowns[k] = {
                        "zaman": float(v.get("zaman", 0)),
                        "son_yon": str(v.get("son_yon", ""))
                    }
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
analitik_hafiza = kalici_veri.get("analitik", {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0, "egitim_verileri": []})
COIN_COOLDOWNLAR = kalici_veri.get("cooldownlar", {})

MAKSIMUM_TOPLAM_POZISYON = 3
COOLDOWN_SURESI_SANIYE = 30 * 60  
TESTERE_KISA_COOLDOWN = 10 * 60   

ai_model = RandomForestClassifier(n_estimators=100, max_depth=6, random_state=42)
ai_model_egitildi = False

def yapay_zekayi_egit_ve_guncelle():
    global ai_model, ai_model_egitildi
    with state_lock:
        veriler = list(analitik_hafiza.get("egitim_verileri", []))
    if len(veriler) < 20:
        ai_model_egitildi = False
        return
    try:
        X = [item[:6] for item in veriler]
        y = [item[6] for item in veriler]
        if len(set(y)) < 2: return
        ai_model.fit(np.array(X), np.array(y))
        ai_model_egitildi = True
    except Exception:
        ai_model_egitildi = False

def mum_ve_hacim_gecerlilik_kontrolu(df):
    try:
        son_mum = df.iloc[-1]
        govde = abs(son_mum['close'] - son_mum['open'])
        tum_boy = son_mum['high'] - son_mum['low']
        if tum_boy == 0: return True, 1.0
        govde_orani = govde / tum_boy
        vol_ort = df['volume'].rolling(window=20).mean().iloc[-1]
        anlik_vol = son_mum['volume']
        hacim_carpani = anlik_vol / vol_ort if vol_ort > 0 else 1.0
        return True, hacim_carpani
    except Exception:
        return True, 1.0

def atr_ve_volatilite_hesapla(df):
    try:
        atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range().iloc[-1]
        return float((atr / df['close'].iloc[-1]) * 100)
    except Exception:
        return 1.5

def btc_trend_kontrolu():
    global ANLIK_PIYASA_MODU
    try:
        ohlcv_btc_1h = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='1h', limit=50)
        df_btc_1h = pd.DataFrame(ohlcv_btc_1h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        ema9_1h = ta.trend.ema_indicator(df_btc_1h['close'], window=9).iloc[-1]
        ema21_1h = ta.trend.ema_indicator(df_btc_1h['close'], window=21).iloc[-1]
        adx_1h = ta.trend.ADXIndicator(df_btc_1h['high'], df_btc_1h['low'], df_btc_1h['close'], window=14).adx().iloc[-1]

        if adx_1h >= 28.0:
            ANLIK_PIYASA_MODU = "TREND"
        else:
            ANLIK_PIYASA_MODU = "TESTERE"

        if ema9_1h > ema21_1h:
            return "LONG"
        else:
            return "SHORT"
    except Exception:
        ANLIK_PIYASA_MODU = "TESTERE"
        return "LONG"

def acik_pozisyon_oi_kontrolu(symbol):
    try:
        oi_data = exchange.fetch_open_interest(symbol)
        current_oi = float(oi_data.get('openInterestAmount', 0) or oi_data.get('openInterest', 0) or 0)
        simdiki_zaman = time.time()
        if symbol in COIN_OI_TAKIP:
            onceki_veri = COIN_OI_TAKIP[symbol]
            eski_oi = onceki_veri["oi"]
            if eski_oi > 0:
                oi_degisim_yuzdesi = ((current_oi - eski_oi) / eski_oi) * 100
                COIN_OI_TAKIP[symbol] = {"oi": current_oi, "zaman": simdiki_zaman}
                return current_oi, oi_degisim_yuzdesi
        COIN_OI_TAKIP[symbol] = {"oi": current_oi, "zaman": simdiki_zaman}
        return current_oi, 0.0
    except Exception:
        return 0.0, 0.0

def telegram_mesaj_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": mesaj, "parse_mode": "Markdown"}, timeout=5)
    except Exception: pass

def pozisyonu_kapat(symbol, yon, miktar, sebep_mesaji, basarili=True, cezali_mi=False, ozel_cooldown=None):
    global GLOBAL_COOLDOWN_BITIS, SON_ZARAR_ZAMANLARI
    kapatma_yonu = 'sell' if yon == 'LONG' else 'buy'
    try:
        try: exchange.cancel_all_orders(symbol)
        except Exception: pass
        exchange.create_order(symbol, 'market', kapatma_yonu, miktar, None, {'reduceOnly': True})
    except Exception as e:
        print(f"⚠️ Kapatma hatası: {e}", flush=True)
        return

    with state_lock:
        bas_sayi = int(analitik_hafiza.get("basarili_islem_sayisi", 0))
        basarisiz_sayi = int(analitik_hafiza.get("basarisiz_islem_sayisi", 0))
        if basarili: 
            bas_sayi += 1
        else:
            basarisiz_sayi += 1
            if cezali_mi:
                simdiki_zaman = time.time()
                SON_ZARAR_ZAMANLARI = [t for t in SON_ZARAR_ZAMANLARI if simdiki_zaman - t < 1800]
                SON_ZARAR_ZAMANLARI.append(simdiki_zaman)
                if len(SON_ZARAR_ZAMANLARI) >= 4:
                    GLOBAL_COOLDOWN_BITIS = simdiki_zaman + (1 * 3600)
                    telegram_mesaj_gonder("🚨 *GENEL SİGORTA ATTI*\n⚠️ Bot ardışık zararlar nedeniyle 1 saat durduruldu!")

        analitik_hafiza["basarili_islem_sayisi"] = bas_sayi
        analitik_hafiza["basarisiz_islem_sayisi"] = basarisiz_sayi

        secilen_cooldown = ozel_cooldown if ozel_cooldown else COOLDOWN_SURESI_SANIYE
        COIN_COOLDOWNLAR[symbol] = {"zaman": float(time.time() + secilen_cooldown), "son_yon": yon}

        if symbol in AKTIF_GRID_SISTEMLERI: del AKTIF_GRID_SISTEMLERI[symbol]
            
    hafizayi_kaydet()
    if sebep_mesaji: telegram_mesaj_gonder(sebep_mesaji)

async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    try:
        balance = await asyncio.to_thread(exchange.fetch_balance)
        total = float(balance['total'].get('USDT', 0))
        borsa_poslari = [p for p in await asyncio.to_thread(exchange.fetch_positions) if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
        toplam_pnl = sum(float(p.get('unrealizedPnl', 0)) for p in borsa_poslari)
        
        basarili = int(analitik_hafiza.get("basarili_islem_sayisi", 0))
        basarisiz = int(analitik_hafiza.get("basarisiz_islem_sayisi", 0))
        toplam_islem = basarili + basarisiz
        basari_orani = (basarili / toplam_islem * 100) if toplam_islem > 0 else 0.0

        pos_detaylari = ""
        for p in borsa_poslari:
            sym = p['symbol']
            yon = str(p.get('side', '')).upper()
            if not yon: yon = "LONG" if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0 else "SHORT"
            giris = float(p.get('entryPrice', 0))
            kaldirac_val = int(p.get('leverage', KALDIRAC))
            ticker_data = await asyncio.to_thread(exchange.fetch_ticker, sym)
            guncel_fiyat = float(ticker_data['last'])
            fark = (guncel_fiyat - giris) / giris if yon == "LONG" else (giris - guncel_fiyat) / giris
            roe = fark * 100 * kaldirac_val
            pos_detaylari += f"\n• `{sym}` | {yon} | Giriş: `{giris}`\n  Anlık ROE: `%{roe:+.2f}`"

        mesaj = (
            f"📊 **BOT DURUM RAPORU**\n\n"
            f"⚙️ **Çalışma Modu: `{ANLIK_PIYASA_MODU} MODU`**\n"
            f"👑 Ana BTC Yönü: `{btc_trend_kontrolu()}`\n"
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
    global BOT_CALISIYOR_MU, GLOBAL_COOLDOWN_BITIS
    BOT_CALISIYOR_MU = True
    GLOBAL_COOLDOWN_BITIS = 0.0
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
                yon = str(pos.get('side', '')).upper()
                if not yon: yon = "LONG"
                pozisyonu_kapat(pos['symbol'], yon, kontrat, f"🛑 Manuel Kapatma", basarili=False, cezali_mi=True)
        try:
            for symbol in TAKIP_EDILENLER:
                try: exchange.cancel_all_orders(symbol)
                except Exception: pass
        except Exception: pass
        await update.message.reply_text("✅ Tüm pozisyonlar ve bekleyen emirler kapatıldı.")
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, GLOBAL_COOLDOWN_BITIS, ANLIK_PIYASA_MODU
    print("🚀 Bot Arka Plan Döngüsü Aktif (Piyasa Emri & Sıkı Limit Kontrollü)...", flush=True)
    try:
        exchange.load_markets()
        yapay_zekayi_egit_ve_guncelle()
    except Exception as e:
        print(f"⚠️ İlk yükleme hatası: {e}", flush=True)
    
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5)
                continue

            if time.time() < GLOBAL_COOLDOWN_BITIS:
                time.sleep(15)
                continue

            btc_yonu = btc_trend_kontrolu()
            print(f"👑 Güncel Piyasa Modu: {ANLIK_PIYASA_MODU} | BTC Yönü: {btc_yonu}", flush=True)

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
                
                tum_acik_emirler = exchange.fetch_open_orders()
                bekleyen_emir_sembolleri = set(emir.get('symbol') for emir in tum_acik_emirler if emir.get('symbol'))
            except Exception as e:
                print(f"⚠️ Pozisyonlar/Emirler çekilirken hata: {e}", flush=True)
                aktif_borsa_map = {}
                aktif_semboller_listesi = []
                bekleyen_emir_sembolleri = set()

            try:
                anlik_aktif_semboller = [p['symbol'] for p in raw_positions if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
                
                for eski_sym in list(AKTIF_GRID_SISTEMLERI.keys()):
                    if eski_sym not in anlik_aktif_semboller:
                        sistem_bilgisi = AKTIF_GRID_SISTEMLERI[eski_sym]
                        giris_fiyati = sistem_bilgisi.get("giris_fiyati", 0) if isinstance(sistem_bilgisi, dict) else 0
                        yon = sistem_bilgisi.get("yon", "LONG") if isinstance(sistem_bilgisi, dict) else "LONG"
                        giris_rsi = sistem_bilgisi.get("giris_rsi", 50.0) if isinstance(sistem_bilgisi, dict) else 50.0
                        
                        islem_karli_mi = False
                        try:
                            ticker = exchange.fetch_ticker(eski_sym)
                            cikis_fiyati = float(ticker['last'])
                            my_trades = exchange.fetch_my_trades(eski_sym, limit=5)
                            if my_trades:
                                son_trade = my_trades[-1]
                                gercek_pnl = float(son_trade.get('realizedPnl', 0) or 0)
                                islem_karli_mi = gercek_pnl > 0
                            else:
                                if yon == "LONG": islem_karli_mi = cikis_fiyati > giris_fiyati
                                else: islem_karli_mi = cikis_fiyati < giris_fiyati
                        except Exception:
                            islem_karli_mi = True

                        with state_lock:
                            bas_sayi = int(analitik_hafiza.get("basarili_islem_sayisi", 0))
                            basarisiz_sayi = int(analitik_hafiza.get("basarisiz_islem_sayisi", 0))
                            
                            if islem_karli_mi:
                                bas_sayi += 1
                                sonuc_mesaj_tipi = "✅ *POZİSYON KÂRLA KAPANDI (TP)*"
                            else:
                                basarisiz_sayi += 1
                                sonuc_mesaj_tipi = "❌ *POZİSYON ZARARLA KAPANDI (SL)*"
                                
                            analitik_hafiza["basarili_islem_sayisi"] = bas_sayi
                            analitik_hafiza["basarisiz_islem_sayisi"] = basarisiz_sayi

                            egitim_listesi = analitik_hafiza.get("egitim_verileri", [])
                            feature_vector = [giris_rsi, float(giris_fiyati), float(btc_yonu == "LONG"), 1.0, 0.0, 0.0]
                            label_val = 1 if islem_karli_mi else 0
                            egitim_listesi.append(feature_vector + [label_val])
                            if len(egitim_listesi) > 500: egitim_listesi.pop(0)
                            analitik_hafiza["egitim_verileri"] = egitim_listesi
                            
                            COIN_COOLDOWNLAR[eski_sym] = {"zaman": float(time.time() + TESTERE_KISA_COOLDOWN), "son_yon": yon}
                            if eski_sym in AKTIF_GRID_SISTEMLERI: del AKTIF_GRID_SISTEMLERI[eski_sym]
                                
                        hafizayi_kaydet()
                        yapay_zekayi_egit_ve_guncelle()
                        telegram_mesaj_gonder(f"{sonuc_mesaj_tipi}\n📌 `{eski_sym}` | Cooldown başlatıldı.")
            except Exception as eo_err:
                print(f"⚠️ Açık emirler kontrol hatası: {eo_err}", flush=True)

            taranan_sinyaller = []

            print("🔍 Coinler taranıyor...", flush=True)
            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU: break
                
                with state_lock:
                    cooldown_veri = COIN_COOLDOWNLAR.get(symbol)
                    if cooldown_veri:
                        zaman_kontrol = cooldown_veri.get("zaman", 0) if isinstance(cooldown_veri, dict) else float(cooldown_veri)
                        kalan_sure = zaman_kontrol - time.time()
                        if kalan_sure > 0:
                            print(f"⏳ Cooldown'da: {symbol} (Kalan: {int(kalan_sure)} sn)", flush=True)
                            continue

                try:
                    ticker = exchange.fetch_ticker(symbol)
                    anlik_fiyat = float(ticker['last'])

                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe='5m', limit=50)
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

                    mum_gecerli_mi, hacim_carpani = mum_ve_hacim_gecerlilik_kontrolu(df)
                    if not mum_gecerli_mi: continue

                    ema5 = ta.trend.ema_indicator(df['close'], window=5).iloc[-1]
                    ema13 = ta.trend.ema_indicator(df['close'], window=13).iloc[-1]
                    rsi = ta.momentum.rsi(df['close'], window=14).iloc[-1]
                    adx_val = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14).adx().iloc[-1]
                    atr = atr_ve_volatilite_hesapla(df)

                    if ema5 >= ema13: grid_yonu = "LONG"
                    else: grid_yonu = "SHORT"

                    sinyal_puani = 80 # Doğrudan yüksek puan vererek anında işlem açmasını sağlıyoruz

                    taranan_sinyaller.append({
                        "symbol": symbol, "puan": sinyal_puani, "yon": grid_yonu, 
                        "rsi": rsi, "adx": adx_val, "fiyat": anlik_fiyat, "atr": atr
                    })
                except Exception as ex:
                    print(f"⚠️ Tarama hatası ({symbol}): {ex}", flush=True)
                    continue

            taranan_sinyaller.sort(key=lambda x: x["puan"], reverse=True)

            for symbol, pos in list(aktif_borsa_map.items()):
                try:
                    yon = str(pos.get('side', '')).upper()
                    if not yon: yon = "LONG" if float(pos.get('contracts', 0) or pos.get('size', 0) or 0) > 0 else "SHORT"
                    merkez = float(pos.get('entryPrice', 0) or 0)
                    if merkez <= 0: continue

                    kontrat = float(pos.get('contracts', 0) or pos.get('size', 0) or 1.0)
                    kaldirac_val = int(pos.get('leverage', KALDIRAC))
                    guncel_fiyat = float(exchange.fetch_ticker(symbol)['last'])
                    
                    fark_yuzdesi = (guncel_fiyat - merkez) / merkez if yon == "LONG" else (merkez - guncel_fiyat) / merkez
                    roe = fark_yuzdesi * 100 * kaldirac_val

                    sistem_hafiza_bilgi = AKTIF_GRID_SISTEMLERI.get(symbol, {})
                    if isinstance(sistem_hafiza_bilgi, dict):
                        en_yuksek_roe = sistem_hafiza_bilgi.get("en_yuksek_roe", 0.0)
                        if roe > en_yuksek_roe:
                            sistem_hafiza_bilgi["en_yuksek_roe"] = roe
                            AKTIF_GRID_SISTEMLERI[symbol] = sistem_hafiza_bilgi

                        if en_yuksek_roe >= 8.0 and roe <= (en_yuksek_roe - 3.0):
                            pozisyonu_kapat(symbol, yon, kontrat, f"💰 *KÂR CEBE ATILDI!*\n📌 `{symbol}` | Tepe ROE: `%{en_yuksek_roe:.1f}` | ROE: `%{roe:.2f}`", basarili=True)
                            continue

                    if roe <= -15.0:
                        pozisyonu_kapat(symbol, yon, kontrat, f"🛑 *ZARAR KESİLDİ (SL)*\n📌 `{symbol}` | ROE: `%{roe:.2f}`", basarili=False, cezali_mi=True)
                        continue

                    if roe >= 2.5:
                        pozisyonu_kapat(symbol, yon, kontrat, f"💡 *TESTERE KÂR AL*\n📌 `{symbol}` | ROE: `%{roe:.2f}`", basarili=True, ozel_cooldown=TESTERE_KISA_COOLDOWN)
                        continue
                except Exception: continue

            for sinyal in taranan_sinyaller:
                if not BOT_CALISIYOR_MU: break
                
                # SIKI LİST KONTROLÜ: Hem açık pozisyonları hem bekleyen emirleri katıca denetliyoruz
                if sinyal["symbol"] in aktif_semboller_listesi or sinyal["symbol"] in bekleyen_emir_sembolleri:
                    continue

                if len(aktif_borsa_map) >= MAKSIMUM_TOPLAM_POZISYON or len(aktif_semboller_listesi) >= MAKSIMUM_TOPLAM_POZISYON:
                    print(f"🛑 Maksimum pozisyon sınırına ulaşıldı ({len(aktif_borsa_map)}/{MAKSIMUM_TOPLAM_POZISYON}). Yeni işlem açılmıyor.", flush=True)
                    break

                try:
                    bakiye_bilgisi = exchange.fetch_balance()
                    toplam_bakiye = float(bakiye_bilgisi['total'].get('USDT', 0))
                    serbest_bakiye = float(bakiye_bilgisi.get('free', {}).get('USDT', 0) or 0)

                    exchange.set_leverage(KALDIRAC, sinyal["symbol"])
                    market = exchange.market(sinyal["symbol"])
                    
                    hedef_butce = toplam_bakiye * (1.0 / MAKSIMUM_TOPLAM_POZISYON)
                    kullanilacak_tutar = min(hedef_butce, serbest_bakiye)
                    
                    if kullanilacak_tutar < 1.0 or serbest_bakiye < 1.0: continue

                    giris_fiyati = sinyal["fiyat"]
                    miktar = float(exchange.amount_to_precision(
                        sinyal["symbol"], 
                        max((kullanilacak_tutar * KALDIRAC) / giris_fiyati / float(market.get('contractSize', 1.0)), 
                        float(market['limits']['amount']['min'] or 1.0))
                    ))
                    
                    islem_yonu = 'buy' if sinyal["yon"] == 'LONG' else 'sell'
                    
                    # Havada kalmaması için DOGRUDAN PİYASA EMRİ (market order) ile anında açıyoruz
                    exchange.create_order(sinyal["symbol"], 'market', islem_yonu, miktar, None)
                    
                    with state_lock:
                        AKTIF_GRID_SISTEMLERI[sinyal["symbol"]] = {
                            "giris_fiyati": giris_fiyati,
                            "yon": sinyal["yon"],
                            "giris_rsi": float(sinyal["rsi"]), 
                            "giris_zamani": time.time(),
                            "en_yuksek_roe": 0.0
                        }
                        aktif_semboller_listesi.append(sinyal["symbol"])
                    hafizayi_kaydet()
                    
                    print(f"⚡ Anlık Piyasa İşlemi Açıldı: {sinyal['symbol']} | Yön: {sinyal['yon']} | Fiyat: {giris_fiyati}", flush=True)
                    telegram_mesaj_gonder(f"⚡ *İŞLEM AÇILDI (PİYASA EMRI)*\n📌 `{sinyal['symbol']}` | Yön: `{sinyal['yon']}`\n🎯 Giriş: `{giris_fiyati}`")
                    break
                except Exception as e:
                    print(f"❌ İşlem açma hatası ({sinyal['symbol']}): {e}", flush=True)

        except Exception as e:
            print(f"⚠️ Döngü genel hata: {e}", flush=True)
        
        time.sleep(8)

async def main():
    app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    try:
        requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook?drop_pending_updates=True", timeout=5)
    except Exception: pass

    app_tg.add_handler(CommandHandler("durum", durum_komutu))
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))
    
    try:
        await app_tg.initialize()
        await app_tg.start()
        await app_tg.updater.start_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
    except Exception: pass

    tarayici_thread = threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True)
    tarayici_thread.start()

    stop_event = asyncio.Event()
    await stop_event.wait()

if __name__ == '__main__':
    try:
        asyncio.main(main()) if hasattr(asyncio, 'main') else asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("🛑 Bot kapatıldı.", flush=True)
