import ccxt
import time

# --- GATE.IO TESTNET BAĞLANTISI ---
exchange = ccxt.gate({
    'apiKey': '82cca880898a88d1a31e86d8eb474c57',
    'secret': '1ac479b9df5e6f2e89560b0d238a250694719b6fcae20da00ebc54ad6aeb8898',
    'enableRateLimit': True,
    'options': {
        'defaultType': 'swap'  # Vadeli işlemler (Perpetual Swap)
    }
})

# Testnet / Sandbox modunu aktif et
exchange.set_sandbox_mode(True)

def test_baglantisi():
    try:
        print("Gate.io Testnet bağlantısı test ediliyor...")
        
        # 1. Bakiye Testi
        bakiye = exchange.fetch_balance()
        usdt_bakiye = bakiye['total'].get('USDT', 0)
        print(f"[BAŞARILI] Testnet Cüzdan Bakiyesi: {usdt_bakiye} USDT")

        # 2. Fiyat Testi
        fiyat = exchange.fetch_ticker('SOL/USDT:USDT')
        print(f"[BAŞARILI] SOL Güncel Fiyat: {fiyat['last']}")

    except Exception as e:
        print(f"[HATA] Bağlantı kurulamadı: {e}")

if __name__ == "__main__":
    test_baglantisi()
    
