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

# Sandbox kapatıldı (Gerçek emir için False)
exchange.set_sandbox_mode(False)

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

MAKSIMUM_TOPLAM_POZISYON = 3
COOLDOWN_SURESI_SANIYE = 45 * 60  # Testereye karşı 45 dakika bekleme

ai_model = RandomForestClassifier(n_estimators=100, max_depth=6, random_state=42)
ai_model_egitildi = False

def yapay_zekayi_egit_ve_guncelle():
    global ai_model, ai_model_egitildi
    with state_lock:
        veriler = list(ANALitik_HAFIZA.get("egitim_verileri", []))
    if len(veriler) < 5:
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
        return (olasiliklar[classes.index(1)] if 1 in classes else 1.0) >= 0.60
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

def pozisyon_kapandi_olarak_isaretle(symbol, yon, kar_zarar=0.0, sebep_mesaji="", cooldown_uygula=True, egitim_ekle=True):
    with state_lock:
        bas_sayi = int(ANALitik_HAFIZA.get("basarili_islem_sayisi", 0))
        basarisiz_sayi = int(ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0))
        
        basarili = kar_zarar > 0
        if basarili:
            bas_sayi += 1
        else:
            basarisiz_sayi += 1
            
        ANALitik_HAFIZA["basarili_islem_sayisi"] = bas_sayi
        ANALitik_HAFIZA["basarisiz_islem_sayisi"] = basarisiz_sayi

        if egitim_ekle:
            aktif_bilgi = AKTIF_GRID_SISTEMLERI.get(symbol, {})
            r = float(aktif_bilgi.get("giris_rsi", 50.0))
            a = float(aktif_bilgi.get("giris_adx", 25.0))
            ef = float(aktif_bilgi.get("giris_ema_fark", 0.0))
            atr_v = float(aktif_bilgi.get("giris_atr", 1.5))
            y_kod = 1 if yon == "LONG" else -1
            c_id = int(COIN_ID_MAP.get(symbol, 0))
            sonuc_kod = 1 if basarili else 0
            
            egitim_satiri = [r, a, ef, y_kod, atr_v, c_id, sonuc_kod]
            if "egitim_verileri" not in ANALitik_HAFIZA:
                ANALitik_HAFIZA["egitim_verileri"] = []
            ANALitik_HAFIZA["egitim_verileri"].append(egitim_satiri)

        if cooldown_uygula:
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
    yapay_zekayi_egit_ve_guncelle()
    if sebep_mesaji:
        telegram_mesaj_gonder(sebep_mesaji)

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
                sym = pos['symbol']
                yon = str(pos.get('side', '')).upper()
                pnl = float(pos.get('unrealizedPnl', 0))
                kapatma_yonu = 'sell' if yon == 'LONG' else 'buy'
                try:
                    exchange.cancel_all_orders(sym)
                    exchange.create_order(sym, 'market', kapatma_yonu, kontrat, None, {'reduceOnly': True})
                except Exception:
                    pass
                pozisyon_kapandi_olarak_isaretle(sym, yon, kar_zarar=pnl, sebep_mesaji=f"🛑 *MANUEL KAPATMA*\n📌 `{sym}` | PnL: `{pnl:+.2f} USDT`", cooldown_uygula=True)
        await update.message.reply_text("✅ Tüm pozisyonlar kapatıldı.")
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

# ==================== ARKA PLAN TARAYICI ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU
    print("🚀 Tarayıcı Döngüsü Başlatıldı (1h Ana Trend Filtreli).", flush=True)
    try:
        exchange.load_markets()
        yapay_zekayi_egit_ve_guncelle()
    except Exception as e:
        print(f"⚠️ İlk yükleme hatası: {e}", flush=True)
    
    onceki_aktif_semboller = set()
    onceki_pnl_takibi = {}
    ruzgar_sayaclari = {}

    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5)
                continue

            try:
                raw_positions = exchange.fetch_positions()
                guncel_borsa_poslari = {}
                for p in raw_positions:
                    kont = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                    if kont > 0:
                        sym = p['symbol']
                        guncel_borsa_poslari[sym] = p
                        onceki_pnl_takibi[sym] = float(p.get('unrealizedPnl', 0))
            except Exception as e:
                print(f"⚠️ Pozisyonlar çekilirken hata: {e}", flush=True)
                guncel_borsa_poslari = {}

            guncel_aktif_semboller = set(guncel_borsa_poslari.keys())

            # 1. Kapanan pozisyonları tespit et
            kapananlar = onceki_aktif_semboller - guncel_aktif_semboller
            for kapatilan_sym in kapananlar:
                son_pnl = onceki_pnl_takibi.get(kapatilan_sym, 0.0)
                durum_emoji = "🎯 *KÂR ALINDI (TP)*" if son_pnl >= 0 else "❌ *STOP OLDU (SL)*"
                print(f"🎯 Pozisyon kapandı: {kapatilan_sym} | PnL: {son_pnl}", flush=True)
                
                pozisyon_kapandi_olarak_isaretle(
                    kapatilan_sym, 
                    yon="BİLİNMİYOR", 
                    kar_zarar=son_pnl, 
                    sebep_mesaji=f"{durum_emoji}\n📌 `{kapatilan_sym}` | PnL: `{son_pnl:+.2f} USDT`",
                    cooldown_uygula=True,
                    egitim_ekle=True
                )
                try:
                    exchange.cancel_all_orders(kapatilan_sym)
                except Exception:
                    pass
                onceki_pnl_takibi.pop(kapatilan_sym, None)
                if kapatilan_sym in ruzgar_sayaclari:
                    del ruzgar_sayaclari[kapatilan_sym]

            onceki_aktif_semboller = guncel_aktif_semboller.copy()

            # 2. AÇIK POZİSYONLARIN RÜZGARINI KONTROL ET (1 SAATLİK TREND FİLTRESİ)
            for symbol, pos in list(guncel_borsa_poslari.items()):
                try:
                    yon = str(pos.get('side', '')).upper()
                    pnl = float(pos.get('unrealizedPnl', 0))
                    kontrat = float(pos.get('contracts', 0) or pos.get('size', 0) or 1.0)

                    # 1 saatlik (1h) mumlara bakarak ana trendi ölçüyoruz (Gürültüyü yok eder)
                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=50)
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    ema9 = ta.trend.ema_indicator(df['close'], window=9).iloc[-1]
                    ema21 = ta.trend.ema_indicator(df['close'], window=21).iloc[-1]
                    adx_val = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14).adx().iloc[-1]
                    atr = atr_ve_volatilite_hesapla(df)

                    if ema9 > ema21:
                        grid_yonu, sinyal_puani = "LONG", 80 if adx_val >= 25 else 70
                    else:
                        grid_yonu, sinyal_puani = "SHORT", 80 if adx_val >= 25 else 70

                    print(f"📌 [1h RÜZGAR] {symbol} | Mevcut: {yon} | 1h Trend: {grid_yonu} (ADX: {adx_val:.1f})", flush=True)

                    # Rüzgarın tersine döndüğünü kesinleştirmek için 5 döngü boyunca aynı kalmalı
                    if sinyal_puani >= 75 and yon != grid_yonu:
                        if symbol not in ruzgar_sayaclari:
                            ruzgar_sayaclari[symbol] = {"yon": grid_yonu, "sayac": 1}
                        elif ruzgar_sayaclari[symbol]["yon"] == grid_yonu:
                            ruzgar_sayaclari[symbol]["sayac"] += 1
                        else:
                            ruzgar_sayaclari[symbol] = {"yon": grid_yonu, "sayac": 1}

                        if ruzgar_sayaclari[symbol]["sayac"] >= 5:  # Üst üste 5 kez (25 saniye boyunca 1h trend teyitli)
                            print(f"🔄 [1h TREND DEĞİŞTİ] {symbol} | Eski: {yon} -> Yeni: {grid_yonu}.", flush=True)
                            try:
                                exchange.cancel_all_orders(symbol)
                                kapatma_yonu = 'sell' if yon == 'LONG' else 'buy'
                                exchange.create_order(symbol, 'market', kapatma_yonu, kontrat, None, {'reduceOnly': True})
                            except Exception:
                                pass
                            
                            pozisyon_kapandi_olarak_isaretle(symbol, yon, kar_zarar=pnl, sebep_mesaji=f"🔄 *1h ANA TREND DÖNDÜ*\n📌 `{symbol}` | PnL: `{pnl:+.2f} USDT`", cooldown_uygula=False, egitim_ekle=True)
                            
                            toplam_bakiye = float(exchange.fetch_balance()['total'].get('USDT', 0))
                            exchange.set_leverage(10, symbol)
                            market = exchange.market(symbol)
                            yeni_miktar = float(exchange.amount_to_precision(symbol, max((toplam_bakiye * 0.20 * 10) / df['close'].iloc[-1] / float(market.get('contractSize', 1.0)), float(market['limits']['amount']['min'] or 1.0))))
                            
                            yeni_islem_yonu = 'buy' if grid_yonu == 'LONG' else 'sell'
                            exchange.create_order(symbol, 'market', yeni_islem_yonu, yeni_miktar)

                            telegram_mesaj_gonder(f"⚡ *YENİ TREND İŞLEMİ AÇILDI*\n📌 `{symbol}` | Yön: `{grid_yonu}`")
                            del ruzgar_sayaclari[symbol]
                    else:
                        if symbol in ruzgar_sayaclari:
                            del ruzgar_sayaclari[symbol]

                except Exception as e:
                    print(f"⚠️ 1h rüzgar kontrol hatası ({symbol}): {e}", flush=True)

            # 3. LİSTEYİ TARAYIP İŞLEM AÇMA (1h Trend ve 75 Puan Kuralı)
            taranan_sinyaller = []

            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU: break
                
                with state_lock:
                    cooldown_veri = COIN_COOLDOWNLAR.get(symbol)
                    if cooldown_veri:
                        kalan_sure = cooldown_veri.get("zaman", 0) - time.time() if isinstance(cooldown_veri, dict) else float(cooldown_veri) - time.time()
                        if kalan_sure > 0:
                            continue

                try:
                    guncel_fiyat = exchange.fetch_ticker(symbol)['last']
                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=50)
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

                    ema9 = ta.trend.ema_indicator(df['close'], window=9).iloc[-1]
                    ema21 = ta.trend.ema_indicator(df['close'], window=21).iloc[-1]
                    rsi = ta.momentum.rsi(df['close'], window=14).iloc[-1]
                    adx_val = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14).adx().iloc[-1]
                    atr = atr_ve_volatilite_hesapla(df)

                    if ema9 > ema21:
                        grid_yonu, sinyal_puani = "LONG", 80 if adx_val >= 25 else 70
                    else:
                        grid_yonu, sinyal_puani = "SHORT", 80 if adx_val >= 25 else 70

                    print(f"🔍 [1h TARAMA] {symbol} | Puan: {sinyal_puani} | Yön: {grid_yonu} | RSI: {rsi:.1f} | ADX: {adx_val:.1f}", flush=True)

                    if symbol in guncel_borsa_poslari:
                        continue

                    if not yapay_zeka_islem_onayi(rsi, adx_val, float(ema9 - ema21), (1 if grid_yonu == 'LONG' else -1), atr, COIN_ID_MAP.get(symbol, 0)):
                        continue

                    taranan_sinyaller.append({
                        "symbol": symbol, "puan": sinyal_puani, "yon": grid_yonu, 
                        "rsi": rsi, "adx": adx_val, "ema_fark": float(ema9 - ema21), 
                        "fiyat": guncel_fiyat, "atr": atr
                    })
                except Exception as e:
                    print(f"⚠️ Sinyal tarama hatası ({symbol}): {e}", flush=True)
                    continue

            taranan_sinyaller.sort(key=lambda x: x["puan"], reverse=True)

            for sinyal in taranan_sinyaller:
                if len(guncel_borsa_poslari) >= MAKSIMUM_TOPLAM_POZISYON: break
                if sinyal["puan"] < 75: continue  # Sadece güçlü 1h trendleri al

                try:
                    toplam_bakiye = float(exchange.fetch_balance()['total'].get('USDT', 0))
                    exchange.set_leverage(10, sinyal["symbol"])
                    
                    market = exchange.market(sinyal["symbol"])
                    miktar = float(exchange.amount_to_precision(sinyal["symbol"], max((toplam_bakiye * 0.20 * 10) / sinyal["fiyat"] / float(market.get('contractSize', 1.0)), float(market['limits']['amount']['min'] or 1.0))))
                    
                    islem_yonu = 'buy' if sinyal["yon"] == 'LONG' else 'sell'
                    exchange.create_order(sinyal["symbol"], 'market', islem_yonu, miktar)

                    with state_lock:
                        AKTIF_GRID_SISTEMLERI[sinyal["symbol"]] = {
                            "giris_rsi": float(sinyal["rsi"]),
                            "giris_adx": float(sinyal["adx"]),
                            "giris_ema_fark": float(sinyal["ema_fark"]),
                            "giris_atr": float(sinyal["atr"])
                        }
                    hafizayi_kaydet()
                    
                    print(f"⚡ [İŞLEM AÇILDI] {sinyal['symbol']} | Yön: {sinyal['yon']}", flush=True)
                    telegram_mesaj_gonder(f"⚡ *YENİ İŞLEM AÇILDI*\n📌 `{sinyal['symbol']}` | Yön: `{sinyal['yon']}`")
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
    
    print("🤖 Telegram Bot Başlatılıyor ve Polling Çalıştırılıyor...", flush=True)
    app_tg.run_polling()
