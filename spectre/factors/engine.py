"""
@author: Heerozh (Zhang Jianhao)
@copyright: Copyright 2019, Heerozh. All rights reserved.
@license: Apache 2.0
@email: heeroz@gmail.com
"""
from typing import Union, Iterable, Tuple
import warnings
from .factor import BaseFactor, DataFactor, FilterFactor, AdjustedDataFactor
from .dataloader import DataLoader
from ..parallel import ParallelGroupBy
import pandas as pd
import numpy as np
import torch


class OHLCV:
    open = DataFactor(inputs=('',), is_data_after_market_close=False)
    high = DataFactor(inputs=('',))
    low = DataFactor(inputs=('',))
    close = DataFactor(inputs=('',))
    volume = DataFactor(inputs=('',))


class FactorEngine:
    """
    Engine for compute factors, used for back-testing and alpha-research both.
    """

    # friend private:

    def get_dataframe_(self):
        return self._dataframe

    def get_assetgroup_(self):
        return self._assetgroup

    def get_timegroup_(self):
        return self._timegroup

    def get_tensor_groupby_asset_(self, column) -> torch.Tensor:
        # cache data with column prevent double copying
        if column in self._column_cache:
            return self._column_cache[column]

        series = self._dataframe[column]
        data = torch.from_numpy(series.values).pin_memory().to(self._device, non_blocking=True)
        data = self._assetgroup.split(data)
        self._column_cache[column] = data
        return data

    def regroup_by_asset_(self, data: Union[torch.Tensor, pd.Series]) -> torch.Tensor:
        if isinstance(data, pd.Series):
            data = torch.tensor(data.values, device=self._device)
        else:
            data = self._timegroup.revert(data, 'regroup_by_asset_')
        data = self._assetgroup.split(data)
        return data

    def regroup_by_time_(self, data: Union[torch.Tensor, pd.Series]) -> torch.Tensor:
        if isinstance(data, pd.Series):
            data = torch.tensor(data.values, device=self._device)
        else:
            data = self._assetgroup.revert(data, 'regroup_by_time_')
        if self._mask is not None:
            data = data.masked_fill(~self._mask, np.nan)

        data = self._timegroup.split(data)
        return data

    def revert_to_series_(self, data: torch.Tensor, is_timegroup: bool) -> pd.Series:
        if is_timegroup:
            ret = self._timegroup.revert(data)
        else:
            ret = self._assetgroup.revert(data)
        return pd.Series(ret, index=self._dataframe.index)

    # private:

    def _prepare_tensor(self, start, end, max_backward):
        # Check cache, just in case, if use some ML techniques, engine may be called repeatedly
        # with same date range.
        if start == self._last_load[0] and end == self._last_load[1] \
                and max_backward <= self._last_load[2]:
            return

        # Get data
        self._dataframe = self._loader.load(start, end, max_backward)

        cat = self._dataframe.index.get_level_values(1).codes
        keys = torch.tensor(cat, device=self._device, dtype=torch.int32)
        self._assetgroup = ParallelGroupBy(keys)

        # time group prepare
        cat = self._dataframe.time_cat_id.values
        keys = torch.tensor(cat, device=self._device, dtype=torch.int32)
        self._timegroup = ParallelGroupBy(keys)

        self._column_cache = {}
        self._last_load = [start, end, max_backward]

    def _compute_and_revert(self, f: BaseFactor, name) -> Union[np.array, pd.Series]:
        """Returning pd.Series will cause very poor performance, please avoid it at 99% costs"""
        data = f.compute_(None)
        if f.is_timegroup:
            return self._timegroup.revert(data, name)
        else:
            return self._assetgroup.revert(data, name)

    # public:

    def __init__(self, loader: DataLoader) -> None:
        self._loader = loader
        self._dataframe = None
        self._assetgroup = None
        self._last_load = [None, None, None]
        self._column_cache = {}
        self._timegroup = None
        self._factors = {}
        self._filter = None
        self._device = torch.device('cpu')
        self._mask = None

    def get_device(self):
        return self._device

    def add(self,
            factor: Union[Iterable[BaseFactor], BaseFactor],
            name: Union[Iterable[str], str],
            ) -> None:
        """
        Add factor or filter to engine, as a column.
        """
        if isinstance(factor, Iterable):
            for i, fct in enumerate(factor):
                self.add(fct, name and name[i] or None)
        else:
            if name in self._factors:
                raise KeyError('A factor with the name {} already exists.'
                               'please specify a new name by engine.add(factor, new_name)'
                               .format(name))
            self._factors[name] = factor

    def set_filter(self, factor: Union[FilterFactor, None]) -> None:
        self._filter = factor

    def get_factor(self, name):
        return self._factors[name]

    def remove_all_factors(self) -> None:
        self._factors = {}
        self._last_load = [None, None, None]

    def to_cuda(self) -> None:
        self._device = torch.device('cuda')
        # Hot start cuda
        torch.tensor([0], device=self._device, dtype=torch.int32)
        self._last_load = [None, None, None]

    def to_cpu(self) -> None:
        self._device = torch.device('cpu')
        self._last_load = [None, None, None]

    def run(self, start: Union[str, pd.Timestamp], end: Union[str, pd.Timestamp],
            delay_factor=True) -> pd.DataFrame:
        """
        Compute factors and filters, return a df contains all.
        """
        if len(self._factors) == 0:
            raise ValueError('Please add at least one factor to engine, then run again.')

        if not delay_factor:
            for c, f in self._factors.items():
                if f.include_close_data():
                    warnings.warn("Warning!! delay_factor is set to False, "
                                  "but {} factor uses data that is only available "
                                  "after the market is closed.".format(c),
                                  RuntimeWarning)

        start, end = pd.to_datetime(start, utc=True), pd.to_datetime(end, utc=True)
        # make columns to data factors.
        OHLCV.open.inputs = (self._loader.get_ohlcv_names()[0], 'price_multi')
        OHLCV.high.inputs = (self._loader.get_ohlcv_names()[1], 'price_multi')
        OHLCV.low.inputs = (self._loader.get_ohlcv_names()[2], 'price_multi')
        OHLCV.close.inputs = (self._loader.get_ohlcv_names()[3], 'price_multi')
        OHLCV.volume.inputs = (self._loader.get_ohlcv_names()[4], 'vol_multi')

        # get factor
        filter_ = self._filter
        if filter_ and delay_factor:
            filter_ = filter_.shift(1)
        factors = {c: delay_factor and f.shift(1) or f for c, f in self._factors.items()}

        # Calculate data that requires backward in tree
        max_backward = max([f.get_total_backward_() for f in factors.values()])
        if filter_:
            max_backward = max(max_backward, filter_.get_total_backward_())
        # Get data
        self._prepare_tensor(start, end, max_backward)
        self._mask = None

        # ready to compute
        if filter_:
            filter_.pre_compute_(self, start, end)
        for f in factors.values():
            f.pre_compute_(self, start, end)

        # if cuda, parallel compute filter and sync
        if filter_ and self._device.type == 'cuda':
            stream = torch.cuda.Stream(device=self._device)
            filter_.compute_(stream)
            torch.cuda.synchronize(device=self._device)

        # get filter data for mask, use un-shifted filter
        if filter_:
            self._mask = self._compute_and_revert(self._filter, 'filter')

        # if cuda, parallel compute factors and sync
        if self._device.type == 'cuda':
            stream = torch.cuda.Stream(device=self._device)
            for col, fct in factors.items():
                fct.compute_(stream)
            torch.cuda.synchronize(device=self._device)

        # compute factors from cpu or read cache
        ret = pd.DataFrame(index=self._dataframe.index.copy())
        ret = ret.assign(**{c: self._compute_and_revert(f, c).cpu().numpy()
                            for c, f in factors.items()})

        # Remove filter False rows
        if filter_:
            shift_mask = self._compute_and_revert(filter_, 'filter')
            ret = ret[shift_mask.cpu().numpy()]

        # 这句话也很慢，如果为了模型计算用可以不需要
        ret = ret.loc[start:]

        # if there is no factor values in first tick, drop
        if ret.loc[ret.index[0][0]].isna().all(axis=None):
            ret.drop(ret.index[0][0], level=0, inplace=True)

        return ret

    def get_factors_raw_value(self):
        return {c: f.compute_(None) for c, f in self._factors.items()}

    def get_price_matrix(self,
                         start: Union[str, pd.Timestamp],
                         end: Union[str, pd.Timestamp],
                         prices: DataFactor = OHLCV.close,
                         ) -> pd.DataFrame:
        """
        Get the price data for Factor Return Analysis.
        :param start: same as run
        :param end: should long than factor end time, for forward returns calculations.
        :param prices: prices data factor. If you traded at the opening, you should set it
                       to OHLCV.open.
        """
        factors_backup = self._factors
        filter_backup = self._filter
        self._factors = {'price': AdjustedDataFactor(prices)}
        self._filter = None
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ret = self.run(start, end, delay_factor=False)
        self._factors = factors_backup
        self._filter = filter_backup

        return ret['price'].unstack(level=[1])

    def full_run(self, start, end, trade_at='close', periods=(1, 4, 9),
                 quantiles=5, filter_zscore=20, preview=True
                 ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        Return this:
        |    	                    |    	|  Returns  |      factor_name          	|
        |date	                    |asset	|10D	    |factor	    |factor_quantile	|
        |---------------------------|-------|-----------|-----------|-------------------|
        |2014-01-08 00:00:00+00:00	|ARNC	|0.070159	|0.215274	|5                  |
        |                           |BA	    |-0.038556	|-1.638784	|1                  |
        for alphalens analysis, you can use this:
        factor_data = full_run_return[['factor_name', 'Returns']].droplevel(0, axis=1)
        al.tears.create_returns_tear_sheet(factor_data)
        :param str, pd.Timestamp start: factor analysis start time
        :param str, pd.Timestamp end: factor analysis end time
        :param trade_at: which price for forward returns. 'open', or 'close.
                         If is 'current_close', same as run engine with delay_factor=False,
                         Meaning use the factor to trade on the same day it generated. Be sure that
                         no any high,low,close data is used in factor, otherwise will cause
                         lookahead bias.
        :param periods: forward return periods
        :param quantiles: number of quantile
        :param filter_zscore: drop extreme factor return, for stability of the analysis.
        :param preview: display a preview chart of the result
        """
        factors = self._factors.copy()

        column_names = {}
        # add quantile factor of all factors
        for c, f in factors.items():
            self.add(f.quantile(quantiles), c + '_q_')
            column_names[c] = (c, 'factor')
            column_names[c + '_q_'] = (c, 'factor_quantile')

        # add the rolling returns of each period
        shift = -1
        inputs = (OHLCV.close,)
        if trade_at == 'open':
            inputs = (OHLCV.open,)
        elif trade_at == 'current_close':
            shift = 0
        from .basic import Returns
        for n in periods:
            rtn = Returns(win=n + 1, inputs=inputs).shift(-n + shift)
            self.add(rtn, str(n) + '_r_')
            self.add(rtn.demean(), str(n) + '_d_')

        # run and get df
        factor_data = self.run(start, end, trade_at != 'current_close')
        self._factors = factors
        factor_data.index = factor_data.index.remove_unused_levels()
        assert len(factor_data.index.levels[0]) > max(periods), \
            'No enough data for forward returns, please expand the end date'
        last_date = factor_data.index.levels[0][-max(periods) + shift - 1]
        factor_data = factor_data.loc[:last_date]

        # todo filter_zscore

        # infer freq
        delta = min(factor_data.index.levels[0][1:] - factor_data.index.levels[0][:-1])
        unit = delta.resolution_string
        freq = int(delta / pd.Timedelta(1, unit))
        # change columns name
        period_cols = {n: str(n * freq) + unit for n in periods}
        for n, period_col in period_cols.items():
            column_names[str(n) + '_r_'] = ('Returns', period_col)
            column_names[str(n) + '_d_'] = ('Demeaned', period_col)
        new_cols = pd.MultiIndex.from_tuples([column_names[c] for c in factor_data.columns])
        factor_data.columns = new_cols
        factor_data.sort_index(axis=1, inplace=True)

        # mean return, return std err
        mean_return = pd.DataFrame(columns=pd.MultiIndex.from_arrays([[], []]))
        for fact_name, _ in factors.items():
            group = [(fact_name, 'factor_quantile'), 'date']
            for n, period_col in period_cols.items():
                demean_col = ('Demeaned', period_col)
                mean_col = (fact_name, period_col)
                grouped_mean = factor_data.groupby(group)[demean_col, ].agg('mean')
                mean_return[mean_col] = grouped_mean[demean_col]
        mean_return.index.levels[0].name = 'quantile'
        mean_return = mean_return.groupby(level=0).agg(['mean', 'sem'])

        # 用pyplot画复杂的图 先做quantile的，之后再做累计收益
        if preview:
            pass

        # 第一个返回factor data， 第二个返回mean return, 第三个返回performance factor return?
        return factor_data, mean_return
