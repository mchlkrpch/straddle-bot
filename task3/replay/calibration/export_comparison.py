"""Export the comparable five accounts as percentages of an independent 150,000 USDC NAV."""
import csv
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parent
def read(name):
    return json.loads((ROOT/name).read_text())
options=read('option_results.json')['books']
trend=read('trend_results.json')['recent']
combo=read('combined_results.json')['recent']
curves={
    'straddle_grid': options['combo']['metrics'],
    'grid': read('grid_walk.json')['metrics'],
    'straddle_delta': options['delta']['metrics'],
    'trend_42d':dict(cagr=trend['metrics']['cagr'],ex_best=trend['ex_best'],equity=trend['end_nav'],
                    annual={str(d['year']):d['total_return'] for d in trend['annual']},
                    annual_ex_best={str(d['year']):d['total_return'] for d in trend['annual_ex_best']}),
    'trend_straddle_50_50':combo}
with (ROOT/'comparison.csv').open('w',newline='') as f:
    w=csv.writer(f)
    w.writerow(['account','year','net_return','net_ex_best','cagr','cagr_ex_best','final_usdc'])
    for name,m in curves.items():
        for year,r in m['annual'].items():
            w.writerow([name,year,r,m['annual_ex_best'][year],m['cagr'],m['ex_best'],m['equity']])
with (ROOT/'comparison_daily.csv').open() as f:
    daily=list(csv.DictReader(f))
for k,m in curves.items():
    assert float(daily[0][k])==150000 and abs(float(daily[-1][k])-m['equity'])<.0001
assert len(daily)==1049
