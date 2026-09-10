import pandas as pd
import ta
import numpy as np

# ==================== AYARLAR ====================
MIN_STOP_PCT = 0.008      # %0.8 minimum stop (komisyon+spread)
MAX_STOP_PCT = 0.030      # %3 maksimum stop (kasa koruması)
ATR_STOP_MULT = 1.5
RISK_REWARD = 2.0         # 1:2 R/R
KOMISYON_ORANI = 0.001    # %0.1 round-trip (taker x2)
FUNDING_TAHMIN = 0.0002   # İşlem başına tahmini funding maliyeti


def hesapla_gostergeler(df_15m: pd.DataFrame, df_1h: pd.DataFrame, df_4h: pd.DataFrame):
    """Tüm göstergeleri tek yerde hesapla."""
    out = {}

    # 4h ana trend (EMA200)
    if len(df_4h) >= 200:
        ema200_4h = ta.trend.ema_indicator(df_4h['close'], window=200).iloc[-1]
        out['trend_4h_yon'] = "LONG" if df_4h['close'].iloc[-1] > ema200_4h else "SHORT"
    else:
        out['trend_4h_yon'] = None

    # 1h trend (EMA7/21)
    ema7_1h = ta.trend.ema_indicator(df_1h['close'], window=7).iloc[-1]
    ema21_1h = ta.trend.ema_indicator(df_1h['close'], window=21).iloc[-1]
    out['trend_1h_yon'] = "LONG" if ema7_1h > ema21_1h else "SHORT"

    # 15m momentum
    out['rsi'] = ta.momentum.rsi(df_15m['close'], window=14).iloc[-1]
    out['adx'] = ta.trend.ADXIndicator(df_15m['high'], df_15m['low'], df_15m['close'], window=14).adx().iloc[-1]

    # ATR
    atr = ta.volatility.AverageTrueRange(df_15m['high'], df_15m['low'], df_15m['close'], window=14).average_true_range().iloc[-1]
    fiyat = df_15m['close'].iloc[-1]
    out['atr_pct'] = float((atr / fiyat) * 100)

    # Hacim onayı (son mum > son 20 mum ortalaması)
    vol_ma = df_15m['volume'].rolling(20).mean().iloc[-1]
    out['hacim_onay'] = df_15m['volume'].iloc[-1] > vol_ma

    # EMA20 üstü/altı (15m)
    ema20_15m = ta.trend.ema_indicator(df_15m['close'], window=20).iloc[-1]
    out['fiyat_ema20_ustu'] = fiyat > ema20_15m

    out['fiyat'] = fiyat
    return out


def sinyal_uret(g: dict, btc_trend: str = None):
    """
    Katmanlı sinyal üretimi. Hepsi sağlanmalı yoksa None döner.
    """
    # 1) 4h ana trend yönü belirle
    if g['trend_4h_yon'] is None:
        return None

    yon = g['trend_4h_yon']

    # 2) 1h trend 4h ile aynı olmalı
    if g['trend_1h_yon'] != yon:
        return None

    # 3) BTC filtresi (LONG için BTC düşmemeli)
    if btc_trend is not None:
        if yon == "LONG" and btc_trend == "DOWN":
            return None
        if yon == "SHORT" and btc_trend == "UP":
            return None

    # 4) ATR filtresi (ne çok dar ne çok geniş)
    if not (0.4 <= g['atr_pct'] <= 4.0):
        return None

    # 5) ADX > 22 (trend gücü yeterli)
    if g['adx'] < 22:
        return None

    # 6) RSI geri çekilme bölgesi (nötr değil)
    if yon == "LONG" and not (30 <= g['rsi'] <= 55):
        return None
    if yon == "SHORT" and not (45 <= g['rsi'] <= 70):
        return None

    # 7) Hacim onayı
    if not g['hacim_onay']:
        return None

    # 8) Fiyat EMA20'nin doğru tarafında
    if yon == "LONG" and not g['fiyat_ema20_ustu']:
        return None
    if yon == "SHORT" and g['fiyat_ema20_ustu']:
        return None

    # Stop ve TP hesapla
    stop_pct = max(MIN_STOP_PCT, min(MAX_STOP_PCT, (g['atr_pct'] * ATR_STOP_MULT) / 100.0))
    tp_pct = stop_pct * RISK_REWARD

    # Komisyon + funding ekle
    tp_pct_net = tp_pct + KOMISYON_ORANI + FUNDING_TAHMIN

    giris = g['fiyat']
    if yon == "LONG":
        stop = giris * (1 - stop_pct)
        tp = giris * (1 + tp_pct_net)
    else:
        stop = giris * (1 + stop_pct)
        tp = giris * (1 - tp_pct_net)

    return {
        "yon": yon,
        "giris": giris,
        "stop": stop,
        "tp": tp,
        "stop_pct": stop_pct,
        "tp_pct": tp_pct_net,
        "rr": RISK_REWARD,
    }


def btc_trend_hesapla(df_1h_btc: pd.DataFrame):
    if len(df_1h_btc) < 50:
        return None
    ema20 = ta.trend.ema_indicator(df_1h_btc['close'], window=20).iloc[-1]
    ema50 = ta.trend.ema_indicator(df_1h_btc['close'], window=50).iloc[-1]
    return "UP" if ema20 > ema50 else "DOWN"
