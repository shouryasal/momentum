"""Week-1 placeholder strategy: loads cleanly in the freqtrade container, never trades.

Replaced by SleeveA/SleeveB via config/earn.yaml `sleeves.<x>.strategy` in weeks 2/4.
"""

from freqtrade.strategy import IStrategy
from pandas import DataFrame


class Scaffold(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "4h"
    can_short = False
    stoploss = -0.99
    process_only_new_candles = True
    startup_candle_count = 0

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 0
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        return dataframe
