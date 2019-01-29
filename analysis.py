# -*- coding: utf8 -*-
import sys
import os
from datetime import datetime, timedelta
import csv
import math

import numpy as np
import xarray as xr
import dask
from dask.diagnostics import ProgressBar
import zarr
import scipy.stats
import numba as nb

import ev_fit
import gof
import helper

DATA_DIR = '/home/lunet/gylc4/geodata/ERA5/'
# DATA_DIR = '../data/MIDAS/'
# HOURLY_FILE1 = 'midas_2000-2017_precip_pairs.nc'
# HOURLY_FILE2 = 'era5_1979-1999_precip.zarr'

ANNUAL_FILE = 'era5_1979-2017_precip_annual_max.zarr'
ANNUAL_FILE_BASENAME = 'era5_1979-2017_ams_{}.zarr'
ANNUAL_FILE_FITTED = 'era5_1979-2017_precip_fitted.zarr'
# ANNUAL_FILE_GRADIENT = 'era5_2000-2017_precip_gradient.zarr'
# ANNUAL_FILE_SCALING = 'midas_2000-2017_precip_pairs_scaling.nc'

LOG_FILENAME = 'Analysis_log_{}.csv'.format(str(datetime.now()))

# Extract
# EXTRACT = dict(latitude=slice(1.0, -0.25),
#                longitude=slice(32.5, 35))
EXTRACT = dict(latitude=slice(45, 40),
               longitude=slice(0, 5))

# Event durations in hours - has to be adjusted to temporal resolution for the moving window
# Selected to be equally spaced on a log scale. Manually adjusted from a call to np.geomspace()
DURATIONS_SUBDAILY = [1, 2, 3, 4, 6, 8, 10, 12, 18, 24]
DURATIONS_DAILY = [24, 48, 72, 96, 120, 144, 192, 240, 288, 360]
# use fromkeys to remove duplicate. need py >= 3.6 to preserve order
DURATIONS_ALL = list(dict.fromkeys(DURATIONS_SUBDAILY + DURATIONS_DAILY))

DURATION_DICT = {'all': DURATIONS_ALL, 'daily': DURATIONS_DAILY, 'subdaily': DURATIONS_SUBDAILY}
# DURATION_DICT = {'daily': DURATIONS_DAILY}
# Temporal resolution of the input in hours
TEMP_RES = 1
# TEMP_RES = 24

LR_RES = ['slope', 'intercept', 'rvalue', 'pvalue', 'stderr']

DTYPE = 'float32'

HOURLY_CHUNKS = {'time': -1, 'latitude': 16, 'longitude': 16}
# 4 cells: 1 degree
ANNUAL_CHUNKS = {'year': -1, 'duration':-1, 'latitude': 45*4, 'longitude': 45*4}
ANNUAL_CHUNKS_1DEG = {'year': -1, 'duration': -1, 'latitude': 30, 'longitude': 30}
EXTRACT_CHUNKS = {'year': -1, 'duration':-1, 'latitude': 4, 'longitude': 4}
GAUGES_CHUNKS = {'year': -1, 'duration':-1, 'station': 200}
GEN_FLOAT_ENCODING = {'dtype': DTYPE, 'compressor': zarr.Blosc(cname='lz4', clevel=9)}
ANNUAL_ENCODING = {'annual_max': GEN_FLOAT_ENCODING,
                   'duration': {'dtype': DTYPE},
                   'latitude': {'dtype': DTYPE},
                   'longitude': {'dtype': DTYPE}}


def step1_annual_maxs_of_roll_mean(ds, precip_var, time_dim, durations, temp_res):
    """for each rolling winfows size:
    compute the annual maximum of a moving mean
    return an array with the durations as a new dimension
    """
    annual_maxs = []
    for duration in durations:
        window_size = int(duration / temp_res)
        precip = ds[precip_var]
        precip_roll_mean = precip.rolling(**{time_dim:window_size}, min_periods=max(int(window_size*.9), 1)).mean(dim=time_dim, skipna=True)
        annual_max = precip_roll_mean.groupby('{}.year'.format(time_dim)).max(dim=time_dim, skipna=True)
        annual_max.name = 'annual_max'
        da = annual_max.expand_dims('duration')
        da.coords['duration'] = [duration]
        annual_maxs.append(da)
    return xr.concat(annual_maxs, 'duration')


def step21_pole_trim(ds):
    # remove north pole to get a dividable latitude number
    lat_second = ds['latitude'][1].item()
    lat_last = ds['latitude'][-1].item()
    ds = ds.sel(latitude=slice(lat_second, lat_last))
    return(ds)


def step22_rank_ecdf(ds_ams, chunks):
    """Compute the rank of AMS and the empirical probability
    """
    n_obs = ds_ams['year'].count()
    # Add rank and turn to dataset
    ds = ev_fit.rank_ams(ds_ams['annual_max'], chunks, DTYPE)
    # Empirical probability
    ds['ecdf_weibull'] = ev_fit.ecdf_weibull(ds['rank'], n_obs)
    ds['ecdf_hosking'] = ev_fit.ecdf_hosking(ds['rank'], n_obs)
    ds['ecdf_gringorten'] = ev_fit.ecdf_gringorten(ds['rank'], n_obs)
    return ds


def step23_fit_ev(ds):
    """Fit EV using various techniques.
    Keep them along a new dimension
    """
    models = [
        # (ev_fit.frechet_mom, (ds), 'frechet_mom'),
        # (ev_fit.gumbel_iter_linear, (ds), 'gumbel_iter_linear'),
        # (frechet_mle_fit, (ds), 'frechet_mle'),
        # (ev_fit.gev_mle_fit, (ds), 'gev_mle'),
        (ev_fit.gev_pwm, (ds,), 'gev_pwm'),
        (ev_fit.gev_pwm, (ds, 0.114), 'frechet_pwm'),
        ]
    ds_list = []
    for fit_func, args, name in models:
        ds_fit = xr.merge(fit_func(*args))
        ds_fit['cdf'] = ev_fit.gev_cdf(ds['annual_max'],
                                       ds_fit['location'],
                                       ds_fit['scale'],
                                       ds_fit['shape'],)
        ds_fit = ds_fit.expand_dims('ev_fit')
        ds_fit.coords['ev_fit'] = [name]
        ds_list.append(ds_fit)
    ds_fit = xr.concat(ds_list, dim='ev_fit')
    ds = xr.merge([ds, ds_fit])
    return ds


def step25_ci(ds):
    """Estimate confidence interval using the bootstrap method
    """
    return ds

def step24_goodness_of_fit(ds):
    # Goodness of fit of the distribution
    # da_crit, da_a2 = gof.anderson_darling(ds)
    # Do the fitting
    # ds = xr.merge([da_crit, da_a2, ds, ds_fit])
    # Perform the Kolmogorov-Smirnov test
    # ds = KS_test(ds)
    ds['KS_D'] = gof.KS_test(ds['ecdf_weibull'], ds['cdf'])
    ds = gof.KS_Dcrit(ds, ANNUAL_CHUNKS)
    return ds


def step3_scaling(ds):
    """Take a Dataset as input containing the fitted EV parameters
    Fit a linear function on the log of the parameters and the log of the duration
    Keep the regression parameters as variables
    The fitting is done on various groups of durations kept as a new dimension
    """
    # log-transform the variables
    var_list = ['duration', 'location', 'scale']
    logvar_list = ['log_duration', 'log_location', 'log_scale']
    for var, log_var in zip(var_list, logvar_list):
        ds[log_var] = np.log10(ds[var])

    ds_list = []
    for dur_name, durations in DURATION_DICT.items():
        # Select only the durations of interest
        ds_sel = ds.sel(duration=durations)
        da_list = []
        for g_param_name in ['location', 'scale']:
            param_col = 'log_{}'.format(g_param_name)
            # Do the linear regression.
            slope, intercept, rvalue, pvalue, stderr = linregress(nanlinregress,
                                                  ds_sel['log_duration'],
                                                  ds_sel[param_col],
                                                  ['duration'])
            for var_name, da in zip(['line_slope', 'line_intercept', 'line_rvalue', 'line_pvalue', 'line_stderr'],
                                    [slope, intercept, rvalue, pvalue, stderr]):
                da.name = '{}_{}'.format(g_param_name, var_name)
                da_list.append(da)
        # Group all DataArrays in a single dataset
        ds_fit = xr.merge(da_list)
        # Keep the the results in their own dimension
        ds_fit = ds_fit.expand_dims('scaling_extent')
        ds_fit.coords['scaling_extent'] = [dur_name]
        ds_list.append(ds_fit)
    ds_fit = xr.concat(ds_list, dim='scaling_extent')
    # Add thos DataArray to the general Dataset
    ds = xr.merge([ds, ds_fit])
    return ds


def adhoc_AD(annual_max):
    """Calculate Anderson-Darling statistic for each duration.
    Save a separate file for each duration
    """
    # for duration in annual_max['duration']:
    for duration in [24]:
        # duration_value = duration.values
        duration_value = duration
        print(duration_value)
        da_sel = annual_max.sel(duration=duration)
        da_a2 = xr.apply_ufunc(anderson_gumbel,
                               da_sel.load(),
                               input_core_dims=[['year']],
                               vectorize=True,
                            #    dask='parallelized',
                               output_dtypes=[DTYPE]
                               ).rename('A2')
        out_name = 'era5_1979-2017_precip_a2_{}.nc'.format(duration_value)
        out_path = os.path.join(DATA_DIR, out_name)
        da_a2.to_dataset().to_netcdf(out_path)


def logger(fields):
    log_file_path = os.path.join(DATA_DIR, LOG_FILENAME)
    with open(log_file_path, mode='a') as log_file:
        csv_writer = csv.writer(log_file)
        csv_writer.writerow(fields)


def main():
    # Log file
    # logger(['operation', 'timestamp', 'cumul_sec'])

    with ProgressBar():
        start_time = datetime.now()
        # Load hourly data #
        # logger(['start computing annual maxima', str(start_time), 0])
        # hourly_path1 = os.path.join(DATA_DIR, HOURLY_FILE1)
        # hourly1 = xr.open_dataset(hourly_path1)#.chunk(HOURLY_CHUNKS).loc[EXTRACT]
        # hourly = xr.open_zarr(hourly_path1)

        # Get annual maxima #
        # annual_maxs = step1_annual_maxs_of_roll_mean(hourly1, 'prcp_amt', 'end_time', DURATIONS_ALL, TEMP_RES)#.chunk(ANNUAL_CHUNKS)
        # ams = step1_annual_maxs_of_roll_mean(hourly2, 'precipitation', 'time', DURATIONS_ALL, TEMP_RES).chunk(ANNUAL_CHUNKS)
        # print(ams)
        amax_path1 = os.path.join(DATA_DIR, ANNUAL_FILE)
        ams1 = xr.open_zarr(amax_path1)

        # reshape to 1 deg
        # ds_trimmed = step21_pole_trim(ams1)
        # print(ds_trimmed)
        # ds_r = helper.da_pool(ds_trimmed['annual_max'], .25, 1).to_dataset().chunk(ANNUAL_CHUNKS_1DEG)
        # print(ds_r)

        # Rank # 
        # ams_path = os.path.join(DATA_DIR, ANNUAL_FILE)
        # ams = xr.open_zarr(ams_path)
        ds_ranked = step22_rank_ecdf(ams1, ANNUAL_CHUNKS)
        # print(ds_ranked)
        ranked_path = os.path.join(DATA_DIR, ANNUAL_FILE_BASENAME.format('ranked'))
        encoding = {v:GEN_FLOAT_ENCODING for v in ds_ranked.data_vars.keys()}
        ds_ranked.to_zarr(ranked_path, mode='w', encoding=encoding)

        # fit EV #
        ds_ranked = xr.open_zarr(ranked_path)#.loc[EXTRACT]
        ds_fitted = step23_fit_ev(ds_ranked)
        fitted_path = os.path.join(DATA_DIR, ANNUAL_FILE_BASENAME.format('fitted'))
        encoding = {v:GEN_FLOAT_ENCODING for v in ds_fitted.data_vars.keys()}
        ds_fitted.to_zarr(fitted_path, mode='w', encoding=encoding)
        # print(ds_fitted)

        # GoF test #
        # ds_fitted = xr.open_zarr(fitted_path)
        # ds_gof = step24_goodness_of_fit(ds_fitted)
        # gof_path = os.path.join(DATA_DIR, ANNUAL_FILE_BASENAME.format('gof_1'))
        # encoding = {v:GEN_FLOAT_ENCODING for v in ds_gof.data_vars.keys()}
        # ds_gof.to_zarr(gof_path, mode='w', encoding=encoding)


        # ds_gof = xr.open_zarr(gof_path)
        # print(ds_gof.load())
        # print(ds_gof['KS_D'].mean(dim=['latitude', 'longitude', 'duration']).load())
        # print(ds_gof['shape'].mean(dim=['latitude', 'longitude']).load())
        # print(ds_gof['shape'].std(dim=['latitude', 'longitude', 'duration']).load())

        # bootstrap
        # ds_gof.sel(ev_fit)



        # ds_fitted = xr.open_zarr(fitted_path)#.loc[EXTRACT]
        # MAE of parameters
        # ds_fitted.load()
        # ds_pwm = ds_fitted.sel(ev_fit='gev_pwm')
        # ds_mle = ds_fitted.sel(ev_fit='gev_mle')
        # shape_mae = np.abs(-ds_pwm['shape'] - ds_mle['shape']).mean()
        # scale_mae = np.abs(ds_pwm['scale'] - ds_mle['scale']).mean()
        # location_mae = np.abs(ds_pwm['location'] - ds_mle['location']).mean()
        # print(location_mae)
        # print(ds_mle['location'].mean())
        # print(scale_mae)
        # print(ds_mle['scale'].mean())
        # print(shape_mae)
        # print(ds_mle['shape'].mean())
        # print(ds_pwm['shape'].mean())


if __name__ == "__main__":
    sys.exit(main())
