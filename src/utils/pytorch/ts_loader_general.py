# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/data__tsloader_general.ipynb (unless otherwise specified).

__all__ = ['TimeSeriesLoader']

# Cell
import numpy as np
import pandas as pd
import random
import torch as t
import copy
from src.utils.pytorch.ts_dataset import TimeSeriesDataset
from collections import defaultdict

# Cell
# TODO: Check if the saturday zero protection is still in place
class TimeSeriesLoader(object):
    def __init__(self,
                 ts_dataset: TimeSeriesDataset,
                 model: str,
                 offset: int,
                 window_sampling_limit: int,
                 input_size: int,
                 output_size: int,
                 idx_to_sample_freq: int,
                 batch_size: int,
                 complete_inputs: bool,
                 complete_sample: bool,
                 shuffle: bool,
                 n_series_per_batch: int=None,
                 verbose: bool=False):
        """
        """
        # Dataloader attributes
        self.model = model
        self.window_sampling_limit = window_sampling_limit
        self.input_size = input_size
        self.output_size = output_size
        self.batch_size = batch_size
        self.complete_inputs = complete_inputs
        self.complete_sample = complete_sample
        self.idx_to_sample_freq = idx_to_sample_freq
        self.offset = offset
        self.ts_dataset = ts_dataset
        self.t_cols = self.ts_dataset.t_cols
        if n_series_per_batch is not None:
            self.n_series_per_batch = n_series_per_batch
        else:
            self.n_series_per_batch = min(batch_size, self.ts_dataset.n_series)
        self.windows_per_serie = self.batch_size // self.n_series_per_batch
        self.shuffle = shuffle
        self.verbose = verbose

        assert offset==0, 'sample_mask and offset interaction not implemented'
        # assert window_sampling_limit==self.ts_dataset.max_len, \
        #     'sample_mask and window_samplig_limit interaction not implemented'

        # Dataloader protections
        assert self.batch_size % self.n_series_per_batch == 0, \
                        f'batch_size {self.batch_size} must be multiple of n_series_per_batch {self.n_series_per_batch}'
        assert self.n_series_per_batch <= self.ts_dataset.n_series, \
                        f'n_series_per_batch {n_series_per_batch} needs to be smaller than n_series {self.ts_dataset.n_series}'
        assert offset < self.ts_dataset.max_len, \
            f'Offset {offset} must be smaller than max_len {self.ts_dataset.max_len}'

    def _get_sampleable_windows_idxs(self, ts_windows_flatten):
        if not self.complete_sample:
            #print("\n")
            #print("INTENTO RARO DE LIMPIEZA8")
            sample_condition = t.sum(ts_windows_flatten[:, self.t_cols.index('sample_mask'), -self.output_size:], axis=1)
            available_condition = t.sum(ts_windows_flatten[:, self.t_cols.index('available_mask'), :self.input_size], axis=1)
            if self.complete_inputs:
                completely_available_condition = (available_condition == (self.input_size)) * 1
                sampling_idx = t.nonzero(completely_available_condition * sample_condition > 0)
            else:
                sampling_idx = t.nonzero(available_condition * sample_condition > 0)
        else:
            sample_condition = t.sum(self.ts_windows[:, self.t_cols.index('sample_mask'), -self.output_size:], axis=1)
            sampling_idx = t.nonzero(sample_condition)

        sampling_idx = list(sampling_idx.flatten().numpy())
        assert len(sampling_idx)>0, 'Check the data and masks as sample_idxs are empty'
        return sampling_idx

    def _create_windows_tensor(self, ts_idxs=None):
        """
        Comment here
        TODO: Cuando creemos el otro dataloader, si es compatible lo hacemos funcion transform en utils
        """
        # Filter function is used to define train tensor and validation tensor with the offset
        # Default ts_idxs=ts_idxs sends all the data, otherwise filters series
        tensor, right_padding = self.ts_dataset.get_filtered_ts_tensor(offset=self.offset, output_size=self.output_size,
                                                                       window_sampling_limit=self.window_sampling_limit,
                                                                       ts_idxs=ts_idxs)
        tensor = t.Tensor(tensor)

        padder = t.nn.ConstantPad1d(padding=(self.input_size, right_padding), value=0)
        tensor = padder(tensor)

        # Creating rolling windows and 'flattens' them
        windows = tensor.unfold(dimension=-1, size=self.input_size + self.output_size, step=self.idx_to_sample_freq)
        # n_serie, n_channel, n_time, window_size -> n_serie, n_time, n_channel, window_size
        #print(f'n_serie, n_channel, n_time, window_size = {windows.shape}')
        windows = windows.permute(0,2,1,3)
        #print(f'n_serie, n_time, n_channel, window_size = {windows.shape}')
        windows = windows.reshape(-1, self.ts_dataset.n_channels, self.input_size + self.output_size)

        # Broadcast s_matrix: This works because unfold in windows_tensor, orders: serie, time
        s_matrix = self.ts_dataset.s_matrix[ts_idxs]
        windows_per_serie = len(windows)//len(ts_idxs)
        s_matrix = s_matrix.repeat(repeats=windows_per_serie, axis=0)

        return windows, s_matrix

    def __iter__(self):
        n_series = self.ts_dataset.n_series
        # Shuffle idx before epoch if self._is_train
        if self.shuffle:
            sample_idxs = np.random.choice(a=range(n_series), size=n_series, replace=False)
        else:
            sample_idxs = np.array(range(n_series))

        n_batches = int(np.ceil(n_series / self.n_series_per_batch)) # Must be multiple of batch_size for paralel gpu

        for idx in range(n_batches):
            ts_idxs = sample_idxs[(idx * self.n_series_per_batch) : (idx + 1) * self.n_series_per_batch]
            batch = self.__get_item__(index=ts_idxs)
            yield batch

    def __get_item__(self, index):
        if (self.model == 'nbeats') or (self.model == 'tcn'):
            return self._windows_batch(index)
        elif self.model == 'esrnn':
            return self._full_series_batch(index)
        else:
            assert 1<0, 'error'

    def _windows_batch(self, index):
        """ NBEATS, TCN models """

        # Create windows for each sampled ts and sample random unmasked windows from each ts
        windows, s_matrix = self._create_windows_tensor(ts_idxs=index)
        sampleable_windows = self._get_sampleable_windows_idxs(ts_windows_flatten=windows)
        self.sampleable_windows = sampleable_windows

        # Get sample windows_idxs of batch
        if self.shuffle:
            windows_idxs = np.random.choice(sampleable_windows, self.batch_size, replace=True)
        else:
            windows_idxs = sampleable_windows

        # Index the windows and s_matrix tensors of batch
        windows = windows[windows_idxs]
        s_matrix = s_matrix[windows_idxs]

        # Parse windows to elements of batch
        insample_y = windows[:, self.t_cols.index('y'), :self.input_size]
        insample_x = windows[:, (self.t_cols.index('y')+1):self.t_cols.index('available_mask'), :self.input_size]
        available_mask = windows[:, self.t_cols.index('available_mask'), :self.input_size]

        outsample_y = windows[:, self.t_cols.index('y'), self.input_size:]
        outsample_x = windows[:, (self.t_cols.index('y')+1):self.t_cols.index('available_mask'), self.input_size:]
        sample_mask = windows[:, self.t_cols.index('sample_mask'), self.input_size:]

        batch = {'s_matrix': s_matrix,
                 'insample_y': insample_y, 'insample_x':insample_x, 'insample_mask':available_mask,
                 'outsample_y': outsample_y, 'outsample_x':outsample_x, 'outsample_mask':sample_mask}
        return batch

    def _full_series_batch(self, index):
        """ ESRNN, RNN models """
        #TODO: think masks, do they make sense for ESRNN and RNN??
        #TODO: window_sampling_limit no es dinamico por el offset no usado!!
        #TODO: padding preventivo
        ts_tensor, _ = self.ts_dataset.get_filtered_ts_tensor(offset=self.offset, output_size=self.output_size,
                                                                 window_sampling_limit=self.window_sampling_limit,
                                                                 ts_idxs=index)
        ts_tensor = t.Tensor(ts_tensor)
        # Trim batch to shorter time series to avoid zero padding, remove non sampleable ts
        # shorter time series is driven by the last ts_idx which is available
        # non-sampleable ts is driver by the first ts_idx which stops beeing sampleable
        available_mask_tensor = ts_tensor[:, self.t_cols.index('available_mask'), :]
        min_time_stamp = int(t.nonzero(t.min(available_mask_tensor, axis=0).values).min())
        sample_mask_tensor = ts_tensor[:, self.t_cols.index('sample_mask'), :]
        max_time_stamp = int(t.nonzero(t.min(sample_mask_tensor, axis=0).values).max())

        available_ts = max_time_stamp - min_time_stamp
        assert available_ts >= self.input_size + self.output_size, 'Time series too short for given input and output size'

        insample_y = ts_tensor[:, self.t_cols.index('y'), :]
        insample_y = insample_y[:, min_time_stamp:max_time_stamp+1] #+1 because is not inclusive

        insample_x = ts_tensor[:, self.t_cols.index('y')+1:self.t_cols.index('available_mask'), :]
        insample_x = insample_x[:, min_time_stamp:max_time_stamp+1] #+1 because is not inclusive

        s_matrix = self.ts_dataset.s_matrix[index]

        batch = {'insample_y': insample_y, 'idxs': index, 'insample_x': insample_x, 's_matrix': s_matrix}

        return batch

    def update_offset(self, offset):
        if offset == self.offset:
            return # Avoid extra computation
        self.offset = offset

    def get_meta_data_col(self, col):
        return self.ts_dataset.get_meta_data_col(col)

    def get_n_variables(self):
        return self.ts_dataset.n_x, self.ts_dataset.n_s

    def get_n_series(self):
        return self.ts_dataset.n_series

    def get_max_len(self):
        return self.ts_dataset.max_len

    def get_n_channels(self):
        return self.ts_dataset.n_channels

    def get_X_cols(self):
        return self.ts_dataset.X_cols

    def get_frequency(self):
        return self.ts_dataset.frequency

    def train(self):
        self._is_train = True

    def eval(self):
        self._is_train = False