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

# --- RİSK VE MARTİNGALE (KURTARMA) PARAMETRELERİ ---
HEDEF_KALDIRAC = 10
TP_YUZDE = 0.20       # %20 Kâr Hedefi
SL_YUZDE = 0.10       # %10 Zarar Kes
BAZ_MARJIN = 5.0      # İlk işlem taban marjini (USDT)
MAKS_MARTINGALE_ADIM = 7 
KOMISYON_ORANI = 0.001  # %0.10 toplam taker komisyonu

BOT_CALISIYOR_MU = True
state_lock = threading.Lock()

def hafizayi_yukle():
    try:
        response = supabase.table("bot_hafiza").select("*").eq("id", 1).execute()
        if response.data and len(response.data) > 0:
            veri = response.data[0]
            return {
                "aktif_sistemler": veri.get("aktif_sistemler", {}),
                "analitik": veri.get("analitik", {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0, "egitim_verileri": [], "martingale_adim": 1, "kumuletif_zarar": 0.0}),
                "cooldownlar": veri.get("cooldownlar", {})
            }
    except Exception: pass
        
    varsayilan = {
        "aktif_sistemler": {},
        "analitik": {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0, "egitim_verileri": [], "martingale_adim": 1, "kumuletif_zarar": 0.0},
        "cooldownlar": {}
    }
    try:
        supabase.table("bot_hafiza").upsert({"id": 1, **varsayilan}).execute()
    except Exception: pass
    return varsayilan

def hafizayi_kaydet():
    with state_lock:
        try:
            payload_analitik = {
                "basarili_islem_sayisi": int(ANALitik_HAFIZA.get("basarili_islem_sayisi", 0)),
                "basarisiz_islem_sayisi": int(ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0)),
                "egitim_verileri": ANALitik_HAFIZA.get("egitim_verileri", []),
                "martingale_adim": int(ANALitik_HAFIZA.get("martingale_adim", 1)),
                "kumuletif_zarar": float(ANALitik_HAFIZA.get("kumuletif_zarar", 0.0))
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
        except Exception: pass

kalici_veri = hafizayi_yukle()
AKTIF_GRID_SISTEMLERI = kalici_veri.get("aktif_sistemler", {})
ANALitik_HAFIZA = kalici_veri.get("analitik", {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0, "egitim_verileri": [], "martingale_adim": 1, "kumuletif_zarar": 0.0})
COIN_COOLDOWNLAR = kalici_veri.get("cooldownlar", {})

COOLDOWN_SURESI_SANIYE = 45 * 60

ai_model = RandomForestClassifier(n_estimators=100, max_depth=6, random_state=42)
ai_model_egitildi = False

def yapay_zekayi_egit_ve_guncelle():
    global ai_model, ai_model_egitildi
    with state_lock:
        veriler = list(ANALitik_HAFIZA.get("egitim_verileri", []))
    if len(veriler) < 10:
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
        return (olasiliklar[classes.index(1)] if 1 in classes else 1.0) >= 0.70
    except Exception:
        return True

def telegram_mesaj_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": mesaj, "parse_mode": "Markdown"}, timeout=5)
    except Exception: pass

def pozisyonu_kapat(symbol, yon, miktar, sebep_mesaji):
    kapatma_yonu = 'sell' if yon == 'LONG' else 'buy'
    try:
        try: exchange.cancel_all_orders(symbol)
        except Exception: pass
        exchange.create_order(symbol, 'market', kapatma_yonu, miktar, None, {'reduceOnly': True})
    except Exception: pass

    with state_lock:
        if symbol in AKTIF_GRID_SISTEMLERI:
            del AKTIF_GRID_SISTEMLERI[symbol]
            
    hafizayi_kaydet()
    if sebep_mesaji: telegram_mesaj_gonder(sebep_mesaji)

async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        balance = exchange.fetch_balance()
        total = float(balance['total'].get('USDT', 0))
        borsa_poslari = [p for p in exchange.fetch_positions() if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
        toplam_pnl = sum(float(p.get('unrealizedPnl', 0)) for p in borsa_poslari)
        
        basarili = int(ANALitik_HAFIZA.get("basarili_islem_sayisi", 0))
        basarisiz = int(ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0))
        m_adim = int(ANALitik_HAFIZA.get("martingale_adim", 1))
        kum_zarar = float(ANALitik_HAFIZA.get("kumuletif_zarar", 0.0))
        
        toplam_islem = basarili + basarisiz
        basari_orani = (basarili / toplam_islem * 100) if toplam_islem > 0 else 0.0

        detay_metni = ""
        for p in borsa_poslari:
            sym = p.get('symbol')
            yon = str(p.get('side', '')).upper()
            pnl_val = float(p.get('unrealizedPnl', 0))
            roe_val = float(p.get('percentage', 0))
            detay_metni += f"\n📌 `{sym}` ({yon}) | PnL: `{pnl_val:+.2f} USDT` (`%{roe_val:.2f}`)"

        if not detay_metni:
            detay_metni = "\n📌 Açık pozisyon bulunmuyor."

        mesaj = (
            "💎 **TESTNET DİNAMİK SEÇİMLİ MARTİNGALE BOTU (10x)**\n\n"
            f"💰 Toplam Kasa: `{total:.2f} USDT`\n"
            f"🟢 Anlık Toplam PnL: `{toplam_pnl:+.2f} USDT`\n"
            f"🔄 Mevcut Martingale Adımı: `{m_adim}. Adım` (Maks: {MAKS_MARTINGALE_ADIM})\n"
            f"📉 Biriken Kümülatif Zarar: `{kum_zarar:.2f} USDT`\n"
            f"---------------------------------------"
            f"{detay_metni}\n\n"
            f"✅ Başarılı TP: `{basarili}` | ❌ Başarısız SL: `{basarisiz}`\n"
            f"📈 Başarı Oranı: `%{basari_orani:.1f}`"
        )
        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = True
    await update.message.reply_text("🟢 Testnet Dinamik Kurtarma Botu Aktif!")

async def durdur_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ Bot durduruldu.")

async def kapat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        for pos in exchange.fetch_positions():
            kontrat = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
            if kontrat > 0:
                pozisyonu_kapat(pos['symbol'], str(pos.get('side', '')).upper(), kontrat, f"🛑 *MANUEL KAPATMA*\n📌 `{pos['symbol']}` kapatıldı.")
        await update.message.reply_text("✅ Tüm pozisyonlar kapatıldı ve emirler iptal edildi.")
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU
    print("🚀 Testnet Tarama & Dinamik Martingale Kurtarma Döngüsü Başlatıldı.", flush=True)
    try:
        exchange.load_markets()
        yapay_zekayi_egit_ve_guncelle()
    except Exception: pass
    
    onceki_aktif_semboller = set()
    
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5)
                continue

            try:
                raw_positions = exchange.fetch_positions()
                aktif_borsa_map = {p['symbol']: p for p in raw_positions if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0}
            except Exception:
                aktif_borsa_map = {}

            su_anki_aktif_semboller = set(aktif_borsa_map.keys())
            kapanan_semboller = onceki_aktif_semboller - su_anki_aktif_semboller
            
            for kapatilan_sym in kapanan_semboller:
                basarili_mi = False
                pnl_cikti = 0.0
                try:
                    ledger = exchange.fetch_ledger(kapatilan_sym, limit=5)
                    for l in reversed(ledger):
                        if l.get('type') == 'realized_pnl':
                            pnl_cikti = float(l.get('amount', 0))
                            break
                    if pnl_cikti > 0: basarili_mi = True
                except Exception:
                    try:
                        trades = exchange.fetch_my_trades(kapatilan_sym, limit=5)
                        if trades:
                            pnl_cikti = float(trades[-1].get('info', {}).get('pnl', trades[-1].get('realizedPnl', 0)) or 0)
                            if pnl_cikti > 0: basarili_mi = True
                    except Exception: pass

                with state_lock:
                    if kapatilan_sym in AKTIF_GRID_SISTEMLERI:
                        del AKTIF_GRID_SISTEMLERI[kapatilan_sym]
                    
                    mevcut_adim = int(ANALitik_HAFIZA.get("martingale_adim", 1))

                    if basarili_mi:
                        ANALitik_HAFIZA["basarili_islem_sayisi"] = int(ANALitik_HAFIZA.get("basarili_islem_sayisi", 0)) + 1
                        mesaj_str = (
                            f"🎯 *KÂR ALINDI - HEDEF TUTTU!*\n"
                            f"📌 `{kapatilan_sym}` başarıyla kârla kapandı (`+{pnl_cikti:.2f} USDT`).\n"
                            f"🏆 Tamamlanan Başarılı Kademe: `{mevcut_adim}. Adım`\n"
                            f"🔄 *Zincir başarıyla tamamlandı -> Tekrar 1. Kademeye dönülüyor.*"
                        )
                        ANALitik_HAFIZA["martingale_adim"] = 1
                        ANALitik_HAFIZA["kumuletif_zarar"] = 0.0
                    else:
                        ANALitik_HAFIZA["basarisiz_islem_sayisi"] = int(ANALitik_HAFIZA.get("basarisiz_islem_sayisi", 0)) + 1
                        mevcut_kum = float(ANALitik_HAFIZA.get("kumuletif_zarar", 0.0))
                        gerceklesen_zarar = abs(pnl_cikti) if pnl_cikti < 0 else (BAZ_MARJIN * SL_YUZDE)
                        yeni_kum = mevcut_kum + gerceklesen_zarar
                        ANALitik_HAFIZA["kumuletif_zarar"] = yeni_kum
                        
                        if mevcut_adim >= MAKS_MARTINGALE_ADIM:
                            mesaj_str = (
                                f"🛑 *MAKSIMUM KADEMEYE ULAŞILDI ({MAKS_MARTINGALE_ADIM}. Adım)*\n"
                                f"📌 `{kapatilan_sym}` stop oldu (`{pnl_cikti:.2f} USDT`).\n"
                                f"⚠️ Kasa güvenliği için kademe döngüsü başa (1. Adım) sıfırlanıyor."
                            )
                            ANALitik_HAFIZA["martingale_adim"] = 1
                            ANALitik_HAFIZA["kumuletif_zarar"] = 0.0
                        else:
                            sonraki_adim = mevcut_adim + 1
                            ANALitik_HAFIZA["martingale_adim"] = sonraki_adim
                            mesaj_str = (
                                f"📉 *ZARAR YAZDI (SL TETİKLENDİ)*\n"
                                f"📌 `{kapatilan_sym}` zarar ile kapandı (`{pnl_cikti:.2f} USDT`).\n"
                                f"📊 Kümülatif Zarar: `{yeni_kum:.2f} USDT`\n"
                                f"🔄 *Sıradaki Kademe:* `{sonraki_adim}. Kademe` (En güçlü fırsat aranıyor...)"
                            )

                    COIN_COOLDOWNLAR[kapatilan_sym] = {"zaman": float(time.time() + COOLDOWN_SURESI_SANIYE), "son_yon": ""}
                hafizayi_kaydet()
                telegram_mesaj_gonder(mesaj_str)

            onceki_aktif_semboller = su_anki_aktif_semboller

            # Canlı Pozisyon Koruma Kontrolü
            for symbol, pos in list(aktif_borsa_map.items()):
                try: guncel_fiyat = exchange.fetch_ticker(symbol)['last']
                except Exception: continue

                yon = str(pos.get('side', '')).upper()
                merkez = float(pos.get('entryPrice', 0))
                kaldirac = int(pos.get('leverage', HEDEF_KALDIRAC))
                pnl = float(pos.get('unrealizedPnl', 0))
                kontrat = float(pos.get('contracts', 0) or pos.get('size', 0) or 1.0)
                
                fark = (guncel_fiyat - merkez) / merkez if yon == "LONG" else (merkez - guncel_fiyat) / merkez
                roe = fark * 100 * kaldirac

                if roe >= (TP_YUZDE * 100):
                    pozisyonu_kapat(symbol, yon, kontrat, f"🎯 *HEDEF KÂR ALINDI*\n📌 `{symbol}` | Kâr: `+{pnl:.2f} USDT` (`%{roe:.2f}`)")
                elif roe <= -(SL_YUZDE * 100):
                    pozisyonu_kapat(symbol, yon, kontrat, f"🛑 *ZARAR KESİLDİ*\n📌 `{symbol}` | Zarar: `{pnl:.2f} USDT` (`%{roe:.2f}`)")

            # ==========================================================
            # TÜM COİNLERİ DETAYLI TARAMA VE EN İYİSİNİ SEÇME (SCORING)
            # ==========================================================
            taranan_sinyaller = []

            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU: break
                
                try:
                    guncel_fiyat = exchange.fetch_ticker(symbol)['last']
                    
                    # Çoklu Zaman Dilimi Trend Doğrulaması
                    ohlcv_4h = exchange.fetch_ohlcv(symbol, timeframe='4h', limit=20)
                    df_4h = pd.DataFrame(ohlcv_4h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    t_4h = "LONG" if ta.trend.ema_indicator(df_4h['close'], window=5).iloc[-1] > ta.trend.ema_indicator(df_4h['close'], window=13).iloc[-1] else "SHORT"

                    ohlcv_1h = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=20)
                    df_1h = pd.DataFrame(ohlcv_1h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    t_1h = "LONG" if ta.trend.ema_indicator(df_1h['close'], window=5).iloc[-1] > ta.trend.ema_indicator(df_1h['close'], window=13).iloc[-1] else "SHORT"

                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=40)
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

                    ema5 = ta.trend.ema_indicator(df['close'], window=5).iloc[-1]
                    ema13 = ta.trend.ema_indicator(df['close'], window=13).iloc[-1]
                    rsi = ta.momentum.rsi(df['close'], window=14).iloc[-1]
                    adx_val = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14).adx().iloc[-1]
                    atr_degeri = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range().iloc[-1]
                    atr_yuzde = float((atr_degeri / guncel_fiyat) * 100)

                    coin_yonu = "LONG" if ema5 > ema13 else "SHORT"
                    
                    # Trend Filtreleri
                    if adx_val < 25 or coin_yonu != t_4h or coin_yonu != t_1h: 
                        continue

                    # Dinamik Puanlama (En güçlü teknik skor)
                    temel_puan = adx_val + (50 - abs(50 - rsi))

                    if symbol not in aktif_borsa_map:
                        with state_lock:
                            cooldown_veri = COIN_COOLDOWNLAR.get(symbol)
                            if cooldown_veri and isinstance(cooldown_veri, dict):
                                if time.time() < cooldown_veri.get("zaman", 0): continue

                        if not yapay_zeka_islem_onayi(rsi, adx_val, float(ema5 - ema13), (1 if coin_yonu == 'LONG' else -1), atr_yuzde, COIN_ID_MAP.get(symbol, 0)):
                            continue

                        taranan_sinyaller.append({
                            "symbol": symbol, "puan": temel_puan, "yon": coin_yonu, 
                            "rsi": rsi, "fiyat": guncel_fiyat, "adx": adx_val
                        })
                except Exception: continue

            # En yüksek puana sahip olan coini seç (Örn: XLP yerine o an en iyi olan Doge vb.)
            taranan_sinyaller.sort(key=lambda x: x["puan"], reverse=True)

            for sinyal in taranan_sinyaller:
                if not BOT_CALISIYOR_MU: break
                if len(aktif_borsa_map) >= 1: break # Tek seferde tek aktif pozisyon kuralı

                try:
                    exchange.set_leverage(HEDEF_KALDIRAC, sinyal["symbol"])
                    market = exchange.market(sinyal["symbol"])
                    
                    with state_lock:
                        m_adim = int(ANALitik_HAFIZA.get("martingale_adim", 1))
                        kum_zarar = float(ANALitik_HAFIZA.get("kumuletif_zarar", 0.0))

                    # Martingal kademe marjin hesaplaması
                    if m_adim == 1:
                        secilen_marjin = BAZ_MARJIN
                    else:
                        hedef_net_artis = 0.50 
                        pay = kum_zarar + hedef_net_artis
                        payda = (HEDEF_KALDIRAC * TP_YUZDE) - (HEDEF_KALDIRAC * KOMISYON_ORANI)
                        secilen_marjin = pay / payda if payda > 0 else BAZ_MARJIN * (m_adim * 1.5)

                    toplam_bakiye = float(exchange.fetch_balance()['total'].get('USDT', 0))
                    if secilen_marjin > toplam_bakiye * 0.50:
                        secilen_marjin = toplam_bakiye * 0.30

                    min_asgari = float(market['limits']['amount']['min'] or 1.0)
                    miktar = float(exchange.amount_to_precision(sinyal["symbol"], max((secilen_marjin * HEDEF_KALDIRAC) / sinyal["fiyat"] / float(market.get('contractSize', 1.0)), min_asgari)))
                    
                    islem_yonu = 'buy' if sinyal["yon"] == 'LONG' else 'sell'
                    giris_fiyati = sinyal["fiyat"]
                    
                    # Testnet emir açılışı
                    exchange.create_order(sinyal["symbol"], 'market', islem_yonu, miktar)

                    if sinyal["yon"] == 'LONG':
                        tp_fiyat = giris_fiyati * (1 + (TP_YUZDE / HEDEF_KALDIRAC))
                        sl_fiyat = giris_fiyati * (1 - (SL_YUZDE / HEDEF_KALDIRAC))
                        kapat_yon = 'sell'
                    else:
                        tp_fiyat = giris_fiyati * (1 - (TP_YUZDE / HEDEF_KALDIRAC))
                        sl_fiyat = giris_fiyati * (1 + (SL_YUZDE / HEDEF_KALDIRAC))
                        kapat_yon = 'buy'

                    try:
                        exchange.create_order(sinyal["symbol"], 'limit', kapat_yon, miktar, tp_fiyat, {'reduceOnly': True})
                        exchange.create_order(sinyal["symbol"], 'stop', kapat_yon, miktar, sl_fiyat, {'stopPrice': sl_fiyat, 'reduceOnly': True})
                    except Exception as order_err:
                        print(f"Emir Kayıt Hatası: {order_err}", flush=True)

                    with state_lock:
                        AKTIF_GRID_SISTEMLERI[sinyal["symbol"]] = {"giris_rsi": float(sinyal["rsi"])}
                    hafizayi_kaydet()
                    
                    telegram_mesaj_gonder(
                        f"💎 *TESTNET KADEME İŞLEMİ AÇILDI*\n"
                        f"📌 `{sinyal['symbol']}` | Yön: `{sinyal['yon']}` (10x)\n"
                        f"🔢 *Mevcut Kademe:* `{m_adim}. Kademe`\n"
                        f"📊 Teknik Skor: `{sinyal['puan']:.1f}` (En iyi fırsat seçildi)\n"
                        f"💰 Marjin: `{secilen_marjin:.2f} USDT`\n"
                        f"🎯 TP: `{tp_fiyat:.4f}` | 🛑 SL: `{sl_fiyat:.4f}`"
                    )
                    break
                except Exception as e:
                    print(f"❌ İşlem hatası: {e}", flush=True)

        except Exception: pass
        time.sleep(5)

if __name__ == '__main__':
    t = threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True)
    t.start()
    
    app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app_tg.add_handler(CommandHandler("durum", durum_komutu))
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))
    
    print("🤖 Testnet Dinamik Kademe Botu Çalışıyor...", flush=True)
    app_tg.run_polling()
