#!/bin/bash
# Round 4 Validation for enhanced_breakout
# Runs: IS/OOS split, walk-forward folds, cross-data, cross-exchange
set -e
cd /Users/swas/Desktop/Swas/Kite/ATO_SUITE/strategy-backtester
source ../.venv/bin/activate

BASE="strategies/enhanced_breakout"
OUT="results/enhanced_breakout"

# Helper: create a temp config with modified static section
make_config() {
    local start_epoch=$1
    local end_epoch=$2
    local data_provider=${3:-nse_charting}
    local prefetch=${4:-800}
    local exchange=${5:-NSE}

    cat << YAML
static:
  strategy_type: enhanced_breakout
  start_margin: 1000000
  start_epoch: $start_epoch
  end_epoch: $end_epoch
  prefetch_days: $prefetch
  data_granularity: day
  data_provider: $data_provider

scanner:
  instruments:
    - [{exchange: $exchange, symbols: []}]
  price_threshold: [50]
  avg_day_transaction_threshold:
    - {period: 125, threshold: 70000000}
  n_day_gain_threshold:
    - {n: 360, threshold: 0}

entry:
  breakout_window: [5]
  consecutive_positive_years: [3]
  min_yearly_return_pct: [0]
  momentum_lookback_days: [21]
  momentum_percentile: [0.25]
  rerank_interval_days: [63]
  rescreen_interval_days: [63]
  volume_multiplier: [0]
  volume_avg_period: [20]
  roe_threshold: [0]
  pe_threshold: [0]
  de_threshold: [0]
  fundamental_missing_mode: [skip]
  regime_instrument: ['$exchange:NIFTYBEES']
  regime_sma_period: [0]

exit:
  trailing_stop_pct: [18]
  max_hold_days: [126]

simulation:
  default_sorting_type: [top_gainer]
  order_sorting_type: [top_gainer]
  order_ranking_window_days: [180]
  max_positions: [10]
  max_positions_per_instrument: [1]
  order_value_multiplier: [1]
  max_order_value:
    - {type: percentage_of_instrument_avg_txn, value: 4.5}
YAML
}

run_test() {
    local name=$1
    local config_file=$2
    echo "=== $name ==="
    python run.py --config "$config_file" --output "$OUT/$name.json" 2>&1 | grep -E "CAGR|Calmar|Pipeline Complete|Best config"
    echo
}

# 4a. OOS Split: IS=2010-2020, OOS=2020-2026
echo ">>> Round 4a: OOS Split"
make_config 1262304000 1577836800 nse_charting 800 NSE > /tmp/eb_r4_is.yaml
run_test "round4_is" /tmp/eb_r4_is.yaml

make_config 1577836800 1735689600 nse_charting 800 NSE > /tmp/eb_r4_oos.yaml
run_test "round4_oos" /tmp/eb_r4_oos.yaml

# 4b. Walk-forward (6 folds, ~2yr test each)
echo ">>> Round 4b: Walk-Forward"
# Fold 1: train 2010-2013, test 2013-2015
make_config 1356998400 1420070400 nse_charting 800 NSE > /tmp/eb_r4_wf1.yaml
run_test "round4_wf1" /tmp/eb_r4_wf1.yaml

# Fold 2: train 2012-2015, test 2015-2017
make_config 1420070400 1483228800 nse_charting 800 NSE > /tmp/eb_r4_wf2.yaml
run_test "round4_wf2" /tmp/eb_r4_wf2.yaml

# Fold 3: train 2014-2017, test 2017-2019
make_config 1483228800 1546300800 nse_charting 800 NSE > /tmp/eb_r4_wf3.yaml
run_test "round4_wf3" /tmp/eb_r4_wf3.yaml

# Fold 4: train 2016-2019, test 2019-2021
make_config 1546300800 1609459200 nse_charting 800 NSE > /tmp/eb_r4_wf4.yaml
run_test "round4_wf4" /tmp/eb_r4_wf4.yaml

# Fold 5: train 2018-2021, test 2021-2023
make_config 1609459200 1672531200 nse_charting 800 NSE > /tmp/eb_r4_wf5.yaml
run_test "round4_wf5" /tmp/eb_r4_wf5.yaml

# Fold 6: train 2020-2023, test 2023-2026
make_config 1672531200 1735689600 nse_charting 800 NSE > /tmp/eb_r4_wf6.yaml
run_test "round4_wf6" /tmp/eb_r4_wf6.yaml

echo ">>> ALL ROUND 4a/4b DONE"
