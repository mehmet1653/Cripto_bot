import ccxt

# Gate.io Testnet (Demo) Bağlantısı
exchange = ccxt.gate({
    'apiKey': '82cca880898a88d1a31e86d8eb474c57',
    'secret': '82cca880898a88d1a31e86d8eb474c57',  # Secret Key'ini buraya tam olarak ekliyoruz
    'enableRateLimit': True,
    'options': {
        'defaultType': 'swap'  # Vadeli işlemler için
    }
})

# Testnet / Sandbox modunu aktif et
exchange.set_sandbox_mode(True)

try:
    # Bakiye Testi
    bakiye = exchange.fetch_balance()
    usdt = bakiye['total'].get('USDT', 0)
    print(f"[BAŞARILI] Testnet Demo Cüzdan Bakiyesi: {usdt} USDT")

    # Fiyat Testi
    fiyat = exchange.fetch_ticker('SOL/USDT:USDT')
    print(f"[BAŞARILI] SOL Güncel Fiyat: {fiyat['last']}")

except Exception as e:
    print(f"[HATA] Bağlantı kurulamadı: {e}")
    
