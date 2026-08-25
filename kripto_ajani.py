import time
import ccxt
import os

exchange = ccxt.gate({
    'apiKey': '82cca880898a88d1a31e86d8eb474c57',
    'secret': '1ac479b9df5e6f2e89560b0d238a250694719b6fcae20da00ebc54ad6aeb8898',
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'}
})

exchange.set_sandbox_mode(True)

print("🚨 SAF TEST BAŞLATILIYOR...")

try:
    # 1. Bakiye kontrolü
    balance = exchange.fetch_balance()
    toplam_usdt = float(balance['total'].get('USDT', 0))
    print(f"💰 Toplam Kasa: {toplam_usdt} USDT")

    # 2. BTC için test emri
    symbol = 'BTC/USDT:USDT'
    ticker = exchange.fetch_ticker(symbol)
    fiyat = ticker['last']
    print(f"📉 BTC Anlık Fiyat: {fiyat}")

    # Kaldıraç ayarla
    exchange.set_leverage(10, symbol)
    print("⚡ Kaldıraç 10x yapıldı.")

    # Çok küçük bir miktar emir hesapla (Testnet minimum sınırına takılmasın diye)
    # 10 USDT'lik marjin * 10 kaldıraç = 100 USDT pozisyon
    miktar = 100 / fiyat
    miktar = exchange.amount_to_precision(symbol, miktar)
    
    print(f"🚀 Piyasaya emir gönderiliyor... Miktar: {miktar}")
    order = exchange.create_market_order(symbol, 'buy', float(miktar))
    print(f"✅ İŞLEM BAŞARIYLA AÇILDI! Detay: {order}")

except Exception as e:
    print(f"❌ PATLADI / HATA ALDIK: {str(e)}")
    
