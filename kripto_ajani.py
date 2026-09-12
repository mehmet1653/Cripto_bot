import os
import time
import threading
import requests
import ccxt
import pandas as pd
import ta
import numpy as np
from flask import Flask, request as flask_request
from sklearn.ensemble import RandomForestClassifier
from supabase import create_client, Client

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
    'SOL/USDT:USDT', 'AVAX/USDT:USDT', 'XRP/USDT:USDT', 'DOGE/USDT:USDT', 'SUI/USDT:USDT'
]

COIN_ID_MAP = {
    'SOL/USDT:USDT': 1,
    'AVAX/USDT:USDT': 2,
    'XRP/USDT:USDT': 3,
    'DOGE/USDT:USDT': 4,
    'SUI/USDT:USDT': 5
}

BOT_CALISIYOR_MU = True

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
                }),
                "cooldownlar": veri.get("cooldownlar", {})
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
        },
        "cooldownlar": {}
    }
    try:
        supabase.table("bot_hafiza").upsert({"id": 1, **varsayilan}).execute()
    except Exception as e:
        print(f"⚠️ Hafıza tablo oluşturma/ilk kayıt hatası: {e}", flush=True)
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
    if len(veriler) < 20:
        ai_model_egitildi = False
        return
    try:
        X = [item[:6] for item in veriler]
        y = [item[6] for item in veriler]
        if len(set(y)) < 2:
            ai_model_egitildi = False
            return
        ai_model.fit(np.array(X), np.array(y))
        ai_model_egitildi = True
    except Exception as e:
        print(f"⚠️ Yapay zeka eğitim hatası: {e}", flush=True)
        ai_model_egitildi = False

def yapay_zeka_islem_onayi(rsi, adx, ema_fark, yon_kod, atr_yuzde, coin_id, symbol):
    if not ai_model_egitildi:
        return True
    try:
        olasiliklar = ai_model.predict_proba(np.array([[rsi, adx, ema_fark, yon_kod, atr_yuzde, coin_id]]))[0]
        classes = list(ai_model.classes_)
        basari_ihtimali = olasiliklar[classes.index(1)] if 1 in classes else 1.0
        return basari_ihtimali >= 0.35
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
        bids, asks = order_book.get('bids', []), order_book.get('asks', [])
        toplam_bid = sum(b[1] for b in bids)
        toplam_ask = sum(a[1] for a in asks)
        if toplam_bid + toplam_ask == 0: return "DENGELI"
        bid_orani = toplam_bid / (toplam_bid + toplam_ask)
        return "ALICI_BASKIN" if bid_orani > 0.55 else ("SATICI_BASKIN" if bid_orani < 0.45 else "DENGELI")
    except Exception:
        return "DENGELI"

def hacim_ve_likidite_kontrolu(df):
    try:
        return df['volume'].iloc[-1] >= (df['volume'].rolling(window=20).mean().iloc[-1] * 0.10)
    except Exception:
        return True

def sinyal_hala_gecerli_mi(symbol, yon):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=30)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        ema7 = ta.trend.ema_indicator(df['close'], window=7).iloc[-1]
        ema21 = ta.trend.ema_indicator(df['close'], window=21).iloc[-1]
        rsi = ta.momentum.rsi(df['close'], window=14).iloc[-1]

        if yon == 'LONG':
            if ema7 < ema21 and rsi < 45:
                return False
        elif yon == 'SHORT':
            if ema7 > ema21 and rsi > 55:
                return False
    except Exception:
        pass
    return True

def telegram_mesaj_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": mesaj, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"⚠️ Telegram Gönderme Hatası: {e}", flush=True)

@app.route('/')
def home():
    return f"Hibrit Bot Aktif | Aktif Pozisyon: {len(AKTIF_GRID_SISTEMLERI)}"

@app.route('/webhook', methods=['POST'])
def telegram_webhook():
    global BOT_CALISIYOR_MU
    try:
        data = flask_request.get_json()
        if not data or 'message' not in data:
            return "OK", 200
        
        message = data['message']
        text = message.get('text', '')
        chat_id = str(message.get('chat', {}).get('id', ''))
        
        if chat_id != CHAT_ID:
            return "OK", 200
            
        if text.startswith('/durum'):
            durum_mesaji_olustur_ve_gonder()
        elif text.startswith('/baslat'):
            BOT_CALISIYOR_MU = True
            telegram_mesaj_gonder("🟢 *Bot Aktif Edildi!*")
        elif text.startswith('/durdur'):
            BOT_CALISIYOR_MU = False
            telegram_mesaj_gonder("⏸️ *Bot durduruldu.*")
        elif text.startswith('/kapat'):
            telegram_mesaj_gonder("🔄 Tüm pozisyonlar kapatılıyor...")
            for pos in exchange.fetch_positions():
                kontrat = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
                if kontrat > 0:
                    pozisyonu_garantili_kapat(pos['symbol'], str(pos.get('side', '')).upper(), kontrat, f"🛑 *MANUEL KAPATMA* - `{pos['symbol']}`", basarili=False)
            AKTIF_GRID_SISTEMLERI.clear()
            hafizayi_kaydet()
            telegram_mesaj_gonder("✅ Tüm pozisyonlar kapatıldı.")
    except Exception as e:
        print(f"Webhook hata: {e}", flush=True)
    return "OK", 200

def webhook_otomatik_ayarla():
    # Railway'in kendi otomatik domain değişkenini yakalar veya manuel eklediğiniz domain'i okur
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN") or os.environ.get("RAILWAY_STATIC_URL")
    if domain:
        if not domain.startswith("http"):
            domain = f"https://{domain}"
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook?url={domain}/webhook"
        try:
            res = requests.get(url, timeout=10)
            print(f"🔗 Otomatik Webhook Bağlantısı: {res.text}", flush=True)
        except Exception as e:
            print(f"⚠️ Webhook otomatik ayarlama hatası: {e}", flush=True)
    else:
        print("ℹ️ Railway domain değişkeni bulunamadı, webhook manuel tetiklenebilir.", flush=True)

def durum_mesaji_olustur_ve_gonder():
    try:
        balance = exchange.fetch_balance()
        total = float(balance['total'].get('USDT', 0))
        borsa_poslari = [p for p in exchange.fetch_positions() if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
        toplam_pnl = sum(float(p.get('unrealizedPnl', 0)) for p in borsa_poslari)
        
        basarili_s = ANALitik_HAFIZA.get("basarili_islem_sayisi", 0)
        basarisiz_s = ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0)
        toplam_i = basarili_s + basarisiz_s
        basari_o = (basarili_s / toplam_i * 100) if toplam_i > 0 else 0.0

        pnl_ikon = "🟢" if toplam_pnl >= 0 else "🔴"
        mesaj = (
            f"📊 *HİBRİT BOT DURUMU*\n\n"
            f"💰 Toplam Kasa: `{total:.2f} USDT`\n"
            f"{pnl_ikon} Anlık PnL: `{toplam_pnl:+.2f} USDT`\n"
            f"📌 Açık Pozisyon: `{len(borsa_poslari)} / {MAKSIMUM_TOPLAM_POZISYON}`\n\n"
        )

        if borsa_poslari:
            mesaj += "📋 *Açık Pozisyonlar Detay:*\n"
            for p in borsa_poslari:
                sym = p.get('symbol')
                yon = str(p.get('side', '')).upper()
                merkez = float(p.get('entryPrice', 0))
                kaldirac = int(p.get('leverage', 10))
                pnl_val = float(p.get('unrealizedPnl', 0))
                
                try:
                    guncel_fiyat = exchange.fetch_ticker(sym)['last']
                    fark = (guncel_fiyat - merkez) / merkez if yon == "LONG" else (merkez - guncel_fiyat) / merkez
                    roe = fark * 100 * kaldirac
                except Exception:
                    roe = 0.0

                pos_ikon = "🟢" if pnl_val >= 0 else "🔴"
                mesaj += f"{pos_ikon} `{sym}` | {yon} ({kaldirac}x)\n   └ PnL: `{pnl_val:+.2f} USDT` (`%{roe:+.2f}`)\n"
            mesaj += "\n"

        mesaj += (
            f"✅ Başarılı TP: `{basarili_s}` | ❌ Başarısız SL: `{basarisiz_s}`\n"
            f"📈 Başarı Oranı: `%{basari_o:.1f}`\n"
            f"🧠 AI Verisi: `{len(ANALitik_HAFIZA.get('egitim_verileri', []))}/20`"
        )
        telegram_mesaj_gonder(mesaj)
    except Exception as e:
        telegram_mesaj_gonder(f"⚠️ Durum hatası: {e}")

def set_leverage_safely(symbol, leverage):
    try:
        exchange.set_leverage(leverage, symbol)
        return True
    except Exception as e:
        print(f"⚠️ Kaldıraç hatası ({symbol}): {e}", flush=True)
        return False

def pozisyonu_garantili_kapat(symbol, yon, miktar, sebep_mesaji, rsi=50, adx=25, ema_fark=0.0, atr_yuzde=1.5, basarili=True):
    kapatma_yonu = 'sell' if yon == 'LONG' else 'buy'
    try:
        for ord_item in exchange.fetch_open_orders(symbol):
            exchange.cancel_order(ord_item['id'], symbol)
    except Exception: pass

    try:
        market_info = exchange.market(symbol)
        min_amount = float(market_info['limits']['amount']['min'] or 1.0)
        if miktar < min_amount: miktar = min_amount
        miktar = float(exchange.amount_to_precision(symbol, miktar))
        exchange.create_order(symbol, 'market', kapatma_yonu, miktar, None, {'reduce_only': True})
    except Exception as e:
        print(f"⚠️ Kapatma API hatası: {e}", flush=True)

    COIN_COOLDOWNLAR[symbol] = time.time() + COOLDOWN_SURESI_SANIYE
    ANALitik_HAFIZA["egitim_verileri"].append([rsi, adx, ema_fark, (1 if yon == 'LONG' else -1), atr_yuzde, COIN_ID_MAP.get(symbol, 0), (1 if basarili else 0)])
    if len(ANALitik_HAFIZA["egitim_verileri"]) > 150: ANALitik_HAFIZA["egitim_verileri"].pop(0)
    yapay_zekayi_egit_ve_guncelle()

    if symbol in AKTIF_GRID_SISTEMLERI:
        del AKTIF_GRID_SISTEMLERI[symbol]
        hafizayi_kaydet()

    if sebep_mesaji: telegram_mesaj_gonder(sebep_mesaji)

# ==================== ARKA PLAN TARAYICI ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, ANALitik_HAFIZA
    print("🚀 Gelişmiş Hibrit Tarayıcı Devrede.", flush=True)
    try:
        exchange.load_markets()
        yapay_zekayi_egit_ve_guncelle()
    except Exception: pass
    
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(3)
                continue

            try:
                raw_positions = exchange.fetch_positions()
                aktif_borsa_map = {p['symbol']: p for p in raw_positions if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0}
            except Exception:
                aktif_borsa_map = {}

            for sym in list(AKTIF_GRID_SISTEMLERI.keys()):
                if sym not in aktif_borsa_map:
                    del AKTIF_GRID_SISTEMLERI[sym]
                    hafizayi_kaydet()

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

                kayitli = AKTIF_GRID_SISTEMLERI.get(symbol, {})
                hedef_roe = kayitli.get("hedef_roe", 20.0)
                stop_roe = kayitli.get("stop_roe", 10.0)

                if not sinyal_hala_gecerli_mi(symbol, yon):
                    basarili_mi = pnl > 0
                    if basarili_mi:
                        ANALitik_HAFIZA["basarili_islem_sayisi"] += 1
                    else:
                        ANALitik_HAFIZA["basarisiz_islem_sayisi"] += 1
                    hafizayi_kaydet()
                    pozisyonu_garantili_kapat(symbol, yon, kontrat, f"🧠 *AKILLI ERKEN ÇIKIŞ (RÜZGAR DÖNDÜ)*\n📌 `{symbol}` | Sinyal bozulduğu için çıkıldı. PnL: `{pnl:+.2f} USDT` (`%{roe:+.2f}`)", basarili=basarili_mi)
                    continue

                if roe >= hedef_roe:
                    ANALitik_HAFIZA["basarili_islem_sayisi"] += 1
                    hafizayi_kaydet()
                    pozisyonu_garantili_kapat(symbol, yon, kontrat, f"🎯 *KÂR ALINDI (TP)*\n📌 `{symbol}` | Kâr: `+{pnl:.2f} USDT` (`%{roe:.2f}`)", basarili=True)
                elif roe <= -stop_roe:
                    ANALitik_HAFIZA["basarisiz_islem_sayisi"] += 1
                    hafizayi_kaydet()
                    pozisyonu_garantili_kapat(symbol, yon, kontrat, f"🛑 *ZARAR KESİLDİ (SL)*\n📌 `{symbol}` | Zarar: `{pnl:.2f} USDT` (`%{roe:.2f}`)", basarili=False)

            taranan_sinyaller = []

            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU: break
                if symbol in aktif_borsa_map or time.time() < COIN_COOLDOWNLAR.get(symbol, 0): continue

                try:
                    guncel_fiyat = exchange.fetch_ticker(symbol)['last']
                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    if not hacim_ve_likidite_kontrolu(df): continue

                    ema7 = ta.trend.ema_indicator(df['close'], window=7).iloc[-1]
                    ema21 = ta.trend.ema_indicator(df['close'], window=21).iloc[-1]
                    rsi = ta.momentum.rsi(df['close'], window=14).iloc[-1]
                    adx_ind = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14)
                    adx_val = adx_ind.adx().iloc[-1]
                    
                    degisim = ((guncel_fiyat - df['close'].iloc[-10]) / df['close'].iloc[-10]) * 100
                    derinlik = emir_defteri_derinlik_analizi(symbol)
                    atr = atr_ve_volatilite_hesapla(df)
                except Exception: continue

                sinyal_puani, grid_yonu = 50, "LONG"
                if degisim >= 2.5 and rsi > 62 and derinlik in ["SATICI_BASKIN", "DENGELI"]:
                    grid_yonu, sinyal_puani = "SHORT", 88
                elif degisim <= -2.5 and rsi < 38 and derinlik in ["ALICI_BASKIN", "DENGELI"]:
                    grid_yonu, sinyal_puani = "LONG", 88
                elif adx_val >= 25:
                    grid_yonu = "LONG" if adx_ind.adx_pos().iloc[-1] > adx_ind.adx_neg().iloc[-1] else "SHORT"
                    sinyal_puani = 75
                else:
                    grid_yonu = "LONG" if ema7 > ema21 else "SHORT"

                if not yapay_zeka_islem_onayi(rsi, adx_val, float(ema7 - ema21), (1 if grid_yonu == 'LONG' else -1), atr, COIN_ID_MAP.get(symbol, 0), symbol):
                    continue

                taranan_sinyaller.append({"symbol": symbol, "puan": sinyal_puani, "yon": grid_yonu, "rsi": rsi, "adx": adx_val, "ema_fark": float(ema7 - ema21), "fiyat": guncel_fiyat, "atr": atr, "altin_atis": sinyal_puani >= 85})

            taranan_sinyaller.sort(key=lambda x: x["puan"], reverse=True)

            for sinyal in taranan_sinyaller:
                if not BOT_CALISIYOR_MU: break
                if len(aktif_borsa_map) >= MAKSIMUM_TOPLAM_POZISYON: break
                if sum(1 for p in aktif_borsa_map.values() if str(p.get('side', '')).upper() == sinyal["yon"]) >= MAKSIMUM_AYNI_YON_SAYISI: continue

                kaldirac = 20 if sinyal["altin_atis"] else 10
                kasa_orani = 0.25 if sinyal["altin_atis"] else 0.20

                try:
                    toplam_bakiye = float(exchange.fetch_balance()['total'].get('USDT', 0))
                    if not set_leverage_safely(sinyal["symbol"], kaldirac): continue
                    
                    market = exchange.market(sinyal["symbol"])
                    miktar = float(exchange.amount_to_precision(sinyal["symbol"], max((toplam_bakiye * kasa_orani * kaldirac) / sinyal["fiyat"] / float(market.get('contractSize', 1.0)), float(market['limits']['amount']['min'] or 1.0))))
                    
                    exchange.create_order(sinyal["symbol"], 'market', 'buy' if sinyal["yon"] == 'LONG' else 'sell', miktar)
                    AKTIF_GRID_SISTEMLERI[sinyal["symbol"]] = {
                        "giris_rsi": sinyal["rsi"], 
                        "giris_adx": sinyal["adx"], 
                        "ema_fark": sinyal["ema_fark"], 
                        "atr_yuzde": sinyal["atr"], 
                        "hedef_roe": 20.0, 
                        "stop_roe": 10.0
                    }
                    hafizayi_kaydet()
                    
                    telegram_mesaj_gonder(f"⚡ *İŞLEM AÇILDI*\n📌 `{sinyal['symbol']}` | Yön: `{sinyal['yon']}` | Puan: `{sinyal['puan']}` | Kaldıraç: `{kaldirac}x`")
                    break
                except Exception as e:
                    print(f"❌ İşlem açma hatası: {e}", flush=True)

        except Exception as e:
            print(f"⚠️ Tarayıcı hatası: {e}", flush=True)
        time.sleep(5)

if __name__ == '__main__':
    threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True).start()
    threading.Thread(target=webhook_otomatik_ayarla, daemon=True).start()
    
    print("🤖 Bot ve Webhook Sunucusu Başlatıldı...", flush=True)
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)), use_reloader=False)
