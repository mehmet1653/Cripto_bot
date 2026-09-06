import time
import threading
import requests
import ccxt
import pandas as pd
import ta
import os
import numpy as np
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler
from sklearn.ensemble import RandomForestClassifier
from supabase import create_client, Client

# Python loglarının tamponda beklemeden anında ekrana düşmesi için:
import sys
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

TAKIP_EDILENLER_YEDEK = [
    'SOL/USDT:USDT', 'AVAX/USDT:USDT', 'XRP/USDT:USDT', 'DOGE/USDT:USDT', 'SUI/USDT:USDT',
    'BTC/USDT:USDT', 'ETH/USDT:USDT', 'ADA/USDT:USDT', 'LINK/USDT:USDT', 'NEAR/USDT:USDT'
]

BOT_CALISIYOR_MU = True
ADAY_SINYALLER = {} 

# ==================== SUPABASE HAFIZA FONKSİYONLARI ====================
def hafizayi_yukle():
    try:
        response = supabase.table("bot_hafiza").select("*").eq("id", 1).execute()
        if response.data and len(response.data) > 0:
            veri = response.data[0]
            print("💾 Hafıza Supabase'den başarıyla yüklendi.", flush=True)
            return {
                "aktif_sistemler": veri.get("aktif_sistemler", {}),
                "analitik": veri.get("analitik", {
                    "basarisiz_analizler": [],
                    "basarili_islem_sayisi": 0,
                    "basarisiz_islem_sayisi": 0,
                    "gunluk_net_kar_usd": 0.0,
                    "egitim_verileri": []
                })
            }
    except Exception as e:
        print(f"⚠️ Hafıza yükleme hatası: {e}", flush=True)
        
    varsayilan = {
        "aktif_sistemler": {},
        "analitik": {
            "basarisiz_analizler": [],
            "basarili_islem_sayisi": 0,
            "basarisiz_islem_sayisi": 0,
            "gunluk_net_kar_usd": 0.0,
            "egitim_verileri": []
        }
    }
    try:
        supabase.table("bot_hafiza").upsert({"id": 1, **varsayilan}).execute()
    except Exception:
        pass
    return varsayilan

def hafizayi_kaydet():
    try:
        supabase.table("bot_hafiza").upsert({
            "id": 1,
            "aktif_sistemler": AKTIF_GRID_SISTEMLERI,
            "analitik": ANALitik_HAFIZA
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
COIN_COOLDOWNLAR = {}

MAKSIMUM_NORMAL_POZISYON = 3
MAKSIMUM_ALTIN_ATIS_POZISYON = 2
COOLDOWN_SURESI_SANIYE = 15 * 60

# ==================== YAPAY ZEKA MODELİ ====================
ai_model = RandomForestClassifier(n_estimators=100, max_depth=6, random_state=42)
ai_model_egitildi = False

def yapay_zekayi_egit_ve_guncelle():
    global ai_model, ai_model_egitildi
    veriler = ANALitik_HAFIZA.get("egitim_verileri", [])
    
    if len(veriler) < 50:
        ai_model_egitildi = False
        return

    try:
        X = [item[:5] for item in veriler]
        y = [item[5] for item in veriler]
        if len(set(y)) < 2:
            ai_model_egitildi = False
            return
            
        ai_model.fit(np.array(X), np.array(y))
        ai_model_egitildi = True
    except Exception as e:
        print(f"⚠️ Yapay zeka eğitim hatası: {e}", flush=True)
        ai_model_egitildi = False

def yapay_zeka_islem_onayi(rsi, adx, ema_fark, yon_kod, atr_yuzde):
    if not ai_model_egitildi:
        return True
    try:
        olasiliklar = ai_model.predict_proba(np.array([[rsi, adx, ema_fark, yon_kod, atr_yuzde]]))[0]
        classes = list(ai_model.classes_)
        basari_ihtimali = olasiliklar[classes.index(1)] if 1 in classes else 1.0
        return basari_ihtimali >= 0.55
    except Exception:
        return True

def atr_ve_volatilite_hesapla(df, period=14):
    try:
        atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=period).average_true_range().iloc[-1]
        fiyat = df['close'].iloc[-1]
        return float((atr / fiyat) * 100)
    except Exception:
        return float(1.5)

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
        return son_hacim >= (ortalama_hacim * 0.15)
    except Exception:
        return True

def dinamik_coin_havuzu_getir():
    try:
        tickers = exchange.fetch_tickers()
        usdt_swap_tickers = {sym: data for sym, data in tickers.items() if '/USDT:USDT' in sym or sym.endswith(':USDT')}
        sorted_tickers = sorted(usdt_swap_tickers.items(), key=lambda x: x[1].get('quoteVolume', 0) or 0, reverse=True)
        top_coins = [item[0] for item in sorted_tickers[:40]]
        if not top_coins:
            return TAKIP_EDILENLER_YEDEK
        return top_coins
    except Exception as e:
        print(f"⚠️ Dinamik havuz çekilemedi, yedek liste kullanılıyor: {e}", flush=True)
        return TAKIP_EDILENLER_YEDEK

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
    return f"Efsanevi Hibrit Bot Aktif | Aktif Pozisyon: {len(AKTIF_GRID_SISTEMLERI)}"

def pozisyonu_garantili_kapat(symbol, yon, miktar, sebep_mesaji, rsi=50, adx=25, ema_fark=0.0, atr_yuzde=1.5, basarili=True):
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
        kapatma_yonu = 'sell' if yon == 'LONG' else 'buy'
        exchange.create_order(symbol, 'market', kapatma_yonu, miktar, None, {'reduce_only': True})
    except Exception as e:
        print(f"⚠️ Kapatma API hatası: {e}", flush=True)

    COIN_COOLDOWNLAR[symbol] = time.time() + COOLDOWN_SURESI_SANIYE

    yon_kod = 1 if yon == 'LONG' else -1
    sonuc_kod = 1 if basarili else 0
    
    ANALitik_HAFIZA["egitim_verileri"].append([rsi, adx, ema_fark, yon_kod, atr_yuzde, sonuc_kod])
    if len(ANALitik_HAFIZA["egitim_verileri"]) > 300:
        ANALitik_HAFIZA["egitim_verileri"].pop(0)
    yapay_zekayi_egit_ve_guncelle()

    if symbol in AKTIF_GRID_SISTEMLERI:
        del AKTIF_GRID_SISTEMLERI[symbol]
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
            borsa_poslari = [p for p in raw_positions if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
        except Exception:
            borsa_poslari = []

        toplam_pnl = sum(float(p.get('unrealizedPnl', 0)) for p in borsa_poslari)
        pnl_ikon = "🟢" if toplam_pnl >= 0 else "🔴"

        basarili_sayisi = ANALitik_HAFIZA.get("basarili_islem_sayisi", 0)
        basarisiz_sayisi = ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0)
        toplam_islem = basarili_sayisi + basarisiz_sayisi
        basari_orani = (basarili_sayisi / toplam_islem * 100) if toplam_islem > 0 else 0.0

        pozisyon_detaylari = ""
        if borsa_poslari:
            pozisyon_detaylari = "\n🔍 *Açık Pozisyonlar:*\n"
            for p in borsa_poslari:
                sym = p.get('symbol', 'Bilinmeyen')
                yon = str(p.get('side', '')).upper()
                pnl_val = float(p.get('unrealizedPnl', 0))
                roe_val = float(p.get('percentage', 0))
                kaldirac_degeri = int(p.get('leverage', 10))
                pozisyon_detaylari += f"• `{sym}` | {yon} ({kaldirac_degeri}x Kaldıraç) | PnL: `{pnl_val:+.2f} USDT` (`%{roe_val:.2f}`)\n"
        else:
            pozisyon_detaylari = "\n🔍 *Açık Pozisyon:* `Yok`\n"

        mesaj = (
            f"📊 *EFSANEVİ HİBRİT BOT DURUMU*\n\n"
            f"💰 Toplam Kasa: `{total:.2f} USDT`\n"
            f"{pnl_ikon} Anlık Kâr/Zarar: `{toplam_pnl:+.2f} USDT`\n"
            f"📌 Açık Pozisyon Sayısı: `{len(borsa_poslari)}`\n"
            f"{pozisyon_detaylari}\n"
            f"🎯 *İstatistikler:*\n"
            f"✅ Başarılı (TP): `{basarili_sayisi}` | ❌ Başarısız (SL): `{basarisiz_sayisi}`\n"
            f"📈 Başarı Oranı: `%{basari_orani:.1f}`\n"
        )
        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = True
    await update.message.reply_text("🟢 *Efsanevi Bot Aktif Edildi!*", parse_mode='Markdown')

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
                pozisyonu_garantili_kapat(pos['symbol'], str(pos.get('side', '')).upper(), kontrat, f"🛑 *MANUEL KAPATMA* - `{pos['symbol']}`", basarili=False)
        AKTIF_GRID_SISTEMLERI.clear()
        hafizayi_kaydet()
        await update.message.reply_text("✅ Tüm pozisyonlar ve hafıza temizlendi.", parse_mode='Markdown')
    except Exception as e:
        AKTIF_GRID_SISTEMLERI.clear()
        hafizayi_kaydet()
        await update.message.reply_text(f"✅ Hafıza temizlendi. (Not: {e})", parse_mode='Markdown')

# ==================== ARKA PLAN TARAYICI ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, ANALitik_HAFIZA, ADAY_SINYALLER
    print("🚀 Efsanevi Dinamik Havuz ve Çoklu Teyit Tarayıcısı Devrede.", flush=True)
    try:
        exchange.load_markets()
        yapay_zekayi_egit_ve_guncelle()
    except Exception as e:
        print(f"⚠️ Piyasalar yüklenemedi: {e}", flush=True)
    
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(3)
                continue

            try:
                raw_positions = exchange.fetch_positions()
                aktif_borsa_map = {}
                for p in raw_positions:
                    kontrat = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                    if kontrat > 0:
                        aktif_borsa_map[p['symbol']] = p
            except Exception:
                aktif_borsa_map = {}

            # --- AÇIK POZİSYONLARIN VE TP / SL TAKİBİ ---
            for sym in list(AKTIF_GRID_SISTEMLERI.keys()):
                if sym not in aktif_borsa_map:
                    kayitli_veri = AKTIF_GRID_SISTEMLERI[sym]
                    rsi_val = kayitli_veri.get("giris_rsi", 50)
                    adx_val = kayitli_veri.get("giris_adx", 25)
                    ema_fark_val = kayitli_veri.get("ema_fark", 0.0)
                    atr_val = kayitli_veri.get("atr_yuzde", 1.5)
                    yon_val = kayitli_veri.get("yon", "LONG")

                    # Güvenli Kar/Zarar Ayrımı (Hatasız ve akıcı)
                    tp_gerceklesti = False
                    try:
                        islem_gecmisi = exchange.fetch_closed_orders(sym, limit=3)
                        if islem_gecmisi:
                            son_emir = islem_gecmisi[-1]
                            emur_tipi = str(son_emir.get('type', '')).lower()
                            if 'limit' in emur_tipi:
                                tp_gerceklesti = True
                            elif 'stop' in emur_tipi or 'market' in emur_tipi:
                                tp_gerceklesti = False
                    except Exception:
                        tp_gerceklesti = False

                    if tp_gerceklesti:
                        ANALitik_HAFIZA["basarili_islem_sayisi"] += 1
                        print(f"🎯 [{sym}] TP (Kâr Al) Hedefine Ulaşıldı!", flush=True)
                        telegram_mesaj_gonder(f"🎯 *KÂR ALINDI (TP)*\n\n📌 *Coin:* `{sym}`\n📊 Durum: Hedef fiyata ulaşıldı.")
                    else:
                        ANALitik_HAFIZA["basarisiz_islem_sayisi"] += 1
                        print(f"🛑 [{sym}] SL (Zarar Kes) Tetiklendi!", flush=True)
                        telegram_mesaj_gonder(f"🛑 *ZARAR KESİLDİ (SL)*\n\n📌 *Coin:* `{sym}`\n📊 Durum: Stop seviyesine ulaşıldı.")
                        try:
                            for ord_item in exchange.fetch_open_orders(sym):
                                exchange.cancel_order(ord_item['id'], sym)
                        except Exception:
                            pass

                    COIN_COOLDOWNLAR[sym] = time.time() + COOLDOWN_SURESI_SANIYE
                    yon_kod = 1 if yon_val == 'LONG' else -1
                    sonuc_kod = 1 if tp_gerceklesti else 0

                    ANALitik_HAFIZA["egitim_verileri"].append([rsi_val, adx_val, ema_fark_val, yon_kod, atr_val, sonuc_kod])
                    if len(ANALitik_HAFIZA["egitim_verileri"]) > 300:
                        ANALitik_HAFIZA["egitim_verileri"].pop(0)
                    yapay_zekayi_egit_ve_guncelle()

                    del AKTIF_GRID_SISTEMLERI[sym]
                    hafizayi_kaydet()

            for symbol, pos in aktif_borsa_map.items():
                try:
                    guncel_fiyat = exchange.fetch_ticker(symbol)['last']
                except Exception:
                    continue

                yon = str(pos.get('side', '')).upper()
                merkez = float(pos.get('entryPrice', 0))
                kaldirac_kullanilan = int(pos.get('leverage', 10))
                
                fark = (guncel_fiyat - merkez) / merkez if yon == "LONG" else (merkez - guncel_fiyat) / merkez
                roe = fark * 100 * kaldirac_kullanilan

                kayitli = AKTIF_GRID_SISTEMLERI.get(symbol, {})
                breakeven_yapildi = kayitli.get("breakeven_yapildi", False)

                if not breakeven_yapildi and roe >= 10.0:
                    kayitli["breakeven_yapildi"] = True
                    try:
                        open_orders = exchange.fetch_open_orders(symbol)
                        for ord_item in open_orders:
                            if ord_item.get('info', {}).get('is_stop') or ord_item.get('type') == 'stop_market':
                                trigger_p = float(ord_item.get('triggerPrice') or ord_item.get('stopPrice') or 0)
                                if (yon == 'LONG' and trigger_p < merkez) or (yon == 'SHORT' and trigger_p > merkez):
                                    exchange.cancel_order(ord_item['id'], symbol)
                        
                        kapatma_yonu = 'sell' if yon == 'LONG' else 'buy'
                        stop_params = {
                            'stopPrice': merkez,
                            'triggerPrice': merkez,
                            'reduceOnly': True,
                            'is_stop': True
                        }
                        exchange.create_order(symbol, 'stop_market', kapatma_yonu, float(pos.get('contracts', 0) or pos.get('size', 0)), merkez, stop_params)
                        print(f"🛡️ [{symbol}] Breakeven devrede, stop giriş fiyatına ({merkez}) sabitlendi!", flush=True)
                    except Exception as ex:
                        print(f"⚠️ Breakeven güncelleme hatası: {ex}", flush=True)

                    hafizayi_kaydet()
                    telegram_mesaj_gonder(f"🛡️ *Breakeven Devrede (0 Risk)*\n📌 `{symbol}` stopu giriş fiyatına (`{merkez}`) çekildi!")

            # --- DİNAMİK HAVUZ TARAMASI VE ÇOKLU TEYİT SİSTEMİ ---
            guncel_takip_listesi = dinamik_coin_havuzu_getir()
            taranan_sinyaller = []
            su_anki_zaman = time.time()
            yeni_aday_sinyalleri = {}

            for symbol in guncel_takip_listesi:
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
                    
                    ohlcv_1h = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=25)
                    df_1h = pd.DataFrame(ohlcv_1h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    
                    if not hacim_ve_likidite_kontrolu(df_15m):
                        continue

                    ema7_1h = ta.trend.ema_indicator(df_1h['close'], window=7).iloc[-1]
                    ema21_1h = ta.trend.ema_indicator(df_1h['close'], window=21).iloc[-1]
                    boga_trend_1h = ema7_1h > ema21_1h

                    ema7 = ta.trend.ema_indicator(df_15m['close'], window=7).iloc[-1]
                    ema21 = ta.trend.ema_indicator(df_15m['close'], window=21).iloc[-1]
                    rsi = ta.momentum.rsi(df_15m['close'], window=14).iloc[-1]
                    
                    adx_indicator = ta.trend.ADXIndicator(df_15m['high'], df_15m['low'], df_15m['close'], window=14)
                    adx_val = adx_indicator.adx().iloc[-1]
                    plus_di = adx_indicator.adx_pos().iloc[-1]
                    minus_di = adx_indicator.adx_neg().iloc[-1]
                    
                    atr_yuzdesi = atr_ve_volatilite_hesapla(df_15m)
                    
                    ortalama_hacim = df_15m['volume'].rolling(window=20).mean().iloc[-1]
                    son_hacim = df_15m['volume'].iloc[-1]
                    hacim_carpani = son_hacim / ortalama_hacim if ortalama_hacim > 0 else 1.0

                    fiyat_10_mum_once = df_15m['close'].iloc[-10]
                    degisim_yuzdesi = ((guncel_fiyat - fiyat_10_mum_once) / fiyat_10_mum_once) * 100
                    derinlik_durumu = emir_defteri_derinlik_analizi(symbol)
                except Exception:
                    continue

                sinyal_puani = 50
                grid_yonu = "LONG"

                guclu_trend_var = adx_val >= 30  
                trend_yonu_boga = plus_di > minus_di

                tepe_kosulu = (degisim_yuzdesi >= 2.0 and rsi > 60 and derinlik_durumu == "SATICI_BASKIN" and not boga_trend_1h)
                dip_kosulu = (degisim_yuzdesi <= -2.0 and rsi < 40 and derinlik_durumu == "ALICI_BASKIN" and boga_trend_1h)

                if tepe_kosulu:
                    grid_yonu = "SHORT"
                    sinyal_puani = 90
                elif dip_kosulu:
                    grid_yonu = "LONG"
                    sinyal_puani = 90
                elif guclu_trend_var and adx_val >= 32:
                    grid_yonu = "LONG" if (trend_yonu_boga and boga_trend_1h) else "SHORT"
                    if grid_yonu == "LONG" and boga_trend_1h:
                        sinyal_puani = 85
                    elif grid_yonu == "SHORT" and not boga_trend_1h:
                        sinyal_puani = 85
                    else:
                        sinyal_puani = 60 
                else:
                    grid_yonu = "LONG" if ema7 > ema21 else "SHORT"
                    if grid_yonu == "LONG" and boga_trend_1h and rsi < 45:
                        sinyal_puani += 20
                    elif grid_yonu == "SHORT" and not boga_trend_1h and rsi > 55:
                        sinyal_puani += 20

                if hacim_carpani >= 1.5 and adx_val >= 28:
                    sinyal_puani += 10

                if sinyal_puani >= 95 and adx_val >= 35 and derinlik_durumu in ["ALICI_BASKIN", "SATICI_BASKIN"]:
                    sinyal_puani = 100

                is_altin_atis = (sinyal_puani >= 95)
                ema_fark_val = float(ema7 - ema21)
                yon_kod = 1 if grid_yonu == 'LONG' else -1
                
                ai_onay = yapay_zeka_islem_onayi(rsi, adx_val, ema_fark_val, yon_kod, atr_yuzdesi)

                if not ai_onay or sinyal_puani < 80:
                    continue

                eski_aday = ADAY_SINYALLER.get(symbol)
                if eski_aday and eski_aday["yon"] == grid_yonu:
                    taranan_sinyaller.append({
                        "symbol": symbol,
                        "puan": sinyal_puani,
                        "yon": grid_yonu,
                        "rsi": rsi,
                        "adx": adx_val,
                        "ema_fark": ema_fark_val,
                        "fiyat": guncel_fiyat,
                        "atr": atr_yuzdesi,
                        "altin_atis": is_altin_atis
                    })
                else:
                    yeni_aday_sinyalleri[symbol] = {"yon": grid_yonu, "puan": sinyal_puani}

            ADAY_SINYALLER = yeni_aday_sinyalleri
            taranan_sinyaller.sort(key=lambda x: x["puan"], reverse=True)

            normal_poz_sayisi = sum(1 for p in aktif_borsa_map.values() if not AKTIF_GRID_SISTEMLERI.get(p['symbol'], {}).get('altin_atis', False))
            altin_atis_poz_sayisi = sum(1 for p in aktif_borsa_map.values() if AKTIF_GRID_SISTEMLERI.get(p['symbol'], {}).get('altin_atis', False))

            aktif_long_sayisi = sum(1 for p in aktif_borsa_map.values() if str(p.get('side', '')).upper() == 'LONG')
            aktif_short_sayisi = sum(1 for p in aktif_borsa_map.values() if str(p.get('side', '')).upper() == 'SHORT')

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

                if grid_yonu == 'LONG' and aktif_long_sayisi >= 2:
                    continue
                if grid_yonu == 'SHORT' and aktif_short_sayisi >= 2:
                    continue

                if is_altin_atis:
                    if altin_atis_poz_sayisi >= MAKSIMUM_ALTIN_ATIS_POZISYON:
                        continue
                else:
                    if normal_poz_sayisi >= MAKSIMUM_NORMAL_POZISYON:
                        continue

                dinamik_kaldirac = 20 if is_altin_atis else 10
                kasa_orani = 0.25 if is_altin_atis else 0.20
                
                hedef_roe = 20.0  
                stop_roe = 10.0   

                try:
                    balance = exchange.fetch_balance()
                    toplam_bakiye = float(balance['total'].get('USDT', 0))
                except Exception:
                    continue

                try:
                    exchange.set_leverage(dinamik_kaldirac, symbol)
                except Exception as e:
                    print(f"⚠️ Kaldıraç hatası ({symbol}): {e}", flush=True)
                    continue
                
                hedef_marjin = toplam_bakiye * kasa_orani
                hedef_pozisyon_usdt = hedef_marjin * dinamik_kaldirac
                ham_miktar = hedef_pozisyon_usdt / guncel_fiyat

                try:
                    market_info = exchange.market(symbol)
                    contract_size = float(market_info.get('contractSize', 1.0))
                    min_amount = float(market_info['limits']['amount']['min'] or 1.0)
                    
                    gercek_ham_miktar = max(ham_miktar / contract_size, min_amount)
                    miktar = float(exchange.amount_to_precision(symbol, gercek_ham_miktar))
                    
                    emir_yonu = 'buy' if grid_yonu == 'LONG' else 'sell'
                    
                    exchange.create_order(symbol, 'market', emir_yonu, miktar)

                    giris_fiyati = guncel_fiyat
                    for _ in range(5):
                        try:
                            time.sleep(0.3)
                            pozlar = exchange.fetch_positions()
                            for p in pozlar:
                                if p['symbol'] == symbol and float(p.get('contracts', 0) or p.get('size', 0)) > 0:
                                    giris_fiyati = float(p.get('entryPrice', guncel_fiyat))
                                    break
                            if giris_fiyati != guncel_fiyat:
                                break
                        except Exception:
                            pass
                    
                    stop_oran_fiyat = stop_roe / 100.0 / dinamik_kaldirac
                    hedef_oran_fiyat = hedef_roe / 100.0 / dinamik_kaldirac
                    
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
                        'is_stop': True
                    }
                    exchange.create_order(symbol, 'stop_market', kapatma_yonu, miktar, stop_fiyat, stop_params)

                    hedef_params = {
                        'reduceOnly': True
                    }
                    exchange.create_order(symbol, 'limit', kapatma_yonu, miktar, hedef_fiyat, hedef_params)
                    
                    print(f"🛡️ [{symbol}] Kaldıraç + TP/SL Emirleri İşlendi -> Giriş: {giris_fiyati} | SL: {stop_fiyat} | TP: {hedef_fiyat}", flush=True)

                    AKTIF_GRID_SISTEMLERI[symbol] = {
                        "yon": grid_yonu,
                        "giris_rsi": rsi,
                        "giris_adx": adx_val,
                        "ema_fark": ema_fark_val,
                        "atr_yuzde": atr_yuzdesi,
                        "hedef_roe": hedef_roe,
                        "stop_roe": stop_roe,
                        "breakeven_yapildi": False,
                        "altin_atis": is_altin_atis
                    }
                    hafizayi_kaydet()
                    
                    tur_mesaji = "🔥 *100 PUANLIK ALTIN ATIŞ*" if is_altin_atis else "⚡ *STANDART İŞLEM*"
                    telegram_mesaj_gonder(
                        f"{tur_mesaji} VE BORSA EMRİ GİRİLDİ\n\n"
                        f"📌 *Coin:* `{symbol}` | 📊 *Yön:* `{grid_yonu}`\n"
                        f"⚙️ *Kaldıraç:* `{dinamik_kaldirac}x Kaldıraçlı`\n"
                        f"🎯 *Hedef TP:* `%+{hedef_roe}` | *Stop SL:* `-%{stop_roe}`"
                    )
                    
                    if is_altin_atis:
                        altin_atis_poz_sayisi += 1
                    else:
                        normal_poz_sayisi += 1

                    if grid_yonu == 'LONG':
                        aktif_long_sayisi += 1
                    else:
                        aktif_short_sayisi += 1
                        
                    break 
                except Exception as e:
                    print(f"❌ Emir açma hatası ({symbol}): {e}", flush=True)

        except Exception as e:
            print(f"⚠️ Tarayıcı döngü genel hatası: {e}", flush=True)
            
        time.sleep(5)

def flask_web_server():
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)), debug=False, use_reloader=False)

if __name__ == '__main__':
    threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True).start()
    threading.Thread(target=flask_web_server, daemon=True).start()
    
    app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app_tg.add_handler(CommandHandler("durum", durum_komutu))
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))
    
    app_tg.run_polling()
