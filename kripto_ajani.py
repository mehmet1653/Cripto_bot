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

TAKIP_EDILENLER = ['AVAX/USDT:USDT', 'HYPE/USDT:USDT', 'SOL/USDT:USDT', 'XRP/USDT:USDT']
BOT_CALISIYOR_MU = True
KALDIRAC = 10

# ==================== SUPABASE HAFIZA FONKSİYONLARI ====================
def hafizayi_yukle():
    try:
        response = supabase.table("bot_hafiza").select("*").eq("id", 1).execute()
        if response.data and len(response.data) > 0:
            veri = response.data[0]
            print("☁️ Supabase hafızası başarıyla yüklendi.")
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
        print(f"Supabase hafıza yükleme hatası: {e}")
        
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
    hafizayi_kaydet_db(varsayilan["aktif_sistemler"], varsayilan["analitik"])
    return varsayilan

def hafizayi_kaydet_db(aktif_sistemler_data, analitik_data):
    try:
        payload = {
            "id": 1,
            "aktif_sistemler": aktif_sistemler_data,
            "analitik": analitik_data
        }
        supabase.table("bot_hafiza").upsert(payload).execute()
    except Exception as e:
        print(f"Supabase hafıza kaydetme hatası: {e}")

def hafizayi_kaydet():
    hafizayi_kaydet_db(AKTIF_GRID_SISTEMLERI, ANALitik_HAFIZA)

kalici_veri = hafizayi_yukle()
AKTIF_GRID_SISTEMLERI = kalici_veri.get("aktif_sistemler", {})
ANALitik_HAFIZA = kalici_veri.get("analitik", {
    "basarisiz_analizler": [],
    "basarili_islem_sayisi": 0,
    "basarisiz_islem_sayisi": 0,
    "gunluk_net_kar_usd": 0.0,
    "egitim_verileri": []
})

# ==================== KASA KORUMA & RİSK YÖNETİMİ ====================
HEDEF_ROESINI_ISTENEN = 10.0      
ZARAR_KES_ROESINI_ISTENEN = 5.0 
MIN_ADX_GUCU = 20.0              
# =================================================================     

# ==================== YAPAY ZEKA MODELİ (ML) ====================
ai_model = RandomForestClassifier(n_estimators=50, random_state=42)
ai_model_egitildi = False

def yapay_zekayi_egit_ve_guncelle():
    global ai_model, ai_model_egitildi
    veriler = ANALitik_HAFIZA.get("egitim_verileri", [])
    
    if len(veriler) < 5:
        ai_model_egitildi = False
        return
        
    try:
        X = []
        y = []
        for item in veriler:
            X.append(item[:4]) 
            y.append(item[4])  
            
        X = np.array(X)
        y = np.array(y)
        
        if len(set(y)) < 2:
            ai_model_egitildi = False
            return
            
        ai_model.fit(X, y)
        ai_model_egitildi = True
        print("🧠 Yapay Zeka Modeli Supabase verileriyle güncellendi.")
    except Exception as e:
        print(f"Yapay zeka eğitim hatası: {e}")
        ai_model_egitildi = False

def yapay_zeka_islem_onayi(rsi, adx, ema_fark, yon_kod):
    if not ai_model_egitildi:
        return True 
    try:
        X_test = np.array([[rsi, adx, ema_fark, yon_kod]])
        tahmin = ai_model.predict(X_test)[0]
        return bool(tahmin == 1)
    except Exception:
        return True
# ================================================================

def telegram_mesaj_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print(f"[TEST EKRANI] -> {mesaj}")
        return
        
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": mesaj, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram Gönderme Hatası: {e}")

@app.route('/')
def home():
    durum_str = "AKTİF 🟢" if BOT_CALISIYOR_MU else "BEKLEMEDE ⏸️"
    ai_durum = "Aktif & Eğitildi 🧠" if ai_model_egitildi else "Veri Toplanıyor 🔄"
    return f"Supabase Hafızalı AI Bot | Durum: {durum_str} | ML Model: {ai_durum}"

def set_isolated_leverage_safely(symbol, leverage):
    try:
        exchange.set_margin_mode('isolated', symbol, {'leverage': leverage})
    except Exception:
        pass
        
    try:
        exchange.set_leverage(leverage, symbol)
        return True
    except Exception:
        return False

def pozisyonu_garantili_kapat(symbol, yon, miktar, sebep_mesaji, rsi=0, adx=0, ema_fark=0, basarili=True, is_noise=False):
    kapatma_yonu = 'sell' if yon == 'LONG' else 'buy'
    
    try:
        open_orders = exchange.fetch_open_orders(symbol)
        for ord_item in open_orders:
            exchange.cancel_order(ord_item['id'], symbol)
    except Exception:
        pass

    try:
        market_info = exchange.market(symbol)
        min_amount = float(market_info['limits']['amount']['min'] or 1.0)
        if miktar < min_amount:
            miktar = min_amount
        miktar = float(exchange.amount_to_precision(symbol, miktar))

        exchange.create_market_order(symbol, kapatma_yonu, miktar, {'reduce_only': True})
    except Exception as e:
        try:
            time.sleep(0.2)
            ticker = exchange.fetch_ticker(symbol)
            guvenli_fiyat = ticker['ask'] if kapatma_yonu == 'buy' else ticker['bid']
            exchange.create_order(symbol, 'limit', kapatma_yonu, miktar, guvenli_fiyat, {'timeInForce': 'IOC', 'reduce_only': True})
        except Exception as e2:
            print(f"Pozisyon kapatma hatası: {e2}")

    if not is_noise:
        yon_kod = 1 if yon == 'LONG' else -1
        sonuc_kod = 1 if basarili else 0
        ANALitik_HAFIZA["egitim_verileri"].append([rsi, adx, ema_fark, yon_kod, sonuc_kod])
        if len(ANALitik_HAFIZA["egitim_verileri"]) > 100:
            ANALitik_HAFIZA["egitim_verileri"].pop(0)
            
        yapay_zekayi_egit_ve_guncelle()

    if symbol in AKTIF_GRID_SISTEMLERI:
        del AKTIF_GRID_SISTEMLERI[symbol]
        hafizayi_kaydet()

    if sebep_mesaji:
        telegram_mesaj_gonder(sebep_mesaji)

def get_account_status_summary():
    try:
        balance = exchange.fetch_balance()
        total_usdt = float(balance['total'].get('USDT', 0))
        free_usdt = float(balance['free'].get('USDT', 0))
        
        try:
            borsa_pozisyonlari = exchange.fetch_positions()
        except Exception:
            borsa_pozisyonlari = []

        toplam_anlik_pnl = 0.0
        aktif_ozet_listesi = []

        for pos in borsa_pozisyonlari:
            contracts = float(pos.get('contracts', 0))
            if contracts > 0:
                sym = pos.get('symbol')
                side = str(pos.get('side', '')).upper()
                if not side:
                    side = "LONG" if float(pos.get('notional', 0)) > 0 else "SHORT"
                
                entry_price = float(pos.get('entryPrice', 0))
                pnl = float(pos.get('unrealizedPnl', 0))
                roe = float(pos.get('percentage', 0))
                
                if roe == 0 and entry_price > 0:
                    mark_price = float(pos.get('markPrice', entry_price))
                    fark = (mark_price - entry_price) / entry_price
                    if side == 'SHORT':
                        fark = -fark
                    roe = fark * 100 * KALDIRAC

                margin = float(pos.get('initialMargin', 0))
                if margin == 0:
                    notional = float(pos.get('notional', 0))
                    if notional > 0:
                        margin = notional / KALDIRAC

                toplam_anlik_pnl += pnl

                aktif_ozet_listesi.append({
                    'symbol': sym,
                    'side': side,
                    'leverage': int(pos.get('leverage', KALDIRAC)),
                    'pnl': pnl,
                    'roe': roe,
                    'contracts': contracts,
                    'entryPrice': entry_price,
                    'margin': margin
                })

        gunluk_pnl = ANALitik_HAFIZA['gunluk_net_kar_usd']
        ai_durum_str = "🧠 Aktif" if ai_model_egitildi else "🔄 Veri Topluyor"
        
        summary = f"📊 *SUPABASE & AI RAPORU*\n\n"
        summary += f"🤖 **AI Model Durumu:** `{ai_durum_str}`\n"
        summary += f"💰 **Toplam Kasa:** `{total_usdt:.2f} USDT`\n"
        summary += f"💵 **Kullanılabilir:** `{free_usdt:.2f} USDT`\n"
        
        if gunluk_pnl >= 0:
            summary += f"📅 **Günlük Net Kâr:** `+{gunluk_pnl:.2f} USDT` 🟢\n"
        else:
            summary += f"📅 **Günlük Net Zarar:** `{gunluk_pnl:.2f} USDT` 🔴\n"
            
        if toplam_anlik_pnl >= 0:
            summary += f"📈 **Anlık Açık PnL:** `+{toplam_anlik_pnl:.2f} USDT` 🟢\n"
        else:
            summary += f"📉 **Anlık Açık PnL:** `{toplam_anlik_pnl:.2f} USDT` 🔴\n"
            
        summary += f"📊 **Aktif Pozisyon:** `{len(aktif_ozet_listesi)}`\n"
        summary += f"🎯 **Başarılı İşlem:** `{ANALitik_HAFIZA['basarili_islem_sayisi']}` adet\n"
        summary += f"🛑 **Başarısız (Zarar Kes):** `{ANALitik_HAFIZA['basarisiz_islem_sayisi']}` adet\n"
        
        if aktif_ozet_listesi:
            summary += f"\n-----------------------------------\n"
            for p in aktif_ozet_listesi:
                pnl_isaret = "+" if p['pnl'] >= 0 else ""
                summary += f"🔹 **{p['symbol']}** ({p['side']} | {p['leverage']}x İzole)\n"
                summary += f"   Giriş: `{p['entryPrice']}` | Kontrat: `{p['contracts']}` | Marjin: `{p['margin']:.2f}$`\n"
                summary += f"   K/Z: `{pnl_isaret}{p['pnl']:.2f} USDT` (`{pnl_isaret}{p['roe']:.2f}%`)\n"
        
        return summary
    except Exception as e:
        return f"Kasa durumu alınırken hata oluştu: {str(e)}"

async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_text = get_account_status_summary()
    await update.message.reply_text(status_text, parse_mode='Markdown')

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = True
    await update.message.reply_text("🟢 *Bot aktif edildi ve taranıyor!*", parse_mode='Markdown')

async def durdur_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ *Bot durduruldu.*", parse_mode='Markdown')

async def kapat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 *Tüm aktif işlemler kapatılıyor...*", parse_mode='Markdown')
    
    try:
        borsa_pozisyonlari = exchange.fetch_positions()
        kapatilanlar = 0
        for pos in borsa_pozisyonlari:
            if float(pos.get('contracts', 0)) > 0:
                sym = pos.get('symbol')
                side = str(pos.get('side', '')).upper()
                if not side:
                    side = "LONG" if float(pos.get('notional', 0)) > 0 else "SHORT"
                
                kayitli_veri = AKTIF_GRID_SISTEMLERI.get(sym, {})
                rsi_val = kayitli_veri.get("giris_rsi", 50.0)
                
                pozisyonu_garantili_kapat(sym, side, float(pos['contracts']), f"🛑 *MANUEL KAPATMA (/kapat)* - `{sym}`", rsi=rsi_val, basarili=False)
                kapatilanlar += 1

        AKTIF_GRID_SISTEMLERI.clear()
        hafizayi_kaydet()
        await update.message.reply_text(f"✅ İşlem tamamlandı. Toplam `{kapatilanlar}` pozisyon kapatıldı.", parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Kapatma sırasında hata: `{str(e)}`", parse_mode='Markdown')

def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, ANALitik_HAFIZA
    print("🚀 Supabase Hafızalı Tarayıcı aktif.")
    
    try:
        exchange.load_markets()
        yapay_zekayi_egit_ve_guncelle()
    except Exception as e:
        print(f"Piyasalar yüklenemedi: {e}")
    
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(3)
                continue

            try:
                borsa_pozisyonlari = exchange.fetch_positions()
            except Exception:
                borsa_pozisyonlari = []

            aktif_borsa_map = {}
            for pos in borsa_pozisyonlari:
                if float(pos.get('contracts', 0)) > 0:
                    sym = pos.get('symbol')
                    side = str(pos.get('side', '')).upper()
                    if not side:
                        side = "LONG" if float(pos.get('notional', 0)) > 0 else "SHORT"
                    aktif_borsa_map[sym] = {
                        "side": side,
                        "contracts": float(pos['contracts']),
                        "entryPrice": float(pos.get('entryPrice', 0)),
                        "unrealizedPnl": float(pos.get('unrealizedPnl', 0)),
                        "percentage": float(pos.get('percentage', 0))
                    }

            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU:
                    break
                    
                try:
                    ticker = exchange.fetch_ticker(symbol)
                    guncel_fiyat = ticker['last']
                except Exception:
                    continue

                if symbol in aktif_borsa_map:
                    pos_bilgi = aktif_borsa_map[symbol]
                    yon = pos_bilgi["side"]
                    merkez = pos_bilgi["entryPrice"]
                    
                    fark_orani = (guncel_fiyat - merkez) / merkez
                    if yon == "SHORT": 
                        fark_orani = -fark_orani
                        
                    net_kar_zarar_yuzdesi = fark_orani * 100 * KALDIRAC
                    
                    istemci_veri = AKTIF_GRID_SISTEMLERI.get(symbol, {})
                    rsi_degeri = istemci_veri.get("giris_rsi", 50.0)
                    adx_degeri = istemci_veri.get("giris_adx", 25.0)
                    ema_fark_degeri = istemci_veri.get("ema_fark", 0.0)
                    
                    if net_kar_zarar_yuzdesi >= HEDEF_ROESINI_ISTENEN or pos_bilgi["percentage"] >= HEDEF_ROESINI_ISTENEN:
                        tahmini_kar_usd = abs(pos_bilgi["unrealizedPnl"]) if pos_bilgi["unrealizedPnl"] > 0 else 2.0
                        ANALitik_HAFIZA["gunluk_net_kar_usd"] += tahmini_kar_usd
                        ANALitik_HAFIZA["basarili_islem_sayisi"] += 1
                        hafizayi_kaydet()
                        
                        mesaj = f"🚀 *KÂR ALINDI* - `{symbol}` (`+{net_kar_zarar_yuzdesi:.2f}%`)"
                        pozisyonu_garantili_kapat(symbol, yon, pos_bilgi["contracts"], mesaj, rsi=rsi_degeri, adx=adx_degeri, ema_fark=ema_fark_degeri, basarili=True)
                        
                    elif net_kar_zarar_yuzdesi <= -ZARAR_KES_ROESINI_ISTENEN or pos_bilgi["percentage"] <= -ZARAR_KES_ROESINI_ISTENEN:
                        tahmini_zarar_usd = abs(pos_bilgi["unrealizedPnl"]) if pos_bilgi["unrealizedPnl"] < 0 else 1.0
                        
                        is_noise_flag = False
                        try:
                            check_ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=20)
                            check_df = pd.DataFrame(check_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                            ortalama_hacim = check_df['volume'].iloc[:-1].mean()
                            son_hacim = check_df['volume'].iloc[-1]
                            if son_hacim > (ortalama_hacim * 3.0):
                                is_noise_flag = True
                        except Exception:
                            pass

                        ANALitik_HAFIZA["gunluk_net_kar_usd"] -= tahmini_zarar_usd
                        ANALitik_HAFIZA["basarisiz_islem_sayisi"] += 1
                        
                        analitik_hata_notu = {
                            "symbol": symbol,
                            "yanlis_yon": yon,
                            "giris_fiyati": merkez,
                            "zarar_orani": net_kar_zarar_yuzdesi,
                            "zarar_usd": tahmini_zarar_usd,
                            "zaman": time.strftime('%H:%M:%S'),
                            "gurultu_mu": is_noise_flag
                        }
                        ANALitik_HAFIZA["basarisiz_analizler"].append(analitik_hata_notu)
                        hafizayi_kaydet()
                        
                        if is_noise_flag:
                            mesaj = f"⚡ *ANOMALİ TESPİT EDİLDİ (GÜRÜLTÜ SÜZÜLDÜ)* - `{symbol}` (`{net_kar_zarar_yuzdesi:.2f}%`)"
                        else:
                            mesaj = f"🛑 *ZARAR KES & ÖĞRETİLDİ* - `{symbol}` (`{net_kar_zarar_yuzdesi:.2f}%`)"
                            
                        pozisyonu_garantili_kapat(symbol, yon, pos_bilgi["contracts"], mesaj, rsi=rsi_degeri, adx=adx_degeri, ema_fark=ema_fark_degeri, basarili=False, is_noise=is_noise_flag)
                        
                    continue

                if symbol in aktif_borsa_map:
                    continue

                try:
                    balance = exchange.fetch_balance()
                    toplam_bakiye = float(balance['total'].get('USDT', 0))
                except Exception:
                    continue

                hedef_marjin = toplam_bakiye / 4.0
                if toplam_bakiye < hedef_marjin:
                    continue

                try:
                    ohlcv_4h = exchange.fetch_ohlcv(symbol, timeframe='4h', limit=30)
                    df_4h = pd.DataFrame(ohlcv_4h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    ema50_4h = ta.trend.ema_indicator(df_4h['close'], window=50).iloc[-1]
                    ema200_4h = ta.trend.ema_indicator(df_4h['close'], window=200).iloc[-1]
                    
                    # 🔥 EKLEDİĞİMİZ FİLTRE: 4 Saatlik Mumun Rengi ve Gövde Durumu Teyidi
                    son_mum_4h = df_4h.iloc[-1]
                    mum_yesil_4h = son_mum_4h['close'] > son_mum_4h['open']
                    
                    ana_trend_yonu = "LONG" if (ema50_4h > ema200_4h and mum_yesil_4h) else ("SHORT" if (ema50_4h < ema200_4h and not mum_yesil_4h) else None)
                    
                    if not ana_trend_yonu:
                        continue # Eğer 4 saatlik mum kararsızsa veya trend ile mum rengi çelişiyorsa işlem arama

                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    
                    ema7 = ta.trend.ema_indicator(df['close'], window=7).iloc[-1]
                    ema21 = ta.trend.ema_indicator(df['close'], window=21).iloc[-1]
                    rsi = ta.momentum.rsi(df['close'], window=14).iloc[-1]
                    
                    adx_obj = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14)
                    adx = adx_obj.adx().iloc[-1]
                    
                    ema_fark = float(ema7 - ema21)
                    
                    sinyal = None
                    if ana_trend_yonu == "LONG" and ema7 > ema21 and rsi < 58 and adx > MIN_ADX_GUCU:
                        yon_kod = 1
                        if yapay_zeka_islem_onayi(rsi, adx, ema_fark, yon_kod):
                            sinyal = "LONG"
                    elif ana_trend_yonu == "SHORT" and ema7 < ema21 and rsi > 42 and adx > MIN_ADX_GUCU:
                        yon_kod = -1
                        if yapay_zeka_islem_onayi(rsi, adx, ema_fark, yon_kod):
                            sinyal = "SHORT"
                            
                    if sinyal:
                        set_isolated_leverage_safely(symbol, KALDIRAC)
                        ticker = exchange.fetch_ticker(symbol)
                        giris_fiyati = ticker['ask'] if sinyal == 'LONG' else ticker['bid']
                        
                        notional_deger = hedef_marjin * KALDIRAC
                        miktar = notional_deger / giris_fiyati
                        
                        market_info = exchange.market(symbol)
                        min_amount = float(market_info['limits']['amount']['min'] or 1.0)
                        if miktar < min_amount:
                            miktar = min_amount
                        miktar = float(exchange.amount_to_precision(symbol, miktar))
                        
                        islem_yonu = 'buy' if sinyal == 'LONG' else 'sell'
                        exchange.create_market_order(symbol, islem_yonu, miktar)
                        
                        AKTIF_GRID_SISTEMLERI[symbol] = {
                            "giris_rsi": float(rsi),
                            "giris_adx": float(adx),
                            "ema_fark": float(ema_fark)
                        }
                        hafizayi_kaydet()
                        
                        telegram_mesaj_gonder(f"🚀 *YENİ İŞLEM AÇILDI ({sinyal})* - `{symbol}`\nGiriş: `{giris_fiyati}` | ADX: `{adx:.1f}` | RSI: `{rsi:.1f}`")
                except Exception as e:
                    print(f"{symbol} tarama hatası: {e}")
                    
        except Exception as e:
            print(f"Tarayıcı döngü hatası: {e}")
            
        time.sleep(10)

def main():
    threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True).start()
    
    app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app_tg.add_handler(CommandHandler("durum", durum_komutu))
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))
    
    threading.Thread(target=app_tg.run_polling, kwargs={'drop_pending_updates': True}, daemon=True).start()
    
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

if __name__ == '__main__':
    main()
