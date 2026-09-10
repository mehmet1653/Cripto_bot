import time
import threading
import sys
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

COIN_ID_MAP = {symbol: idx + 1 for idx, symbol in enumerate(TAKIP_EDILENLER)}

BOT_CALISIYOR_MU = True
KALDIRAC = 5
MAKSIMUM_TOPLAM_POZISYON = 3
KAR_HEDEF_YUZDESI = 0.015  # %1.5 Take Profit
ZARAR_KES_YUZDESI = 0.01   # %1.0 Stop Loss

# ==================== SUPABASE HAFIZA FONKSİYONLARI ====================
def hafizayi_yukle():
    varsayilan = {
        "aktif_pozisyonlar": {},
        "analitik": {
            "basarili_islem": 0,
            "basarisiz_islem": 0,
            "egitim_verileri": []
        }
    }
    try:
        response = supabase.table("bot_hafiza").select("*").eq("id", 1).execute()
        if response.data and len(response.data) > 0:
            veri = response.data[0]
            return {
                "aktif_pozisyonlar": veri.get("aktif_sistemler", {}),
                "analitik": veri.get("analitik", varsayilan["analitik"])
            }
    except Exception as e:
        print(f"⚠️ [SUPABASE] Hafıza yükleme hatası: {e}", flush=True)
    
    try:
        supabase.table("bot_hafiza").upsert({"id": 1, "aktif_sistemler": {}, "analitik": varsayilan["analitik"]}).execute()
    except Exception as e:
        print(f"⚠️ [SUPABASE] Varsayılan hafıza kayıt hatası: {e}", flush=True)
        
    return varsayilan

def hafizayi_kaydet():
    try:
        supabase.table("bot_hafiza").upsert({
            "id": 1,
            "aktif_sistemler": AKTIF_POZISYONLAR,
            "analitik": ANALITIK_HAFIZA
        }).execute()
    except Exception as e:
        print(f"⚠️ [SUPABASE] Hafıza kayıt hatası: {e}", flush=True)

kalici_veri = hafizayi_yukle()
AKTIF_POZISYONLAR = kalici_veri.get("aktif_pozisyonlar", {})
ANALITIK_HAFIZA = kalici_veri.get("analitik", {
    "basarili_islem": 0,
    "basarisiz_islem": 0,
    "egitim_verileri": []
})

# ==================== YAPAY ZEKA ÖĞRENME MOTORU ====================
ai_model = RandomForestClassifier(n_estimators=150, max_depth=7, random_state=42)
ai_model_egitildi = False

def yapay_zekayi_egit():
    global ai_model, ai_model_egitildi
    veriler = ANALITIK_HAFIZA.get("egitim_verileri", [])
    if len(veriler) < 10:
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
        print(f"🧠 [AI] Model başarıyla güncellendi. Toplam Eğitim Verisi: {len(veriler)}", flush=True)
    except Exception as e:
        ai_model_egitildi = False
        print(f"⚠️ [AI HATA] Eğitim hatası: {e}", flush=True)

def olasilik_tahmini(features):
    if not ai_model_egitildi:
        return 1.0  # Model henüz eğitilmediyse ilk aşamada serbest bırak
    try:
        prob = ai_model.predict_proba(np.array([features]))[0]
        return float(max(prob))  # En yüksek sınıfın olasılık skoru (%70+ aranacak)
    except Exception:
        return 1.0

def telegram_mesaj_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": mesaj, "parse_mode": "Markdown"}, timeout=10)
    except Exception:
        pass

@app.route('/')
def home():
    return f"Yapay Zeka Destekli Yüksek Olasılıklı Bot Aktif | Aktif Poz: {len(AKTIF_POZISYONLAR)}"

# ==================== TELEGRAM KOMUTLARI ====================
async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        balance = exchange.fetch_balance()
        total = float(balance['total'].get('USDT', 0))
        free = float(balance['free'].get('USDT', 0))
        
        try:
            raw_positions = exchange.fetch_positions()
            borsa_poslari = {p['symbol']: p for p in raw_positions if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0}
        except Exception:
            borsa_poslari = {}

        toplam_pnl = sum(float(p.get('unrealizedPnl', 0)) for p in borsa_poslari.values())
        pnl_ikon = "🟢" if toplam_pnl >= 0 else "🔴"
        
        basari = ANALITIK_HAFIZA.get("basarili_islem", 0)
        basarisiz = ANALITIK_HAFIZA.get("basarisiz_islem", 0)
        toplam_islem = basari + basarisiz
        win_rate = (basari / toplam_islem * 100) if toplam_islem > 0 else 0.0

        mesaj = (
            f"🎯 *YÜKSEK OLASILIKLI AI TRADING RAPORU*\n\n"
            f"💰 Kasa: `{total:.2f} USDT` (Serbest: `{free:.2f} USDT`)\n"
            f"{pnl_ikon} Toplam PnL: `{toplam_pnl:+.2f} USDT`\n"
            f"📊 AI Başarı Oranı: `% {win_rate:.1f}` ({basari} Başarılı / {basarisiz} Başarısız)\n"
            f"📌 Aktif Pozisyon: `{len(AKTIF_POZISYONLAR)} / {MAKSIMUM_TOPLAM_POZISYON}`\n"
            f"-------------------------------------\n"
        )

        if not AKTIF_POZISYONLAR:
            mesaj += "⚠️ Şuan açık pozisyon bulunmuyor."
        else:
            for sym, pos_data in AKTIF_POZISYONLAR.items():
                yon = pos_data.get("yon", "LONG")
                giris = pos_data.get("giris_fiyati", 0)
                pos = borsa_poslari.get(sym)
                pnl = float(pos.get('unrealizedPnl', 0)) if pos else 0.0
                mesaj += f"🔹 *{sym}* (`{yon}`)\n   • Giriş: `{giris}` | PnL: `{pnl:+.2f} USDT`\n\n"

        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata oluştu: {e}")

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = True
    await update.message.reply_text("🚀 *AI Karar Motoru Aktif Edildi!*", parse_mode='Markdown')

async def durdur_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ *Bot Durduruldu.*", parse_mode='Markdown')

async def kapat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        for pos in exchange.fetch_positions():
            kontrat = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
            if kontrat > 0:
                symbol = pos['symbol']
                yon = str(pos.get('side', '')).upper()
                exchange.create_order(symbol, 'market', 'sell' if yon == 'LONG' else 'buy', kontrat, None, {'reduce_only': True})
        AKTIF_POZISYONLAR.clear()
        hafizayi_kaydet()
        await update.message.reply_text("✅ Tüm açık pozisyonlar kapatıldı ve hafıza sıfırlandı.", parse_mode='Markdown')
    except Exception as e:
        AKTIF_POZISYONLAR.clear()
        hafizayi_kaydet()
        await update.message.reply_text(f"✅ Hafıza temizlendi ({e}).", parse_mode='Markdown')

# ==================== ANA ARKA PLAN MOTORU ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, ANALITIK_HAFIZA
    print("🚀 [AI BOT] Yüksek Olasılıklı Karar Motoru Başlatıldı.", flush=True)
    try:
        exchange.load_markets()
    except Exception:
        pass
    
    yapay_zekayi_egit()
    
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5)
                continue

            try:
                raw_positions = exchange.fetch_positions()
                borsa_poslari = {p['symbol']: p for p in raw_positions if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0}
            except Exception:
                borsa_poslari = {}

            # --- 1. AÇIK POZİSYONLARI TAKİP ET (Kar Al / Zarar Kes Kontrolü) ---
            for sym in list(AKTIF_POZISYONLAR.items()[0] if False else list(AKTIF_POZISYONLAR.keys())):
                pos_info = AKTIF_POZISYONLAR[sym]
                try:
                    guncel_fiyat = exchange.fetch_ticker(sym)['last']
                    giris = pos_info["giris_fiyati"]
                    yon = pos_info["yon"]
                    
                    # Kar / Zarar Hesabı
                    if yon == "LONG":
                        degisim = (guncel_fiyat - giris) / giris
                    else:
                        degisim = (giris - guncel_fiyat) / giris

                    print(f"👀 [TAKİP] {sym} ({yon}) | Giriş: {giris} | Anlık: {guncel_fiyat} | Değişim: %{degisim*100:.2f}", flush=True)

                    # Hedefe ulaşıldı mı?
                    if degisim >= KAR_HEDEF_YUZDESI or degisim <= -ZARAR_KES_YUZDESI:
                        basarili mi = degisim >= KAR_HEDEF_YUZDESI
                        print(f"🏁 [POZİSYON KAPANIŞI] {sym} kapatılıyor. Sonuç: {'BAŞARILI 🎉' + str(degisim) if basarili else 'ZARAR ❌'}", flush=True)
                        
                        if sym in borsa_poslari:
                            pos = borsa_poslari[sym]
                            kontrat = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
                            exchange.create_order(sym, 'market', 'sell' if yon == 'LONG' else 'buy', kontrat, None, {'reduce_only': True})

                        # Hafızaya ve eğitime işle
                        features = pos_info["features"]
                        hedef_sinif = 1 if basarili else 0
                        ANALITIK_HAFIZA["egitim_verileri"].append(features + [hedef_sinif])
                        if basarili:
                            ANALITIK_HAFIZA["basarili_islem"] += 1
                        else:
                            ANALITIK_HAFIZA["basarisiz_islem"] += 1

                        AKTIF_POZISYONLAR.pop(sym)
                        hafizayi_kaydet()
                        yapay_zekayi_egit()
                        telegram_mesaj_gonder(f"📊 *Pozisyon Kapandı* -> `{sym}` ({yon})\nSonuç: `{'Kârda 🟢' if basarili else 'Zararda 🔴'}` | Oran: `%{degisim*100:.2f}`")

                except Exception as e:
                    print(f"⚠️ [POZİSYON TAKİP HATA] ({sym}): {e}", flush=True)

            # --- 2. YENİ YÜKSEK OLASILIKLI POZİSYON AÇMA ---
            if len(AKTIF_POZISYONLAR) < MAKSIMUM_TOPLAM_POZISYON:
                for symbol in TAKIP_EDILENLER:
                    if symbol in AKTIF_POZISYONLAR:
                        continue

                    try:
                        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=30)
                        if len(ohlcv) < 25:
                            continue
                        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                        
                        rsi = float(ta.momentum.rsi(df['close'], window=14).iloc[-1])
                        adx_ind = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14)
                        adx = float(adx_ind.adx().iloc[-1])
                        p_di = float(adx_ind.adx_pos().iloc[-1])
                        n_di = float(adx_ind.adx_neg().iloc[-1])
                        
                        guncel_fiyat = float(df['close'].iloc[-1])

                        # Özellik vektörü (Machine Learning Giriş Parametreleri)
                        features = [rsi, adx, p_di, n_di, COIN_ID_MAP.get(symbol, 1)]
                        guven_orani = olasilik_tahmini(features)

                        print(f"🔍 [TARAMA] {symbol} | RSI: {rsi:.1f} | ADX: {adx:.1f} | AI Güven: %{guven_orani*100:.1f}", flush=True)

                        # Yön Tespiti ve AI Onayı (%70+ güven şartı)
                        if guven_orani >= 0.70:
                            yon = "LONG" if p_di > n_di and rsi < 55 else "SHORT"
                            
                            print(f"✨ [AI ONAYLI İŞLEM] {symbol} için {yon} pozisyonu açılıyor...", flush=True)
                            
                            try:
                                exchange.set_leverage(KALDIRAC, symbol)
                                exchange.set_margin_mode('isolated', symbol)
                            except Exception:
                                pass

                            balance = exchange.fetch_balance()
                            serbest_bakiye = float(balance['free'].get('USDT', 0))
                            
                            if serbest_bakiye < 10:
                                continue

                            islem_butcesi = (serbest_bakiye / (MAKSIMUM_TOPLAM_POZISYON - len(AKTIF_POZISYONLAR))) * 0.9
                            market_info = exchange.market(symbol)
                            
                            ham_miktar = (islem_butcesi * KALDIRAC) / guncel_fiyat
                            miktar = float(exchange.amount_to_precision(symbol, max(round(ham_miktar / float(market_info.get('contractSize', 1.0))), 1)))

                            exchange.create_order(symbol, 'market', 'buy' if yon == 'LONG' else 'sell', miktar, None)

                            AKTIF_POZISYONLAR[symbol] = {
                                "yon": yon,
                                "giris_fiyati": guncel_fiyat,
                                "features": features
                            }
                            hafizayi_kaydet()
                            telegram_mesaj_gonder(f"🚀 *Yüksek Olasılıklı İşlem Açıldı* -> `{symbol}` (`{yon}`)\nGiriş: `{guncel_fiyat}` | AI Güven: `% {guven_orani*100:.1f}`")
                            break

                    except Exception as e:
                        print(f"⚠️ [TARAMA HATA] ({symbol}): {e}", flush=True)

        except Exception as e:
            print(f"⚠️ [DÖNGÜ HATA]: {e}", flush=True)
        time.sleep(15)

def flask_web_server():
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

if __name__ == '__main__':
    threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True).start()
    threading.Thread(target=flask_web_server, daemon=True).start()
    
    app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app_tg.add_handler(CommandHandler("durum", durum_komutu))
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))
    
    app_tg.run_polling()
