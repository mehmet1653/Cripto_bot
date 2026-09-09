import time
import threading
import sys
import requests
import ccxt
import pandas as pd
import ta
import os
import numpy as np
from datetime import datetime
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler
from sklearn.ensemble import RandomForestClassifier
from supabase import create_client, Client

sys.stdout.reconfigure(line_buffering=True)

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
    'SOL/USDT:USDT', 'AVAX/USDT:USDT', 'XRP/USDT:USDT', 
    'DOGE/USDT:USDT', 'SUI/USDT:USDT', 'LINK/USDT:USDT', 'ADA/USDT:USDT'
]

COIN_ID_MAP = {
    'SOL/USDT:USDT': 1,
    'AVAX/USDT:USDT': 2,
    'XRP/USDT:USDT': 3,
    'DOGE/USDT:USDT': 4,
    'SUI/USDT:USDT': 5,
    'LINK/USDT:USDT': 6,
    'ADA/USDT:USDT': 7
}

BOT_CALISIYOR_MU = True

# ==================== SUPABASE HAFIZA FONKSİYONLARI ====================
def hafizayi_yukle():
    varsayilan = {
        "aktif_sistemler": {},
        "analitik": {
            "basarisiz_analizler": [],
            "basarili_islem_sayisi": 0,
            "basarisiz_islem_sayisi": 0,
            "gunluk_net_kar_usd": 0.0,
            "egitim_verileri": []
        },
        "cooldownlar": {}
    }
    try:
        response = supabase.table("bot_hafiza").select("*").eq("id", 1).execute()
        if response.data and len(response.data) > 0:
            veri = response.data[0]
            print("💾 Hafıza Supabase'den başarıyla yüklendi.", flush=True)
            return {
                "aktif_sistemler": veri.get("aktif_sistemler", {}),
                "analitik": veri.get("analitik", varsayilan["analitik"]),
                "cooldownlar": veri.get("cooldownlar", {})
            }
    except Exception as e:
        print(f"⚠️ Hafıza yükleme uyarısı: {e}", flush=True)
        
    try:
        supabase.table("bot_hafiza").upsert({"id": 1, **varsayilan}).execute()
        print("💾 Varsayılan hafıza Supabase'e kaydedildi.", flush=True)
    except Exception as ex:
        print(f"⚠️ Varsayılan kayıt oluşturulamadı: {ex}", flush=True)
        
    return varsayilan

def hafizayi_kaydet():
    try:
        supabase.table("bot_hafiza").upsert({
            "id": 1,
            "aktif_sistemler": AKTIF_GRID_SISTEMLERI,
            "analitik": ANALitik_HAFIZA,
            "cooldownlar": COIN_COOLDOWNLAR
        }).execute()
    except Exception as e:
        print(f"⚠️ Hafıza kaydetme hatası: {e}", flush=True)

kalici_veri = hafizayi_yukle()
AKTIF_GRID_SISTEMLERI = kalici_veri.get("aktif_sistemler", {})
ANALitik_HAFIZA = kalici_veri.get("analitik", {
    "basarisiz_analizler": [],
    "basarili_islem_sayisi": 0,
    "basarisiz_islem_sayisi": 0,
    "gunluk_net_kar_usd": 0.0,
    "egitim_verileri": []
})
COIN_COOLDOWNLAR = kalici_veri.get("cooldownlar", {})

MAKSIMUM_AYNI_YON_SAYISI = 2
MAKSIMUM_TOPLAM_POZISYON = 3
COOLDOWN_SURESI_SANIYE = 15 * 60

# ==================== YAPAY ZEKA MODELİ ====================
ai_model = RandomForestClassifier(n_estimators=100, max_depth=6, random_state=42)
ai_model_egitildi = False

def yapay_zekayi_egit_ve_guncelle():
    global ai_model, ai_model_egitildi
    veriler = ANALitik_HAFIZA.get("egitim_verileri", [])
    
    if len(veriler) < 30:
        ai_model_egitildi = False
        return

    try:
        X = [item[:9] for item in veriler]
        y = [item[9] for item in veriler]
        if len(set(y)) < 2:
            ai_model_egitildi = False
            return
            
        ai_model.fit(np.array(X), np.array(y))
        ai_model_egitildi = True
        print(f"🤖 Yapay zeka modeli güncellendi ve aktif! (Toplam Eğitim Verisi: {len(veriler)})", flush=True)
    except Exception as e:
        print(f"⚠️ Yapay zeka eğitim hatası: {e}", flush=True)
        ai_model_egitildi = False

def yapay_zeka_islem_onayi(rsi, adx, ema_fark, yon_kod, atr_yuzde, coin_id, derinlik_kod, hacim_orani, fiyat_degisim, sinyal_puani):
    if not ai_model_egitildi:
        return True
    try:
        features = np.array([[rsi, adx, ema_fark, yon_kod, atr_yuzde, coin_id, derinlik_kod, hacim_orani, fiyat_degisim]])
        olasiliklar = ai_model.predict_proba(features)[0]
        classes = list(ai_model.classes_)
        basari_ihtimali = olasiliklar[classes.index(1)] if 1 in classes else 1.0
        
        dinamik_esik = 0.40 if sinyal_puani >= 90 else 0.50
        return basari_ihtimali >= dinamik_esik
    except Exception:
        return True

def atr_ve_volatilite_hesapla(df, period=14):
    try:
        atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=period).average_true_range().iloc[-1]
        fiyat = df['close'].iloc[-1]
        return float((atr / fiyat) * 100)
    except Exception:
        return 1.5

def emir_defteri_derinlik_analizi(symbol):
    try:
        order_book = exchange.fetch_order_book(symbol, limit=20)
        bids = order_book.get('bids', [])
        asks = order_book.get('asks', [])
        toplam_bid_hacim = sum(bid[1] for bid in bids)
        toplam_ask_hacim = sum(ask[1] for ask in asks)
        if toplam_bid_hacim + toplam_ask_hacim == 0:
            return "DENGELI"
        bid_orani = toplam_bid_hacim / (toplam_bid_hacim + toplam_ask_hacim)
        if bid_orani > 0.55:
            return "ALICI_BASKIN"
        elif bid_orani < 0.45:
            return "SATICI_BASKIN"
        return "DENGELI"
    except Exception:
        return "DENGELI"

def hacim_ve_likidite_kontrolu(df):
    try:
        ortalama_hacim = df['volume'].rolling(window=20).mean().iloc[-1]
        son_hacim = df['volume'].iloc[-1]
        return son_hacim >= (ortalama_hacim * 0.10)
    except Exception:
        return True

def tum_emirleri_iptal_et(symbol):
    try:
        acik_emirler = exchange.fetch_open_orders(symbol)
        for emir in acik_emirler:
            try:
                exchange.cancel_order(emir['id'], symbol)
            except Exception:
                pass
    except Exception:
        pass

    try:
        exchange.cancel_all_orders(symbol)
    except Exception:
        pass

def telegram_mesaj_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": mesaj, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"⚠️ Telegram Gönderme Hatası: {e}", flush=True)

@app.route('/')
def home():
    return f"Gelişmiş Dinamik Hedefli Hibrit Bot Aktif | Aktif Pozisyon: {len(AKTIF_GRID_SISTEMLERI)}"

def set_leverage_and_margin_safely(symbol, leverage):
    try:
        exchange.set_leverage(leverage, symbol)
        try:
            exchange.set_margin_mode('isolated', symbol)
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"⚠️ Kaldıraç/Margin ayarlama hatası ({symbol}): {e}", flush=True)
        return False

# ==================== TELEGRAM KOMUTLARI ====================
async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        balance = exchange.fetch_balance()
        total = float(balance['total'].get('USDT', 0))
        
        try:
            raw_positions = exchange.fetch_positions()
            borsa_poslari = [p for p in raw_positions if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
        except Exception:
            borsa_poslari = []

        toplam_pnl = sum(float(p.get('unrealizedPnl', 0)) for p in borsa_poslari)
        pnl_ikon = "🟢" if toplam_pnl >= 0 else "🔴"

        basarili_sayisi = ANALitik_HAFIZA.get("basarili_islem_sayisi", 0)
        basarisiz_sayisi = ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0)
        toplam_islem = basarili_sayisi + basarisiz_sayisi
        basari_orani = (basarili_sayisi / toplam_islem * 100) if toplam_islem > 0 else 0.0

        ai_durum = f"Aktif (Eğitildi - {len(ANALitik_HAFIZA.get('egitim_verileri', []))} veri)" if ai_model_egitildi else f"Veri Bekliyor ({len(ANALitik_HAFIZA.get('egitim_verileri', []))}/30)"

        pozisyon_detaylari = ""
        if borsa_poslari:
            pozisyon_detaylari = "\n🔍 *Açık Pozisyonlar (Dinamik Hedef & Trailing):*\n"
            for p in borsa_poslari:
                sym = p.get('symbol', 'Bilinmeyen')
                yon = str(p.get('side', '')).upper()
                pnl_val = float(p.get('unrealizedPnl', 0))
                roe_val = float(p.get('percentage', 0))
                hedef_str = f"%{AKTIF_GRID_SISTEMLERI.get(sym, {}).get('hedef_oran_fiyat', 0.02)*100:.0f}"
                trailing_durum = "Aktif 🛡️" if sym in AKTIF_GRID_SISTEMLERI and AKTIF_GRID_SISTEMLERI[sym].get("trailing_aktif") else "Bekliyor ⏳"
                pozisyon_detaylari += f"• `{sym}` | {yon} | Hedef: {hedef_str} | PnL: `{pnl_val:+.2f} USDT` (`%{roe_val:.2f}`) | Trail: {trailing_durum}\n"
        else:
            pozisyon_detaylari = "\n🔍 *Açık Pozisyon:* `Yok`\n"

        mesaj = (
            f"📊 *DİNAMİK HEDEFLİ & TRAILING BOT*\n\n"
            f"💰 Toplam Kasa: `{total:.2f} USDT`\n"
            f"{pnl_ikon} Anlık Kâr/Zarar: `{toplam_pnl:+.2f} USDT`\n"
            f"🤖 Yapay Zeka: `{ai_durum}`\n"
            f"📌 Açık Pozisyon Sayısı: `{len(borsa_poslari)} / {MAKSIMUM_TOPLAM_POZISYON}`\n"
            f"{pozisyon_detaylari}\n"
            f"🎯 *İstatistikler:*\n"
            f"✅ Kâr (`TP`): `{basarili_sayisi}` | ❌ Zarar (`Stop`): `{basarisiz_sayisi}`\n"
            f"📈 Başarı Oranı: `%{basari_orani:.1f}`\n"
        )
        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = True
    await update.message.reply_text("🟢 *Dinamik Hedefli Bot Aktif Edildi!*", parse_mode='Markdown')

async def durdur_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ *Bot durduruldu.*", parse_mode='Markdown')

async def kapat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 *Tüm pozisyonlar kapatılıyor...*", parse_mode='Markdown')
    try:
        for pos in exchange.fetch_positions():
            kontrat = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
            if kontrat > 0:
                symbol = pos['symbol']
                yon = str(pos.get('side', '')).upper()
                kapatma_yonu = 'sell' if yon == 'LONG' else 'buy'
                tum_emirleri_iptal_et(symbol)
                exchange.create_order(symbol, 'market', kapatma_yonu, kontrat, None, {'reduce_only': True})
                telegram_mesaj_gonder(f"🛑 *MANUEL KAPATMA* - `{symbol}` pozisyonu kapatıldı.")
        
        AKTIF_GRID_SISTEMLERI.clear()
        hafizayi_kaydet()
        await update.message.reply_text("✅ Tüm pozisyonlar ve hafıza temizlendi.", parse_mode='Markdown')
    except Exception as e:
        AKTIF_GRID_SISTEMLERI.clear()
        hafizayi_kaydet()
        await update.message.reply_text(f"✅ Hafıza temizlendi. (Not: {e})", parse_mode='Markdown')

# ==================== ARKA PLAN TARAYICI ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, ANALitik_HAFIZA
    print("🚀 [BAŞLANGIÇ] Dinamik Kâr Hedefi (%1 veya %2) ve Trailing Modu Devrede.", flush=True)
    try:
        exchange.load_markets()
        yapay_zekayi_egit_ve_guncelle()
    except Exception as e:
        print(f"⚠️ Piyasalar yüklenemedi: {e}", flush=True)
    
    derinlik_kodlar = {"ALICI_BASKIN": 1, "DENGELI": 0, "SATICI_BASKIN": -1}
    tur_sayaci = 0
    
    while True:
        try:
            tur_sayaci += 1
            zaman_str = datetime.now().strftime('%H:%M:%S')
            print(f"\n==================================================", flush=True)
            print(f"[{zaman_str}] 🔄 DİNAMİK HEDEF & YÖNETİM TURU (Tur #{tur_sayaci})", flush=True)
            print(f"==================================================", flush=True)

            if not BOT_CALISIYOR_MU:
                print(f"[{zaman_str}] ⏸️ Bot durdurulmuş durumda (Bekliyor).", flush=True)
                time.sleep(5)
                continue

            try:
                raw_positions = exchange.fetch_positions()
                aktif_borsa_map = {}
                for p in raw_positions:
                    kontrat = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                    if kontrat > 0:
                        aktif_borsa_map[p['symbol']] = p
                print(f"📌 Borsa Üzerindeki Aktif Pozisyon Sayısı: {len(aktif_borsa_map)}", flush=True)
            except Exception as e:
                print(f"⚠️ Pozisyonlar çekilirken hata: {e}", flush=True)
                aktif_borsa_map = {}

            # --- AÇIK POZİSYONLAR İÇİN TRAILING STOP VE GÜNCEL KONTROL ---
            for sym, kayitli_veri in list(AKTIF_GRID_SISTEMLERI.items()):
                if sym in aktif_borsa_map:
                    poz = aktif_borsa_map[sym]
                    try:
                        guncel_fiyat = exchange.fetch_ticker(sym)['last']
                        giris_fiyati = float(poz.get('entryPrice', kayitli_veri.get('giris_fiyati', guncel_fiyat)))
                        yon = kayitli_veri.get("yon", "LONG")
                        
                        if yon == "LONG":
                            fiyat_farki_yuzde = ((guncel_fiyat - giris_fiyati) / giris_fiyati) * 100
                        else:
                            fiyat_farki_yuzde = ((giris_fiyati - guncel_fiyat) / giris_fiyati) * 100

                        # Eğer fiyat lehimize %0.8 üstüne çıktıysa ve Trailing aktifleşmediyse -> Stop'u Giriş Fiyata Çek
                        if fiyat_farki_yuzde >= 0.8 and not kayitli_veri.get("trailing_aktif", False):
                            kayitli_veri["trailing_aktif"] = True
                            tum_emirleri_iptal_et(sym)
                            
                            kapatma_yonu = 'sell' if yon == 'LONG' else 'buy'
                            miktar = float(poz.get('contracts', 0) or poz.get('size', 0))
                            
                            breakeven_fiyat = float(exchange.price_to_precision(sym, giris_fiyati))
                            stop_params = {
                                'stopPrice': breakeven_fiyat,
                                'triggerPrice': breakeven_fiyat,
                                'reduceOnly': True,
                                'price_type': 'mark_price'
                            }
                            try:
                                exchange.create_order(sym, 'stop', kapatma_yonu, miktar, breakeven_fiyat, stop_params)
                            except Exception:
                                exchange.create_order(sym, 'stop_market', kapatma_yonu, miktar, breakeven_fiyat, stop_params)

                            hedef_fiyat = kayitli_veri.get("hedef_fiyat")
                            if hedef_fiyat:
                                exchange.create_order(sym, 'limit', kapatma_yonu, miktar, hedef_fiyat, {'reduceOnly': True})

                            hafizayi_kaydet()
                            print(f"🛡️ [TRAILING AKTİF] {sym} kârda ilerledi, stop giriş fiyatına sabitlendi!", flush=True)
                            telegram_mesaj_gonder(f"🛡️ *KÂR KİLİTLENDİ (Trailing)*\n📌 `{sym}` pozisyonu kârda ilerlediği için stop seviyesi giriş fiyatına çekildi.")
                    except Exception as ex:
                        print(f"⚠️ Trailing yönetimi hatası ({sym}): {ex}", flush=True)

            # --- KAPATILAN POZİSYONLARI KONTROL ETME ---
            for sym in list(AKTIF_GRID_SISTEMLERI.keys()):
                if sym not in aktif_borsa_map:
                    print(f"🔍 Pozisyon Kapandı Algılandı: {sym} kontrol ediliyor...", flush=True)
                    kayitli_veri = AKTIF_GRID_SISTEMLERI.pop(sym)
                    
                    basarili_islem = False
                    try:
                        tum_emirleri_iptal_et(sym)
                        my_trades = exchange.fetch_my_trades(sym, limit=3)
                        if my_trades:
                            son_trade = my_trades[-1]
                            info = son_trade.get('info', {})
                            realized_pnl = float(info.get('pnl', 0) or info.get('profit', 0) or 0)
                            
                            if realized_pnl != 0:
                                basarili_islem = realized_pnl > 0
                            else:
                                trade_fiyat = float(son_trade.get('price', 0))
                                giris_fiyat = float(kayitli_veri.get('giris_fiyati', trade_fiyat))
                                yon = kayitli_veri.get("yon", "LONG")
                                
                                if yon == "LONG":
                                    basarili_islem = trade_fiyat > giris_fiyat
                                else:
                                    basarili_islem = trade_fiyat < giris_fiyat
                    except Exception as e:
                        print(f"⚠️ Geçmiş PnL okunurken hata ({sym}): {e}", flush=True)

                    if basarili_islem:
                        ANALitik_HAFIZA["basarili_islem_sayisi"] = ANALitik_HAFIZA.get("basarili_islem_sayisi", 0) + 1
                        telegram_mesaj_gonder(f"🎉 *Kâr Alındı (Dinamik Hedef)*\n📌 Coindaki pozisyon başarıyla kapandı: `{sym}` 🟢")
                    else:
                        ANALitik_HAFIZA["basarisiz_islem_sayisi"] = ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0) + 1
                        telegram_mesaj_gonder(f"❌ *Pozisyon Kapandı (Stop/Trailing)*\n📌 Coindaki pozisyon kapandı: `{sym}` 🔴")

                    COIN_COOLDOWNLAR[sym] = time.time() + COOLDOWN_SURESI_SANIYE
                    
                    rsi_v = kayitli_veri.get("giris_rsi", 50)
                    adx_v = kayitli_veri.get("giris_adx", 25)
                    ema_f = kayitli_veri.get("ema_fark", 0.0)
                    atr_y = kayitli_veri.get("atr_yuzde", 1.5)
                    yon_k = 1 if kayitli_veri.get("yon", "LONG") == "LONG" else -1
                    c_id = COIN_ID_MAP.get(sym, 0)
                    derinlik_v = derinlik_kodlar.get(kayitli_veri.get("derinlik_durumu", "DENGELI"), 0)
                    hacim_v = kayitli_veri.get("hacim_orani", 1.0)
                    degisim_v = kayitli_veri.get("fiyat_degisim", 0.0)
                    sonuc_k = 1 if basarili_islem else 0

                    ANALitik_HAFIZA["egitim_verileri"].append([
                        rsi_v, adx_v, ema_f, yon_k, atr_y, c_id, derinlik_v, hacim_v, degisim_v, sonuc_k
                    ])
                    if len(ANALitik_HAFIZA["egitim_verileri"]) > 150:
                        ANALitik_HAFIZA["egitim_verileri"].pop(0)
                    
                    yapay_zekayi_egit_ve_guncelle()
                    hafizayi_kaydet()

            # --- TÜM COİNLERİ TARAYIP MULTI-TIMEFRAME VE MARJ ONAYI ARAMA ---
            taranan_sinyaller = []
            su_anki_zaman = time.time()

            print(f"👀 Takip edilen {len(TAKIP_EDILENLER)} coin taraniyor...", flush=True)
            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU:
                    break
                if symbol in aktif_borsa_map:
                    continue

                bitis_zamani = COIN_COOLDOWNLAR.get(symbol, 0)
                if su_anki_zaman < bitis_zamani:
                    continue

                try:
                    guncel_fiyat = exchange.fetch_ticker(symbol)['last']
                    
                    ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
                    df_15m = pd.DataFrame(ohlcv_15m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    
                    ohlcv_1h = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=30)
                    df_1h = pd.DataFrame(ohlcv_1h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    
                    if not hacim_ve_likidite_kontrolu(df_15m):
                        continue

                    ema7_1h = ta.trend.ema_indicator(df_1h['close'], window=7).iloc[-1]
                    ema21_1h = ta.trend.ema_indicator(df_1h['close'], window=21).iloc[-1]
                    trend_1h_boga = ema7_1h > ema21_1h

                    ema7 = ta.trend.ema_indicator(df_15m['close'], window=7).iloc[-1]
                    ema21 = ta.trend.ema_indicator(df_15m['close'], window=21).iloc[-1]
                    rsi = ta.momentum.rsi(df_15m['close'], window=14).iloc[-1]
                    
                    adx_indicator = ta.trend.ADXIndicator(df_15m['high'], df_15m['low'], df_15m['close'], window=14)
                    adx_val = adx_indicator.adx().iloc[-1]
                    plus_di = adx_indicator.adx_pos().iloc[-1]
                    minus_di = adx_indicator.adx_neg().iloc[-1]
                    
                    atr_yuzdesi = atr_ve_volatilite_hesapla(df_15m)
                    fiyat_10_mum_once = df_15m['close'].iloc[-10]
                    degisim_yuzdesi = ((guncel_fiyat - fiyat_10_mum_once) / fiyat_10_mum_once) * 100
                    derinlik_durumu = emir_defteri_derinlik_analizi(symbol)

                    son_mum = df_15m.iloc[-1]
                    m_acilis = son_mum['open']
                    m_kapanis = son_mum['close']
                    
                    long_onayli = (rsi < 48) and (m_kapanis >= m_acilis or degisim_yuzdesi <= -0.8) and trend_1h_boga
                    short_onayli = (rsi > 52) and (m_kapanis <= m_acilis or degisim_yuzdesi >= 0.8) and (not trend_1h_boga)

                except Exception as e:
                    print(f"      ❌ [{symbol}] Veri çekme hatası: {e}", flush=True)
                    continue

                sinyal_puani = 50
                grid_yonu = "LONG"

                guclu_trend_var = adx_val >= 25
                trend_yonu_boga = plus_di > minus_di

                if degisim_yuzdesi >= 1.2 and short_onayli:
                    grid_yonu = "SHORT"
                    sinyal_puani = 92
                elif degisim_yuzdesi <= -1.2 and long_onayli:
                    grid_yonu = "LONG"
                    sinyal_puani = 92
                elif guclu_trend_var and ((trend_yonu_boga and long_onayli) or (not trend_yonu_boga and short_onayli)):
                    grid_yonu = "LONG" if trend_yonu_boga else "SHORT"
                    sinyal_puani = 78
                else:
                    continue

                is_altin_atis = sinyal_puani >= 90
                ema_fark_val = float(ema7 - ema21)
                yon_kod = 1 if grid_yonu == 'LONG' else -1
                coin_id = COIN_ID_MAP.get(symbol, 0)
                derinlik_val = derinlik_kodlar.get(derinlik_durumu, 0)
                
                ortalama_hacim = df_15m['volume'].rolling(window=20).mean().iloc[-1]
                son_hacim = df_15m['volume'].iloc[-1]
                hacim_orani = float(son_hacim / ortalama_hacim) if ortalama_hacim > 0 else 1.0
                
                if not yapay_zeka_islem_onayi(rsi, adx_val, ema_fark_val, yon_kod, atr_yuzdesi, coin_id, derinlik_val, hacim_orani, degisim_yuzdesi, sinyal_puani):
                    continue

                taranan_sinyaller.append({
                    "symbol": symbol,
                    "puan": sinyal_puani,
                    "yon": grid_yonu,
                    "rsi": rsi,
                    "adx": adx_val,
                    "ema_fark": ema_fark_val,
                    "fiyat": guncel_fiyat,
                    "atr": atr_yuzdesi,
                    "altin_atis": is_altin_atis,
                    "derinlik_durumu": derinlik_durumu,
                    "hacim_orani": hacim_orani,
                    "fiyat_degisim": degisim_yuzdesi
                })

            taranan_sinyaller.sort(key=lambda x: x["puan"], reverse=True)

            # --- İŞLEM AÇMA ---
            for sinyal in taranan_sinyaller:
                if not BOT_CALISIYOR_MU:
                    break

                symbol = sinyal["symbol"]
                grid_yonu = sinyal["yon"]
                sinyal_puani = sinyal["puan"]
                rsi = sinyal["rsi"]
                adx_val = sinyal["adx"]
                ema_fark_val = sinyal["ema_fark"]
                guncel_fiyat = sinyal["fiyat"]
                atr_yuzdesi = sinyal["atr"]
                is_altin_atis = sinyal["altin_atis"]
                derinlik_durumu = sinyal["derinlik_durumu"]
                hacim_orani = sinyal["hacim_orani"]
                degisim_yuzdesi = sinyal["fiyat_degisim"]

                if sinyal_puani < 70 and not is_altin_atis:
                    continue

                izin_verilen_maks_poz = MAKSIMUM_TOPLAM_POZISYON + 1 if is_altin_atis else MAKSIMUM_TOPLAM_POZISYON

                if len(aktif_borsa_map) >= izin_verilen_maks_poz:
                    break

                ayni_yon_sayisi = sum(1 for p in aktif_borsa_map.values() if str(p.get('side', '')).upper() == grid_yonu)
                if ayni_yon_sayisi >= MAKSIMUM_AYNI_YON_SAYISI:
                    continue 

                dinamik_kaldirac = 5 if atr_yuzdesi > 2.5 else 10
                kasa_orani = 0.20

                try:
                    balance = exchange.fetch_balance()
                    toplam_bakiye = float(balance['total'].get('USDT', 0))
                except Exception as e:
                    continue

                if not set_leverage_and_margin_safely(symbol, dinamik_kaldirac):
                    continue
                
                hedef_marjin = toplam_bakiye * kasa_orani
                hedef_pozisyon_usdt = hedef_marjin * dinamik_kaldirac
                ham_miktar = hedef_pozisyon_usdt / guncel_fiyat

                try:
                    market_info = exchange.market(symbol)
                    contract_size = float(market_info.get('contractSize', 1.0))
                    min_amount = float(market_info['limits']['amount']['min'] or 1.0)
                    
                    hesaplanan_kontrat = ham_miktar / contract_size
                    gercek_ham_miktar = max(round(hesaplanan_kontrat), min_amount)
                    miktar = float(exchange.amount_to_precision(symbol, gercek_ham_miktar))
                    
                    tum_emirleri_iptal_et(symbol)

                    emir_yonu = 'buy' if grid_yonu == 'LONG' else 'sell'
                    exchange.create_order(symbol, 'market', emir_yonu, miktar)

                    giris_fiyati = guncel_fiyat
                    for _ in range(3):
                        try:
                            time.sleep(0.2)
                            pozlar = exchange.fetch_positions()
                            for p in pozlar:
                                if p['symbol'] == symbol and float(p.get('contracts', 0) or p.get('size', 0)) > 0:
                                    giris_fiyati = float(p.get('entryPrice', guncel_fiyat))
                                    break
                            if giris_fiyati != guncel_fiyat:
                                break
                        except Exception:
                            pass

                    # ==========================================
                    # DİNAMİK HEDEF SEÇİMİ (%1 veya %2)
                    # ==========================================
                    # Oynaklık düşükse veya fiyat hareketi yatay/orta seviyedeyse %1 hedefleyip seri kâr al,
                    # Güçlü trend ve yüksek momentum varsa %2 hedefle.
                    if atr_yuzdesi < 1.2 or abs(degisim_yuzdesi) < 1.5:
                        hedef_oran_fiyat = 0.01  # Net %1 Fiyat Hedefi
                        hedef_roe = 10.0         # %10 ROE (10x kaldıraçla)
                    else:
                        hedef_oran_fiyat = 0.02  # Net %2 Fiyat Hedefi
                        hedef_roe = 20.0         # %20 ROE

                    guvenli_stop_yuzdesi = max(atr_yuzdesi * 1.5, 1.0)
                    stop_oran_fiyat = guvenli_stop_yuzdesi / 100.0

                    if grid_yonu == 'LONG':
                        stop_fiyat = giris_fiyati * (1.0 - stop_oran_fiyat)
                        hedef_fiyat = giris_fiyati * (1.0 + hedef_oran_fiyat)
                        kapatma_yonu = 'sell'
                    else:
                        stop_fiyat = giris_fiyati * (1.0 + stop_oran_fiyat)
                        hedef_fiyat = giris_fiyati * (1.0 - hedef_oran_fiyat)
                        kapatma_yonu = 'buy'
                        
                    stop_fiyat = float(exchange.price_to_precision(symbol, stop_fiyat))
                    hedef_fiyat = float(exchange.price_to_precision(symbol, hedef_fiyat))

                    stop_params = {
                        'stopPrice': stop_fiyat,
                        'triggerPrice': stop_fiyat,
                        'reduceOnly': True,
                        'price_type': 'mark_price'
                    }
                    
                    try:
                        exchange.create_order(symbol, 'stop', kapatma_yonu, miktar, stop_fiyat, stop_params)
                    except Exception:
                        exchange.create_order(symbol, 'stop_market', kapatma_yonu, miktar, stop_fiyat, stop_params)

                    hedef_params = {
                        'reduceOnly': True
                    }
                    exchange.create_order(symbol, 'limit', kapatma_yonu, miktar, hedef_fiyat, hedef_params)

                    AKTIF_GRID_SISTEMLERI[symbol] = {
                        "yon": grid_yonu,
                        "giris_rsi": rsi,
                        "giris_adx": adx_val,
                        "ema_fark": ema_fark_val,
                        "atr_yuzde": atr_yuzdesi,
                        "hedef_roe": hedef_roe,
                        "stop_roe": guvenli_stop_yuzdesi * dinamik_kaldirac,
                        "giris_fiyati": giris_fiyati,
                        "derinlik_durumu": derinlik_durumu,
                        "hacim_orani": hacim_orani,
                        "fiyat_degisim": degisim_yuzdesi,
                        "hedef_fiyat": hedef_fiyat,
                        "hedef_oran_fiyat": hedef_oran_fiyat,
                        "trailing_aktif": False
                    }
                    hafizayi_kaydet()
                    
                    islem_tipi_str = f"🌟 Dinamik Hedef (%{hedef_oran_fiyat*100:.0f}) + Trailing"

                    print(f"🚀 İŞLEM AÇILDI: {symbol} | Kaldıraç: {dinamik_kaldirac}x | Hedef: %{hedef_oran_fiyat*100:.0f} | Giriş: {giris_fiyati} | TP: {hedef_fiyat}", flush=True)
                    telegram_mesaj_gonder(
                        f"🛡️ *DİNAMİK HEDEFLİ İŞLEM AÇILDI*\n\n"
                        f"📌 *Coin:* `{symbol}` | 📊 *Yön:* `{grid_yonu}`\n"
                        f"🎯 *İşlem Türü:* `{islem_tipi_str}`\n"
                        f"📈 *Sinyal Puanı:* `{sinyal_puani} / 100`\n"
                        f"⚙️ *Dinamik Kaldıraç:* `{dinamik_kaldirac}x`\n"
                        f"🎯 *Hedef TP:* `+{hedef_roe:.0f}% ROE` (`{hedef_fiyat}` - Net %{hedef_oran_fiyat*100:.0f})\n"
                        f"🛑 *Stop SL:* `-{guvenli_stop_yuzdesi*dinamik_kaldirac:.1f}% ROE` (`{stop_fiyat}`)\n"
                        f"⚡ *Özellik:* `%0.8 kârda stop otomatik giriş fiyatına kilitlenecek.`"
                    )
                    break 
                except Exception as e:
                    print(f"❌ Emir açma hatası ({symbol}): {e}", flush=True)

        except Exception as e:
            print(f"⚠️ Tarayıcı döngü genel hatası: {e}", flush=True)
            
        time.sleep(10)

def flask_web_server():
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))

if __name__ == '__main__':
    threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True).start()
    threading.Thread(target=flask_web_server, daemon=True).start()
    
    app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app_tg.add_handler(CommandHandler("durum", durum_komutu))
    app.add_handler if hasattr(app_tg, 'add_handler') else None
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))
    
    app_tg.run_polling()
