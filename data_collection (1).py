import yfinance as yf
import pandas as pd
import numpy as np
import statsmodels.api as sm

# Configuration
TICKERS = ["MSFT", "NVDA", "GOOGL", "AMZN", "META", "ADBE", "CRM", "ASML", "TSM", "NOW"]
BENCHMARK = "SPY"
ALL_TICKERS = TICKERS + [BENCHMARK]
START = "1999-01-01"
END = "2025-08-01"  # inclusive
RISK_FREE_ANNUAL = 0.04  # 4% annual risk-free
MONTE_CARLO_RUNS = 500
BLOCK_SIZE = 21  # roughly monthly blocks for bootstrap
TRAILING_DAYS = 126  # ~6 months of trading days

def fetch_and_build(ticker, start=START, end=END):
    print(f"Fetching {ticker}...")
    data = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=False)
    if data.empty:
        raise RuntimeError(f"No data fetched for {ticker}")
    ticker_obj = yf.Ticker(ticker)

    # Dividends
    divs = ticker_obj.dividends
    if hasattr(divs.index, 'tz'):
        divs.index = divs.index.tz_convert(None)
    divs = divs[(divs.index >= pd.to_datetime(start)) & (divs.index <= pd.to_datetime(end))]

    # Price + dividend
    df = data[['Close']].rename(columns={'Close': 'Price'}).copy()
    df['Dividend'] = 0.0
    for dt, amt in divs.items():
        if dt in df.index:
            df.loc[dt, 'Dividend'] = amt

    # Total return index with reinvested dividends (simplified: reinvest on same day)
    price_array = df['Price'].to_numpy(dtype=float)
    div_array = df['Dividend'].to_numpy(dtype=float)
    total_return = np.ones(len(df), dtype=float)
    for i in range(1, len(df)):
        if price_array[i - 1] != 0:
            price_ret_factor = price_array[i] / price_array[i - 1]
        else:
            price_ret_factor = 1.0
        cumulative = total_return[i - 1] * price_ret_factor
        if div_array[i] > 0 and price_array[i] != 0:
            reinvest_factor = 1 + (div_array[i] / price_array[i])
            cumulative *= reinvest_factor
        total_return[i] = float(cumulative)  # ensure scalar, avoids future deprecation issues
    df['Total_Return_Index'] = total_return

    # Log returns of total return index
    df['Log_Return'] = np.log(df['Total_Return_Index']).diff().fillna(0)

    # Trailing 6-month total return
    df['Trailing_6m_TR'] = df['Total_Return_Index'] / df['Total_Return_Index'].shift(TRAILING_DAYS) - 1

    # Drawdown
    df['CumMax'] = df['Total_Return_Index'].cummax()
    df['Drawdown'] = df['Total_Return_Index'] / df['CumMax'] - 1

    return df

def compute_beta_alpha_sharpe(portfolio_series, benchmark_series, freq=252):
    df = pd.concat([portfolio_series, benchmark_series], axis=1, join='inner').dropna()
    df.columns = ['Portfolio', 'Benchmark']

    port_log = np.log(df['Portfolio']).diff().dropna()
    bench_log = np.log(df['Benchmark']).diff().dropna()
    combined = pd.concat([port_log, bench_log], axis=1, join='inner').dropna()
    combined.columns = ['Port_Log', 'Bench_Log']

    X = sm.add_constant(combined['Bench_Log'])
    model = sm.OLS(combined['Port_Log'], X).fit()
    beta = model.params['Bench_Log']
    alpha_annualized = model.params['const'] * freq

    rf_daily = (1 + RISK_FREE_ANNUAL) ** (1 / freq) - 1
    rf_log = np.log(1 + rf_daily)
    excess = combined['Port_Log'] - rf_log
    sharpe = (excess.mean() * np.sqrt(freq)) / (combined['Port_Log'].std() + 1e-12)

    return {
        'beta': beta,
        'alpha_annualized': alpha_annualized,
        'sharpe': sharpe,
        'regression_summary': model.summary()
    }

def monte_carlo_simulation(log_returns, n_years=25, freq=252, runs=MONTE_CARLO_RUNS, block_size=BLOCK_SIZE):
    n_points = n_years * freq
    blocks = []
    for i in range(0, len(log_returns) - block_size + 1):
        blocks.append(log_returns.iloc[i:i + block_size].values)
    blocks = np.array(blocks)
    final_multiples = []
    for _ in range(runs):
        sampled = []
        while sum(len(b) for b in sampled) < n_points:
            idx = np.random.randint(0, len(blocks))
            sampled.append(blocks[idx])
        path = np.concatenate(sampled)[:n_points]
        cum_log = np.cumsum(path)
        total_return_index = np.exp(cum_log)
        final_multiples.append(total_return_index[-1])
    return np.array(final_multiples)

def compute_summary(portfolio_df, benchmark_df):
    stats = compute_beta_alpha_sharpe(portfolio_df['Total_Return_Index'], benchmark_df['Total_Return_Index'])
    max_dd = portfolio_df['Drawdown'].min()
    latest_trend = portfolio_df['Trailing_6m_TR'].iloc[-1]
    return {
        'beta': stats['beta'],
        'alpha_annualized': stats['alpha_annualized'],
        'sharpe': stats['sharpe'],
        'max_drawdown': max_dd,
        'latest_6m_trend': latest_trend,
        'regression_summary': stats['regression_summary']
    }

def main():
    all_data = {}
    for ticker in ALL_TICKERS:
        all_data[ticker] = fetch_and_build(ticker)

    # Equal-weighted core
    core_tr = pd.concat([all_data[t]['Total_Return_Index'] for t in TICKERS], axis=1, join='inner')
    core_tr.columns = TICKERS
    eqw_index = core_tr.mean(axis=1)
    portfolio_df = pd.DataFrame({'Total_Return_Index': eqw_index})
    portfolio_df['Log_Return'] = np.log(portfolio_df['Total_Return_Index']).diff().fillna(0)
    portfolio_df['CumMax'] = portfolio_df['Total_Return_Index'].cummax()
    portfolio_df['Drawdown'] = portfolio_df['Total_Return_Index'] / portfolio_df['CumMax'] - 1
    portfolio_df['Trailing_6m_TR'] = portfolio_df['Total_Return_Index'] / portfolio_df['Total_Return_Index'].shift(TRAILING_DAYS) - 1

    summary = compute_summary(portfolio_df, all_data[BENCHMARK])

    mc = monte_carlo_simulation(portfolio_df['Log_Return'].dropna())

    # Write Excel
    with pd.ExcelWriter("fti_etf_full_output.xlsx", engine="openpyxl") as writer:
        for ticker, df in all_data.items():
            df.to_excel(writer, sheet_name=ticker[:31])
        portfolio_df.to_excel(writer, sheet_name="EqualWeightedCore")

        summary_table = pd.DataFrame({
            'Metric': [
                'Beta', 'Alpha (annualized)', 'Sharpe', 'Max Drawdown', 'Latest 6m Trend',
                'MC Median Final Multiple', 'MC 5th%', 'MC 95th%'
            ],
            'Value': [
                summary['beta'],
                summary['alpha_annualized'],
                summary['sharpe'],
                summary['max_drawdown'],
                summary['latest_6m_trend'],
                np.median(mc),
                np.percentile(mc, 5),
                np.percentile(mc, 95),
            ]
        })
        summary_table.to_excel(writer, sheet_name="Portfolio_Summary", index=False)
        pd.DataFrame({'Final_Multiple': mc}).to_excel(writer, sheet_name="Monte_Carlo_Sims", index=False)

    print("Saved fti_etf_full_output.xlsx")
    print("Summary metrics:")
    print(f"Beta: {summary['beta']}")
    print(f"Alpha (annualized): {summary['alpha_annualized']}")
    print(f"Sharpe: {summary['sharpe']}")
    print(f"Max Drawdown: {summary['max_drawdown']:.2%}")
    print(f"Latest 6m Trend: {summary['latest_6m_trend']:.2%}")
    print(f"Monte Carlo percentiles 5% / median / 95%: {np.percentile(mc,5):.3f} / {np.median(mc):.3f} / {np.percentile(mc,95):.3f}")
    print(summary['regression_summary'])

if __name__ == "__main__":
    main()
