"""Read the frozen local panel first; download only when it is absent."""
from pathlib import Path
from functools import lru_cache
import pandas as pd
ROOT = Path(__file__).resolve().parents[1]
PRICE_PATH = ROOT / "data/raw/csi300_prices_2020_2025.parquet"

@lru_cache(maxsize=1)
def local_panel():
    return pd.read_parquet(PRICE_PATH) if PRICE_PATH.exists() else None

def get_stock_data(code, start_date="20200101", end_date="20251231"):
    code = str(code).zfill(6)
    panel = local_panel()
    if panel is not None:
        result = panel.loc[(panel.code.astype(str).str.zfill(6) == code) &
                           (panel.date >= pd.Timestamp(start_date)) &
                           (panel.date <= pd.Timestamp(end_date))].copy()
    else:
        import akshare as ak
        symbol = ("sh" if code.startswith(("6", "9")) else "sz") + code
        result = ak.stock_zh_a_hist_tx(symbol=symbol,start_date=start_date,
                                     end_date=end_date,adjust="hfq")
        if not result.empty:
            result["date"] = pd.to_datetime(result["date"])
            result["code"] = code
    return None if result.empty else result.sort_values("date").reset_index(drop=True)
