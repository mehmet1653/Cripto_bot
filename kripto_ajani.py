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
from datetime import date, datetime
from dotenv import load_dotenv
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler
from supabase import create_client, Client

# ==================== .ENV ====================
if os.path.exists('/etc/secrets/.env'):
    load_dotenv('/etc/secrets/.env', override=True)
    print("✅ .env yüklendi", flush=True)
else:
    load_dotenv(override=True)

# ==================== FLASK ====================
app = Flask(__name__)

@app.route('/')
def home():
    return "Nötr Grid Bot aktif!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ==================== API ====================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_KEY: sys.exit(1)
if not TELEGRAM_TOKEN or not CHAT_ID: sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

exchange = ccxt.gate({
    'apiKey': os.environ.get("GATE_API_KEY", "").strip(),
    'secret': os.environ.get("GATE_SECRET", "").strip(),
    'enableRateLimit': True,
    'timeout': 30000,
    'options': {'defaultType': 'swap'}
})
exchange.set_sandbox_mode(True)

# ==================== AYARLAR ====================
SEMBOL = 'BTC/USDT:USDT'
KALDIRAC = 3
POZISYON_ORANI = 0.15

KOMISYON_ORANI = 0.001
SPREAD_MALIYETI = 0.0005
TOPLAM_MALIYET_ORANI = (KOMISYON_ORANI * 2) + SPREAD_MALIYETI

# ✅ GRID - YAKIN ARALIKLAR
TOPLAM_SEVIYE = 12              # 6 LONG + 6 SHORT
LONG_SEVIYE = 6
SHORT_SEVIYE = 6
SEVIYE_ARASI_YUZDE = 0.003       # Her seviye arası %0.3 (yakın)

# Trend algılama
ADX_YATAY_ESIGI = 20
ADX_TREND_ESIGI = 28
ATR_VOLATIL_CARPAN = 2.5

# Risk
MAKS_GRID_ZARAR = 0.10
KAR_KILITLEME = 5.0

# Zamanlama
PIYASA_ANALIZ_SURESI = 15 * 60
GRID_KONTROL_SURESI = 20
ANA_DONGU_SURESI = 30

BOT_CALISIYOR_MU = True
state_lock = threading.Lock()

# ==================== GRID DURUMU ====================
AKTIF_GRID = {
    'aktif': False,
    'merkez': 0,
    'ust': 0,
    'alt': 0,
    'seviyeler': [],
    'marj_per_seviye': 0,
    'kurulus_zamani': 0,
    'toplam_kar': 0,
    'kilitlenen_kar': 0
}

ARDISIK_ZARAR_SAYACI = 0
SON_ARDISIK_ZARAR_ZAMANI = 0
KILL_SWITCH_AKTIF = False

# ==================== PİYASA ANALİZİ ====================
def piyasa_modu_bul():
    try:
        ohlcv_1m = exchange.fetch_ohlcv(SEMBOL, timeframe='1m', limit=100)
        ohlcv_5m = exchange.fetch_ohlcv(SEMBOL, timeframe='5m', limit=100)
        ohlcv_15m = exchange.fetch_ohlcv(SEMBOL, timeframe='15m', limit=100)
        ohlcv_1h = exchange.fetch_ohlcv(SEMBOL, timeframe='1h', limit=100)
        
        df_1m = pd.DataFrame(ohlcv_1m, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        df_5m = pd.DataFrame(ohlcv_5m, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        df_15m = pd.DataFrame(ohlcv_15m, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        df_1h = pd.DataFrame(ohlcv_1h, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        
        adx_1m = ta.trend.ADXIndicator(high=df_1m['h'], low=df_1m['l'], close=df_1m['c'], window=14).adx().iloc[-1]
        adx_5m = ta.trend.ADXIndicator(high=df_5m['h'], low=df_5m['l'], close=df_5m['c'], window=14).adx().iloc[-1]
        adx_15m = ta.trend.ADXIndicator(high=df_15m['h'], low=df_15m['l'], close=df_15m['c'], window=14).adx().iloc[-1]
        adx_1h = ta.trend.ADXIndicator(high=df_1h['h'], low=df_1h['l'], close=df_1h['c'], window=14).adx().iloc[-1]
        
        atr_serisi = ta.volatility.AverageTrueRange(high=df_15m['h'], low=df_15m['l'], close=df_15m['c'], window=14).average_true_range()
        atr = atr_serisi.iloc[-1]
        atr_ort = atr_serisi.tail(50).mean()
        
        rsi = ta.momentum.RSIIndicator(close=df_15m['c'], window=14).rsi().iloc[-1]
        anlik = float(df_15m['c'].iloc[-1])
        
        adx_ort = (adx_1m + adx_5m + adx_15m + adx_1h) / 4
        
        if atr_ort > 0 and atr > atr_ort * ATR_VOLATIL_CARPAN:
            return 'VOLATIL', adx_ort, atr, anlik, rsi, atr_ort
        
        if adx_ort > ADX_TREND_ESIGI:
            return 'TREND', adx_ort, atr, anlik, rsi, atr_ort
        
        if adx_ort < ADX_YATAY_ESIGI:
            return 'YATAY', adx_ort, atr, anlik, rsi, atr_ort
        
        return 'BELIRSIZ', adx_ort, atr, anlik, rsi, atr_ort
    
    except Exception as e:
        print(f"⚠️ Analiz hatası: {e}", flush=True)
        return 'BELIRSIZ', 0, 0, 0, 50, 0

# ==================== GRID KUR (NÖTR) ====================
def grid_kur(fiyat, bakiye):
    """
    Nötr grid: Hem LONG hem SHORT seviyeler.
    Alt seviyeler → LONG aç
    Üst seviyeler → SHORT aç
    """
    try:
        adim = fiyat * SEVIYE_ARASI_YUZDE
        
        marj_per_seviye = (bakiye * POZISYON_ORANI) / TOPLAM_SEVIYE
        
        market = exchange.market(SEMBOL)
        contract_size = float(market.get('contractSize', 1.0))
        
        seviyeler = []
        
        print(f"\n🔧 [NÖTR GRID KURULUYOR] {SEMBOL}", flush=True)
        print(f"   Merkez: {fiyat:.2f}", flush=True)
        print(f"   Adım: {adim:.2f} (%{SEVIYE_ARASI_YUZDE*100})", flush=True)
        print(f"   Marj/seviye: {marj_per_seviye:.2f} USDT", flush=True)
        
        # ✅ ALT SEVİYELER (LONG)
        for i in range(1, LONG_SEVIYE + 1):
            seviye_fiyat = fiyat - (adim * i)
            miktar = (marj_per_seviye * KALDIRAC) / seviye_fiyat
            miktar = float(exchange.amount_to_precision(SEMBOL, miktar))
            
            if miktar <= 0: continue
            
            seviyeler.append({
                'fiyat': seviye_fiyat,
                'miktar': miktar,
                'yon': 'LONG',
                'emir_id': None,
                'aktif': True,
                'pozisyon': False
            })
        
        # ✅ ÜST SEVİYELER (SHORT)
        for i in range(1, SHORT_SEVIYE + 1):
            seviye_fiyat = fiyat + (adim * i)
            miktar = (marj_per_seviye * KALDIRAC) / seviye_fiyat
            miktar = float(exchange.amount_to_precision(SEMBOL, miktar))
            
            if miktar <= 0: continue
            
            seviyeler.append({
                'fiyat': seviye_fiyat,
                'miktar': miktar,
                'yon': 'SHORT',
                'emir_id': None,
                'aktif': True,
                'pozisyon': False
            })
        
        # ✅ HER SEVİYEYE EMİR KOY
        for sev in seviyeler:
            try:
                if sev['yon'] == 'LONG':
                    # LONG aç - limit buy
                    order = exchange.create_order(
                        SEMBOL, 'limit', 'buy', sev['miktar'], sev['fiyat'],
                        {'reduceOnly': False}
                    )
                else:
                    # SHORT aç - limit sell
                    order = exchange.create_order(
                        SEMBOL, 'limit', 'sell', sev['miktar'], sev['fiyat'],
                        {'reduceOnly': False}
                    )
                sev['emir_id'] = order['id']
                print(f"   ✅ {sev['yon']} emri: {sev['fiyat']:.2f}", flush=True)
            except Exception as e:
                print(f"   ⚠️ {sev['yon']} emri {sev['fiyat']:.2f}: {e}", flush=True)
                sev['aktif'] = False
        
        AKTIF_GRID['aktif'] = True
        AKTIF_GRID['merkez'] = fiyat
        AKTIF_GRID['alt'] = fiyat - (adim * LONG_SEVIYE)
        AKTIF_GRID['ust'] = fiyat + (adim * SHORT_SEVIYE)
        AKTIF_GRID['seviyeler'] = seviyeler
        AKTIF_GRID['marj_per_seviye'] = marj_per_seviye
        AKTIF_GRID['kurulus_zamani'] = time.time()
        AKTIF_GRID['toplam_kar'] = 0
        AKTIF_GRID['kilitlenen_kar'] = 0
        
        print(f"   ✅ Grid kuruldu | Aralık: {AKTIF_GRID['alt']:.2f} - {AKTIF_GRID['ust']:.2f}", flush=True)
        tg_gonder(
            f"🔧 *NÖTR GRID KURULDU*\n\n"
            f"📌 {SEMBOL}\n"
            f"💰 Merkez: `{fiyat:.2f}`\n"
            f"📉 Alt: `{AKTIF_GRID['alt']:.2f}` ({LONG_SEVIYE} LONG)\n"
            f"📈 Üst: `{AKTIF_GRID['ust']:.2f}` ({SHORT_SEVIYE} SHORT)\n"
            f"💵 Marj/seviye: `{marj_per_seviye:.2f} USDT`\n"
            f"⚙️ Kaldıraç: `{KALDIRAC}x`\n"
            f"🎯 Adım: `%{SEVIYE_ARASI_YUZDE*100}`"
        )
        return True
    except Exception as e:
        print(f"⚠️ Grid kurma hatası: {e}", flush=True)
        return False

# ==================== GRID KAPAT ====================
def grid_kapat(sebep=""):
    try:
        print(f"\n🛑 [GRID KAPATILIYOR] {sebep}", flush=True)
        
        # Tüm emirleri iptal
        try:
            open_orders = exchange.fetch_open_orders(SEMBOL)
            for order in open_orders:
                try: exchange.cancel_order(order['id'], SEMBOL)
                except: pass
        except: pass
        
        # Tüm pozisyonları kapat
        try:
            positions = exchange.fetch_positions([SEMBOL])
            for p in positions:
                k = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                if k > 0:
                    y = str(p.get('side', '')).upper()
                    ky = 'sell' if y == 'LONG' else 'buy'
                    try:
                        exchange.create_order(SEMBOL, 'market', ky, k, None, {'reduceOnly': True})
                    except: pass
        except: pass
        
        toplam_kar = AKTIF_GRID.get('toplam_kar', 0)
        AKTIF_GRID['aktif'] = False
        AKTIF_GRID['seviyeler'] = []
        
        print(f"   ✅ Grid kapatıldı | Kâr: {toplam_kar:.2f} USDT", flush=True)
        tg_gonder(f"🛑 *GRID KAPATILDI*\n\n📌 {SEMBOL}\n📊 {sebep}\n💰 Kâr: `{toplam_kar:.2f} USDT`")
        return True
    except Exception as e:
        print(f"⚠️ Kapatma hatası: {e}", flush=True)
        return False

# ==================== GRID KONTROL ====================
def grid_kontrol():
    if not AKTIF_GRID['aktif']:
        return
    
    try:
        ticker = exchange.fetch_ticker(SEMBOL)
        anlik = float(ticker['last'])
        
        # Aralık dışı mı?
        if anlik < AKTIF_GRID['alt'] * 0.99 or anlik > AKTIF_GRID['ust'] * 1.01:
            grid_kapat(f"Aralık dışı: {anlik:.2f}")
            return
        
        # Açık emirleri al
        open_orders = exchange.fetch_open_orders(SEMBOL)
        open_ids = {o['id']: o for o in open_orders}
        
        # Açık pozisyonları al
        positions = exchange.fetch_positions([SEMBOL])
        aktif_poz = {}
        for p in positions:
            k = float(p.get('contracts', 0) or p.get('size', 0) or 0)
            if k > 0:
                side = str(p.get('side', '')).upper()
                entry = float(p.get('entryPrice', 0))
                aktif_poz[side] = {'miktar': k, 'giris': entry, 'pnl': float(p.get('unrealizedPnl', 0) or 0)}
        
        # Her seviyeyi kontrol et
        for sev in AKTIF_GRID['seviyeler']:
            if not sev['aktif']:
                continue
            
            # Emir açık mı?
            if sev['emir_id'] and sev['emir_id'] not in open_ids:
                # Emir tetiklenmiş olabilir
                try:
                    order = exchange.fetch_order(sev['emir_id'], SEMBOL)
                    if order['status'] == 'closed':
                        # ✅ Tetiklendi
                        print(f"   ✅ Tetiklendi: {sev['yon']} @ {sev['fiyat']:.2f}", flush=True)
                        
                        # Bu seviye için karşıt emir koy (TP)
                        if sev['yon'] == 'LONG':
                            # LONG açıldı → üstte TP (kapat)
                            tp_fiyat = sev['fiyat'] * (1 + SEVIYE_ARASI_YUZDE)
                            try:
                                tp_order = exchange.create_order(
                                    SEMBOL, 'limit', 'sell', sev['miktar'], tp_fiyat,
                                    {'reduceOnly': True}
                                )
                                sev['tp_emir_id'] = tp_order['id']
                                sev['pozisyon'] = True
                                print(f"      🎯 TP: {tp_fiyat:.2f}", flush=True)
                            except Exception as e:
                                print(f"      ⚠️ TP hatası: {e}", flush=True)
                        
                        elif sev['yon'] == 'SHORT':
                            # SHORT açıldı → altta TP
                            tp_fiyat = sev['fiyat'] * (1 - SEVIYE_ARASI_YUZDE)
                            try:
                                tp_order = exchange.create_order(
                                    SEMBOL, 'limit', 'buy', sev['miktar'], tp_fiyat,
                                    {'reduceOnly': True}
                                )
                                sev['tp_emir_id'] = tp_order['id']
                                sev['pozisyon'] = True
                                print(f"      🎯 TP: {tp_fiyat:.2f}", flush=True)
                            except Exception as e:
                                print(f"      ⚠️ TP hatası: {e}", flush=True)
                    else:
                        sev['aktif'] = False
                except:
                    sev['aktif'] = False
        
        # Kâr kilitleme
        if AKTIF_GRID['toplam_kar'] - AKTIF_GRID['kilitlenen_kar'] >= KAR_KILITLEME:
            kilit = AKTIF_GRID['toplam_kar']
            AKTIF_GRID['kilitlenen_kar'] = kilit
            tg_gonder(f"💰 *KÂR KİLİTLENDİ*\n\n{SEMBOL}\nToplam: `{kilit:.2f} USDT`")
    
    except Exception as e:
        print(f"⚠️ Grid kontrol: {e}", flush=True)

# ==================== POZİSYON TAKİBİ ====================
def pozisyon_kar_takip():
    """
    Açık pozisyonları izle, kâr realize olunca AKTIF_GRID['toplam_kar'] güncelle.
    """
    if not AKTIF_GRID['aktif']:
        return
    
    try:
        positions = exchange.fetch_positions([SEMBOL])
        for p in positions:
            k = float(p.get('contracts', 0) or p.get('size', 0) or 0)
            if k > 0:
                # Açık pozisyon var
                pass  # PnL zaten unrealized, kapanınca realize olur
    except: pass

# ==================== TELEGRAM ====================
def tg_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": mesaj}, timeout=5)
    except: pass

async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    try:
        balance = await asyncio.to_thread(exchange.fetch_balance)
        total = float(balance['total'].get('USDT', 0))
        
        grid_durum = "🟢 AKTİF" if AKTIF_GRID['aktif'] else "🔴 PASİF"
        
        pos = [p for p in await asyncio.to_thread(exchange.fetch_positions, [SEMBOL]) if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
        pos_detay = ""
        for p in pos:
            y = str(p.get('side', '')).upper()
            g = float(p.get('entryPrice', 0))
            u = float(p.get('unrealizedPnl', 0) or 0)
            nokta = "🟢" if u >= 0 else "🔴"
            pos_detay += f"\n{nokta} {y} @ `{g:.2f}` | K/Z: `{u:+.2f}`"
        
        grid_detay = ""
        if AKTIF_GRID['aktif']:
            aktif_sev = sum(1 for s in AKTIF_GRID['seviyeler'] if s['aktif'])
            poz_sev = sum(1 for s in AKTIF_GRID['seviyeler'] if s.get('pozisyon', False))
            grid_detay = (
                f"\n📊 Merkez: `{AKTIF_GRID['merkez']:.2f}`"
                f"\n📉 Alt: `{AKTIF_GRID['alt']:.2f}` | 📈 Üst: `{AKTIF_GRID['ust']:.2f}`"
                f"\n🎯 Aktif emir: `{aktif_sev}` | Pozisyon: `{poz_sev}`"
                f"\n💰 Toplam kâr: `{AKTIF_GRID['toplam_kar']:.2f} USDT`"
                f"\n🔒 Kilit: `{AKTIF_GRID['kilitlenen_kar']:.2f} USDT`"
            )
        
        mesaj = (
            f"📊 *NÖTR GRID DURUM*\n\n"
            f"💰 Kasa: `{total:.2f} USDT`\n"
            f"📌 Sembol: `{SEMBOL}`\n"
            f"⚙️ Kaldıraç: `{KALDIRAC}x`\n"
            f"🎯 Grid: {grid_durum}"
            f"{grid_detay}"
            f"{pos_detay}\n\n"
            f"⚡ Kill-Switch: `{'AKTİF' if KILL_SWITCH_AKTIF else 'Pasif'}`"
        )
        await update.message.reply_text(mesaj)
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = True
    await update.message.reply_text("🟢 Nötr grid bot aktif!")

async def durdur_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ Durduruldu.")

async def kapat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    try:
        await asyncio.to_thread(grid_kapat, "Manuel kapatma")
        await update.message.reply_text("✅ Grid kapatıldı.")
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def analiz_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    try:
        mod, adx, atr, fiyat, rsi, atr_ort = await asyncio.to_thread(piyasa_modu_bul)
        await update.message.reply_text(
            f"📊 *PİYASA ANALİZİ*\n\n"
            f"📌 {SEMBOL}\n"
            f"💰 Fiyat: `{fiyat:.2f}`\n"
            f"🎯 Mod: `{mod}`\n"
            f"📈 ADX: `{adx:.1f}`\n"
            f"📊 ATR: `{atr:.2f}` (ort: `{atr_ort:.2f}`)\n"
            f"📉 RSI: `{rsi:.0f}`"
        )
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

# ==================== ANA DÖNGÜ ====================
def ana_dongu():
    global ARDISIK_ZARAR_SAYACI, SON_ARDISIK_ZARAR_ZAMANI, KILL_SWITCH_AKTIF
    
    print(f"🚀 [NÖTR GRID BOT] {SEMBOL}", flush=True)
    try:
        exchange.load_markets()
    except: pass
    
    son_analiz = 0
    son_grid_kontrol = 0
    
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(10)
                continue
            
            simdi = time.time()
            
            # Kill-switch
            if KILL_SWITCH_AKTIF:
                if simdi - SON_ARDISIK_ZARAR_ZAMANI > 3600:
                    KILL_SWITCH_AKTIF = False
                    ARDISIK_ZARAR_SAYACI = 0
                    tg_gonder("✅ Kill-switch kapandı")
                else:
                    time.sleep(30)
                    continue
            
            # Piyasa analizi
            if simdi - son_analiz > PIYASA_ANALIZ_SURESI:
                son_analiz = simdi
                
                mod, adx, atr, fiyat, rsi, atr_ort = piyasa_modu_bul()
                
                print(f"\n📊 [ANALİZ] {mod} | ADX:{adx:.1f} | ATR:{atr:.2f} | Fiyat:{fiyat:.2f}", flush=True)
                
                if mod == 'YATAY':
                    if not AKTIF_GRID['aktif']:
                        bakiye = exchange.fetch_balance()
                        toplam_b = float(bakiye['total'].get('USDT', 0))
                        if toplam_b >= 20:
                            exchange.set_leverage(KALDIRAC, SEMBOL)
                            grid_kur(fiyat, toplam_b)
                
                elif mod == 'TREND':
                    if AKTIF_GRID['aktif']:
                        grid_kapat(f"Trend başladı (ADX:{adx:.1f})")
                    tg_gonder(f"📈 *TREND ALGILANDI*\nADX: `{adx:.1f}`\nGrid kapalı, beklemede.")
                
                elif mod == 'VOLATIL':
                    if AKTIF_GRID['aktif']:
                        grid_kapat(f"Volatilite yüksek (ATR:{atr:.2f})")
                    tg_gonder(f"⚡ *VOLATİLİTE YÜKSEK*\nATR: `{atr:.2f}`\nBeklemede.")
            
            # Grid kontrolü
            if AKTIF_GRID['aktif'] and simdi - son_grid_kontrol > GRID_KONTROL_SURESI:
                son_grid_kontrol = simdi
                grid_kontrol()
            
            time.sleep(ANA_DONGU_SURESI)
        
        except Exception as e:
            print(f"⚠️ Ana döngü: {e}", flush=True)
            time.sleep(30)

# ==================== MAIN ====================
async def main():
    web_thread = threading.Thread(target=run_web, daemon=True)
    web_thread.start()
    
    app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    try:
        requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook?drop_pending_updates=True", timeout=10)
        time.sleep(2)
        print("✅ Telegram webhook temizlendi.", flush=True)
    except Exception as e:
        print(f"⚠️ Webhook: {e}", flush=True)
    
    app_tg.add_handler(CommandHandler("durum", durum_komutu))
    app_tg.add_handler(CommandHandler("analiz", analiz_komutu))
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))
    
    await app_tg.initialize()
    await app_tg.start()
    await app_tg.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    
    t = threading.Thread(target=ana_dongu, daemon=True)
    t.start()
    
    stop = asyncio.Event()
    await stop.wait()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
