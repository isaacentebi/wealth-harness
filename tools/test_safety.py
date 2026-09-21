# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "yfinance>=0.2.50",
#   "pandas",
#   "numpy",
#   "scipy",
#   "statsmodels",
#   "pytest",
# ]
# ///
"""Independent regression cases for the redesigned contracts. Offline only."""
import importlib.util
import json
import subprocess
import tempfile
import sys
from argparse import Namespace
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

spec=importlib.util.spec_from_file_location('wm_safety',Path(__file__).with_name('wm.py'))
wm=importlib.util.module_from_spec(spec);spec.loader.exec_module(wm)

@pytest.fixture
def px(monkeypatch):
    rng=np.random.default_rng(260916); idx=pd.bdate_range('2023-01-02',periods=600)
    market=rng.normal(.0002,.011,len(idx))
    data=pd.DataFrame({'MARKET':100*np.cumprod(1+market),
                       'A':100*np.cumprod(1+market+rng.normal(0,.003,len(idx))),
                       'B':100*np.cumprod(1+rng.normal(.0001,.003,len(idx)))},index=idx)
    data.attrs.update(currency='USD',source='synthetic regression fixture',data_kind='synthetic',risk_free_policy='omit')
    monkeypatch.setattr(wm,'_ticker_meta',lambda names: {})
    monkeypatch.setattr(wm,'_load_factors',lambda model=3: (_ for _ in ()).throw(AssertionError('network forbidden')))
    return data

@pytest.mark.parametrize('r,expected',[([-.3,.05],-.3),([.1,.1],0),([-.2,-.25],-.4),([.25,-.2],-.2),([0,-1],-1)])
def test_drawdown_initial_wealth(r,expected):
    assert wm.max_drawdown(pd.Series(r))==pytest.approx(expected)

@pytest.mark.parametrize('weights',[{}, {'A':-1,'B':2},{'A':0},{'A':float('nan')},{'A':float('inf')},{'MISSING':1}])
def test_invalid_weights_fail_closed(px,weights):
    with pytest.raises(ValueError): wm.analyze_frame(px,'MARKET',weights)

@pytest.mark.parametrize('damage',['nan','negative','zero','duplicate_dates','reverse_dates'])
def test_invalid_prices(px,damage):
    bad=px.copy()
    if damage=='nan': bad.iloc[4,0]=np.nan
    elif damage=='negative': bad.iloc[4,0]=-1
    elif damage=='zero': bad.iloc[4,0]=0
    elif damage=='duplicate_dates': bad.index=[bad.index[0]]*len(bad)
    else: bad=bad.iloc[::-1]
    with pytest.raises(ValueError): wm.daily_returns(bad)

def test_held_benchmark_is_in_correlation(px):
    res=wm.analyze_frame(px,'MARKET',{'MARKET':.8,'A':.2})
    assert set(res['correlation'])=={'MARKET','A'}
    assert res['portfolio']['weights']['MARKET']==.8

def test_correlation_not_direction_probability():
    d=wm.pair_diagnostics(pd.DataFrame({'x':[-.02,-.01,.01,.02],'y':[.01,.02,.04,.05]}))[0]
    assert d['pearson']==pytest.approx(1)
    assert d['same_sign_fraction']==pytest.approx(.5)

def test_constant_series_is_unknown_correlation(px):
    px['B']=100
    res=wm.analyze_frame(px,'MARKET',{'A':.5,'B':.5})
    assert res['correlation']['A']['B'] is None
    assert res['pair_diagnostics'][0]['pearson'] is None
    assert 'n/a' in wm.svg_heatmap(['A','B'],[[1,None],[None,1]])

@pytest.mark.parametrize('failure',['missing','future','stale','zero','negative','duplicate'])
def test_fx_failure_never_relabels(px,failure):
    meta={t:{'currency':'USD'} for t in px.columns}
    fx=pd.Series(20.,index=px.index)
    if failure=='missing': fx=None
    elif failure=='future': fx=fx.iloc[1:]
    elif failure=='stale': fx=fx.iloc[:1]
    elif failure=='zero': fx.iloc[0]=0
    elif failure=='negative': fx.iloc[0]=-1
    else: fx.index=[px.index[0]]*len(fx)
    with pytest.raises(ValueError): wm.to_currency(px,'MXN',[],meta,lambda a,b:fx)
    assert px.attrs['currency']=='USD'

@pytest.mark.parametrize('unit',['GBp','GBX','ZAc','ILA'])
def test_quote_subunits_are_not_whole_currencies(px,unit):
    with pytest.raises(ValueError,match='subunits'):
        wm.to_currency(px,'USD',[],{t:{'currency':unit} for t in px},lambda a,b:pd.Series(1,index=px.index))

def test_fx_conversion_and_provenance(px):
    out=wm.to_currency(px,'MXN',[],{t:{'currency':'USD'} for t in px},lambda a,b:pd.Series(20,index=px.index))
    assert out.iloc[-1,0]==pytest.approx(px.iloc[-1,0]*20)
    assert out.attrs['currency']=='MXN' and set(out.attrs['fx_converted'])==set(px)

def test_partial_fees_not_zero(px):
    res=wm.analyze_frame(px,'MARKET',{'A':.6,'B':.4},meta={'A':{'expense_ratio':.002}})['portfolio']
    assert res['expense_ratio'] is None
    assert res['fee_known_weight']==.6 and res['fee_known_contribution']==.0012
    assert res['fee_missing']==['B']

def test_known_zero_fee_distinct_from_unknown(px):
    res=wm.analyze_frame(px,'MARKET',{'A':.6,'B':.4},meta={'A':{'expense_ratio':0},'B':{'expense_ratio':.001}})['portfolio']
    assert res['fee_known_weight']==1 and res['expense_ratio']==.0004

@pytest.mark.parametrize('fee',[-.1,1,10,float('nan')])
def test_bad_fee_units_rejected(px,fee):
    with pytest.raises(ValueError): wm.analyze_frame(px,'MARKET',{'A':1},meta={'A':{'expense_ratio':fee}})

def test_non_usd_rf_not_assumed_usd(px):
    px.attrs['currency']='MXN';px.attrs.pop('risk_free_policy')
    res=wm.analyze_frame(px,'MARKET',{'A':1})
    assert res['rf_annual'] is None and res['portfolio']['alpha'] is None and res['portfolio']['sharpe'] is None

def test_sharpe_arithmetic_not_cagr():
    r=pd.Series([-.1,.2,-.05,.1]);rf=.02
    expected=(r-rf/252).mean()/r.std(ddof=1)*np.sqrt(252)
    assert wm.sharpe(r,rf)==pytest.approx(expected)

@pytest.mark.parametrize('n,cap',[(2,.2),(3,.3),(1,.5)])
def test_infeasible_caps_rejected(n,cap):
    with pytest.raises(ValueError): wm._cap(np.ones(n),cap)

@pytest.mark.parametrize('seed',list(range(6)))
def test_cap_properties(seed):
    w=wm._cap(np.random.default_rng(seed).random(8),.2)
    assert sum(w)==pytest.approx(1) and w.min()>=0 and w.max()<=.2+1e-9

def test_global_not_per_sleeve_cap(px):
    r=wm.daily_returns(px[['A','B']])
    with pytest.raises(ValueError):
        wm.build_weights(r,'equal',.4,{'core':['A'],'other':['B']},{'core':.8,'other':.2})

@pytest.mark.parametrize('method',['equal','invvol','minvar','riskparity'])
def test_builder_constraints(px,method):
    w=wm.solve_weights(wm.daily_returns(px),method,.6)
    assert w.sum()==pytest.approx(1) and min(w)>=0 and max(w)<=.6+1e-7

@pytest.mark.parametrize('seed',[0,1,2,3])
def test_shrinkage_matches_independent_library(seed):
    # sklearn is verification-only, not a runtime dependency.
    sklearn=pytest.importorskip('sklearn.covariance')
    x=np.random.default_rng(seed).normal(size=(100,6))*np.arange(1,7)
    expected=sklearn.LedoitWolf().fit(x)
    actual,ratio=wm.shrink_covariance(pd.DataFrame(x))
    assert actual==pytest.approx(expected.covariance_,rel=1e-10,abs=1e-10)
    assert ratio==pytest.approx(expected.shrinkage_,abs=1e-10)

@pytest.fixture
def profile():
    return dict(currency='MXN',as_of=date.today().isoformat(),available_capital=500000,
                monthly_essentials=25000,reserve_months=6,reserve_outside_pool=50000,
                goals=[dict(name='Tuition',currency='MXN',due='2027-09-01',protect_now=True,target_amount=100000,funded_outside_pool=20000)],
                debts=[dict(name='Card',currency='MXN',balance=40000,apr=.45)],debt_payments_from_pool=40000)

def test_plan_exact_reservations(profile):
    r=wm.money_plan(profile)
    assert r['reserve_from_pool']==100000 and r['protected_goals_from_pool']==80000
    assert r['uncommitted_capital']==280000
    assert r['debts'][0]['annual_interest_at_unchanged_balance']==18000

@pytest.mark.parametrize('missing',['available_capital','monthly_essentials','reserve_months','reserve_outside_pool','goals','debts','debt_payments_from_pool'])
def test_incomplete_plan_does_not_approve(profile,missing):
    profile.pop(missing);r=wm.money_plan(profile)
    assert r['uncommitted_capital'] is None and missing in r['missing']

def test_foreign_goal_blocks_pool_total(profile):
    profile['goals'][0]['currency']='USD';r=wm.money_plan(profile)
    assert r['uncommitted_capital'] is None and any('conversion' in s for s in r['missing'])

def test_goal_outside_pool_cannot_exceed_goal(profile):
    profile['goals'][0]['funded_outside_pool']=200000
    with pytest.raises(ValueError): wm.money_plan(profile)

@pytest.mark.parametrize('bad',[{},'none',[None]])
def test_plan_list_schema(profile,bad):
    profile['goals']=bad
    with pytest.raises(ValueError):wm.money_plan(profile)

def test_plan_funding_shortfall(profile):
    profile['available_capital']=100000;r=wm.money_plan(profile)
    assert r['uncommitted_capital']==-120000 and r['funding_shortfall']==120000

def test_impossible_debt_payment(profile):
    profile['debt_payments_from_pool']=40001
    with pytest.raises(ValueError): wm.money_plan(profile)

def test_snapshot_roundtrip(px):
    saved=json.loads(json.dumps(wm._snapshot(px)))
    assert wm.price_fingerprint(wm._from_snapshot(saved))==wm.price_fingerprint(px)

@pytest.mark.parametrize('damage',['price','currency'])
def test_stat_chart_snapshot_mismatch(px,damage):
    result=wm.analyze_frame(px,'MARKET',{'A':.6,'B':.4});other=px.copy()
    if damage=='price':other.iloc[-1,0]*=1.1
    else:other.attrs['currency']='MXN'
    with pytest.raises(ValueError):wm.card_analyze(result,other)

def test_report_weights_mismatch(px,tmp_path):
    stats=wm.analyze_frame(px,'MARKET',{'A':.6,'B':.4})
    with pytest.raises(ValueError):wm.write_report({'bench':'MARKET','weights':{'A':.8,'B':.2},'stats':stats},px,tmp_path/'bad.html')

def test_comparison_identical_portfolios(px):
    r=wm.compare_portfolios(px,{'A':.6,'B':.4},{'A':.6,'B':.4},'MARKET',meta={})
    assert r['current']['portfolio']==r['proposed']['portfolio']
    html=wm.card_compare(r,px)
    assert 'aria-pressed="true"' in html and 'Same-sign' not in html
    # the Geist <link> in the standalone head is the only outbound request
    import re as _re
    assert len(_re.findall(r'(?:src|href)=', html)) == len(_re.findall(r'(?:src|href)="https://fonts\.', html))
    assert 'src=' not in wm.card_compare(r, px, fragment=True)
    assert 'href=' not in wm.card_compare(r, px, fragment=True)

@pytest.mark.parametrize('symbol',["<script>alert(1)</script>",'A" onmouseover="bad'])
def test_html_escapes_injected_labels(px,symbol):
    px=px.rename(columns={'A':symbol});r=wm.analyze_frame(px,'MARKET',{symbol:.6,'B':.4})
    html=wm.card_analyze(r,px)
    assert symbol not in html and '&lt;script&gt;' in html if symbol.startswith('<') else symbol not in html

def test_csv_duplicate_header_fails_before_pandas_mangles(tmp_path):
    f=tmp_path/'bad.csv';f.write_text('date,A,A\n2023-01-02,100,100\n2023-01-03,101,101\n2023-01-04,102,102\n')
    args=Namespace(price_file=str(f),metadata_file=None,currency='USD',years=5,tickers=['A'])
    with pytest.raises(ValueError,match='duplicate'):wm._cli_prices(args)

def test_offline_cli_and_overwrite(px,tmp_path):
    f=tmp_path/'px.csv';px.to_csv(f)
    card=tmp_path/'card.html'
    cmd=[sys.executable,str(Path(wm.__file__)),'analyze','A','B','--weights','.6','.4','--bench','MARKET','--currency','USD','--price-file',str(f),'--card',str(card)]
    run=subprocess.run(cmd,capture_output=True,text=True,timeout=30)
    assert run.returncode==0,run.stderr
    assert json.loads(run.stdout)['portfolio']['weights']=={'A':.6,'B':.4}
    again=subprocess.run(cmd,capture_output=True,text=True,timeout=30)
    assert again.returncode==2 and 'output exists' in again.stderr

def test_calendar_daily_series_cannot_use_252_silently(px):
    px.index=pd.date_range('2023-01-01',periods=len(px))
    with pytest.raises(ValueError,match='weekday'):wm.analyze_frame(px,'MARKET',{'A':1})


def test_regimes_weekend_series_cannot_use_252_silently(px):
    px.index=pd.date_range('2023-01-01',periods=len(px))
    with pytest.raises(ValueError,match='weekday'):
        wm.regimes_frame(px,'MARKET',{'A':1})


def test_official_factor_archive_parser_offline(monkeypatch,tmp_path):
    import io, zipfile
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w') as z:
        z.writestr('F-F_Research_Data_Factors_daily.csv',
            'Synthetic provider-contract fixture\n,Mkt-RF,SMB,HML,RF\n20260914,1.0,2.0,3.0,0.01\n20260915,-99.99,0.0,0.0,0.02\n\nAnnual Factors\n2025,99,99,99,99\n')
    monkeypatch.setattr(wm,'CACHE',tmp_path)
    urls=[]
    def fetch(request,timeout):
        urls.append(request.full_url)
        return io.BytesIO(buffer.getvalue())
    monkeypatch.setattr(wm.urllib.request,'urlopen',fetch)
    actual=wm._load_factors(3)
    assert urls[0].startswith('https://mba.tuck.dartmouth.edu/')
    assert actual.index.tolist()==[pd.Timestamp('2026-09-14'),pd.Timestamp('2026-09-15')]
    assert actual.iloc[0]['Mkt-RF']==pytest.approx(.01)
    assert actual.iloc[0]['RF']==pytest.approx(.0001)
    assert pd.isna(actual.iloc[1]['Mkt-RF'])


@pytest.mark.parametrize('content',[{'readme.txt':'no CSV'}, {'daily.csv':'missing header\n20260915,0,0,0'}, {'one.csv':'','two.csv':''}])
def test_malformed_factor_archives_rejected(monkeypatch,tmp_path,content):
    import io,zipfile
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w') as z:
        for filename,text in content.items():z.writestr(filename,text)
    monkeypatch.setattr(wm,'CACHE',tmp_path)
    monkeypatch.setattr(wm.urllib.request,'urlopen',lambda *a,**k:io.BytesIO(buffer.getvalue()))
    with pytest.raises(ValueError):wm._load_factors(3)


@pytest.mark.parametrize('task',['plan','review','fees'])
def test_additional_offline_cli(task,tmp_path,profile):
    command=[sys.executable,str(Path(wm.__file__)),task]
    if task=='fees':
        command+=['--initial','100000','--gross-return','.05','--fees','.001','.01','--years','20']
    else:
        data=profile if task=='plan' else dict(values={'A':60,'B':40},target={'A':.5,'B':.5},threshold_pp=5,currency='USD',as_of=date.today().isoformat())
        path=tmp_path/f'{task}.json';path.write_text(json.dumps(data));command.append(str(path))
    run=subprocess.run(command,capture_output=True,text=True,timeout=30)
    assert run.returncode==0,run.stderr
    result=json.loads(run.stdout)
    if task=='plan':assert result['uncommitted_capital']==280000
    elif task=='review':assert result['review_needed'] is True
    else:assert result['scenarios'][0]['ending_value']>result['scenarios'][1]['ending_value']


# --------------------------------------------------------------------------
# risk-free carry-forward: Ken French publishes with a lag, so a live window
# routinely ends after his last date. Sharpe and alpha survive that; a hole
# inside the published range still does not.
# --------------------------------------------------------------------------
def _ff_stub(monkeypatch, last: str, idx):
    ff = pd.DataFrame({'Mkt-RF': .0003, 'SMB': 0., 'HML': 0., 'RF': .0002},
                      index=idx[idx <= pd.Timestamp(last)])
    monkeypatch.setattr(wm, '_load_factors', lambda model=3: ff)
    return ff


def test_rf_carries_the_last_published_rate_forward_and_says_so(monkeypatch):
    idx = pd.bdate_range('2024-01-01', periods=300)
    _ff_stub(monkeypatch, str(idx[-20].date()), idx)
    warnings = []
    rf = wm._rf_daily(idx, warnings, 'USD')
    assert rf is not None and not rf.isna().any()
    assert float(rf.iloc[-1]) == pytest.approx(.0002)
    assert any('carried forward' in w for w in warnings)
    assert any('stale short rate' in w for w in warnings)


def test_rf_refuses_to_carry_forward_past_ninety_days(monkeypatch):
    idx = pd.bdate_range('2024-01-01', periods=400)
    _ff_stub(monkeypatch, str(idx[-120].date()), idx)   # ~170 calendar days short
    warnings = []
    assert wm._rf_daily(idx, warnings, 'USD') is None
    assert any('Sharpe and alpha omitted' in w for w in warnings)


def test_rf_still_refuses_a_hole_inside_the_published_range(monkeypatch):
    idx = pd.bdate_range('2024-01-01', periods=300)
    ff = _ff_stub(monkeypatch, str(idx[-20].date()), idx).copy()
    ff.loc[ff.index[100], 'RF'] = np.nan
    monkeypatch.setattr(wm, '_load_factors', lambda model=3: ff)
    warnings = []
    assert wm._rf_daily(idx, warnings, 'USD') is None


def test_sharpe_survives_a_window_ending_after_the_last_factor_date(px, monkeypatch):
    _ff_stub(monkeypatch, str(px.index[-15].date()), px.index)
    px = px.copy(); px.attrs['currency'] = 'USD'; px.attrs.pop('risk_free_policy', None)
    res = wm.analyze_frame(px, 'MARKET', {'A': .6, 'B': .4},
                           meta={t: {'currency': 'USD'} for t in px.columns})
    assert res['portfolio']['sharpe'] is not None
    assert res['portfolio']['alpha'] is not None


# --------------------------------------------------------------------------
# 13F: investable weights stay off by default; the opt-in states its coverage
# --------------------------------------------------------------------------
_13F_ROWS = [{'name': 'APPLE INC', 'cusip': '037833100', 'value': 600., 'shares': 3.,
              'put_call': None, 'share_class': 'COM', 'amount_type': 'SH'},
             {'name': 'NVIDIA CORP', 'cusip': '67066G104', 'value': 300., 'shares': 2.,
              'put_call': None, 'share_class': 'COM', 'amount_type': 'SH'},
             {'name': 'SOME PRIVATE PLACEMENT', 'cusip': '999999999', 'value': 100.,
              'shares': 1., 'put_call': None, 'share_class': None, 'amount_type': 'SH'}]
_NAMES = {wm._norm_issuer('APPLE INC'): 'AAPL',
          wm._norm_issuer('NVIDIA CORP'): 'NVDA'}


def _mapped():
    holdings, _, _ = wm.holdings_from_table(_13F_ROWS, _NAMES)
    return holdings


def test_holdings_still_emit_no_weights_by_default():
    holdings, weights, warn = wm.holdings_from_table(_13F_ROWS, _NAMES)
    assert weights == {}
    assert any('no investable weights' in w for w in warn)
    assert all(h['mapping_status'] == 'unverified' for h in holdings)


def test_suggested_weights_renormalise_the_mapped_subset_and_report_coverage():
    weights, coverage = wm.suggested_weights(_mapped())
    assert set(weights) == {'AAPL', 'NVDA'}
    assert sum(weights.values()) == pytest.approx(1.0)
    assert weights['AAPL'] == pytest.approx(2 / 3, abs=1e-6)
    assert coverage == pytest.approx(0.9)          # the unmapped 10% is excluded


def test_suggested_weights_never_include_options():
    rows = _13F_ROWS + [{'name': 'APPLE INC', 'cusip': '037833100', 'value': 500.,
                         'shares': 1., 'put_call': 'Put', 'share_class': 'COM',
                         'amount_type': 'SH'}]
    weights, coverage = wm.suggested_weights(wm.holdings_from_table(rows, _NAMES)[0])
    assert set(weights) == {'AAPL', 'NVDA'}
    assert weights['AAPL'] == pytest.approx(2 / 3, abs=1e-6)


def test_opt_in_flag_attaches_coverage_and_a_stated_caveat(monkeypatch):
    monkeypatch.setattr(wm, '_resolve_filer', lambda q: ('0000000001', 'Test Fund'))
    monkeypatch.setattr(wm, '_latest_13f', lambda cik: {'accession': 'x', 'period': '2025-06-30',
                                                        'filed': '2025-08-14', 'fund': 'Test Fund'})
    monkeypatch.setattr(wm, '_information_table', lambda cik, acc: '')
    monkeypatch.setattr(wm, 'parse_13f_table', lambda xml: list(_13F_ROWS))
    monkeypatch.setattr(wm, '_sec_name_tickers', lambda: dict(_NAMES))
    monkeypatch.setattr(wm, '_cache_path', lambda name: Path(tempfile.mkdtemp()) / name)
    plain = wm.fund_holdings('Test Fund', use_cache=False)
    assert plain['weights'] == {} and 'coverage' not in plain
    opted = wm.fund_holdings('Test Fund', use_cache=False, weights_from_suggestions=True)
    assert set(opted['weights']) == {'AAPL', 'NVDA'}
    assert opted['coverage'] == pytest.approx(0.9)
    assert any('weights-from-suggestions' in w for w in opted['warnings'])
    # and the contradictory default disclaimer is withdrawn, not left standing
    assert not any('no investable weights produced' in w for w in opted['warnings'])
    assert any('unverified' in w for w in opted['warnings'])
    # the disclosure never gains a verified-mapping claim it cannot support
    assert opted['verified_mapped_value_fraction'] == 0.0


# --------------------------------------------------------------------------
# DESIGN v2.2: the page is descriptive. Method notes stay in JSON and --pretty;
# only "this is not a picture of what you asked for" reaches the reader.
# --------------------------------------------------------------------------
@pytest.mark.parametrize('text', [
    'risk-free series ends 2026-07-31; the last published rate is carried forward',
    'no matching-currency risk-free series for MXN; the US daily risk-free rate is substituted',
    'complete weighted expense ratio unavailable; fee coverage is partial',
    'fewer than 126 return observations; estimates are particularly unstable',
    'report refreshed from a new price sample; all statistics recomputed',
    'US factor model; correlations and coefficients are not causal',
])
def test_method_warnings_never_reach_the_page(text):
    assert wm._page_warnings([text]) == []


@pytest.mark.parametrize('text', [
    'requested ticker ZZZZ dropped: no price history',
    'exchange rate for MXN unavailable on the last observation',
    'benchmark ACWI unavailable; beta and alpha omitted',
])
def test_warnings_that_change_the_picture_do_reach_the_page(text):
    assert wm._page_warnings([text]) == [text]


def test_the_page_shows_at_most_one_warning_line():
    many = ['requested ticker AAA dropped: no price history',
            'requested ticker BBB dropped: no price history',
            'exchange rate unavailable']
    assert len(wm._page_warnings(many)) == 1


def test_report_page_carries_no_method_warning(tmp_path, px):
    px = px.copy(); px.attrs['currency'] = 'USD'; px.attrs.pop('risk_free_policy', None)
    stats = wm.analyze_frame(px, 'MARKET', {'A': .6, 'B': .4},
                             meta={t: {'currency': 'USD'} for t in px.columns})
    portfolio = {'name': 'T', 'bench': 'MARKET', 'weights': {'A': .6, 'B': .4},
                 'rebalance': 'annual', 'stats': stats}
    html = wm.write_report(portfolio, px, tmp_path / 'r.html').read_text()
    # nothing in the masthead: that is the part of the page a reader actually reads
    mast = html[html.index('wm-mast'):html.index('wm-grid')]
    assert 'wm-warn' not in mast
    assert 'fee coverage' not in mast and 'risk-free' not in mast
    # the notes survive where they belong: the closed provenance disclosure and the JSON
    evidence = html[html.index('wm-evidence'):]
    assert 'fee coverage is partial' in evidence
    assert any('expense ratio' in w for w in stats['warnings'])


# --------------------------------------------------------------------------
# masthead meta reads in months, not ISO dates
# --------------------------------------------------------------------------
def test_masthead_meta_uses_months_not_iso_dates():
    res = {'window_start': '2021-09-15', 'window_end': '2026-09-14',
           'bench': 'SPY', 'currency': 'MXN'}
    meta = wm._meta(res, 'the S&P 500')
    assert meta.startswith('Sep 2021')
    assert 'Sep 2026' in meta and '2021-09-15' not in meta
    assert meta.endswith('MXN') and 'vs S&P' in meta


# --------------------------------------------------------------------------
# fees: provider lookup, unit guard, stocks are a known zero, 90% headline rule
# --------------------------------------------------------------------------
@pytest.mark.parametrize('raw,kind,expected', [
    ({'netExpenseRatio': 0.03}, 'ETF', 0.0003),      # quoted in percent
    ({'netExpenseRatio': 0.0945}, 'ETF', 0.000945),  # still percent
    ({'netExpenseRatio': 0.35}, 'ETF', 0.0035),
    ({'annualReportExpenseRatio': 0.0009}, 'ETF', 0.0009),  # already decimal
    ({}, 'EQUITY', 0.0),                             # a share is not a fund
    ({}, 'MUTUALFUND', None),                        # genuinely unknown
    ({'netExpenseRatio': 250.0}, 'ETF', None),       # survives neither reading
    ({'netExpenseRatio': -1.0}, 'ETF', None),
])
def test_provider_expense_ratio_units_and_guards(monkeypatch, raw, kind, expected, tmp_path):
    info = dict(raw); info['quoteType'] = kind; info['currency'] = 'USD'
    class _T:
        def __init__(self, t): pass
        def get_info(self): return info
    monkeypatch.setitem(sys.modules, 'yfinance', type('m', (), {'Ticker': _T}))
    monkeypatch.setattr(wm, '_cache_path', lambda name: tmp_path / name)
    got = wm._ticker_meta(['X'])['X']
    assert (got['expense_ratio'] is None if expected is None
            else got['expense_ratio'] == pytest.approx(expected))
    assert got['fee_status'] == ('not-a-fund' if kind == 'EQUITY' and not raw
                                 else 'provider' if expected is not None else 'unavailable')


def test_annual_cost_quotes_the_blend_above_ninety_percent_coverage():
    keys = wm._keys({'n_holdings': 4, 'expense_ratio': None, 'fee_known_weight': 0.95,
                     'fee_known_contribution': 0.00095}, {}, 'the S&P 500')
    assert '0.10%' in keys and 'of the weight' not in keys


def test_annual_cost_states_coverage_below_ninety_percent():
    keys = wm._keys({'n_holdings': 4, 'expense_ratio': None, 'fee_known_weight': 0.70,
                     'fee_known_contribution': 0.000511}, {}, 'the S&P 500')
    assert '0.07%' in keys and 'published for 70% of the weight' in keys


def test_annual_cost_stays_an_em_dash_with_no_coverage_at_all():
    keys = wm._keys({'n_holdings': 4, 'expense_ratio': None, 'fee_known_weight': 0.0,
                     'fee_known_contribution': 0.0}, {}, 'the S&P 500')
    assert 'no published fee for these holdings' in keys and '—' in keys


# --------------------------------------------------------------------------
# a non-USD investor still gets Sharpe and alpha, with the substitution stated
# --------------------------------------------------------------------------
def test_non_usd_substitutes_the_us_risk_free_and_says_so(monkeypatch):
    idx = pd.bdate_range('2024-01-01', periods=300)
    _ff_stub(monkeypatch, str(idx[-1].date()), idx)
    warnings = []
    rf = wm._rf_daily(idx, warnings, 'MXN')
    assert rf is not None and not rf.isna().any()
    assert any('no matching-currency risk-free series for MXN' in w for w in warnings)
    assert any('substituted' in w for w in warnings)


def test_undeclared_currency_still_omits_the_risk_free():
    warnings = []
    assert wm._rf_daily(pd.bdate_range('2024-01-01', periods=30), warnings, None) is None
    assert any('not declared' in w for w in warnings)
