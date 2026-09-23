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
KALDIRAC = 3

GLOBAL_COOLDOWN_BITIS = 0.0

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
    
    varsayilan = {
        "aktif_sistemler": {},
        "analitik": {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0, "egitim_verileri": []},
        "cooldownlar": {}
    }
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
COOLDOWN_SURESI_SANIYE = 20 * 60

def piyasa_rejimini_tespit_et():
    print("🌐 [PİYASA] BTC rejimi ve yönü analiz ediliyor...", flush=True)
    try:
        ohlcv_btc = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='1h', limit=30)
        df_btc = pd.DataFrame(ohlcv_btc, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        adx_1h = ta.trend.ADXIndicator(df_btc['high'], df_btc['low'], df_btc['close'], window=14).adx().iloc[-1]
        ema9 = ta.trend.ema_indicator(df_btc['close'], window=9).iloc[-1]
        ema21 = ta.trend.ema_indicator(df_btc['close'], window=21).iloc[-1]
        trend_yonu = "LONG" if ema9 > ema21 else "SHORT"
        if adx_1h < 22.0:
            rejim = "YATAY"
        else:
            rejim = "TREND"
        print(f"🌐 [PİYASA SONUÇ] Rejim: {rejim} | BTC Yön: {trend_yonu} | ADX: {adx_1h:.2f}", flush=True)
        return rejim, trend_yonu
    except Exception as e:
        print(f"⚠️ [PİYASA HATA] Rejim tespit edilemedi: {e}. Varsayılan YATAY/LONG", flush=True)
        return "YATAY", "LONG"

def emir_defteri_ve_seviye_analizi(symbol, anlik_fiyat, ticker_data):
    """
    Emir defteri derinliğini ve 24h tepe/dip noktalarını analiz eder.
    Zirvedeyken Long, dipteyken Short açılmasını engeller.
    """
    try:
        # 1. 24h Tepe / Dip Kontrolü
        high_24h = float(ticker_data.get('high') or anlik_fiyat * 1.02)
        low_24h = float(ticker_data.get('low') or anlik_fiyat * 0.98)
        
        # Eğer fiyat 24 saatlik zirvenin %0.6'lık dilimi içindeyse, dirençtedir -> LONG AÇILMAZ!
        tepeye_yakin_mi = anlik_fiyat >= (high_24h * 0.994)
        # Eğer fiyat 24 saatlik dip seviyenin %0.6'lık dilimi içindeyse, destektedir -> SHORT AÇILMAZ!
        dipe_yakin_mi = anlik_fiyat <= (low_24h * 1.006)

        # 2. Order Book Derinlik Analizi (Alış/Satış Baskısı)
        order_book = exchange.fetch_order_book(symbol, limit=20)
        bids = order_book.get('bids', [])
        asks = order_book.get('asks', [])

        toplam_alis_hacmi = sum([b[1] for b in bids]) if bids else 1.0
        toplam_satis_hacmi = sum([a[1] for a in asks]) if asks else 1.0
        toplam_hacim = toplam_alis_hacmi + toplam_satis_hacmi

        alis_orani = (toplam_alis_hacmi / toplam_hacim) * 100
        satis_orani = (toplam_satis_hacmi / toplam_hacim) * 100

        print(f"     📚 [DERİNLİK] {symbol} | Alış: %{alis_orani:.1f} | Satış: %{satis_orani:.1f} | 24h High: {high_24h} | Anlık: {anlik_fiyat}", flush=True)
        
        return {
            "tepeye_yakin": tepeye_yakin_mi,
            "dipe_yakin": dipe_yakin_mi,
            "alis_orani": alis_orani,
            "satis_orani": satis_orani
        }
    except Exception as e:
        print(f"     ⚠️ [DERİNLİK HATA] {symbol} defter okunamadı: {e}", flush=True)
        return {"tepeye_yakin": False, "dipe_yakin": False, "alis_orani": 50.0, "satis_orani": 50.0}

def akilli_seviye_hesapla(anlik_fiyat, yon, df):
    atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range().iloc[-1]
    if yon == 'LONG':
        tp_fiyat = anlik_fiyat + (atr * 2.5)
        sl_fiyat = anlik_fiyat - (atr * 1.5)
        kapat_yon = 'sell'
    else:
        tp_fiyat = anlik_fiyat - (atr * 2.5)
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
            f"📊 **BOT DURUM RAPORU (Derinlik Korumalı)**\n\n"
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
                yon = str(pos.get('side', '')).upper() or "LONG"
                kapatma_yonu = 'sell' if yon == 'LONG' else 'buy'
                try: exchange.cancel_all_orders(pos['symbol'])
                except Exception: pass
                exchange.create_order(pos['symbol'], 'market', kapatma_yonu, kontrat, None, {'reduceOnly': True})
        await update.message.reply_text("✅ Tüm pozisyonlar ve emirler kapatıldı.")
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

def otomatik_arkaplan_tarayici():
    print("🚀 [BAŞLANGIÇ] Bot Derinlik ve Seviye Korumalı Modda Aktif...", flush=True)
    try:
        exchange.load_markets()
        print("✅ [BAŞLANGIÇ] Piyasalar başarıyla yüklendi.", flush=True)
    except Exception as e:
        print(f"⚠️ [BAŞLANGIÇ HATA] Piyasalar yüklenemedi: {e}", flush=True)
    
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                print("⏸️ [DURUM] Bot durduruldu modunda, bekleniyor...", flush=True)
                time.sleep(5)
                continue

            print("\n--------------------------------------------------", flush=True)
            print("🔄 [DÖNGÜ] Yeni tarama turu başlatılıyor...", flush=True)

            piyasa_rejimi, btc_yonu = piyasa_rejimini_tespit_et()

            # 1. Borsa pozisyonlarını kontrol et
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
                print(f"📌 [POZİSYONLAR] Borsadaki aktif pozisyonlar: {aktif_semboller_listesi if aktif_semboller_listesi else 'Yok'}", flush=True)
            except Exception as e:
                print(f"⚠️ [POZİSYON HATA] Pozisyonlar çekilemedi: {e}", flush=True)
                aktif_borsa_map = {}
                aktif_semboller_listesi = []

            # 2. Kapanan pozisyonları kontrol et ve sonuçlandır
            try:
                anlik_aktif_semboller = [p['symbol'] for p in raw_positions if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
                for eski_sym in list(AKTIF_GRID_SISTEMLERI.keys()):
                    if eski_sym not in anlik_aktif_semboller:
                        print(f"🔍 [KAPANMA] {eski_sym} pozisyonu kapanmış görünüyor, sonuç hesaplanıyor...", flush=True)
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
                                print(f"✅ [SONUÇ] {eski_sym} kârla kapandı!", flush=True)
                            else:
                                basarisiz_sayi += 1
                                sonuc_mesaj_tipi = "❌ *İŞLEM ZARARLA KAPANDI (SL)*"
                                print(f"❌ [SONUÇ] {eski_sym} zararla kapandı!", flush=True)
                                
                            ANALitik_HAFIZA["basarili_islem_sayisi"] = bas_sayi
                            ANALitik_HAFIZA["basarisiz_islem_sayisi"] = basarisiz_sayi
                            COIN_COOLDOWNLAR[eski_sym] = {"zaman": float(time.time() + COOLDOWN_SURESI_SANIYE), "son_yon": yon}
                            if eski_sym in AKTIF_GRID_SISTEMLERI: del AKTIF_GRID_SISTEMLERI[eski_sym]
                                
                        hafizayi_kaydet()
                        telegram_mesaj_gonder(f"{sonuc_mesaj_tipi}\n📌 `{eski_sym}`")
            except Exception as e:
                print(f"⚠️ [KAPANMA KONTROL HATA] {e}", flush=True)

            taranan_sinyaller = []

            # 3. Coin coin tarama, filtreleme ve derinlik kontrolü
            print("🔎 [TARAMA BAŞLIYOR] Takip edilen coinler teknik ve derinlik filtresinden geçiriliyor...", flush=True)
            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU: break
                
                print(f"   ↳ [COİN KONTROL] {symbol} inceleniyor...", flush=True)

                with state_lock:
                    cooldown_veri = COIN_COOLDOWNLAR.get(symbol)
                    if cooldown_veri:
                        zaman_kontrol = cooldown_veri.get("zaman", 0) if isinstance(cooldown_veri, dict) else float(cooldown_veri)
                        kalan_sure = zaman_kontrol - time.time()
                        if kalan_sure > 0:
                            print(f"     ⏳ [COOLDOWN] {symbol} bekleme süresinde. Kalan: {int(kalan_sure)} saniye.", flush=True)
                            continue

                try:
                    ticker = exchange.fetch_ticker(symbol)
                    anlik_fiyat = float(ticker['last'])
                    
                    # Derinlik ve Seviye Kontrolü (Zirvede Long / Dipte Short engelleme)
                    derinlik_bilgi = emir_defteri_ve_seviye_analizi(symbol, anlik_fiyat, ticker)

                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    rsi = ta.momentum.rsi(df['close'], window=14).iloc[-1]

                    print(f"     📊 [VERİ] {symbol} | Fiyat: {anlik_fiyat} | RSI(14): {rsi:.2f}", flush=True)

                    islem_yonu = None
                    if piyasa_rejimi == "YATAY":
                        if rsi < 35: 
                            islem_yonu = "LONG"
                            print(f"     🎯 [FİLTRE] {symbol} YATAY rejimde RSI düşük ({rsi:.2f} < 35) -> LONG sinyali.", flush=True)
                        elif rsi > 65: 
                            islem_yonu = "SHORT"
                            print(f"     🎯 [FİLTRE] {symbol} YATAY rejimde RSI yüksek ({rsi:.2f} > 65) -> SHORT sinyali.", flush=True)
                        else:
                            print(f"     🚫 [FİLTRE] {symbol} nötr bölgede (RSI: {rsi:.2f}), pas geçiliyor.", flush=True)
                            continue
                    else:
                        if btc_yonu == "LONG" and rsi < 60: 
                            islem_yonu = "LONG"
                            print(f"     🎯 [FİLTRE] {symbol} TREND rejiminde (BTC LONG, RSI: {rsi:.2f} < 60) -> LONG sinyali.", flush=True)
                        elif btc_yonu == "SHORT" and rsi > 40: 
                            islem_yonu = "SHORT"
                            print(f"     🎯 [FİLTRE] {symbol} TREND rejiminde (BTC SHORT, RSI: {rsi:.2f} > 40) -> SHORT sinyali.", flush=True)
                        else:
                            print(f"     🚫 [FİLTRE] {symbol} trend koşullarına uymuyor (RSI: {rsi:.2f}), pas geçiliyor.", flush=True)
                            continue

                    # ==================== YENİ: DİRENÇ / DERİNLİK BLOKAJ FİLTRESİ ====================
                    if islem_yonu == "LONG" and derinlik_bilgi["tepeye_yakin"]:
                        print(f"     🛑 [BLOKLANDI] {symbol} 24 saatlik tepe noktasına çok yakın! Dirençte LONG açmak riskli, işlem iptal.", flush=True)
                        continue
                    
                    if islem_yonu == "LONG" and derinlik_bilgi["satis_orani"] > 65.0:
                        print(f"     🛑 [BLOKLANDI] {symbol} emir defterinde satıcı baskısı çok yüksek (%{derinlik_bilgi['satis_orani']:.1f}), LONG açılmıyor.", flush=True)
                        continue

                    if islem_yonu == "SHORT" and derinlik_bilgi["dipe_yakin"]:
                        print(f"     🛑 [BLOKLANDI] {symbol} 24 saatlik dip noktasına çok yakın! Destekte SHORT açmak riskli, işlem iptal.", flush=True)
                        continue

                    if islem_yonu == "SHORT" and derinlik_bilgi["alis_orani"] > 65.0:
                        print(f"     🛑 [BLOKLANDI] {symbol} emir defterinde alıcı baskısı çok yüksek (%{derinlik_bilgi['alis_orani']:.1f}), SHORT açılmıyor.", flush=True)
                        continue

                    taranan_sinyaller.append({
                        "symbol": symbol, "yon": islem_yonu, "rsi": rsi, "fiyat": anlik_fiyat, "df": df
                    })
                except Exception as e:
                    print(f"     ⚠️ [HATA] {symbol} analiz edilirken hata oluştu: {e}", flush=True)
                    continue

            # 4. Sinyal veren coinler için işlem açma
            for sinyal in taranan_sinyaller:
                if not BOT_CALISIYOR_MU: break
                if sinyal["symbol"] in aktif_semboller_listesi:
                    print(f"   ℹ️ [İŞLEM ATLANDI] {sinyal['symbol']} için zaten açık pozisyon var.", flush=True)
                    continue
                if len(aktif_borsa_map) >= MAKSIMUM_TOPLAM_POZISYON:
                    print(f"   ⚠️ [ LİMİT DOLU ] Maksimum pozisyon sınırına ({MAKSIMUM_TOPLAM_POZISYON}) ulaşıldı, yeni işlem açılmayacak.", flush=True)
                    break

                print(f"🚀 [EMİR SÜRECİ] {sinyal['symbol']} için {sinyal['yon']} emri hazırlanıyor...", flush=True)
                try:
                    bakiye_bilgisi = exchange.fetch_balance()
                    toplam_bakiye = float(bakiye_bilgisi['total'].get('USDT', 0))
                    serbest_bakiye = float(bakiye_bilgisi.get('free', {}).get('USDT', 0) or 0)

                    exchange.set_leverage(KALDIRAC, sinyal["symbol"])
                    market = exchange.market(sinyal["symbol"])
                    
                    kullanilacak_tutar = min(toplam_bakiye * 0.4, serbest_bakiye)
                    if kullanilacak_tutar < 1.0:
                        print(f"   ⚠️ [BAKİYE YETERSİZ] Kullanılabilir bakiye yetersiz: {kullanilacak_tutar} USDT", flush=True)
                        continue

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
                    
                    print(f"   📤 [GÖNDERİLİYOR] Piyasaya market emri atılıyor: {sinyal['symbol']} {islem_yonu} Miktar: {miktar}", flush=True)
                    exchange.create_order(sinyal["symbol"], 'market', islem_yonu, miktar)
                    time.sleep(0.5)
                    try:
                        exchange.create_order(sinyal["symbol"], 'limit', kapat_yon, miktar, tp_fiyat, {'reduceOnly': True})
                        exchange.create_order(sinyal["symbol"], 'stop', kapat_yon, miktar, sl_fiyat, {'stopPrice': sl_fiyat, 'reduceOnly': True})
                        print(f"   🎯 [TP/SL EKLENDİ] TP: {tp_fiyat} | SL: {sl_fiyat}", flush=True)
                    except Exception as order_err:
                        print(f"   ⚠️ [TP/SL HATA] TP/SL emirleri kurulamadı: {order_err}", flush=True)

                    with state_lock:
                        AKTIF_GRID_SISTEMLERI[sinyal["symbol"]] = {
                            "giris_fiyati": giris_fiyati, "yon": sinyal["yon"],
                            "giris_rsi": float(sinyal["rsi"]), "giris_zamani": time.time()
                        }
                        aktif_semboller_listesi.append(sinyal["symbol"])
                    hafizayi_kaydet()
                    
                    telegram_mesaj_gonder(
                        f"🎯 *DERİNLİK KORUMALI İŞLEM GİRİŞİ*\n"
                        f"📌 `{sinyal['symbol']}` | Yön: `{sinyal['yon']}`\n"
                        f"🎯 Giriş: `{giris_fiyati}`\n"
                        f"💰 Hedef TP: `{tp_fiyat}` (Hedef ROE: `%{hedef_roe:.1f}`)\n"
                        f"🛑 Stop-Loss: `{sl_fiyat}`"
                    )
                    break
                except Exception as e:
                    print(f"❌ [İŞLEM AÇMA HATA] {sinyal['symbol']} için emir başarısız: {e}", flush=True)

        except Exception as e:
            print(f"⚠️ [DÖNGÜ GENEL HATA] {e}", flush=True)
        
        print("💤 [BEKLEME] Tur tamamlandı, 10 saniye sonra tekrar taranacak...", flush=True)
        time.sleep(10)

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
        print("🛑 Bot kapatıldı.", flush=True)
