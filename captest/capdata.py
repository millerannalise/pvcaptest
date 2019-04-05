# standard library imports
import os
import datetime
import re
import math
import copy
import collections
from functools import wraps
import warnings
import pytz
import importlib

# anaconda distribution defaults
import dateutil
import numpy as np
import pandas as pd

# anaconda distribution defaults
# visualization library imports
from bokeh.io import output_notebook, show
from bokeh.plotting import figure
from bokeh.palettes import Category10, Category20c, Category20b
from bokeh.layouts import gridplot
from bokeh.models import Legend, HoverTool, tools, ColumnDataSource

# pvlib imports
pvlib_spec = importlib.util.find_spec('pvlib')
if pvlib_spec is not None:
    from pvlib.location import Location
    from pvlib.pvsystem import PVSystem
    from pvlib.tracking import SingleAxisTracker
    from pvlib.pvsystem import retrieve_sam
    from pvlib.modelchain import ModelChain
else:
    warnings.warn('Clear sky functions will not work without the '
                  'pvlib package.')


plot_colors_brewer = {'real_pwr': ['#2b8cbe', '#7bccc4', '#bae4bc', '#f0f9e8'],
                      'irr-poa': ['#e31a1c', '#fd8d3c', '#fecc5c', '#ffffb2'],
                      'irr-ghi': ['#91003f', '#e7298a', '#c994c7', '#e7e1ef'],
                      'temp-amb': ['#238443', '#78c679', '#c2e699', '#ffffcc'],
                      'temp-mod': ['#88419d', '#8c96c6', '#b3cde3', '#edf8fb'],
                      'wind': ['#238b45', '#66c2a4', '#b2e2e2', '#edf8fb']}

met_keys = ['poa', 't_amb', 'w_vel', 'power']

# The search strings for types cannot be duplicated across types.
type_defs = collections.OrderedDict([
             ('irr', [['irradiance', 'irr', 'plane of array', 'poa', 'ghi',
                       'global', 'glob', 'w/m^2', 'w/m2', 'w/m', 'w/'],
                      (-10, 1500)]),
             ('temp', [['temperature', 'temp', 'degrees', 'deg', 'ambient',
                        'amb', 'cell temperature', 'TArray'],
                       (-49, 127)]),
             ('wind', [['wind', 'speed'],
                       (0, 18)]),
             ('pf', [['power factor', 'factor', 'pf'],
                     (-1, 1)]),
             ('op_state', [['operating state', 'state', 'op', 'status'],
                           (0, 10)]),
             ('real_pwr', [['real power', 'ac power', 'e_grid'],
                           (-1000000, 1000000000000)]),  # set to very lax bounds
             ('shade', [['fshdbm', 'shd', 'shade'], (0, 1)]),
             ('pvsyt_losses', [['IL Pmax', 'IL Pmin', 'IL Vmax', 'IL Vmin'],
                               (-1000000000, 100000000)]),
             ('index', [['index'], ('', 'z')])])

sub_type_defs = collections.OrderedDict([
                 ('ghi', [['sun2', 'global horizontal', 'ghi', 'global',
                           'GlobHor']]),
                 ('poa', [['sun', 'plane of array', 'poa', 'GlobInc']]),
                 ('amb', [['TempF', 'ambient', 'amb']]),
                 ('mod', [['Temp1', 'module', 'mod', 'TArray']]),
                 ('mtr', [['revenue meter', 'rev meter', 'billing meter', 'meter']]),
                 ('inv', [['inverter', 'inv']])])

irr_sensors_defs = {'ref_cell': [['reference cell', 'reference', 'ref',
                                  'referance', 'pvel']],
                    'pyran': [['pyranometer', 'pyran']],
                    'clear_sky':[['csky']]}

columns = ['pts_before_filter', 'pts_removed', 'filter_arguments']

def update_summary(func):
    """
    Todo
    ----
    not in place
        Check if summary is updated when function is called with inplace=False.
        It should not be.
    """
    @wraps(func)
    def wrapper(self, *args, **kwargs):
        pts_before = self.df_flt.shape[0]
        if pts_before == 0:
            pts_before = self.df.shape[0]
            self.summary_ix.append((self.name, 'count'))
            self.summary.append({columns[0]: pts_before,
                                 columns[1]: 0,
                                 columns[2]: 'no filters'})

        ret_val = func(self, *args, **kwargs)

        arg_str = args.__repr__()
        lst = arg_str.split(',')
        arg_lst = [item.strip("'() ") for item in lst]
        # arg_lst_one = arg_lst[0]
        # if arg_lst_one == 'das' or arg_lst_one == 'sim':
        #     arg_lst = arg_lst[1:]
        # arg_str = ', '.join(arg_lst)

        kwarg_str = kwargs.__repr__()
        kwarg_str = kwarg_str.strip('{}')

        if len(arg_str) == 0 and len(kwarg_str) == 0:
            arg_str = 'no arguments'
        elif len(arg_str) == 0:
            arg_str = kwarg_str
        else:
            arg_str = arg_str + ', ' + kwarg_str

        pts_after = self.df_flt.shape[0]
        pts_removed = pts_before - pts_after
        self.summary_ix.append((self.name, func.__name__))
        self.summary.append({columns[0]: pts_after,
                             columns[1]: pts_removed,
                             columns[2]: arg_str})

        return ret_val
    return wrapper

def perc_wrap(p):
    def numpy_percentile(x):
        return np.percentile(x.T, p, interpolation='nearest')
    return numpy_percentile

def flt_irr(df, irr_col, low, high, ref_val=None):
    """
    Top level filter on irradiance values.

    Parameters
    ----------
    df : DataFrame
        Dataframe to be filtered.
    irr_col : str
        String that is the name of the column with the irradiance data.
    low : float or int
        Minimum value as fraction (0.8) or absolute 200 (W/m^2)
    high : float or int
        Max value as fraction (1.2) or absolute 800 (W/m^2)
    ref_val : float or int
        Must provide arg when min/max are fractions

    Returns
    -------
    DataFrame
    """
    if ref_val is not None:
        low *= ref_val
        high *= ref_val

    df_renamed = df.rename(columns={irr_col: 'poa'})

    flt_str = '@low <= ' + 'poa' + ' <= @high'
    indx = df_renamed.query(flt_str).index

    return df.loc[indx, :]

def pvlib_location(loc):
    """
    Creates a pvlib location object.

    Parameters
    ----------
    loc : dict
        Dictionary of values required to instantiate a pvlib Location object.

        loc = {'latitude': float,
               'longitude': float,
               'altitude': float/int,
               'tz': str, int, float, or pytz.timezone, default 'UTC'}
        See
        http://en.wikipedia.org/wiki/List_of_tz_database_time_zones
        for a list of valid time zones.
        pytz.timezone objects will be converted to strings.
        ints and floats must be in hours from UTC.

    Returns
    -------
    pvlib location object.
    """
    return Location(**loc)

def pvlib_system(sys):
    """
    Creates a pvlib PVSystem or SingleAxisTracker object.

    A SingleAxisTracker object is created if any of the keyword arguments for
    initiating a SingleAxisTracker object are found in the keys of the passed
    dictionary.

    Parameters
    ----------
    sys : dict
        Dictionary of keywords required to create a pvlib SingleAxisTracker
        or PVSystem.

        Example dictionaries:

        fixed_sys = {'surface_tilt': 20,
                     'surface_azimuth': 180,
                     'albedo': 0.2}

        tracker_sys1 = {'axis_tilt': 0, 'axis_azimuth': 0,
                       'max_angle': 90, 'backtrack': True,
                       'gcr': 0.2, 'albedo': 0.2}

        Refer to pvlib documentation for details.
        https://pvlib-python.readthedocs.io/en/latest/generated/pvlib.pvsystem.PVSystem.html
        https://pvlib-python.readthedocs.io/en/latest/generated/pvlib.tracking.SingleAxisTracker.html

    Returns
    -------
    pvlib PVSystem or SingleAxisTracker object.
    """
    sandia_modules = retrieve_sam('SandiaMod')
    cec_inverters = retrieve_sam('cecinverter')
    sandia_module = sandia_modules['Canadian_Solar_CS5P_220M___2009_']
    cec_inverter = cec_inverters['ABB__MICRO_0_25_I_OUTD_US_208_208V__CEC_2014_']

    trck_kwords = ['axis_tilt', 'axis_azimuth', 'max_angle', 'backtrack', 'gcr']
    if any(kword in sys.keys() for kword in trck_kwords):
        system = SingleAxisTracker(**sys,
                                   module_parameters=sandia_module,
                                   inverter_parameters=cec_inverter)
    else:
        system = PVSystem(**sys,
                          module_parameters=sandia_module,
                          inverter_parameters=cec_inverter)

    return system


def get_tz_index(time_source, loc):
    """
    Creates DatetimeIndex with timezone aligned with location dictionary.

    Handles generating a DatetimeIndex with a timezone for use as an agrument
    to pvlib ModelChain prepare_inputs method or pvlib Location get_clearsky
    method.

    Parameters
    ----------
    time_source : dataframe or DatetimeIndex
        If passing a dataframe the index of the dataframe will be used.  If the
        index does not have a timezone the timezone will be set using the
        timezone in the passed loc dictionary.
        If passing a DatetimeIndex with a timezone it will be returned directly.
        If passing a DatetimeIndex without a timezone the timezone in the
        timezone dictionary will be used.

    Returns
    -------
    DatetimeIndex with timezone
    """

    if isinstance(time_source, pd.core.indexes.datetimes.DatetimeIndex):
        if time_source.tz is None:
            time_source = time_source.tz_localize(loc['tz'], ambiguous='infer',
                                                  errors='coerce')
            return time_source
        else:
            if pytz.timezone(loc['tz']) != time_source.tz:
                warnings.warn('Passed a DatetimeIndex with a timezone that '
                              'does not match the timezone in the loc dict. '
                              'Using the timezone of the DatetimeIndex.')
            return time_source
    elif isinstance(time_source, pd.core.frame.DataFrame):
        if time_source.index.tz is None:
            return time_source.index.tz_localize(loc['tz'], ambiguous='infer',
                                                 errors='coerce')
        else:
            if pytz.timezone(loc['tz']) != time_source.index.tz:
                warnings.warn('Passed a DataFrame with a timezone that '
                              'does not match the timezone in the loc dict. '
                              'Using the timezone of the DataFrame.')
            return time_source.index

def csky(time_source, loc=None, sys=None, concat=True, output='both'):
    """
    Calculate clear sky poa and ghi.

    Parameters
    ----------
    time_source : dataframe or DatetimeIndex
        If passing a dataframe the index of the dataframe will be used.  If the
        index does not have a timezone the timezone will be set using the
        timezone in the passed loc dictionary.
        If passing a DatetimeIndex with a timezone it will be returned directly.
        If passing a DatetimeIndex without a timezone the timezone in the
        timezone dictionary will be used.
    loc : dict
        Dictionary of values required to instantiate a pvlib Location object.

        loc = {'latitude': float,
               'longitude': float,
               'altitude': float/int,
               'tz': str, int, float, or pytz.timezone, default 'UTC'}
        See
        http://en.wikipedia.org/wiki/List_of_tz_database_time_zones
        for a list of valid time zones.
        pytz.timezone objects will be converted to strings.
        ints and floats must be in hours from UTC.
    sys : dict
        Dictionary of keywords required to create a pvlib SingleAxisTracker
        or PVSystem.

        Example dictionaries:

        fixed_sys = {'surface_tilt': 20,
                     'surface_azimuth': 180,
                     'albedo': 0.2}

        tracker_sys1 = {'axis_tilt': 0, 'axis_azimuth': 0,
                       'max_angle': 90, 'backtrack': True,
                       'gcr': 0.2, 'albedo': 0.2}

        Refer to pvlib documentation for details.
        https://pvlib-python.readthedocs.io/en/latest/generated/pvlib.pvsystem.PVSystem.html
        https://pvlib-python.readthedocs.io/en/latest/generated/pvlib.tracking.SingleAxisTracker.html
    concat : bool, default True
        If concat is True then returns columns as defined by return argument
        added to passed dataframe, otherwise returns just clear sky data.
    output : str, default 'both'
        both - returns only total poa and ghi
        poa_all - returns all components of poa
        ghi_all - returns all components of ghi
        all - returns all components of poa and ghi
    """
    location = pvlib_location(loc)
    system = pvlib_system(sys)
    mc = ModelChain(system, location)
    times = get_tz_index(time_source, loc)

    if output == 'both':
        ghi = location.get_clearsky(times=times)
        mc.prepare_inputs(times=times)
        csky_df = pd.DataFrame({'poa_mod_csky': mc.total_irrad['poa_global'],
                                'ghi_mod_csky': ghi['ghi']})
    if output == 'poa_all':
        mc.prepare_inputs(times=times)
        csky_df = mc.total_irrad
    if output == 'ghi_all':
        csky_df = location.get_clearsky(times=times)
    if output == 'all':
        ghi = location.get_clearsky(times=times)
        mc.prepare_inputs(times=times)
        csky_df = pd.concat([mc.total_irrad, ghi], axis=1)

    ix_no_tz = csky_df.index.tz_localize(None, ambiguous='infer',
                                         errors='coerce')
    csky_df.index = ix_no_tz

    if concat:
        if isinstance(time_source, pd.core.frame.DataFrame):
            df_with_csky = pd.concat([time_source, csky_df], axis=1)
            return df_with_csky
        else:
            warnings.warn('time_source is not a dataframe; only clear sky data\
                           returned')
            return csky_df
    else:
        return csky_df


class CapData(object):
    """
    Class to store capacity test data and translation of column names.

    CapData objects store a pandas dataframe of measured or simulated data
    and a translation dictionary used to translate and group the raw column
    names provided in the data.

    The translation dictionary allows maintaining the column names in the raw
    data while also grouping measurements of the same type from different
    sensors.

    Parameters
    ----------
    name : str
        Name for the CapData object.
    df : pandas dataframe
        Used to store measured or simulated data imported from csv.
    df_flt : pandas dataframe
        Holds filtered data.  Filtering methods act on and write to this
        attribute.
    trans : dictionary
        A dictionary with keys that are algorithimically determined based on
        the data of each imported column in the dataframe and values that are
        the column labels in the raw data.
    trans_keys : list
        Simply a list of the translation dictionary (trans) keys.
    reg_trans : dictionary
        Dictionary that is manually set to link abbreviations for
        for the independent variables of the ASTM Capacity test regression
        equation to the translation dictionary keys.
    trans_abrev : dictionary
        Enumerated translation dict keys mapped to original column names.
        Enumerated translation dict keys are used in plot hover tooltip.
    col_colors : dictionary
        Original column names mapped to a color for use in plot function.
    summary_ix : list of tuples
        Holds the row index data modified by the update_summary decorator
        function.
    summary : list of dicts
        Holds the data modifiedby the update_summary decorator function.
    rc : DataFrame
        Dataframe for the reporting conditions (poa, t_amb, and w_vel).
    ols_model : statsmodels linear regression model
        Holds the linear regression model object.
    reg_fml : str
        Regression formula to be fit to measured and simulated data.  Must
        follow the requirements of statsmodels use of patsy.
    """

    def __init__(self, name):
        super(CapData, self).__init__()
        self.name = name
        self.df = pd.DataFrame()
        self.df_flt = None
        self.trans = {}
        self.trans_keys = []
        self.reg_trans = {}
        self.trans_abrev = {}
        self.col_colors = {}
        self.summary_ix = []
        self.summary = []
        self.rc = None
        self.ols_model = None
        self.reg_fml = 'power ~ poa + I(poa * poa) + I(poa * t_amb) + I(poa * w_vel) - 1'

    def set_reg_trans(self, power='', poa='', t_amb='', w_vel=''):
        """
        Create a dictionary linking the regression variables to trans_keys.

        Links the independent regression variables to the appropriate
        translation keys.  Sets attribute and returns nothing.

        Parameters
        ----------
        power : str
            Translation key for the power variable.
        poa : str
            Translation key for the plane of array (poa) irradiance variable.
        t_amb : str
            Translation key for the ambient temperature variable.
        w_vel : str
            Translation key for the wind velocity key.
        """
        self.reg_trans = {'power': power,
                          'poa': poa,
                          't_amb': t_amb,
                          'w_vel': w_vel}

    def copy(self):
        """Creates and returns a copy of self."""
        cd_c = CapData()
        cd_c.df = self.df.copy()
        cd_c.trans = copy.copy(self.trans)
        cd_c.trans_keys = copy.copy(self.trans_keys)
        cd_c.reg_trans = copy.copy(self.reg_trans)
        cd_c.trans_abrev = copy.copy(self.trans_abrev)
        cd_c.col_colors = copy.copy(self.col_colors)
        return cd_c

    def empty(self):
        """Returns a boolean indicating if the CapData object contains data."""
        if self.df.empty and len(self.trans_keys) == 0 and len(self.trans) == 0:
            return True
        else:
            return False

    def load_das(self, path, filename, source=None, **kwargs):
        """
        Reads measured solar data from a csv file.

        Utilizes pandas read_csv to import measure solar data from a csv file.
        Attempts a few diferent encodings, trys to determine the header end
        by looking for a date in the first column, and concantenates column
        headings to a single string.

        Parameters
        ----------

        path : str
            Path to file to import.
        filename : str
            Name of file to import.
        **kwargs
            Use to pass additional kwargs to pandas read_csv.

        Returns
        -------
        pandas dataframe
        """

        data = os.path.normpath(path + filename)

        encodings = ['utf-8', 'latin1', 'iso-8859-1', 'cp1252']
        for encoding in encodings:
            try:
                all_data = pd.read_csv(data, encoding=encoding, index_col=0,
                                       parse_dates=True, skip_blank_lines=True,
                                       low_memory=False, **kwargs)
            except UnicodeDecodeError:
                continue
            else:
                break

        if not isinstance(all_data.index[0], pd.Timestamp):
            for i, indice in enumerate(all_data.index):
                try:
                    isinstance(dateutil.parser.parse(str(all_data.index[i])),
                               datetime.date)
                    header_end = i + 1
                    break
                except ValueError:
                    continue

            if source == 'AlsoEnergy':
                header = 'infer'
            else:
                header = list(np.arange(header_end))

            for encoding in encodings:
                try:
                    all_data = pd.read_csv(data, encoding=encoding,
                                           header=header, index_col=0,
                                           parse_dates=True, skip_blank_lines=True,
                                           low_memory=False, **kwargs)
                except UnicodeDecodeError:
                    continue
                else:
                    break

            if source == 'AlsoEnergy':
                row0 = all_data.iloc[0, :]
                row1 = all_data.iloc[1, :]
                row2 = all_data.iloc[2, :]

                row0_noparen = []
                for val in row0:
                    if type(val) is str:
                        row0_noparen.append(val.split('(')[0].strip())
                    else:
                        row0_noparen.append(val)

                row1_nocomm = []
                for val in row1:
                    if type(val) is str:
                        strings = val.split(',')
                        if len(strings) == 1:
                            row1_nocomm.append(val)
                        else:
                            row1_nocomm.append(strings[-1].strip())
                    else:
                        row1_nocomm.append(val)

                row2_noNan = []
                for val in row2:
                    if val is pd.np.nan:
                        row2_noNan.append('')
                    else:
                        row2_noNan.append(val)

                new_cols = []
                for one, two, three in zip(row0_noparen, row1_nocomm, row2_noNan):
                    new_cols.append(str(one) + ' ' + str(two) + ', ' + str(three))

                all_data.columns = new_cols

        all_data = all_data.apply(pd.to_numeric, errors='coerce')
        all_data.dropna(axis=1, how='all', inplace=True)
        all_data.dropna(how='all', inplace=True)

        if source is not 'AlsoEnergy':
            all_data.columns = [' '.join(col).strip() for col in all_data.columns.values]
        else:
            all_data.index = pd.to_datetime(all_data.index)

        return all_data

    def load_pvsyst(self, path, filename, **kwargs):
        """
        Load data from a PVsyst energy production model.

        Parameters
        ----------
        path : str
            Path to file to import.
        filename : str
            Name of file to import.
        **kwargs
            Use to pass additional kwargs to pandas read_csv.

        Returns
        -------
        pandas dataframe
        """
        dirName = os.path.normpath(path + filename)

        encodings = ['utf-8', 'latin1', 'iso-8859-1', 'cp1252']
        for encoding in encodings:
            try:
                # pvraw = pd.read_csv(dirName, skiprows=10, encoding=encoding,
                #                     header=[0, 1], parse_dates=[0],
                #                     infer_datetime_format=True, **kwargs)
                pvraw = pd.read_csv(dirName, skiprows=10, encoding=encoding,
                                    header=[0, 1], **kwargs)
            except UnicodeDecodeError:
                continue
            else:
                break

        pvraw.columns = pvraw.columns.droplevel(1)
        dates = pvraw.loc[:, 'date']
        try:
            dt_index = pd.to_datetime(dates, format='%m/%d/%y %H:%M')
        except ValueError:
            dt_index = pd.to_datetime(dates)
        pvraw.index = dt_index
        pvraw.drop('date', axis=1, inplace=True)
        pvraw = pvraw.rename(columns={"T Amb": "TAmb"})
        return pvraw

    def load_data(self, path='./data/', fname=None, set_trans=True,
                  trans_report=True, source=None, load_pvsyst=False,
                  clear_sky=False, loc=None, sys=None, **kwargs):
        """
        Import data from csv files.

        Parameters
        ----------
        path : str, default './data/'
            Path to directory containing csv files to load.
        fname: str, default None
            Filename of specific file to load. If filename is none method will
            load all csv files into one dataframe.
        set_trans : bool, default True
            Generates translation dicitionary for column names after loading
            data.
        trans_report : bool, default True
            If set_trans is true, then method prints summary of translation
            dictionary process including any possible data issues.  No effect
            on method when set to False.
        source : str, default None
            Default of None uses general approach that concatenates header data.
            Set to 'AlsoEnergy' to use column heading parsing specific to
            downloads from AlsoEnergy.
        load_pvsyst : bool, default False
            By default skips any csv file that has 'pvsyst' in the name.  Is
            not case sensitive.  Set to true to import a csv with 'pvsyst' in
            the name and skip all other files.
        clear_sky : bool, default False
            Set to true and provide loc and sys arguments to add columns of
            clear sky modeled poa and ghi to loaded data.
        loc : dict
            See the csky function for details on dictionary options.
        sys : dict
            See the csky function for details on dictionary options.
        **kwargs
            Will pass kwargs onto load_pvsyst or load_das, which will pass to
            Pandas.read_csv.  Useful to adjust the separator (Ex. sep=';').

        Returns
        -------
        None
        """
        if fname is None:
            files_to_read = []
            for file in os.listdir(path):
                if file.endswith('.csv'):
                    files_to_read.append(file)
                elif file.endswith('.CSV'):
                    files_to_read.append(file)

            all_sensors = pd.DataFrame()

            if not load_pvsyst:
                for filename in files_to_read:
                    if filename.lower().find('pvsyst') != -1:
                        print("Skipped file: " + filename)
                        continue
                    nextData = self.load_das(path, filename, source=source,
                                             **kwargs)
                    all_sensors = pd.concat([all_sensors, nextData], axis=0)
                    print("Read: " + filename)
            elif load_pvsyst:
                for filename in files_to_read:
                    if filename.lower().find('pvsyst') == -1:
                        print("Skipped file: " + filename)
                        continue
                    nextData = self.load_pvsyst(path, filename, **kwargs)
                    all_sensors = pd.concat([all_sensors, nextData], axis=0)
                    print("Read: " + filename)
        else:
            if not load_pvsyst:
                all_sensors = self.load_das(path, fname, source=source, **kwargs)
            elif load_pvsyst:
                all_sensors = self.load_pvsyst(path, fname, **kwargs)

        ix_ser = all_sensors.index.to_series()
        all_sensors['index'] = ix_ser.apply(lambda x: x.strftime('%m/%d/%Y %H %M'))
        self.df = all_sensors

        if not load_pvsyst:
            if clear_sky:
                if loc is None:
                    warnings.warn('Must provide loc and sys dictionary\
                                  when clear_sky is True.  Loc dict missing.')
                if sys is None:
                    warnings.warn('Must provide loc and sys dictionary\
                                  when clear_sky is True.  Sys dict missing.')
                self.df = csky(self.df, loc=loc, sys=sys, concat=True,
                               output='both')

        if set_trans:
            self.__set_trans(trans_report=trans_report)

        self.df_flt = self.df.copy()

    def __series_type(self, series, type_defs, bounds_check=True,
                      warnings=False):
        """
        Assigns columns to a category by analyzing the column names.

        The type_defs parameter is a dictionary which defines search strings
        and value limits for each key, where the key is a categorical name
        and the search strings are possible related names.  For example an
        irradiance sensor has the key 'irr' with search strings 'irradiance'
        'plane of array', 'poa', etc.

        Parameters
        ----------
        series : pandas series
            Pandas series, row or column of dataframe passed by pandas.df.apply.
        type_defs : dictionary
            Dictionary with the following structure.  See type_defs
            {'category abbreviation': [[category search strings],
                                       (min val, max val)]}
        bounds_check : bool, default True
            When true checks series values against min and max values in the
            type_defs dictionary.
        warnings : bool, default False
            When true prints warning that values in series are outside expected
            range and adds '-valuesError' to returned str.

        Returns
        -------
        string
            Returns a string representing the category for the series.
            Concatenates '-valuesError' if bounds_check and warnings are both
            True and values within the series are outside the expected range.
        """
        for key in type_defs.keys():
            # print('################')
            # print(key)
            for search_str in type_defs[key][0]:
                # print(search_str)
                if series.name.lower().find(search_str.lower()) == -1:
                    continue
                else:
                    if bounds_check:
                        type_min = type_defs[key][1][0]
                        type_max = type_defs[key][1][1]
                        ser_min = series.min()
                        ser_max = series.max()
                        min_bool = ser_min >= type_min
                        max_bool = ser_max <= type_max
                        if min_bool and max_bool:
                            return key
                        else:
                            if warnings:
                                if not min_bool and not max_bool:
                                    print('{} in {} is below {} for '
                                    '{}'.format(ser_min, series.name,
                                    type_min, key))
                                    print('{} in {} is above {} for '
                                    '{}'.format(ser_max, series.name,
                                    type_max, key))
                                elif not min_bool:
                                    print('{} in {} is below {} for '
                                    '{}'.format(ser_min, series.name,
                                    type_min, key))
                                elif not max_bool:
                                    print('{} in {} is above {} for '
                                    '{}'.format(ser_max, series.name,
                                    type_max, key))
                            return key
                    else:
                        return key
        return ''

    def set_plot_attributes(self):
        dframe = self.df

        for key in self.trans_keys:
            df = dframe[self.trans[key]]
            cols = df.columns.tolist()
            for i, col in enumerate(cols):
                abbrev_col_name = key + str(i)
                self.trans_abrev[abbrev_col_name] = col

                col_key0 = key.split('-')[0]
                col_key1 = key.split('-')[1]
                if col_key0 in ('irr', 'temp'):
                    col_key = col_key0 + '-' + col_key1
                else:
                    col_key = col_key0

                try:
                    j = i % 4
                    self.col_colors[col] = plot_colors_brewer[col_key][j]
                except KeyError:
                    j = i % 10
                    self.col_colors[col] = Category10[10][j]

    def __set_trans(self, trans_report=True):
        """
        Creates a dict of raw column names paired to categorical column names.

        Uses multiple type_def formatted dictionaries to determine the type,
        sub-type, and equipment type for data series of a dataframe.  The determined
        types are concatenated to a string used as a dictionary key with a list
        of one or more oringal column names as the paried value.

        Parameters
        ----------
        trans_report : bool, default True
            Sets the warnings option of __series_type when applied to determine
            the column types.

        Returns
        -------
        None
            Sets attributes self.trans and self.trans_keys

        Todo
        ----
        type_defs parameter
            Consider refactoring to have a list of type_def dictionaries as an
            input and loop over each dict in the list.
        """
        col_types = self.df.apply(self.__series_type, args=(type_defs,),
                                  warnings=trans_report).tolist()
        sub_types = self.df.apply(self.__series_type, args=(sub_type_defs,),
                                  bounds_check=False).tolist()
        irr_types = self.df.apply(self.__series_type, args=(irr_sensors_defs,),
                                  bounds_check=False).tolist()

        col_indices = []
        for typ, sub_typ, irr_typ in zip(col_types, sub_types, irr_types):
            col_indices.append('-'.join([typ, sub_typ, irr_typ]))

        names = []
        for new_name, old_name in zip(col_indices, self.df.columns.tolist()):
            names.append((new_name, old_name))
        names.sort()
        orig_names_sorted = [name_pair[1] for name_pair in names]

        trans = {}
        col_indices.sort()
        cols = list(set(col_indices))
        cols.sort()
        for name in set(cols):
            start = col_indices.index(name)
            count = col_indices.count(name)
            trans[name] = orig_names_sorted[start:start + count]

        self.trans = trans

        trans_keys = list(self.trans.keys())
        if 'index--' in trans_keys:
            trans_keys.remove('index--')
        trans_keys.sort()
        self.trans_keys = trans_keys

        self.set_plot_attributes()

    def drop_cols(self, columns):
        """
        Drops columns from CapData dataframe and translation dictionary.

        Parameters
        ----------
        Columns (list) List of columns to drop.

        Todo
        ----
        Change to accept a string column name or list of strings
        """
        for key, value in self.trans.items():
            for col in columns:
                try:
                    value.remove(col)
                    self.trans[key] = value
                except ValueError:
                    continue
        self.df.drop(columns, axis=1, inplace=True)

    def view(self, tkey):
        """
        Convience function returns columns using translation dictionary names.

        Parameters
        ----------
        tkey: int or str or list of int or strs
            String or list of strings from self.trans_keys or int postion or
            list of int postitions of value in self.trans_keys.
        """

        if isinstance(tkey, int):
            keys = self.trans[self.trans_keys[tkey]]
        elif isinstance(tkey, list) and len(tkey) > 1:
            keys = []
            for key in tkey:
                if isinstance(key, str):
                    keys.extend(self.trans[key])
                elif isinstance(key, int):
                    keys.extend(self.trans[self.trans_keys[key]])
        elif tkey in self.trans_keys:
            keys = self.trans[tkey]

        return self.df[keys]

    def rview(self, ind_var, filtered_data=False):
        """
        Convience fucntion to return regression independent variable.

        Parameters
        ----------
        ind_var: string or list of strings
            may be 'power', 'poa', 't_amb', 'w_vel', a list of some subset of
            the previous four strings or 'all'
        """

        if ind_var == 'all':
            keys = list(self.reg_trans.values())
        elif isinstance(ind_var, list) and len(ind_var) > 1:
            keys = [self.reg_trans[key] for key in ind_var]
        elif ind_var in met_keys:
            ind_var = [ind_var]
            keys = [self.reg_trans[key] for key in ind_var]

        lst = []
        for key in keys:
            lst.extend(self.trans[key])
        if filtered_data:
            return self.df_flt[lst]
        else:
            return self.df[lst]

    def __comb_trans_keys(self, grp):
        comb_keys = []

        for key in self.trans_keys:
            if key.find(grp) != -1:
                comb_keys.append(key)

        cols = []
        for key in comb_keys:
            cols.extend(self.trans[key])

        grp_comb = grp + '_comb'
        if grp_comb not in self.trans_keys:
            self.trans[grp_comb] = cols
            self.trans_keys.extend([grp_comb])
            print('Added new group: ' + grp_comb)

    def plot(self, marker='line', ncols=2, width=400, height=350,
             legends=False, merge_grps=['irr', 'temp'], subset=None,
             filtered=False, **kwargs):
        """
        Plots a Bokeh line graph for each group of sensors in self.trans.

        Function returns a Bokeh grid of figures.  A figure is generated for each
        key in the translation dictionary and a line is plotted for each raw
        column name paired with that key.

        For example, if there are multiple plane of array irradiance sensors,
        the data from each one will be plotted on a single figure.

        Figures are not generated for categories that would plot more than 10
        lines.

        Parameters
        ----------
        marker : str, default 'line'
            Accepts 'line', 'circle', 'line-circle'.  These are bokeh marker
            options.
        ncols : int, default 2
            Number of columns in the bokeh gridplot.
        width : int, default 400
            Width of individual plots in gridplot.
        height: int, default 350
            Height of individual plots in gridplot.
        legends : bool, default False
            Turn on or off legends for individual plots.
        merge_grps : list, default ['irr', 'temp']
            List of strings to search for in the translation dictionary keys.
            A new key and group is created in the translation dictionary for
            each group.  By default will combine all irradiance measurements
            into a group and temperature measurements into a group.
            Pass empty list to not merge any plots.
            Use 'irr-poa' and 'irr-ghi' to plot clear sky modeled with measured
            data.
        subset : list, default None
            List of the translation dictionary keys to use to control order of
            plots or to plot only a subset of the plots.
        filtered : bool, default False
            Set to true to plot the filtered data.
        kwargs
            Pass additional options to bokeh gridplot.  Merge_tools=False will
            shows the hover tool icon, so it can be turned off.

        Returns
        -------
        show(grid)
            Command to show grid of figures.  Intended for use in jupyter
            notebook.
        """
        for str_val in merge_grps:
            self.__comb_trans_keys(str_val)

        if filtered:
            dframe = self.df_flt
        else:
            dframe = self.df
        dframe.index.name = 'Timestamp'

        names_to_abrev = {val: key for key, val in self.trans_abrev.items()}

        plots = []
        x_axis = None

        source = ColumnDataSource(dframe)

        hover = HoverTool()
        hover.tooltips = [
            ("Name", "$name"),
            ("Datetime", "@Timestamp{%D %H:%M}"),
            ("Value", "$y"),
        ]
        hover.formatters = {"Timestamp": "datetime"}

        if isinstance(subset, list):
            plot_keys = subset
        else:
            plot_keys = self.trans_keys

        for j, key in enumerate(plot_keys):
            df = dframe[self.trans[key]]
            cols = df.columns.tolist()

            if x_axis is None:
                p = figure(title=key, plot_width=width, plot_height=height,
                           x_axis_type='datetime', tools='pan, xwheel_pan, xwheel_zoom, box_zoom, save, reset')
                p.tools.append(hover)
                x_axis = p.x_range
            if j > 0:
                p = figure(title=key, plot_width=width, plot_height=height,
                           x_axis_type='datetime', x_range=x_axis, tools='pan, xwheel_pan, xwheel_zoom, box_zoom, save, reset')
                p.tools.append(hover)
            legend_items = []
            for i, col in enumerate(cols):
                abbrev_col_name = key + str(i)
                if col.find('csky') == -1:
                    line_dash = 'solid'
                else:
                    line_dash = (5, 2)
                if marker == 'line':
                    series = p.line('Timestamp', col, source=source,
                                    line_color=self.col_colors[col],
                                    line_dash=line_dash,
                                    name=names_to_abrev[col])
                elif marker == 'circle':
                    series = p.circle('Timestamp', col,
                                      source=source,
                                      line_color=self.col_colors[col],
                                      size=2, fill_color="white",
                                      name=names_to_abrev[col])
                if marker == 'line-circle':
                    series = p.line('Timestamp', col, source=source,
                                    line_color=self.col_colors[col],
                                    name=names_to_abrev[col])
                    series = p.circle('Timestamp', col,
                                      source=source,
                                      line_color=self.col_colors[col],
                                      size=2, fill_color="white",
                                      name=names_to_abrev[col])
                legend_items.append((col, [series, ]))

            legend = Legend(items=legend_items, location=(40, -5))
            legend.label_text_font_size = '8pt'
            if legends:
                p.add_layout(legend, 'below')

            plots.append(p)

        grid = gridplot(plots, ncols=ncols, **kwargs)
        return show(grid)

    def reset_flt(self):
        """
        Copies over filtered dataframe with raw data and removes all summary
        history.

        Parameters
        ----------
        data : str
            'sim' or 'das' determines if filter is on sim or das data.
        """
        self.df_flt = self.df.copy()
        self.summary_ix = []
        self.summary = []

    @update_summary
    def filter_irr(self, low, high, ref_val=None, col_name=None, inplace=True):
        """
        Filter on irradiance values.

        Parameters
        ----------
        low : float or int
            Minimum value as fraction (0.8) or absolute 200 (W/m^2)
        high : float or int
            Max value as fraction (1.2) or absolute 800 (W/m^2)
        ref_val : float or int
            Must provide arg when min/max are fractions
        col_name : str, default None
            Column name of irradiance data to filter.  By default uses the POA
            irradiance set in reg_trans attribute or average of the POA columns.
        inplace : bool, default True
            Default true write back to df_flt or return filtered dataframe.

        Returns
        -------
        DataFrame
            Filtered dataframe if inplace is False.
        """
        if col_name is None:
            poa_cols = self.trans[self.reg_trans['poa']]
            if len(poa_cols) > 1:
                return warnings.warn('{} columns of irradiance data. '
                                     'Use col_name to specify a single '
                                     'column.'.format(len(poa_cols)))
            else:
                irr_col = poa_cols[0]
        else:
            irr_col = col_name

        df_flt = flt_irr(self.df_flt, irr_col, low, high,
                         ref_val=ref_val)
        if inplace:
            self.df_flt = df_flt
        else:
            return df_flt

    def get_summary(self):
        """
        Prints summary dataframe of the filtering applied df_flt attribute.

        The summary dataframe shows the history of the filtering steps applied
        to the data including the timestamps remaining after each step, the
        timestamps removed by each step and the arguments used to call each
        filtering method.

        Parameters
        ----------
        None

        Returns
        -------
        Pandas DataFrame
        """
        try:
            df = pd.DataFrame(data=self.summary,
                              index=pd.MultiIndex.from_tuples(self.summary_ix),
                              columns=columns)
            return df
        except TypeError:
            print('No filters have been run.')

    # @update_summary
    def rep_cond(self, *args,
                 func={'poa': perc_wrap(60), 't_amb': 'mean', 'w_vel': 'mean'},
                 freq=None, irr_bal=False, w_vel=None, inplace=True, **kwargs):

        """
        Calculate reporting conditons.

        NOTE: Can pass additional positional arguments for low/high irradiance
        filter.

        Parameters
        ----------
        func: callable, string, dictionary, or list of string/callables
            Determines how the reporting condition is calculated.
            Default is a dictionary poa - 60th numpy_percentile, t_amb - mean
                                          w_vel - mean
            Can pass a string function ('mean') to calculate each reporting
            condition the same way.
        freq: str
            String pandas offset alias to specify aggregattion frequency
            for reporting condition calculation. Ex '60D' for 60 Days or
            'M' for months. Typical 'M', '2M', or 'BQ-NOV'.
            'BQ-NOV' is business quarterly year ending in Novemnber i.e. seasons.
        irr_bal: boolean, default False
            If true, pred is set to True, and frequency is specified then the
            predictions for each group of reporting conditions use the
            irrRC_balanced function to determine the reporting conditions.
        w_vel: int
            If w_vel is not none, then wind reporting condition will be set to
            value specified for predictions. Does not affect output unless pred
            is True and irr_bal is True.
        inplace: bool, True by default
            When true updates object rc parameter, when false returns dicitionary
            of reporting conditions.

        Returns
        -------
        dict
            Returns a dictionary of reporting conditions if inplace=False
            otherwise returns None.
        pandas DataFrame
            If pred=True, then returns a pandas dataframe of results.
        """
        df = self.rview(['poa', 't_amb', 'w_vel'],
                        filtered_data=True)
        df = df.rename(columns={df.columns[0]: 'poa',
                                df.columns[1]: 't_amb',
                                df.columns[2]: 'w_vel'})

        RCs_df = pd.DataFrame(df.agg(func)).T

        if irr_bal:
            results = irrRC_balanced(mnth, *args, irr_col='poa',
                                     **kwargs)
            flt_df = results[1]
            flt_dfs = flt_dfs.append(results[1])
            temp_RC = flt_df['t_amb'].mean()
            wind_RC = flt_df['w_vel'].mean()
            if w_vel is not None:
                wind_RC = w_vel
            RCs_df = RCs_df.append({'poa': results[0],
                                    't_amb': temp_RC,
                                    'w_vel': wind_RC}, ignore_index=True)

        if w_vel is not None:
            RCs_df['w_vel'][0] = w_vel

        if freq is not None:
            check_freqs = ['BQ-JAN', 'BQ-FEB', 'BQ-APR', 'BQ-MAY', 'BQ-JUL',
                           'BQ-AUG', 'BQ-OCT', 'BQ-NOV']
            mnth_int = {'JAN': 1, 'FEB': 2, 'APR': 4, 'MAY': 5, 'JUL': 7,
                        'AUG': 8, 'OCT': 10, 'NOV': 11}

            if freq in check_freqs:
                mnth = mnth_int[freq.split('-')[1]]
                year = df.index[0].year
                mnths_eoy = 12 - mnth
                mnths_boy = 3 - mnths_eoy
                if int(mnth) >= 10:
                    str_date = str(mnths_boy) + '/' + str(year)
                else:
                    str_date = str(mnth) + '/' + str(year)
                tdelta = df.index[1] - df.index[0]
                date_to_offset = df.loc[str_date].index[-1].to_pydatetime()
                start = date_to_offset + tdelta
                end = date_to_offset + pd.DateOffset(years=1)
                if mnth < 8 or mnth >= 10:
                    df = cntg_eoy(df, start, end)
                else:
                    df = cntg_eoy(df, end, start)

            df_grpd = df.groupby(pd.Grouper(freq=freq, label='left'))
            RCs_df = df_grpd.agg(func)
            # RCs = RCs_df.to_dict('list')

            if irr_bal:
                RCs_df = pd.DataFrame()
                flt_dfs = pd.DataFrame()
                for name, mnth in df_grpd:
                    results = irrRC_balanced(mnth, *args, irr_col='poa',
                                             **kwargs)
                    flt_df = results[1]
                    flt_dfs = flt_dfs.append(results[1])
                    temp_RC = flt_df['t_amb'].mean()
                    wind_RC = flt_df['w_vel'].mean()
                    RCs_df = RCs_df.append({'poa': results[0],
                                            't_amb': temp_RC,
                                            'w_vel': wind_RC}, ignore_index=True)
                # df_grpd = flt_dfs.groupby(by=pd.Grouper(freq='M'))
            if w_vel is not None:
                RCs_df['w_vel'] = w_vel

        if inplace:
            print('Reporting conditions saved to rc attribute.')
            print(RCs_df)
            self.rc = RCs_df
        else:
            return RCs_df

    # def pred_rcs(self):
    #     """
    #     Calculate expected capacities.
    #
    #     pred: boolean, default False
    #         If true and frequency is specified, then method returns a dataframe
    #         with reporting conditions, regression parameters, predicted
    #         capacites, and point quantities for each group.
    #     """
    #             if predict:
    #                 if irr_bal:
    #                     RCs_df = pd.DataFrame()
    #                     flt_dfs = pd.DataFrame()
    #                     for name, mnth in df_grpd:
    #                         results = irrRC_balanced(mnth, *args, irr_col='poa',
    #                                                  **kwargs)
    #                         flt_df = results[1]
    #                         flt_dfs = flt_dfs.append(results[1])
    #                         temp_RC = flt_df['t_amb'].mean()
    #                         wind_RC = flt_df['w_vel'].mean()
    #                         if w_vel is not None:
    #                             wind_RC = w_vel
    #                         RCs_df = RCs_df.append({'poa': results[0],
    #                                                 't_amb': temp_RC,
    #                                                 'w_vel': wind_RC}, ignore_index=True)
    #                     df_grpd = flt_dfs.groupby(by=pd.Grouper(freq='M'))
    #
    #                 error = float(self.tolerance.split(sep=' ')[1]) / 100
    #                 results = pred_summary(df_grpd, RCs_df, error,
    #                                        fml=self.reg_fml)
    #
    #         if inplace:
    #             if pred:
    #                 print('Results dataframe saved to rc attribute.')
    #                 print(results)
    #                 self.rc = results
    #             else:
    #                 print('Reporting conditions saved to rc attribute.')
    #                 print(RCs)
    #                 self.rc = RCs
    #         else:
    #             if pred:
    #                 return results
    #             else:
    #                 return RCs
