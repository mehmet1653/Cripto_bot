import ccxt

# Demo bağlantısı
exchange = ccxt.gate({
    'apiKey': 'c0e36a4b95d010e8d7f28726547dde8c',
    'secret': '67b6d79ceb1f18d7492d630e7e178b444202130bb4985506dd77dc8063d23330',
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'}
})
exchange.set_sandbox_mode(True)

try:
    # 1. Bakiye Testi
    bakiye = exchange.fetch_balance()
    usdt = bakiye['total'].get('USDT', 0)
    print(f"[BAŞARILI] Demo Cüzdan Bakiyesi: {usdt} USDT")

    # 2. Fiyat Testi
    fiyat = exchange.fetch_ticker('SOL/USDT:USDT')
    print(f"[BAŞARILI] SOL Güncel Fiyat: {fiyat['last']}")

except Exception as e:
    print(f"[HATA] Bağlantı kurulamadı: {e}")
    
