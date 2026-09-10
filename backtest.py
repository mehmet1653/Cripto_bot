import ccxt
import pandas as pd
import numpy as np
from strategy import hesapla_gostergeler, sinyal_uret, btc_trend_hesapla, KOMISYON_ORANI


def backtest_coin(symbol: str, gun_sayisi: int = 180):
    exchange = ccxt.gate({'enableRateLimit': True, 'options': {'defaultType': 'swap'}})

    # BTC trendi referans
    btc_ohlcv = exchange.fetch_ohlcv('BTC/USDT:USDT', '1h', limit=gun_sayisi * 24)
    df_btc_1h = pd.DataFrame(btc_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

    ohlcv_15m = exchange.fetch_ohlcv(symbol, '15m', limit=gun_sayisi * 24 * 4)
    ohlcv_1h = exchange.fetch_ohlcv(symbol, '1h', limit=gun_sayisi * 24)
    ohlcv_4h = exchange.fetch_ohlcv(symbol, '4h', limit=gun_sayisi * 6)

    df15 = pd.DataFrame(ohlcv_15m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df1h = pd.DataFrame(ohlcv_1h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df4h = pd.DataFrame(ohlcv_4h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

    # Sadece 200 mum sonrası test et
    baslangic = 200 if len(df4h) > 200 else 0

    trades = []
    pozisyon = None

    for i in range(100, len(df15)):
        ts = df15['timestamp'].iloc[i]
        # Bu ts'e denk gelen 1h ve 4h dilimlerini al
        df1h_slice = df1h[df1h['timestamp'] <= ts]
        df4h_slice = df4h[df4h['timestamp'] <= ts]
        df_btc_slice = df_btc_1h[df_btc_1h['timestamp'] <= ts]

        if len(df1h_slice) < 30 or len(df4h_slice) < 200:
            continue

        df15_slice = df15.iloc[max(0, i - 100):i + 1].reset_index(drop=True)

        # Açık pozisyon kontrolü
        if pozisyon is not None:
            high = df15['high'].iloc[i]
            low = df15['low'].iloc[i]
            if pozisyon['yon'] == 'LONG':
                if low <= pozisyon['stop']:
                    trades.append({**pozisyon, 'cikis': pozisyon['stop'], 'sonuc': 'STOP'})
                    pozisyon = None
                elif high >= pozisyon['tp']:
                    trades.append({**pozisyon, 'cikis': pozisyon['tp'], 'sonuc': 'TP'})
                    pozisyon = None
            else:
                if high >= pozisyon['stop']:
                    trades.append({**pozisyon, 'cikis': pozisyon['stop'], 'sonuc': 'STOP'})
                    pozisyon = None
                elif low <= pozisyon['tp']:
                    trades.append({**pozisyon, 'cikis': pozisyon['tp'], 'sonuc': 'TP'})
                    pozisyon = None
            continue

        # Yeni sinyal
        try:
            g = hesapla_gostergeler(df15_slice, df1h_slice, df4h_slice)
            btc_trend = btc_trend_hesapla(df_btc_slice)
            sig = sinyal_uret(g, btc_trend)
        except Exception:
            continue

        if sig:
            pozisyon = {**sig, 'giris_zaman': ts}

    # İstatistikler
    if not trades:
        return {"symbol": symbol, "islem": 0, "win_rate": 0, "profit_factor": 0,
                "beklenen_deger": 0, "max_dd": 0, "toplam_getiri_pct": 0}

    kazanclar = []
    for t in trades:
        if t['yon'] == 'LONG':
            pct = (t['cikis'] - t['giris']) / t['giris']
        else:
            pct = (t['giris'] - t['cikis']) / t['giris']
        pct -= KOMISYON_ORANI  # komisyon düş
        kazanclar.append(pct)

    kazanclar = np.array(kazanclar)
    kazanan = kazanclar[kazanclar > 0]
    kaybeden = kazanclar[kazanclar < 0]

    win_rate = len(kazanan) / len(kazanclar) * 100 if len(kazanclar) else 0
    profit_factor = abs(kazanan.sum() / kaybeden.sum()) if len(kaybeden) and kaybeden.sum() != 0 else float('inf')

    # Equity curve ve max drawdown
    equity = np.cumprod(1 + kazanclar)
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / peak
    max_dd = dd.min() * 100 if len(dd) else 0

    return {
        "symbol": symbol,
        "islem": len(trades),
        "win_rate": round(win_rate, 2),
        "profit_factor": round(profit_factor, 3) if profit_factor != float('inf') else 999,
        "beklenen_deger": round(kazanclar.mean() * 100, 4),
        "max_dd": round(max_dd, 2),
        "toplam_getiri_pct": round((equity[-1] - 1) * 100, 2) if len(equity) else 0,
    }


if __name__ == "__main__":
    COINLER = ['SOL/USDT:USDT', 'AVAX/USDT:USDT', 'XRP/USDT:USDT',
               'DOGE/USDT:USDT', 'SUI/USDT:USDT', 'LINK/USDT:USDT', 'ADA/USDT:USDT']
    print(f"{'COIN':<22} {'İŞLEM':>6} {'WIN%':>7} {'PF':>7} {'EV%':>8} {'MAX_DD%':>9} {'TOPLAM%':>10}")
    print("-" * 75)
    for c in COINLER:
        try:
            r = backtest_coin(c, gun_sayisi=180)
            print(f"{r['symbol']:<22} {r['islem']:>6} {r['win_rate']:>7} {r['profit_factor']:>7} "
                  f"{r['beklenen_deger']:>8} {r['max_dd']:>9} {r['toplam_getiri_pct']:>10}")
        except Exception as e:
            print(f"{c}: HATA - {e}")
