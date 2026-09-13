import os
import time
import threading
import sys
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

COIN_ID_MAP = {
    'SOL/USDT:USDT': 1,
    'XRP/USDT:USDT': 2,
    'DOGE/USDT:USDT': 3,
    'LTC/USDT:USDT': 4,
    'LINK/USDT:USDT': 5
}

BOT_CALISIYOR_MU = True
state_lock = threading.Lock()

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
                "basarili_islem_sayisi": int(ANALitik_HAFIZA.get("basarili_islem_sayisi", 0)),
                "basarisiz_islem_sayisi": int(ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0)),
                "egitim_verileri": ANALitik_HAFIZA.get("egitim_verileri", [])
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
ANALitik_HAFIZA = kalici_veri.get("analitik", {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0, "egitim_verileri": []})
COIN_COOLDOWNLAR = kalici_veri.get("cooldownlar", {})

MAKSIMUM_AYNI_YON_SAYISI = 2
MAKSIMUM_TOPLAM_POZISYON = 3
COOLDOWN_SURESI_SANIYE = 15 * 60

ai_model = RandomForestClassifier(n_estimators=100, max_depth=6, random_state=42)
ai_model_egitildi = False

def yapay_zekayi_egit_ve_guncelle():
    global ai_model, ai_model_egitildi
    with state_lock:
        veriler = list(ANALitik_HAFIZA.get("egitim_verileri", []))
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

def yapay_zeka_islem_onayi(rsi, adx, ema_fark, yon_kod, atr_yuzde, coin_id):
    if not ai_model_egitildi: return True
    try:
        olasiliklar = ai_model.predict_proba(np.array([[float(rsi), float(adx), float(ema_fark), int(yon_kod), float(atr_yuzde), int(coin_id)]]))[0]
        classes = list(ai_model.classes_)
        return (olasiliklar[classes.index(1)] if 1 in classes else 1.0) >= 0.55
    except Exception:
        return True

def atr_ve_volatilite_hesapla(df):
    try:
        atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range().iloc[-1]
        return float((atr / df['close'].iloc[-1]) * 100)
    except Exception:
        return 1.5

def telegram_mesaj_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": mesaj, "parse_mode": "Markdown"}, timeout=5)
    except Exception: pass

def pozisyonu_kapat(symbol, yon, miktar, sebep_mesaji, basarili=True, ruzgar_dondu=False):
    kapatma_yonu = 'sell' if yon == 'LONG' else 'buy'
    try:
        try:
            exchange.cancel_all_orders(symbol)
        except Exception:
            pass
            
        exchange.create_order(symbol, 'market', kapatma_yonu, miktar, None, {'reduceOnly': True})
    except Exception as e:
        print(f"⚠️ Kapatma hatası: {e}", flush=True)

    with state_lock:
        bas_sayi = int(ANALitik_HAFIZA.get("basarili_islem_sayisi", 0))
        basarisiz_sayi = int(ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0))
        if basarili:
            bas_sayi += 1
        else:
            basarisiz_sayi += 1
        ANALitik_HAFIZA["basarili_islem_sayisi"] = bas_sayi
        ANALitik_HAFIZA["basarisiz_islem_sayisi"] = basarisiz_sayi

        if not ruzgar_dondu:
            COIN_COOLDOWNLAR[symbol] = {
                "zaman": float(time.time() + COOLDOWN_SURESI_SANIYE),
                "son_yon": yon
            }
        else:
            if symbol in COIN_COOLDOWNLAR:
                del COIN_COOLDOWNLAR[symbol]

        if symbol in AKTIF_GRID_SISTEMLERI:
            del AKTIF_GRID_SISTEMLERI[symbol]
            
    hafizayi_kaydet()
    if sebep_mesaji: telegram_mesaj_gonder(sebep_mesaji)

# ==================== TELEGRAM KOMUTLARI ====================
async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        balance = exchange.fetch_balance()
        total = float(balance['total'].get('USDT', 0))
        borsa_poslari = [p for p in exchange.fetch_positions() if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
        toplam_pnl = sum(float(p.get('unrealizedPnl', 0)) for p in borsa_poslari)
        
        basarili = int(ANALitik_HAFIZA.get("basarili_islem_sayisi", 0))
        basarisiz = int(ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0))
        toplam_islem = basarili + basarisiz
        basari_orani = (basarili / toplam_islem * 100) if toplam_islem > 0 else 0.0
        egitim_veri_sayisi = len(ANALitik_HAFIZA.get("egitim_verileri", []))

        pos_detaylari = ""
        for p in borsa_poslari:
            sym = p['symbol']
            yon = str(p.get('side', '')).upper()
            pnl_val = float(p.get('unrealizedPnl', 0))
            giris = float(p.get('entryPrice', 0))
            kaldirac = int(p.get('leverage', 10))
            guncel_fiyat = exchange.fetch_ticker(sym)['last']
            
            fark = (guncel_fiyat - giris) / giris if yon == "LONG" else (giris - guncel_fiyat) / giris
            roe = fark * 100 * kaldirac
            
            pos_detaylari += f"\n• `{sym}` | {yon} | Giriş: `{giris}`\n  PnL: `{pnl_val:+.2f} USDT` (`%{roe:+.2f}`)"

        mesaj = (
            "📊 **HİBRİT BOT DURUMU**\n\n"
            f"💰 Toplam Kasa: `{total:.2f} USDT`\n"
            f"🟢 Anlık PnL: `{toplam_pnl:+.2f} USDT`\n"
            f"📌 Açık Pozisyon: `{len(borsa_poslari)} / {MAKSIMUM_TOPLAM_POZISYON}`"
            f"{pos_detaylari}\n\n"
            f"✅ Başarili TP: `{basarili}` | ❌ Başarısız SL: `{basarisiz}`\n"
            f"📈 Başarı Oranı: `%{basari_orani:.1f}`\n"
            f"🧠 Al Verisi: `{egitim_veri_sayisi}/20`"
        )
        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = True
    await update.message.reply_text("🟢 Bot Aktif!")

async def durdur_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ Bot durduruldu.")

async def kapat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        for pos in exchange.fetch_positions():
            kontrat = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
            if kontrat > 0:
                pozisyonu_kapat(pos['symbol'], str(pos.get('side', '')).upper(), kontrat, f"🛑 Manuel Kapatma - `{pos['symbol']}`", basarili=False)
        await update.message.reply_text("✅ Tüm pozisyonlar kapatıldı.")
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

# ==================== ARKA PLAN TARAYICI ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU
    print("🚀 Tarayıcı Döngüsü Başlatıldı.", flush=True)
    try:
        exchange.load_markets()
        yapay_zekayi_egit_ve_guncelle()
    except Exception: pass
    
    while True:
        try:
            print("🔄 Döngü taraması yapılıyor...", flush=True)
            if not BOT_CALISIYOR_MU:
                time.sleep(5)
                continue

            try:
                raw_positions = exchange.fetch_positions()
                aktif_borsa_map = {p['symbol']: p for p in raw_positions if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0}
            except Exception:
                aktif_borsa_map = {}

            for symbol, pos in aktif_borsa_map.items():
                try:
                    guncel_fiyat = exchange.fetch_ticker(symbol)['last']
                except Exception: continue

                yon = str(pos.get('side', '')).upper()
                merkez = float(pos.get('entryPrice', 0))
                kaldirac = int(pos.get('leverage', 10))
                
                fark = (guncel_fiyat - merkez) / merkez if yon == "LONG" else (merkez - guncel_fiyat) / merkez
                roe = fark * 100 * kaldirac
                pnl = float(pos.get('unrealizedPnl', 0))
                kontrat = float(pos.get('contracts', 0) or pos.get('size', 0) or 1.0)

                if roe >= 20.0:
                    pozisyonu_kapat(symbol, yon, kontrat, f"🎯 *KÂR ALINDI (TP)*\n📌 `{symbol}` | Kâr: `+{pnl:.2f} USDT`", basarili=True, ruzgar_dondu=False)
                elif roe <= -10.0:
                    pozisyonu_kapat(symbol, yon, kontrat, f"🛑 *ZARAR KESİLDİ (SL)*\n📌 `{symbol}` | Zarar: `{pnl:.2f} USDT`", basarili=False, ruzgar_dondu=False)

            taranan_sinyaller = []

            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU: break
                
                try:
                    guncel_fiyat = exchange.fetch_ticker(symbol)['last']
                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

                    ema5 = ta.trend.ema_indicator(df['close'], window=5).iloc[-1]
                    ema13 = ta.trend.ema_indicator(df['close'], window=13).iloc[-1]
                    
                    rsi = ta.momentum.rsi(df['close'], window=14).iloc[-1]
                    adx_val = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14).adx().iloc[-1]
                    atr = atr_ve_volatilite_hesapla(df)

                    son_kapanan_close = df['close'].iloc[-2]
                    son_kapanan_open = df['open'].iloc[-2]
                    onceki_kapanan_close = df['close'].iloc[-3]
                    onceki_kapanan_open = df['open'].iloc[-3]

                    short_formasyon_onayi = (onceki_kapanan_close > onceki_kapanan_open) and (son_kapanan_close < son_kapanan_open)
                    long_formasyon_onayi = (onceki_kapanan_close < onceki_kapanan_open) and (son_kapanan_close > son_kapanan_open)

                except Exception as e:
                    print(f"⚠️ Veri çekme hatası ({symbol}): {e}", flush=True)
                    continue

                if adx_val >= 25:
                    temel_puan = 75
                else:
                    temel_puan = 60

                if ema5 > ema13 and long_formasyon_onayi:
                    grid_yonu = "LONG"
                    sinyal_puani = temel_puan + 10
                elif ema5 < ema13 and short_formasyon_onayi:
                    grid_yonu = "SHORT"
                    sinyal_puani = temel_puan + 10
                else:
                    grid_yonu = "LONG" if ema5 > ema13 else "SHORT"
                    sinyal_puani = temel_puan

                if symbol in aktif_borsa_map:
                    mevcut_pos = aktif_borsa_map[symbol]
                    mevcut_yon = str(mevcut_pos.get('side', '')).upper()
                    mevcut_kontrat = float(mevcut_pos.get('contracts', 0) or mevcut_pos.get('size', 0) or 1.0)
                    
                    if sinyal_puani >= 70 and mevcut_yon != grid_yonu:
                        print(f"🔄 [RÜZGAR DÖNDÜ] {symbol} | Eski Yön: {mevcut_yon} -> Yeni Yön: {grid_yonu}. Pozisyon ters çevriliyor!", flush=True)
                        pozisyonu_kapat(symbol, mevcut_yon, mevcut_kontrat, f"🔄 *RÜZGAR TERSİNE DÖNDÜ*\n📌 `{symbol}` | Pozisyon kapatılıp zıt yöne dönülüyor.", basarili=False, ruzgar_dondu=True)
                        aktif_borsa_map.pop(symbol, None)

                if symbol not in aktif_borsa_map:
                    with state_lock:
                        cooldown_veri = COIN_COOLDOWNLAR.get(symbol)
                        if cooldown_veri:
                            if isinstance(cooldown_veri, dict):
                                if time.time() < cooldown_veri.get("zaman", 0):
                                    continue
                            else:
                                if time.time() < float(cooldown_veri):
                                    continue

                    if not yapay_zeka_islem_onayi(rsi, adx_val, float(ema5 - ema13), (1 if grid_yonu == 'LONG' else -1), atr, COIN_ID_MAP.get(symbol, 0)):
                        continue

                    taranan_sinyaller.append({"symbol": symbol, "puan": sinyal_puani, "yon": grid_yonu, "rsi": rsi, "adx": adx_val, "ema_fark": float(ema5 - ema13), "fiyat": guncel_fiyat, "atr": atr})

            taranan_sinyaller.sort(key=lambda x: x["puan"], reverse=True)

            for sinyal in taranan_sinyaller:
                if not BOT_CALISIYOR_MU: break
                if len(aktif_borsa_map) >= MAKSIMUM_TOPLAM_POZISYON: break
                if sinyal["puan"] < 70: continue

                try:
                    toplam_bakiye = float(exchange.fetch_balance()['total'].get('USDT', 0))
                    exchange.set_leverage(10, sinyal["symbol"])
                    
                    market = exchange.market(sinyal["symbol"])
                    miktar = float(exchange.amount_to_precision(sinyal["symbol"], max((toplam_bakiye * 0.20 * 10) / sinyal["fiyat"] / float(market.get('contractSize', 1.0)), float(market['limits']['amount']['min'] or 1.0))))
                    
                    islem_yonu = 'buy' if sinyal["yon"] == 'LONG' else 'sell'
                    giris_fiyati = sinyal["fiyat"]
                    
                    exchange.create_order(sinyal["symbol"], 'market', islem_yonu, miktar)

                    kaldirac = 10
                    if sinyal["yon"] == 'LONG':
                        tp_fiyat = giris_fiyati * (1 + (0.20 / kaldirac))
                        sl_fiyat = giris_fiyati * (1 - (0.10 / kaldirac))
                        kapat_yon = 'sell'
                    else:
                        tp_fiyat = giris_fiyati * (1 - (0.20 / kaldirac))
                        sl_fiyat = giris_fiyati * (1 + (0.10 / kaldirac))
                        kapat_yon = 'buy'

                    try:
                        exchange.create_order(sinyal["symbol"], 'limit', kapat_yon, miktar, tp_fiyat, {'reduceOnly': True})
                        exchange.create_order(sinyal["symbol"], 'stop', kapat_yon, miktar, sl_fiyat, {'stopPrice': sl_fiyat, 'reduceOnly': True})
                    except Exception as emir_hata:
                        print(f"⚠️ TP/SL borsaya iletilirken hata oluştu: {emir_hata}", flush=True)

                    with state_lock:
                        AKTIF_GRID_SISTEMLERI[sinyal["symbol"]] = {"giris_rsi": float(sinyal["rsi"])}
                    hafizayi_kaydet()
                    
                    print(f"⚡ [İŞLEM AÇILDI] {sinyal['symbol']} | Yön: {sinyal['yon']} | TP/SL Emirleri Gönderildi", flush=True)
                    telegram_mesaj_gonder(f"⚡ *İŞLEM AÇILDI & TP/SL KURULDU*\n📌 `{sinyal['symbol']}` | Yön: `{sinyal['yon']}`")
                    break
                except Exception as e:
                    print(f"❌ İşlem açma hatası: {e}", flush=True)

        except Exception as e:
            print(f"⚠️ Döngü hatası: {e}", flush=True)
        time.sleep(5)

if __name__ == '__main__':
    t = threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True)
    t.start()
    
    app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app_tg.add_handler(CommandHandler("durum", durum_komutu))
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))
    
    print("🤖 Telegram Bot Başlatılıyor...", flush=True)
    app_tg.run_polling()
