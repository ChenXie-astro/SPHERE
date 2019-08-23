import os
import glob
import pandas as pd
import subprocess
import numpy as np
import scipy.ndimage as ndimage
import scipy.interpolate as interp
import scipy.optimize as optim
import shutil
import matplotlib
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import matplotlib.colors as colors
import configparser

from astropy.io import fits
from astropy.modeling import models, fitting
from matplotlib.backends.backend_pdf import PdfPages

import vltpf
import vltpf.utils.imutils as imutils
import vltpf.utils.aperture as aperture
import vltpf.transmission as transmission
import vltpf.ReductionPath as ReductionPath
import vltpf.toolbox as toolbox


def get_wavelength_calibration(wave_calib, centers, wave_min, wave_max):
    '''
    Return the linear wavelength calibration for each IRDIS field

    Parameters
    ----------
    wave_calib : array
        Wavelength calibration data computed by esorex recipe

    centers : tuple
        Center of each field

    wave_min : float
        Minimal usable wavelength

    wave_max : float
        Maximal usable wavelength

    Returns
    -------
    wave_lin : array
        Array with the linear calibration for each field, as a function 
        of pixel coordinate
    '''
    wave_map = np.zeros((2, 1024, 1024))
    wave_map[0] = wave_calib[:, 0:1024]
    wave_map[1] = wave_calib[:, 1024:]
    wave_map[(wave_map < wave_min) | (wave_max < wave_map)] = np.nan
    
    wave_ext = 10
    wave_lin = np.zeros((2, 1024))
    
    wave_lin[0] = np.mean(wave_map[0, :, centers[0, 0]-wave_ext:centers[0, 0]+wave_ext], axis=1)
    wave_lin[1] = np.mean(wave_map[1, :, centers[1, 0]-wave_ext:centers[1, 0]+wave_ext], axis=1)
    
    return wave_lin


class SpectroReduction(object):
    '''
    SPHERE/IRDIS long-slit spectroscopy reduction class. It handles
    both the low and medium resolution modes (LRS, MRS)
    '''

    ##################################################
    # Class variables
    ##################################################

    # specify for each recipe which other recipes need to have been executed before
    recipe_requirements = {
        'sort_files': [],
        'sort_frames': ['sort_files'],
        'check_files_association': ['sort_files'],
        'sph_ird_cal_dark': ['sort_files'],
        'sph_ird_cal_detector_flat': ['sort_files'],
        'sph_ird_wave_calib': ['sort_files', 'sph_ird_cal_detector_flat'],
        'sph_ird_preprocess_science': ['sort_files', 'sort_frames', 'sph_ird_cal_dark', 
                                       'sph_ird_cal_detector_flat'],
        'sph_ird_star_center': ['sort_files', 'sort_frames', 'sph_ird_wave_calib'],
        'sph_ird_wavelength_recalibration': ['sort_files', 'sort_frames', 'sph_ird_wave_calib',
                                             'sph_ird_star_center'],
        'sph_ird_combine_data': ['sort_files', 'sort_frames', 'sph_ird_preprocess_science',
                                 'sph_ird_star_center', 'sph_ird_wavelength_recalibration']
    }
    
    ##################################################
    # Constructor
    ##################################################
    
    def __init__(self, path):
        '''Initialization of the SpectroReduction instances

        Parameters
        ----------
        path : str
            Path to the directory containing the raw data

        '''

        # expand path
        path = os.path.expanduser(os.path.join(path, ''))
        
        # zeroth-order reduction validation
        raw = os.path.join(path, 'raw')
        if not os.path.exists(raw):
            raise ValueError('No raw/ subdirectory. {0} is not a valid reduction path!'.format(path))
        
        # init path and name
        self._path = ReductionPath.Path(path)
        self._instrument = 'IRDIS'
        
        # configuration
        package_directory = os.path.dirname(os.path.abspath(vltpf.__file__))
        configfile = os.path.join(package_directory, 'instruments', self._instrument+'.ini')
        config = configparser.ConfigParser()
        try:
            config.read(configfile)

            # instrument
            self._pixel = float(config.get('instrument', 'pixel'))
            self._nwave = int(config.get('instrument', 'nwave'))
            self._wave_cal_lasers = [float(w) for w in config.get('calibration', 'wave_cal_lasers').split(',')]

            # reduction
            self._config = dict(config.items('reduction-spectro'))
            for key, value in self._config.items():
                try:
                    val = eval(value)
                except NameError:
                    val = value                    
                self._config[key] = val
        except configparser.Error as e:
            raise ValueError('Error reading configuration file for instrument {0}: {1}'.format(self._instrument, e.message))
        
        # execution of recipes
        self._recipe_execution = {
            'sort_files': False,
            'sort_frames': False,
            'check_files_association': False,
            'sph_ifs_cal_dark': False,
            'sph_ifs_cal_detector_flat': False,
            'sph_ird_wave_calib': False
        }
        
        # reload any existing data frames
        self.read_info()
    
    ##################################################
    # Representation
    ##################################################
    
    def __repr__(self):
        return '<SpectroReduction, instrument={0}, path={1}>'.format(self._instrument, self._path)
    
    def __format__(self):
        return self.__repr__()
    
    ##################################################
    # Properties
    ##################################################
    
    @property
    def instrument(self):
        return self._instrument

    @property
    def pixel(self):
        return self._pixel
    
    @property
    def nwave(self):
        return self._nwave
    
    @property
    def path(self):
        return self._path

    @property
    def files_info(self):
        return self._files_info
    
    @property
    def frames_info(self):
        return self._frames_info
    
    @property
    def frames_info_preproc(self):
        return self._frames_info_preproc

    @property
    def recipe_execution(self):
        return self._recipe_execution
    
    @property
    def config(self):
        return self._config    

    ##################################################
    # Generic class methods
    ##################################################

    def show_config(self):
        '''
        Shows the reduction configuration
        '''

        # dictionary
        dico = self._config

        # silent parameter
        print('{0:<30s}{1}'.format('Parameter', 'Value'))
        print('-'*35)
        key = 'silent'
        print('{0:<30s}{1}'.format(key, dico[key]))

        # pre-processing
        print('-'*35)
        keys = [key for key in dico if key.startswith('preproc')]
        for key in keys:
            print('{0:<30s}{1}'.format(key, dico[key]))

        # centring
        print('-'*35)
        keys = [key for key in dico if key.startswith('center')]
        for key in keys:
            print('{0:<30s}{1}'.format(key, dico[key]))

        # wave
        print('-'*35)
        keys = [key for key in dico if key.startswith('wave')]
        for key in keys:
            print('{0:<30s}{1}'.format(key, dico[key]))
            
        # combining
        print('-'*35)
        keys = [key for key in dico if key.startswith('combine')]
        for key in keys:
            print('{0:<30s}{1}'.format(key, dico[key]))

        # clean
        print('-'*35)
        keys = [key for key in dico if key.startswith('clean')]
        for key in keys:
            print('{0:<30s}{1}'.format(key, dico[key]))
        print('-'*35)
            
        print()
        
           
    def init_reduction(self):
        '''
        Sort files and frames, perform sanity check
        '''

        # make sure we have sub-directories
        self._path.create_subdirectories()
                
        self.sort_files()
        self.sort_frames()
        self.check_files_association()
    
    
    def create_static_calibrations(self):
        '''
        Create static calibrations with esorex
        '''

        config = self._config
        
        self.sph_ird_cal_dark(silent=config['silent'])
        self.sph_ird_cal_detector_flat(silent=config['silent'])
        self.sph_ird_wave_calib(silent=config['silent'])

    
    def preprocess_science(self):
        '''
        Clean and collapse images
        '''
        
        config = self._config
        
        self.sph_ird_preprocess_science(subtract_background=config['preproc_subtract_background'],
                                        fix_badpix=config['preproc_fix_badpix'],
                                        collapse_science=config['preproc_collapse_science'],
                                        collapse_psf=config['preproc_collapse_psf'],
                                        collapse_center=config['preproc_collapse_center'])
    

    def process_science(self):
        '''
        Perform star center, combine cubes into final (x,y,time,lambda)
        cubes, correct anamorphism and scale the images
        '''
        
        config = self._config
        
        self.sph_ird_star_center(high_pass=config['center_high_pass'],
                                 display=config['center_display'],
                                 save=config['center_save'])
        self.sph_ird_wavelength_recalibration(fit_scaling=config['wave_fit_scaling'])
        self.sph_ird_combine_data(cpix=config['combine_cpix'],
                                  psf_dim=config['combine_psf_dim'],
                                  science_dim=config['combine_science_dim'],
                                  correct_mrs_chromatism=config['combine_correct_mrs_chromatism'],
                                  split_posang=config['combine_split_posang'],
                                  shift_method=config['combine_shift_method'],
                                  manual_center=config['combine_manual_center'],
                                  skip_center=config['combine_skip_center'])
    
    def clean(self):
        '''
        Clean the reduction directory, leaving only the raw and products
        sub-directory
        '''
        
        config = self._config

        if config['clean']:
            self.sph_ird_clean(delete_raw=config['clean_delete_raw'],
                               delete_products=config['clean_delete_products'])
    
        
    def full_reduction(self):
        '''
        Performs a full reduction of a data set, from the static
        calibrations to the final (x,y,time,lambda) cubes
        '''
        
        self.init_reduction()
        self.create_static_calibrations()
        self.preprocess_science()
        self.process_science()
        self.clean()

    ##################################################
    # SPHERE/IRDIS methods
    ##################################################
    
    def read_info(self):
        '''
        Read the files, calibs and frames information from disk

        files_info : dataframe
            The data frame with all the information on files

        frames_info : dataframe
            The data frame with all the information on science frames

        frames_info_preproc : dataframe
            The data frame with all the information on science frames after pre-processing
        '''

        # path
        path = self._path
        
        # files info
        fname = os.path.join(path.preproc, 'files.csv')
        if os.path.exists(fname):
            files_info = pd.read_csv(fname, index_col=0)

            # convert times
            files_info['DATE-OBS'] = pd.to_datetime(files_info['DATE-OBS'], utc=False)
            files_info['DATE'] = pd.to_datetime(files_info['DATE'], utc=False)
            files_info['DET FRAM UTC'] = pd.to_datetime(files_info['DET FRAM UTC'], utc=False)
            
            # update recipe execution
            self._recipe_execution['sort_files'] = True
            if np.any(files_info['PRO CATG'] == 'IRD_MASTER_DARK'):
                self._recipe_execution['sph_ird_cal_dark'] = True
            if np.any(files_info['PRO CATG'] == 'IRD_FLAT_FIELD'):
                self._recipe_execution['sph_ird_cal_detector_flat'] = True
            if np.any(files_info['PRO CATG'] == 'IRD_WAVECALIB'):
                self._recipe_execution['sph_ird_wave_calib'] = True
        else:
            files_info = None

        fname = os.path.join(path.preproc, 'frames.csv')
        if os.path.exists(fname):
            frames_info = pd.read_csv(fname, index_col=(0, 1))

            # convert times
            frames_info['DATE-OBS'] = pd.to_datetime(frames_info['DATE-OBS'], utc=False)
            frames_info['DATE'] = pd.to_datetime(frames_info['DATE'], utc=False)
            frames_info['DET FRAM UTC'] = pd.to_datetime(frames_info['DET FRAM UTC'], utc=False)
            frames_info['TIME START'] = pd.to_datetime(frames_info['TIME START'], utc=False)
            frames_info['TIME'] = pd.to_datetime(frames_info['TIME'], utc=False)
            frames_info['TIME END'] = pd.to_datetime(frames_info['TIME END'], utc=False)

            # update recipe execution
            self._recipe_execution['sort_frames'] = True
        else:
            frames_info = None

        fname = os.path.join(path.preproc, 'frames_preproc.csv')
        if os.path.exists(fname):
            frames_info_preproc = pd.read_csv(fname, index_col=(0, 1))

            # convert times
            frames_info_preproc['DATE-OBS'] = pd.to_datetime(frames_info_preproc['DATE-OBS'], utc=False)
            frames_info_preproc['DATE'] = pd.to_datetime(frames_info_preproc['DATE'], utc=False)
            frames_info_preproc['DET FRAM UTC'] = pd.to_datetime(frames_info_preproc['DET FRAM UTC'], utc=False)
            frames_info_preproc['TIME START'] = pd.to_datetime(frames_info_preproc['TIME START'], utc=False)
            frames_info_preproc['TIME'] = pd.to_datetime(frames_info_preproc['TIME'], utc=False)
            frames_info_preproc['TIME END'] = pd.to_datetime(frames_info_preproc['TIME END'], utc=False)            
        else:
            frames_info_preproc = None

        # save data frames in instance variables
        self._files_info = files_info
        self._frames_info = frames_info
        self._frames_info_preproc = frames_info_preproc

        # additional checks to update recipe execution
        if frames_info_preproc is not None:
            self._recipe_execution['sph_ird_wavelength_recalibration'] \
                = os.path.exists(os.path.join(path.preproc, 'wavelength_final.fits'))
            
            done = True
            files = frames_info_preproc.index
            for file, idx in files:
                fname = '{0}_DIT{1:03d}_preproc'.format(file, idx)
                file = glob.glob(os.path.join(path.preproc, fname+'.fits'))
                done = done and (len(file) == 1)
            self._recipe_execution['sph_ird_preprocess_science'] = done

            done = True
            files = frames_info_preproc[(frames_info_preproc['DPR TYPE'] == 'OBJECT,FLUX') |
                                        (frames_info_preproc['DPR TYPE'] == 'OBJECT,CENTER')].index
            for file, idx in files:
                fname = '{0}_DIT{1:03d}_preproc_centers'.format(file, idx)
                file = glob.glob(os.path.join(path.preproc, fname+'.fits'))
                done = done and (len(file) == 1)
            self._recipe_execution['sph_ird_star_center'] = done

        
    def sort_files(self):
        '''
        Sort all raw files and save result in a data frame

        files_info : dataframe
            Data frame with the information on raw files
        '''

        print('Sorting raw files')

        # parameters
        path = self._path
        
        # list files
        files = glob.glob(os.path.join(path.raw, '*.fits'))
        files = [os.path.splitext(os.path.basename(f))[0] for f in files]

        if len(files) == 0:
            raise ValueError('No raw FITS files in reduction path')

        print(' * found {0} FITS files in {1}'.format(len(files), path.raw))

        # read list of keywords
        package_directory = os.path.dirname(os.path.abspath(vltpf.__file__))
        keywords = []
        file = open(os.path.join(package_directory, 'instruments', 'keywords.dat'), 'r')
        for line in file:
            line = line.strip()
            if line:
                if line[0] != '#':
                    keywords.append(line)
        file.close()

        # short keywords
        keywords_short = keywords.copy()
        for idx in range(len(keywords_short)):
            key = keywords_short[idx]
            if key.find('HIERARCH ESO ') != -1:
                keywords_short[idx] = key[13:]
        
        # files table
        files_info = pd.DataFrame(index=pd.Index(files, name='FILE'), columns=keywords_short, dtype='float')

        for f in files:
            hdu = fits.open(os.path.join(path.raw, f+'.fits'))
            hdr = hdu[0].header

            for k, sk in zip(keywords, keywords_short):
                files_info.loc[f, sk] = hdr.get(k)

            hdu.close()

        # drop files that are not handled, based on DPR keywords
        files_info.dropna(subset=['DPR TYPE'], inplace=True)
        files_info = files_info[(files_info['DPR CATG'] != 'ACQUISITION') & (files_info['DPR TYPE'] != 'OBJECT,AO')]
        
        # check instruments
        instru = files_info['SEQ ARM'].unique()
        if len(instru) != 1:
            raise ValueError('Sequence is mixing different instruments: {0}'.format(instru))
        
        # processed column
        files_info.insert(len(files_info.columns), 'PROCESSED', False)
        files_info.insert(len(files_info.columns), 'PRO CATG', ' ')

        # convert times
        files_info['DATE-OBS'] = pd.to_datetime(files_info['DATE-OBS'], utc=False)
        files_info['DATE'] = pd.to_datetime(files_info['DATE'], utc=False)
        files_info['DET FRAM UTC'] = pd.to_datetime(files_info['DET FRAM UTC'], utc=False)

        # sort by acquisition time
        files_info.sort_values(by='DATE-OBS', inplace=True)
        
        # save files_info
        files_info.to_csv(os.path.join(path.preproc, 'files.csv'))    
        self._files_info = files_info

        # update recipe execution
        self._recipe_execution['sort_files'] = True

        
    def sort_frames(self):
        '''
        Extract the frames information from the science files and save
        result in a data frame

        calibs : dataframe
            A data frame with the information on all frames
        '''

        print('Extracting frames information')

        # check if recipe can be executed
        toolbox.check_recipe_execution(self._recipe_execution, 'sort_frames', self.recipe_requirements)
        
        # parameters
        path = self._path
        files_info = self._files_info
        
        # science files
        sci_files = files_info[(files_info['DPR CATG'] == 'SCIENCE') & (files_info['DPR TYPE'] != 'SKY')]    

        # raise error when no science frames are present
        if len(sci_files) == 0:
            raise ValueError('This dataset contains no science frame. There should be at least one!')
        
        # build indices
        files = []
        img   = []
        for file, finfo in sci_files.iterrows():
            NDIT = int(finfo['DET NDIT'])

            files.extend(np.repeat(file, NDIT))
            img.extend(list(np.arange(NDIT)))

        # create new dataframe
        frames_info = pd.DataFrame(columns=sci_files.columns, index=pd.MultiIndex.from_arrays([files, img], names=['FILE', 'IMG']))

        # expand files_info into frames_info
        frames_info = frames_info.align(files_info, level=0)[1]    

        # compute timestamps
        toolbox.compute_times(frames_info)

        # compute angles (ra, dec, parang)
        toolbox.compute_angles(frames_info)

        # save
        frames_info.to_csv(os.path.join(path.preproc, 'frames.csv'))
        self._frames_info = frames_info

        # update recipe execution
        self._recipe_execution['sort_frames'] = True
        
        #
        # print some info
        #
        cinfo = frames_info[frames_info['DPR TYPE'] == 'OBJECT']
        if len(cinfo) == 0:
            cinfo = frames_info[frames_info['DPR TYPE'] == 'OBJECT,CENTER']
        
        ra_drot   = cinfo['INS4 DROT2 RA'][0]
        ra_drot_h = np.floor(ra_drot/1e4)
        ra_drot_m = np.floor((ra_drot - ra_drot_h*1e4)/1e2)
        ra_drot_s = ra_drot - ra_drot_h*1e4 - ra_drot_m*1e2
        RA = '{:02.0f}:{:02.0f}:{:02.3f}'.format(ra_drot_h, ra_drot_m, ra_drot_s)
        
        dec_drot  = cinfo['INS4 DROT2 DEC'][0]
        sign = np.sign(dec_drot)
        udec_drot  = np.abs(dec_drot)
        dec_drot_d = np.floor(udec_drot/1e4)
        dec_drot_m = np.floor((udec_drot - dec_drot_d*1e4)/1e2)
        dec_drot_s = udec_drot - dec_drot_d*1e4 - dec_drot_m*1e2
        dec_drot_d *= sign
        DEC = '{:02.0f}:{:02.0f}:{:02.2f}'.format(dec_drot_d, dec_drot_m, dec_drot_s)

        pa_start = cinfo['PARANG'][0]
        pa_end   = cinfo['PARANG'][-1]

        posang   = cinfo['INS4 DROT2 POSANG'].unique()
        
        date = str(cinfo['DATE'][0])[0:10]
        
        print(' * Object:      {0}'.format(cinfo['OBJECT'][0]))
        print(' * RA / DEC:    {0} / {1}'.format(RA, DEC))
        print(' * Date:        {0}'.format(date))
        print(' * Instrument:  {0}'.format(cinfo['SEQ ARM'][0]))
        print(' * Derotator:   {0}'.format(cinfo['INS4 DROT2 MODE'][0]))
        print(' * Coronagraph: {0}'.format(cinfo['INS COMB ICOR'][0]))
        print(' * Mode:        {0}'.format(cinfo['INS1 MODE'][0]))
        print(' * Filter:      {0}'.format(cinfo['INS COMB IFLT'][0]))
        print(' * DIT:         {0:.2f} sec'.format(cinfo['DET SEQ1 DIT'][0]))
        print(' * NDIT:        {0:.0f}'.format(cinfo['DET NDIT'][0]))
        print(' * Texp:        {0:.2f} min'.format(cinfo['DET SEQ1 DIT'].sum()/60))
        print(' * PA:          {0:.2f}° ==> {1:.2f}° = {2:.2f}°'.format(pa_start, pa_end, np.abs(pa_end-pa_start)))
        print(' * POSANG:      {0}'.format(', '.join(['{:.2f}°'.format(p) for p in posang])))

        
    def check_files_association(self):
        '''
        Performs the calibration files association as a sanity check.

        Warnings and errors are reported at the end. Execution is
        interupted in case of error.
        '''

        # check if recipe can be executed
        toolbox.check_recipe_execution(self._recipe_execution, 'check_files_association', self.recipe_requirements)

        print('Performing file association for calibrations')

        # parameters
        path = self._path
        files_info = self._files_info
        
        # instrument arm
        arm = files_info['SEQ ARM'].unique()
        if len(arm) != 1:
            raise ValueError('Sequence is mixing different instruments: {0}'.format(arm))
        
        # IRDIS obs mode and filter combination
        modes = files_info.loc[files_info['DPR CATG'] == 'SCIENCE', 'INS1 MODE'].unique()
        if len(modes) != 1:
            raise ValueError('Sequence is mixing different types of observations: {0}'.format(modes))
        
        filter_combs = files_info.loc[files_info['DPR CATG'] == 'SCIENCE', 'INS COMB IFLT'].unique()
        if len(filter_combs) != 1:
            raise ValueError('Sequence is mixing different types of filters combinations: {0}'.format(filter_combs))
        filter_comb = filter_combs[0]
        if (filter_comb != 'S_LR') and (filter_comb != 'S_MR'):
            raise ValueError('Unknown IRDIS-LSS filter combination/mode {0}'.format(filter_comb))

        # specific data frame for calibrations
        # keep static calibrations and sky backgrounds
        calibs = files_info[(files_info['DPR CATG'] == 'CALIB') |
                            ((files_info['DPR CATG'] == 'SCIENCE') & (files_info['DPR TYPE'] == 'SKY'))]
        
        ###############################################
        # static calibrations not dependent on DIT
        ###############################################
        error_flag = 0
        warning_flag = 0

        # flat
        cfiles = calibs[(calibs['DPR TYPE'] == 'FLAT,LAMP') & (calibs['INS COMB IFLT'] == filter_comb)]
        if len(cfiles) <= 1:
            error_flag += 1
            print(' * Error: there should be more than 1 flat in filter combination {0}'.format(filter_comb))
        
        # wave
        cfiles = calibs[(calibs['DPR TYPE'] == 'LAMP,WAVE') & (calibs['INS COMB IFLT'] == filter_comb)]
        if len(cfiles) == 0:
            error_flag += 1
            print(' * Error: there should be 1 wavelength calibration file, found none.')
        elif len(cfiles) > 1:
            warning_flag += 1
            print(' * Warning: there should be 1 wavelength calibration file, found {0}. Using the closest from science.'.format(len(cfiles)))

            # find the two closest to science files
            sci_files = files_info[(files_info['DPR CATG'] == 'SCIENCE')]
            time_sci   = sci_files['DATE-OBS'].min()
            time_flat  = cfiles['DATE-OBS']            
            time_delta = np.abs(time_sci - time_flat).argsort()

            # drop the others
            files_info.drop(time_delta[1:].index, inplace=True)
        
        ##################################################
        # static calibrations that depend on science DIT
        ##################################################
        
        obj = files_info.loc[files_info['DPR CATG'] == 'SCIENCE', 'DPR TYPE'].apply(lambda s: s[0:6])
        DITs = files_info.loc[(files_info['DPR CATG'] == 'SCIENCE') & (obj == 'OBJECT'), 'DET SEQ1 DIT'].unique().round(2)
        
        # handle darks in a slightly different way because there might be several different DITs
        for DIT in DITs:
            # instrumental backgrounds
            cfiles = calibs[((calibs['DPR TYPE'] == 'DARK') | (calibs['DPR TYPE'] == 'DARK,BACKGROUND')) &
                            (calibs['DET SEQ1 DIT'].round(2) == DIT)]
            if len(cfiles) == 0:
                warning_flag += 1
                print(' * Warning: there is no dark/background for science files with DIT={0} sec. '.format(DIT) +
                      'It is *highly recommended* to include one to obtain the best data reduction. ' +
                      'A single dark/background file is sufficient, and it can easily be downloaded ' +
                      'from the ESO archive')

            # sky backgrounds
            cfiles = files_info[(files_info['DPR TYPE'] == 'SKY') & (files_info['DET SEQ1 DIT'].round(2) == DIT)]
            if len(cfiles) == 0:
                warning_flag += 1
                print(' * Warning: there is no sky background for science files with DIT={0} sec. '.format(DIT) +
                      'Using a sky background instead of an internal instrumental background can ' +
                      'usually provide a cleaner data reduction')

        # error reporting
        print('There are {0} warning(s) and {1} error(s) in the classification of files'.format(warning_flag, error_flag))
        if error_flag:
            raise ValueError('There is {0} errors that should be solved before proceeding'.format(error_flag))

        # save
        files_info.to_csv(os.path.join(path.preproc, 'files.csv'))
        self._files_info = files_info

    
    def sph_ird_cal_dark(self, silent=True):
        '''
        Create the dark and background calibrations

        Parameters
        ----------
        silent : bool
            Suppress esorex output. Default is True
        '''

        # check if recipe can be executed
        toolbox.check_recipe_execution(self._recipe_execution, 'sph_ird_cal_dark', self.recipe_requirements)
        
        print('Creating darks and backgrounds')

        # parameters
        path = self._path
        files_info = self._files_info
        
        # get list of files
        calibs = files_info[np.logical_not(files_info['PROCESSED']) &
                            ((files_info['DPR TYPE'] == 'DARK') |
                             (files_info['DPR TYPE'] == 'DARK,BACKGROUND') |
                             (files_info['DPR TYPE'] == 'SKY'))]

        # loops on type and DIT value
        types = ['DARK', 'DARK,BACKGROUND', 'SKY']
        DITs = calibs['DET SEQ1 DIT'].unique().round(2)
        filter_combs = calibs['INS COMB IFLT'].unique()

        for ctype in types:
            for DIT in DITs:
                for cfilt in filter_combs:
                    cfiles = calibs[(calibs['DPR TYPE'] == ctype) & (calibs['DET SEQ1 DIT'].round(2) == DIT) &
                                    (calibs['INS COMB IFLT'] == cfilt)]
                    files = cfiles.index

                    # skip non-existing combinations
                    if len(cfiles) == 0:
                        continue

                    print(' * {0} in filter {1} with DIT={2:.2f} sec ({3} files)'.format(ctype, cfilt, DIT, len(cfiles)))

                    # create sof
                    sof = os.path.join(path.sof, 'dark_filt={0}_DIT={1:.2f}.sof'.format(cfilt, DIT))
                    file = open(sof, 'w')
                    for f in files:
                        file.write('{0}{1}.fits     {2}\n'.format(path.raw, f, 'IRD_DARK_RAW'))
                    file.close()

                    # products
                    if ctype == 'SKY':
                        loc = 'sky'
                    else:
                        loc = 'internal'
                    dark_file = 'dark_{0}_filt={1}_DIT={2:.2f}'.format(loc, cfilt, DIT)
                    bpm_file  = 'dark_{0}_bpm_filt={1}_DIT={2:.2f}'.format(loc, cfilt, DIT)

                    # different max level in LRS
                    max_level = 1000
                    if cfilt in ['S_LR']:
                        max_level = 15000
                    
                    # esorex parameters    
                    args = ['esorex',
                            '--no-checksum=TRUE',
                            '--no-datamd5=TRUE',
                            'sph_ird_master_dark',
                            '--ird.master_dark.sigma_clip=5.0',
                            '--ird.master_dark.save_addprod=TRUE',
                            '--ird.master_dark.max_acceptable={0}'.format(max_level),
                            '--ird.master_dark.outfilename={0}{1}.fits'.format(path.calib, dark_file),
                            '--ird.master_dark.badpixfilename={0}{1}.fits'.format(path.calib, bpm_file),
                            sof]

                    # check esorex
                    if shutil.which('esorex') is None:
                        raise NameError('esorex does not appear to be in your PATH. Please make sure ' +
                                        'that the ESO pipeline is properly installed before running VLTPF.')

                    # execute esorex
                    if silent:
                        proc = subprocess.run(args, cwd=path.tmp, stdout=subprocess.DEVNULL)
                    else:
                        proc = subprocess.run(args, cwd=path.tmp)

                    if proc.returncode != 0:
                        raise ValueError('esorex process was not successful')

                    # store products
                    files_info.loc[dark_file, 'DPR CATG'] = cfiles['DPR CATG'][0]
                    files_info.loc[dark_file, 'DPR TYPE'] = cfiles['DPR TYPE'][0]
                    files_info.loc[dark_file, 'INS COMB IFLT'] = cfiles['INS COMB IFLT'][0]
                    files_info.loc[dark_file, 'INS4 FILT2 NAME'] = cfiles['INS4 FILT2 NAME'][0]
                    files_info.loc[dark_file, 'INS1 MODE'] = cfiles['INS1 MODE'][0]
                    files_info.loc[dark_file, 'INS1 FILT NAME'] = cfiles['INS1 FILT NAME'][0]
                    files_info.loc[dark_file, 'INS1 OPTI2 NAME'] = cfiles['INS1 OPTI2 NAME'][0]
                    files_info.loc[dark_file, 'DET SEQ1 DIT'] = cfiles['DET SEQ1 DIT'][0]
                    files_info.loc[dark_file, 'PROCESSED'] = True
                    files_info.loc[dark_file, 'PRO CATG'] = 'IRD_MASTER_DARK'

                    files_info.loc[bpm_file, 'DPR CATG'] = cfiles['DPR CATG'][0]
                    files_info.loc[bpm_file, 'DPR TYPE'] = cfiles['DPR TYPE'][0]
                    files_info.loc[bpm_file, 'INS COMB IFLT'] = cfiles['INS COMB IFLT'][0]
                    files_info.loc[bpm_file, 'INS4 FILT2 NAME'] = cfiles['INS4 FILT2 NAME'][0]
                    files_info.loc[bpm_file, 'INS1 MODE'] = cfiles['INS1 MODE'][0]
                    files_info.loc[bpm_file, 'INS1 FILT NAME'] = cfiles['INS1 FILT NAME'][0]
                    files_info.loc[bpm_file, 'INS1 OPTI2 NAME'] = cfiles['INS1 OPTI2 NAME'][0]
                    files_info.loc[bpm_file, 'PROCESSED'] = True
                    files_info.loc[bpm_file, 'PRO CATG']  = 'IRD_STATIC_BADPIXELMAP'

        # save
        files_info.to_csv(os.path.join(path.preproc, 'files.csv'))

        # update recipe execution
        self._recipe_execution['sph_ird_cal_dark'] = True


    def sph_ird_cal_detector_flat(self, silent=True):
        '''
        Create the detector flat calibrations

        Parameters
        ----------
        silent : bool
            Suppress esorex output. Default is True
        '''

        # check if recipe can be executed
        toolbox.check_recipe_execution(self._recipe_execution, 'sph_ird_cal_detector_flat', self.recipe_requirements)
        
        print('Creating flats')

        # parameters
        path = self._path
        files_info = self._files_info
        
        # get list of files
        calibs = files_info[np.logical_not(files_info['PROCESSED']) &
                            (files_info['DPR TYPE'] == 'FLAT,LAMP')]
        filter_combs = calibs['INS COMB IFLT'].unique()
        
        for cfilt in filter_combs:
            cfiles = calibs[calibs['INS COMB IFLT'] == cfilt]
            files = cfiles.index

            print(' * filter {0} ({1} files)'.format(cfilt, len(cfiles)))
            
            # create sof
            sof = os.path.join(path.sof, 'flat_filt={0}.sof'.format(cfilt))
            file = open(sof, 'w')
            for f in files:
                file.write('{0}{1}.fits     {2}\n'.format(path.raw, f, 'IRD_FLAT_FIELD_RAW'))
            file.close()

            # products
            flat_file = 'flat_filt={0}'.format(cfilt)
            bpm_file  = 'flat_bpm_filt={0}'.format(cfilt)
            
            # esorex parameters    
            args = ['esorex',
                    '--no-checksum=TRUE',
                    '--no-datamd5=TRUE',
                    'sph_ird_instrument_flat',
                    '--ird.instrument_flat.save_addprod=TRUE',
                    '--ird.instrument_flat.outfilename={0}{1}.fits'.format(path.calib, flat_file),
                    '--ird.instrument_flat.badpixfilename={0}{1}.fits'.format(path.calib, bpm_file),
                    sof]

            # check esorex
            if shutil.which('esorex') is None:
                raise NameError('esorex does not appear to be in your PATH. Please make sure ' +
                                'that the ESO pipeline is properly installed before running VLTPF.')

            # execute esorex
            if silent:
                proc = subprocess.run(args, cwd=path.tmp, stdout=subprocess.DEVNULL)
            else:
                proc = subprocess.run(args, cwd=path.tmp)

            if proc.returncode != 0:
                raise ValueError('esorex process was not successful')

            # store products
            files_info.loc[flat_file, 'DPR CATG'] = cfiles['DPR CATG'][0]
            files_info.loc[flat_file, 'DPR TYPE'] = cfiles['DPR TYPE'][0]
            files_info.loc[flat_file, 'INS COMB IFLT'] = cfiles['INS COMB IFLT'][0]
            files_info.loc[flat_file, 'INS4 FILT2 NAME'] = cfiles['INS4 FILT2 NAME'][0]
            files_info.loc[flat_file, 'INS1 MODE'] = cfiles['INS1 MODE'][0]
            files_info.loc[flat_file, 'INS1 FILT NAME'] = cfiles['INS1 FILT NAME'][0]
            files_info.loc[flat_file, 'INS1 OPTI2 NAME'] = cfiles['INS1 OPTI2 NAME'][0]
            files_info.loc[flat_file, 'DET SEQ1 DIT'] = cfiles['DET SEQ1 DIT'][0]
            files_info.loc[flat_file, 'PROCESSED'] = True
            files_info.loc[flat_file, 'PRO CATG'] = 'IRD_FLAT_FIELD'

            files_info.loc[bpm_file, 'DPR CATG'] = cfiles['DPR CATG'][0]
            files_info.loc[bpm_file, 'DPR TYPE'] = cfiles['DPR TYPE'][0]
            files_info.loc[bpm_file, 'INS COMB IFLT'] = cfiles['INS COMB IFLT'][0]
            files_info.loc[bpm_file, 'INS4 FILT2 NAME'] = cfiles['INS4 FILT2 NAME'][0]
            files_info.loc[bpm_file, 'INS1 MODE'] = cfiles['INS1 MODE'][0]
            files_info.loc[bpm_file, 'INS1 FILT NAME'] = cfiles['INS1 FILT NAME'][0]
            files_info.loc[bpm_file, 'INS1 OPTI2 NAME'] = cfiles['INS1 OPTI2 NAME'][0]
            files_info.loc[bpm_file, 'PROCESSED'] = True
            files_info.loc[bpm_file, 'PRO CATG']  = 'IRD_NON_LINEAR_BADPIXELMAP'
        
        # save
        files_info.to_csv(os.path.join(path.preproc, 'files.csv'))

        # update recipe execution
        self._recipe_execution['sph_ird_cal_detector_flat'] = True

    
    def sph_ird_wave_calib(self, silent=True):
        '''
        Create the wavelength calibration

        Parameters
        ----------
        silent : bool
            Suppress esorex output. Default is True
        '''

        # check if recipe can be executed
        toolbox.check_recipe_execution(self._recipe_execution, 'sph_ird_wave_calib', self.recipe_requirements)
        
        print('Creating wavelength calibration')

        # parameters
        path = self._path
        files_info = self._files_info
        
        # get list of files
        wave_file = files_info[np.logical_not(files_info['PROCESSED']) & (files_info['DPR TYPE'] == 'LAMP,WAVE')]
        if len(wave_file) != 1:
            raise ValueError('There should be exactly 1 raw wavelength calibration file. Found {0}.'.format(len(wave_file)))
        
        DIT = wave_file['DET SEQ1 DIT'][0]
        dark_file = files_info[files_info['PROCESSED'] & (files_info['PRO CATG'] == 'IRD_MASTER_DARK') & 
                               (files_info['DPR CATG'] == 'CALIB') & (files_info['DET SEQ1 DIT'].round(2) == DIT)]
        if len(dark_file) == 0:
            raise ValueError('There should at least 1 dark file for wavelength calibration. Found none.')

        filter_comb = wave_file['INS COMB IFLT'][0]
        flat_file = files_info[files_info['PROCESSED'] & (files_info['PRO CATG'] == 'IRD_FLAT_FIELD')]
        if len(flat_file) == 0:
            raise ValueError('There should at least 1 flat file for wavelength calibration. Found none.')
        
        bpm_file = files_info[files_info['PROCESSED'] & (files_info['PRO CATG'] == 'IRD_NON_LINEAR_BADPIXELMAP')]
        if len(flat_file) == 0:
            raise ValueError('There should at least 1 bad pixel map file for wavelength calibration. Found none.')
        
        # products
        wav_file = 'wave_calib'
        
        # esorex parameters
        if filter_comb == 'S_LR':
            # create standard sof in LRS
            sof = os.path.join(path.sof, 'wave.sof')
            file = open(sof, 'w')
            file.write('{0}{1}.fits     {2}\n'.format(path.raw, wave_file, 'IRD_WAVECALIB_RAW'))
            file.write('{0}{1}.fits     {2}\n'.format(path.calib, dark_file.index[0], 'IRD_MASTER_DARK'))
            file.write('{0}{1}.fits     {2}\n'.format(path.calib, flat_file.index[0], 'IRD_FLAT_FIELD'))
            file.write('{0}{1}.fits     {2}\n'.format(path.calib, bpm_file.index[0], 'IRD_STATIC_BADPIXELMAP'))
            file.close()
            
            args = ['esorex',
                    '--no-checksum=TRUE',
                    '--no-datamd5=TRUE',
                    'sph_ird_wave_calib',
                    '--ird.wave_calib.column_width=200',
                    '--ird.wave_calib.grism_mode=FALSE',
                    '--ird.wave_calib.threshold=2000',
                    '--ird.wave_calib.number_lines=6',
                    '--ird.wave_calib.outfilename={0}{1}.fits'.format(path.calib, wav_file),
                    sof]
        elif filter_comb == 'S_MR':            
            # masking of second order spectrum in MRS
            wave_fname = wave_file.index[0]
            wave_data, hdr = fits.getdata(os.path.join(path.raw, wave_fname+'.fits'), header=True)
            wave_data = wave_data.squeeze()
            wave_data[:60, :] = 0
            fits.writeto(os.path.join(path.preproc, wave_fname+'_masked.fits'), wave_data, hdr, overwrite=True, 
                         output_verify='silentfix')
            
            # create sof using the masked file
            sof = os.path.join(path.sof, 'wave.sof')
            file = open(sof, 'w')
            file.write('{0}{1}_masked.fits {2}\n'.format(path.preproc, wave_fname, 'IRD_WAVECALIB_RAW'))
            file.write('{0}{1}.fits        {2}\n'.format(path.calib, dark_file.index[0], 'IRD_MASTER_DARK'))
            file.write('{0}{1}.fits        {2}\n'.format(path.calib, flat_file.index[0], 'IRD_FLAT_FIELD'))
            file.write('{0}{1}.fits        {2}\n'.format(path.calib, bpm_file.index[0], 'IRD_STATIC_BADPIXELMAP'))
            file.close()

            args = ['esorex',
                    '--no-checksum=TRUE',
                    '--no-datamd5=TRUE',
                    'sph_ird_wave_calib',
                    '--ird.wave_calib.column_width=200',
                    '--ird.wave_calib.grism_mode=TRUE',
                    '--ird.wave_calib.threshold=1000',
                    '--ird.wave_calib.number_lines=5',
                    '--ird.wave_calib.outfilename={0}{1}.fits'.format(path.calib, wav_file),
                    sof]

        # check esorex
        if shutil.which('esorex') is None:
            raise NameError('esorex does not appear to be in your PATH. Please make sure ' +
                            'that the ESO pipeline is properly installed before running VLTPF.')
        
        # execute esorex
        if silent:
            proc = subprocess.run(args, cwd=path.tmp, stdout=subprocess.DEVNULL)
        else:
            proc = subprocess.run(args, cwd=path.tmp)

        if proc.returncode != 0:
            raise ValueError('esorex process was not successful')

        # store products
        files_info.loc[wav_file, 'DPR CATG'] = wave_file['DPR CATG'][0]
        files_info.loc[wav_file, 'DPR TYPE'] = wave_file['DPR TYPE'][0]
        files_info.loc[wav_file, 'INS COMB IFLT'] = wave_file['INS COMB IFLT'][0]
        files_info.loc[wav_file, 'INS4 FILT2 NAME'] = wave_file['INS4 FILT2 NAME'][0]
        files_info.loc[wav_file, 'INS1 MODE'] = wave_file['INS1 MODE'][0]
        files_info.loc[wav_file, 'INS1 FILT NAME'] = wave_file['INS1 FILT NAME'][0]
        files_info.loc[wav_file, 'INS1 OPTI2 NAME'] = wave_file['INS1 OPTI2 NAME'][0]
        files_info.loc[wav_file, 'DET SEQ1 DIT'] = wave_file['DET SEQ1 DIT'][0]
        files_info.loc[wav_file, 'PROCESSED'] = True
        files_info.loc[wav_file, 'PRO CATG'] = 'IRD_WAVECALIB'
        
        # save
        files_info.to_csv(os.path.join(path.preproc, 'files.csv'))

        # update recipe execution
        self._recipe_execution['sph_ird_wave_calib'] = True


    def sph_ird_preprocess_science(self,
                                   subtract_background=True, fix_badpix=True,
                                   collapse_science=False, collapse_psf=True, collapse_center=True):
        '''Pre-processes the science frames.

        This function can perform multiple steps:
          - collapse of the frames
          - subtract the background
          - correct bad pixels
          - reformat IRDIS data in (x,y,lambda) cubes

        For the science, PSFs or star center frames, the full cubes
        are mean-combined into a single frame.

        The pre-processed frames are saved in the preproc
        sub-directory and will be combined later.
        
        Parameters
        ----------
        subtract_background : bool
            Performs background subtraction. Default is True

        fix_badpix : bool
            Performs correction of bad pixels. Default is True

        collapse_science :  bool
            Collapse data for OBJECT cubes. Default is False

        collapse_psf :  bool
            Collapse data for OBJECT,FLUX cubes. Default is True. Note
            that the collapse type is mean and cannot be changed.

        collapse_center :  bool
            Collapse data for OBJECT,CENTER cubes. Default is True. Note
            that the collapse type is mean and cannot be changed.

        '''

        # check if recipe can be executed
        toolbox.check_recipe_execution(self._recipe_execution, 'sph_ird_preprocess_science', self.recipe_requirements)
        
        print('Pre-processing science files')

        # parameters
        path = self._path
        files_info = self._files_info
        frames_info = self._frames_info
        
        # clean before we start
        files = glob.glob(os.path.join(path.preproc, '*_DIT???_preproc.fits'))
        for file in files:
            os.remove(file)

        # filter combination
        filter_comb = files_info.loc[files_info['DPR CATG'] == 'SCIENCE', 'INS COMB IFLT'].unique()[0]

        # bpm
        if fix_badpix:
            bpm_files = files_info[(files_info['PRO CATG'] == 'IRD_STATIC_BADPIXELMAP') |
                                   (files_info['PRO CATG'] == 'IRD_NON_LINEAR_BADPIXELMAP')].index
            bpm_files = [os.path.join(path.calib, f+'.fits') for f in bpm_files]

            bpm = toolbox.compute_bad_pixel_map(bpm_files)

            # mask dead regions
            bpm[:15, :]      = 0
            bpm[1013:, :]    = 0
            bpm[:, :50]      = 0
            bpm[:, 941:1078] = 0
            bpm[:, 1966:]    = 0
            
        # flat        
        flat_file = files_info[files_info['PROCESSED'] & (files_info['PRO CATG'] == 'IRD_FLAT_FIELD') &
                               (files_info['INS COMB IFLT'] == filter_comb)]
        if len(flat_file) != 1:
            raise ValueError('There should be exactly 1 flat file. Found {0}.'.format(len(flat_file)))
        flat = fits.getdata(os.path.join(path.calib, flat_file.index[0]+'.fits'))
            
        # final dataframe
        index = pd.MultiIndex(names=['FILE', 'IMG'], levels=[[], []], codes=[[], []])
        frames_info_preproc = pd.DataFrame(index=index, columns=frames_info.columns, dtype='float')
        
        # loop on the different type of science files
        sci_types = ['OBJECT,CENTER', 'OBJECT,FLUX', 'OBJECT']
        dark_types = ['SKY', 'DARK,BACKGROUND', 'DARK']
        for typ in sci_types:
            # science files
            sci_files = files_info[(files_info['DPR CATG'] == 'SCIENCE') & (files_info['DPR TYPE'] == typ)]
            sci_DITs = list(sci_files['DET SEQ1 DIT'].round(2).unique())

            if len(sci_files) == 0:
                continue        

            for DIT in sci_DITs:
                sfiles = sci_files[sci_files['DET SEQ1 DIT'].round(2) == DIT]

                print('{0} files of type {1} with DIT={2} sec'.format(len(sfiles), typ, DIT))

                if subtract_background:
                    # look for sky, then background, then darks
                    # normally there should be only one with the proper DIT
                    dfiles = []
                    for d in dark_types:
                        dfiles = files_info[(files_info['PRO CATG'] == 'IRD_MASTER_DARK') &
                                            (files_info['DPR TYPE'] == d) & (files_info['DET SEQ1 DIT'].round(2) == DIT)]
                        if len(dfiles) != 0:
                            break
                    print('   ==> found {0} corresponding {1} file'.format(len(dfiles), d))

                    if len(dfiles) == 0:
                        # issue a warning if absolutely no background is found
                        print('Warning: no background has been found. Pre-processing will continue but data quality will likely be affected')
                        bkg = np.zeros((1024, 2048))
                    elif len(dfiles) == 1:
                        bkg = fits.getdata(os.path.join(path.calib, dfiles.index[0]+'.fits'))
                    elif len(dfiles) > 1:
                        # FIXME: handle cases when multiple backgrounds are found?
                        raise ValueError('Unexpected number of background files ({0})'.format(len(dfiles)))

                # process files
                for idx, (fname, finfo) in enumerate(sfiles.iterrows()):
                    # frames_info extract
                    finfo = frames_info.loc[(fname, slice(None)), :]

                    print(' * file {0}/{1}: {2}, NDIT={3}'.format(idx+1, len(sfiles), fname, len(finfo)))

                    # read data
                    print('   ==> read data')
                    img, hdr = fits.getdata(os.path.join(path.raw, fname+'.fits'), header=True)
                    
                    # add extra dimension to single images to make cubes
                    if img.ndim == 2:
                        img = img[np.newaxis, ...]

                    # mask dead regions
                    img[:, :15, :]      = np.nan
                    img[:, 1013:, :]    = np.nan
                    img[:, :, :50]      = np.nan
                    img[:, :, 941:1078] = np.nan
                    img[:, :, 1966:]    = np.nan
                    
                    # collapse
                    if (typ == 'OBJECT,CENTER'):
                        if collapse_center:
                            print('   ==> collapse: mean')
                            img = np.mean(img, axis=0, keepdims=True)
                            frames_info_new = toolbox.collapse_frames_info(finfo, fname, 'mean')
                        else:
                            frames_info_new = toolbox.collapse_frames_info(finfo, fname, 'none')
                    elif (typ == 'OBJECT,FLUX'):
                        if collapse_psf:
                            print('   ==> collapse: mean')
                            img = np.mean(img, axis=0, keepdims=True)
                            frames_info_new = toolbox.collapse_frames_info(finfo, fname, 'mean')
                        else:
                            frames_info_new = toolbox.collapse_frames_info(finfo, fname, 'none')
                    elif (typ == 'OBJECT'):
                        if collapse_science:
                            print('   ==> collapse: mean ({0} -> 1 frame, 0 dropped)'.format(len(img)))
                            img = np.mean(img, axis=0, keepdims=True)

                            frames_info_new = toolbox.collapse_frames_info(finfo, fname, 'mean')
                        else:
                            frames_info_new = toolbox.collapse_frames_info(finfo, fname, 'none')

                    frames_info_preproc = pd.concat((frames_info_preproc, frames_info_new))
                    
                    # background subtraction
                    if subtract_background:
                        print('   ==> subtract background')
                        for f in range(len(img)):
                            img[f] -= bkg

                    # divide flat
                    if subtract_background:
                        print('   ==> divide by flat field')
                        for f in range(len(img)):
                            img[f] /= flat
                        
                    # bad pixels correction
                    if fix_badpix:
                        print('   ==> correct bad pixels')
                        for f in range(len(img)):                            
                            frame = img[f]
                            frame = imutils.fix_badpix(frame, bpm, npix=12, weight=True)

                            # additional sigma clipping to remove transitory bad pixels
                            # not done for OBJECT,FLUX because PSF peak can be clipped
                            if (typ != 'OBJECT,FLUX'):
                                frame = imutils.sigma_filter(frame, box=7, nsigma=4, iterate=False)

                            img[f] = frame

                    # reshape data
                    print('   ==> reshape data')
                    NDIT = img.shape[0]
                    nimg = np.zeros((NDIT, 2, 1024, 1024))
                    for f in range(len(img)):
                        nimg[f, 0] = img[f, :, 0:1024]
                        nimg[f, 1] = img[f, :, 1024:]
                    img = nimg
                        
                    # save DITs individually
                    for f in range(len(img)):
                        frame = nimg[f, ...].squeeze()                    
                        hdr['HIERARCH ESO DET NDIT'] = 1
                        fits.writeto(os.path.join(path.preproc, fname+'_DIT{0:03d}_preproc.fits'.format(f)), frame, hdr,
                                     overwrite=True, output_verify='silentfix')

                    print()

            print()

        # sort and save final dataframe
        frames_info_preproc.sort_values(by='TIME', inplace=True)
        frames_info_preproc.to_csv(os.path.join(path.preproc, 'frames_preproc.csv'))

        self._frames_info_preproc = frames_info_preproc

        # update recipe execution
        self._recipe_execution['sph_ird_preprocess_science'] = True


    def sph_ird_star_center(self, high_pass=False, display=False, save=True):
        '''Determines the star center for all frames where a center can be
        determined (OBJECT,CENTER and OBJECT,FLUX)

        Parameters
        ----------
        high_pass : bool
            Apply high-pass filter to the image before searching for the satelitte spots.
            Default is False

        display : bool
            Display the fit of the satelitte spots

        save : bool
            Save the fit of the sattelite spot for quality check. Default is True,
            although it is a bit slow.

        '''

        # check if recipe can be executed
        toolbox.check_recipe_execution(self._recipe_execution, 'sph_ird_star_center', self.recipe_requirements)
        
        print('Star centers determination')

        # parameters
        path = self._path
        pixel = self._pixel
        files_info  = self._files_info
        frames_info = self._frames_info_preproc

        # filter combination
        filter_comb = frames_info['INS COMB IFLT'].unique()[0]
        # FIXME: centers should be stored in .ini files and passed to
        # function when needed (ticket #60)
        if filter_comb == 'S_LR':
            centers = np.array(((484, 496), 
                                (488, 486)))
            wave_min = 920
            wave_max = 2330
        elif filter_comb == 'S_MR':
            centers = np.array(((474, 519), 
                                (479, 509)))
            wave_min = 940
            wave_max = 1820
        
        # wavelength map
        wave_file  = files_info[files_info['PROCESSED'] & (files_info['PRO CATG'] == 'IRD_WAVECALIB')]
        wave_calib = fits.getdata(os.path.join(path.calib, wave_file.index[0]+'.fits'))
        wave_lin = get_wavelength_calibration(wave_calib, centers, wave_min, wave_max)
        
        # start with OBJECT,FLUX
        flux_files = frames_info[frames_info['DPR TYPE'] == 'OBJECT,FLUX']        
        if len(flux_files) != 0:
            for file, idx in flux_files.index:
                print('  ==> OBJECT,FLUX: {0}'.format(file))

                # read data
                fname = '{0}_DIT{1:03d}_preproc'.format(file, idx)
                files = glob.glob(os.path.join(path.preproc, fname+'.fits'))
                cube, hdr = fits.getdata(files[0], header=True)

                # centers
                if save:
                    save_path = os.path.join(path.products, fname+'_PSF_fitting.pdf')
                else:
                    save_path = None
                psf_center = toolbox.star_centers_from_PSF_lss_cube(cube, wave_lin, pixel, display=display, save_path=save_path)

                # save
                fits.writeto(os.path.join(path.preproc, fname+'_centers.fits'), psf_center, overwrite=True)
                print()

        # then OBJECT,CENTER (if any)
        starcen_files = frames_info[frames_info['DPR TYPE'] == 'OBJECT,CENTER']
        DIT = starcen_files['DET SEQ1 DIT'].round(2)[0]
        starsci_files = frames_info[(frames_info['DPR TYPE'] == 'OBJECT') & (frames_info['DET SEQ1 DIT'].round(2) == DIT)]
        if len(starcen_files) != 0:
            for file, idx in starcen_files.index:
                print('  ==> OBJECT,CENTER: {0}'.format(file))

                # read center data
                fname = '{0}_DIT{1:03d}_preproc'.format(file, idx)
                files = glob.glob(os.path.join(path.preproc, fname+'.fits'))
                cube_cen, hdr = fits.getdata(files[0], header=True)

                # read science data
                if len(starsci_files) != 0:
                    fname2 = '{0}_DIT{1:03d}_preproc'.format(starsci_files.index[0][0], idx)
                    files2 = glob.glob(os.path.join(path.preproc, fname2+'.fits'))
                    cube_sci, hdr = fits.getdata(files2[0], header=True)                    
                else:
                    cube_sci = None
                
                # centers
                if save:
                    save_path = os.path.join(path.products, fname+'_spots_fitting.pdf')
                else:
                    save_path = None
                spot_centers, spot_dist, img_centers \
                    = toolbox.star_centers_from_waffle_lss_cube(cube_cen, cube_sci, wave_lin, centers, pixel, 
                                                                high_pass=high_pass, display=display, 
                                                                save_path=save_path)

                # save
                fits.writeto(os.path.join(path.preproc, fname+'_centers.fits'), img_centers, overwrite=True)
                fits.writeto(os.path.join(path.preproc, fname+'_spot_distance.fits'), spot_dist, overwrite=True)
                print()

        # update recipe execution
        self._recipe_execution['sph_ird_star_center'] = True


    def sph_ird_wavelength_recalibration(self, fit_scaling=True, display=False, save=True):
        '''Performs a recalibration of the wavelength, if star center frames
        are available.

        It follows a similar process to that used for the IFS
        data. The method for the IFS is described in Vigan et
        al. (2015, MNRAS, 454, 129):

        https://ui.adsabs.harvard.edu/#abs/2015MNRAS.454..129V/abstract

        Parameters
        ----------
        fit_scaling : bool
            Perform a polynomial fitting of the wavelength scaling
            law. It helps removing high-frequency noise that can
            result from the waffle fitting. Default is True

        display : bool
            Display the result of the recalibration. Default is False.

        save : bool
            Save the fit of the sattelite spot for quality check. Default is True,
            although it is a bit slow.

        '''
        
        # check if recipe can be executed
        toolbox.check_recipe_execution(self._recipe_execution, 'sph_ird_wavelength_recalibration', self.recipe_requirements)
        
        print('Wavelength recalibration')

        # parameters
        path = self._path
        pixel = self._pixel
        lasers = self._wave_cal_lasers
        files_info  = self._files_info
        frames_info = self._frames_info_preproc

        # filter combination
        filter_comb = frames_info['INS COMB IFLT'].unique()[0]
        # FIXME: centers should be stored in .ini files and passed to
        # function when needed (ticket #60)
        if filter_comb == 'S_LR':
            centers = np.array(((484, 496), 
                                (488, 486)))
            wave_min = 920
            wave_max = 2330
        elif filter_comb == 'S_MR':
            centers = np.array(((474, 519), 
                                (479, 509)))
            wave_min = 940
            wave_max = 1820
        
        # wavelength map
        wave_file  = files_info[files_info['PROCESSED'] & (files_info['PRO CATG'] == 'IRD_WAVECALIB')]
        wave_calib = fits.getdata(os.path.join(path.calib, wave_file.index[0]+'.fits'))
        wave_lin = get_wavelength_calibration(wave_calib, centers, wave_min, wave_max)

        # reference wavelength
        idx_ref = 3
        wave_ref = lasers[idx_ref]
        
        # get spot distance from the first OBJECT,CENTER in the sequence
        starcen_files = frames_info[frames_info['DPR TYPE'] == 'OBJECT,CENTER']
        if len(starcen_files) == 0:
            print(' ==> no OBJECT,CENTER file in the data set. Wavelength cannot be recalibrated. ' +
                  'The standard wavelength calibrated by the ESO pripeline will be used.')
            fits.writeto(os.path.join(path.preproc, 'wavelength_final.fits'), wave_lin, overwrite=True)
            return

        fname = '{0}_DIT{1:03d}_preproc_spot_distance'.format(starcen_files.index.values[0][0], starcen_files.index.values[0][1])
        spot_dist = fits.getdata(os.path.join(path.preproc, fname+'.fits'))
        
        if save:
            pdf = PdfPages(os.path.join(path.products, 'wavelength_recalibration.pdf'))        
        
        pix = np.arange(1024)
        wave_final = np.zeros((1024, 2))
        for fidx in range(2):
            print('  field {0:2d}/{1:2d}'.format(fidx+1, 2))
            
            wave = wave_lin[fidx]
            dist = spot_dist[:, fidx]

            imin = np.nanargmin(np.abs(wave-wave_ref))
            
            # scaling factor
            scaling_raw = dist / dist[imin]
            
            if filter_comb == 'S_LR':
                # FIXME: implement smoothing of the scaling factor for
                # LRS mode
                raise ValueError('Wavelength recalibration is not yet implemented for IRDIS-LRS mode')
            elif filter_comb == 'S_MR':
                # linear fit with a 5-degree polynomial
                good = np.where(np.isfinite(wave))
                p = np.polyfit(pix[good], scaling_raw[good], 5)
                
                scaling_fit = np.polyval(p, pix)
            
            wave_final_raw = wave[imin] * scaling_raw
            wave_final_fit = wave[imin] * scaling_fit

            bad = np.where(np.logical_not(np.isfinite(wave)))
            wave_final_raw[bad] = np.nan
            wave_final_fit[bad] = np.nan
            
            wave_diff = np.abs(wave_final_fit - wave)
            print('   ==> difference with calibrated wavelength: ' +
                  'min={0:.1f} nm, max={1:.1f} nm'.format(np.nanmin(wave_diff), np.nanmax(wave_diff)))

            if fit_scaling:
                wave_final[:, fidx] = wave_final_fit
                use_r = ''
                use_f = ' <=='
            else:
                wave_final[:, fidx] = wave_final_raw
                use_r = ' <=='
                use_f = ''
            
            # plot
            if save or display:
                plt.figure('Wavelength recalibration', figsize=(10, 10))
                plt.clf()
                
                plt.subplot(211)
                plt.axvline(imin, color='k', linestyle='--')
                plt.plot(pix, wave, label='DRH', color='r', lw=3)
                plt.plot(pix, wave_final_raw, label='Recalibrated [raw]'+use_r)
                plt.plot(pix, wave_final_fit, label='Recalibrated [fit]'+use_f)
                plt.legend(loc='upper left')
                plt.ylabel('Wavelength r[nm]')
                plt.title('Field #{}'.format(fidx))
                plt.xlim(1024, 0)
                
                plt.subplot(212)
                plt.axvline(imin, color='k', linestyle='--')
                plt.plot(pix, wave-wave_final_raw)
                plt.plot(pix, wave-wave_final_fit)
                plt.ylabel('Residuals r[nm]')
                plt.xlabel('Detector coordinate [pix]')
                plt.xlim(1024, 0)
                
                plt.tight_layout()
            
            if save:                
                pdf.savefig()

            if display:
                plt.pause(1e-3)

        if save:
            pdf.close()

        # save
        print(' * saving')
        fits.writeto(os.path.join(path.preproc, 'wavelength_final.fits'), wave_final, overwrite=True)

    
        # update recipe execution
        self._recipe_execution['sph_ird_wavelength_recalibration'] = True


    def sph_ird_combine_data(self, cpix=True, psf_dim=80, science_dim=800, correct_mrs_chromatism=True,
                             split_posang=True, shift_method='fft', manual_center=None, skip_center=False):
        '''Combine and save the science data into final cubes

        All types of data are combined independently: PSFs
        (OBJECT,FLUX), star centers (OBJECT,CENTER) and standard
        coronagraphic images (OBJECT). 

        Depending on the observing strategy, there can be several
        position angle positions in the sequence. Images taken at
        different position angles can be either kept together or 
        split into different cubes. In either case a posang vector 
        is saved alongside the science cube(s).

        For each type of data, the method saves 3 different files:
        
          - *_cube: the (x,y,time) cube
        
          - *_posang: the position angle vector.

          - *_frames: a csv file with all the information for every
                      frames. There is one line by time step in the
                      data cube.

        Data are save separately for each field.
        
        Parameters
        ----------
        cpix : bool
            If True the images are centered on the pixel at coordinate
            dim//2 in the spatial dimension. If False the images are
            centered between 2 pixels, at coordinates
            (dim-1)/2. Default is True.

        psf_dim : even int
            Size of the PSF images along in the spatial
            dimension. Default is 80x pixels

        science_dim : even int    
            Size of the science images (star centers and standard
            coronagraphic images) in the spatial dimension. Default is
            800 pixels

        correct_mrs_chromatism : bool
            Correct for the slight chromatism in the MRS mode. This
            chromatism induces a slight shift of the PSF center with
            wavelength. Default is True.

        split_posang : bool
            Save data taken at different position angles in separate 
            science files. Default is True

        manual_center : array
            User provided spatial center for the OBJECT,CENTER and
            OBJECT frames. This should be an array of 2 values (cx for
            the 2 IRDIS fields). If a manual center is provided, the
            value of skip_center is ignored for the OBJECT,CENTER and
            OBJECT frames. Default is None

        skip_center : bool
            Control if images are finely centered or not before being
            combined. However the images are still roughly centered by
            shifting them by an integer number of pixel to bring the
            center of the data close to the center of the images. This
            option is useful if fine centering must be done afterwards.
        
        shift_method : str
            Method to shifting the images: fft or interp.  Default is
            fft

        '''
        
        # check if recipe can be executed
        toolbox.check_recipe_execution(self._recipe_execution, 'sph_ird_combine_data', self.recipe_requirements)
        
        print('Combine science data')

        # parameters
        path = self._path
        nwave = self._nwave
        frames_info = self._frames_info_preproc

        # filter combination
        filter_comb = frames_info['INS COMB IFLT'].unique()[0]
        # FIXME: centers should be stored in .ini files and passed to
        # function when needed (ticket #60)
        if filter_comb == 'S_LR':
            centers = np.array(((484, 496), 
                                (488, 486)))
            wave_min = 920
            wave_max = 2330
        elif filter_comb == 'S_MR':
            centers = np.array(((474, 519), 
                                (479, 509)))
            wave_min = 940
            wave_max = 1820
        
        # wavelength solution: make sure we have the same number of
        # wave points in each field
        wave   = fits.getdata(os.path.join(path.preproc, 'wavelength_final.fits'))
        mask   = ((wave_min <= wave) & (wave <= wave_max))
        iwave0 = np.where(mask[:, 0])[0]
        iwave1 = np.where(mask[:, 1])[0]
        nwave  = np.min([iwave0.size, iwave1.size])
        
        iwave = np.empty((nwave, 2), dtype=np.int)        
        iwave[:, 0] = iwave0[:nwave]
        iwave[:, 1] = iwave1[:nwave]

        final_wave = np.empty((nwave, 2))
        final_wave[:, 0] = wave[iwave[:, 0], 0]
        final_wave[:, 1] = wave[iwave[:, 1], 1]
        
        fits.writeto(os.path.join(path.products, 'wavelength.fits'), final_wave.squeeze().T, overwrite=True)
        
        # max images size
        if psf_dim > 1024:
            print('Warning: psf_dim cannot be larger than 1024 pix. A value of 1024 will be used.')
            psf_dim = 1024

        if science_dim > 1024:
            print('Warning: science_dim cannot be larger than 1024 pix. A value of 1024 will be used.')
            science_dim = 1024

        # centering
        centers_default = centers[:, 0]
        if skip_center:
            print('Warning: images will not be fine centered. They will just be combined.')
            shift_method = 'roll'

        if manual_center is not None:
            manual_center = np.array(manual_center)
            if manual_center.shape != (2,):
                raise ValueError('manual_center does not have the right number of dimensions.')
            
            print('Warning: images will be centered at the user-provided values.')

        if correct_mrs_chromatism and (filter_comb == 'S_MR'):
            print('Warning: fine centering will be done anyway to correct for MRS chromatism')
            
        #
        # OBJECT,FLUX
        #
        flux_files = frames_info[frames_info['DPR TYPE'] == 'OBJECT,FLUX']
        nfiles = len(flux_files)
        if nfiles != 0:
            print(' * OBJECT,FLUX data')

            # final arrays
            psf_cube   = np.zeros((2, nfiles, nwave, psf_dim))
            psf_posang = np.zeros(nfiles)

            # final center
            if cpix:
                cc = psf_dim // 2
            else:
                cc = (psf_dim - 1) / 2

            # read and combine files
            for file_idx, (file, idx) in enumerate(flux_files.index):
                print('  ==> file {0}/{1}: {2}, DIT={3}'.format(file_idx+1, len(flux_files), file, idx))

                # read data
                fname = '{0}_DIT{1:03d}_preproc'.format(file, idx)
                files = glob.glob(os.path.join(path.preproc, fname+'.fits'))
                cube = fits.getdata(files[0])
                centers = fits.getdata(os.path.join(path.preproc, fname+'_centers.fits'))
                                
                # DIT, angles, etc
                DIT = frames_info.loc[(file, idx), 'DET SEQ1 DIT']
                psf_posang[file_idx] = frames_info.loc[(file, idx), 'INS4 DROT2 POSANG'] + 90

                # center 
                for field_idx, img in enumerate(cube):
                    # wavelength solution for this field
                    ciwave = iwave[:, field_idx]

                    if correct_mrs_chromatism and (filter_comb == 'S_MR'):
                        img = img.astype(np.float)
                        for wave_idx, widx in enumerate(ciwave):
                            cx = centers[widx, field_idx]
                            
                            line = img[widx, :]
                            nimg = imutils.shift(line, cc-cx, method=shift_method)
                            nimg = nimg / DIT
                            
                            psf_cube[field_idx, file_idx, wave_idx] = nimg[:psf_dim]
                    else:
                        if skip_center:
                            cx = centers_default[field_idx]
                        else:
                            cx = centers[ciwave, field_idx].mean()
                    
                        img  = img.astype(np.float)
                        nimg = imutils.shift(img, (cc-cx, 0), method=shift_method)
                        nimg = nimg / DIT

                        psf_cube[field_idx, file_idx] = nimg[ciwave, :psf_dim]
                
                    # neutral density
                    cwave  = final_wave[:, field_idx] 
                    ND = frames_info.loc[(file, idx), 'INS4 FILT2 NAME']
                    w, attenuation = transmission.transmission_nd(ND, wave=cwave)
                    psf_cube[field_idx, file_idx] = (psf_cube[field_idx, file_idx].T / attenuation).T

            if split_posang:
                pas = np.unique(psf_posang)
                for pa in pas:
                    ii = np.where(psf_posang == pa)[0]
                    
                    # save metadata
                    flux_files[(flux_files['INS4 DROT2 POSANG'] + 90) == pa].to_csv(os.path.join(path.products, 'psf_posang={:06.2f}_frames.csv'.format(pa)))
                    fits.writeto(os.path.join(path.products, 'psf_posang={:06.2f}_posang.fits'.format(pa)), psf_posang[ii], overwrite=True)

                    # save final cubes
                    fits.writeto(os.path.join(path.products, 'psf_posang={:06.2f}_cube.fits'.format(pa)), psf_cube[:, ii], overwrite=True)
            else:
                # save metadata
                flux_files.to_csv(os.path.join(path.products, 'psf_posang=all_frames.csv'))
                fits.writeto(os.path.join(path.products, 'psf_posang=all_posang.fits'), psf_posang, overwrite=True)

                # save final cubes
                fits.writeto(os.path.join(path.products, 'psf_posang=all_cube.fits'), psf_cube, overwrite=True)

            # delete big cubes
            del psf_cube

            print()

        #
        # OBJECT,CENTER
        #
        starcen_files = frames_info[frames_info['DPR TYPE'] == 'OBJECT,CENTER']
        nfiles = len(starcen_files)
        if nfiles != 0:
            print(' * OBJECT,CENTER data')

            # final arrays
            cen_cube   = np.zeros((2, nfiles, nwave, science_dim))
            cen_posang = np.zeros(nfiles)

            # final center
            if cpix:
                cc = science_dim // 2
            else:
                cc = (science_dim - 1) / 2

            # read and combine files
            for file_idx, (file, idx) in enumerate(starcen_files.index):
                print('  ==> file {0}/{1}: {2}, DIT={3}'.format(file_idx+1, len(starcen_files), file, idx))

                # read data
                fname = '{0}_DIT{1:03d}_preproc'.format(file, idx)
                files = glob.glob(os.path.join(path.preproc, fname+'.fits'))
                cube = fits.getdata(files[0])
                centers = fits.getdata(os.path.join(path.preproc, fname+'_centers.fits'))

                # DIT, angles, etc
                DIT = frames_info.loc[(file, idx), 'DET SEQ1 DIT']
                cen_posang[file_idx] = frames_info.loc[(file, idx), 'INS4 DROT2 POSANG'] + 90

                # center 
                for field_idx, img in enumerate(cube):                    
                    # wavelength solution for this field
                    ciwave = iwave[:, field_idx]

                    if correct_mrs_chromatism and (filter_comb == 'S_MR'):
                        img = img.astype(np.float)
                        for wave_idx, widx in enumerate(ciwave):
                            cx = centers[widx, field_idx]
                            
                            line = img[widx, :]
                            nimg = imutils.shift(line, cc-cx, method=shift_method)
                            nimg = nimg / DIT
                            
                            cen_cube[field_idx, file_idx, wave_idx] = nimg[:science_dim]
                    else:
                        if skip_center:
                            cx = centers_default[field_idx]
                        else:
                            cx = centers[ciwave, field_idx].mean()
                    
                        img  = img.astype(np.float)
                        nimg = imutils.shift(img, (cc-cx, 0), method=shift_method)
                        nimg = nimg / DIT

                        cen_cube[field_idx, file_idx] = nimg[ciwave, :science_dim]
            
                    # neutral density
                    cwave  = final_wave[:, field_idx] 
                    ND = frames_info.loc[(file, idx), 'INS4 FILT2 NAME']
                    w, attenuation = transmission.transmission_nd(ND, wave=cwave)
                    cen_cube[field_idx, file_idx] = (cen_cube[field_idx, file_idx].T / attenuation).T
                    
            if split_posang:
                pas = np.unique(cen_posang)
                for pa in pas:
                    ii = np.where(cen_posang == pa)[0]
                    
                    # save metadata
                    starcen_files[(starcen_files['INS4 DROT2 POSANG'] + 90) == pa].to_csv(os.path.join(path.products, 'starcenter_posang={:06.2f}_frames.csv'.format(pa)))
                    fits.writeto(os.path.join(path.products, 'starcenter_posang={:06.2f}_posang.fits'.format(pa)), cen_posang[ii], overwrite=True)

                    # save final cubes
                    fits.writeto(os.path.join(path.products, 'starcenter_posang={:06.2f}_cube.fits'.format(pa)), cen_cube[:, ii], overwrite=True)
            else:
                # save metadata
                starcen_files.to_csv(os.path.join(path.products, 'starcenter_posang=all_frames.csv'))
                fits.writeto(os.path.join(path.products, 'starcenter_posang=all_posang.fits'), cen_posang, overwrite=True)

                # save final cubes
                fits.writeto(os.path.join(path.products, 'starcenter_posang=all_cube.fits'), cen_cube, overwrite=True)

            # delete big cubes
            del cen_cube

            print()

        #
        # OBJECT
        #
        object_files = frames_info[frames_info['DPR TYPE'] == 'OBJECT']
        nfiles = len(object_files)
        if nfiles != 0:
            print(' * OBJECT data')

            # final arrays
            sci_cube   = np.zeros((2, nfiles, nwave, science_dim))
            sci_posang = np.zeros(nfiles)

            # FIXME: ticket #12. Use first DIT of first OBJECT,CENTER
            # in the sequence, but it would be better to be able to
            # select which CENTER to use
            starcen_files = frames_info[frames_info['DPR TYPE'] == 'OBJECT,CENTER']
            if (len(starcen_files) == 0) or skip_center or (manual_center is not None):
                print('Warning: no OBJECT,CENTER file in the data set. Images cannot be accurately centred. ' +
                      'They will just be combined.')

                # choose between manual center or default centers
                if manual_center is not None:
                    centers = manual_center
                else:
                    centers = centers_default
            else:
                fname = '{0}_DIT{1:03d}_preproc_centers.fits'.format(starcen_files.index.values[0][0], starcen_files.index.values[0][1])
                centers = fits.getdata(os.path.join(path.preproc, fname))
            
            # final center
            if cpix:
                cc = science_dim // 2
            else:
                cc = (science_dim - 1) / 2

            # read and combine files
            for file_idx, (file, idx) in enumerate(object_files.index):
                print('  ==> file {0}/{1}: {2}, DIT={3}'.format(file_idx+1, len(object_files), file, idx))

                # read data
                fname = '{0}_DIT{1:03d}_preproc'.format(file, idx)
                files = glob.glob(os.path.join(path.preproc, fname+'.fits'))
                cube = fits.getdata(files[0])

                # DIT, angles, etc
                DIT = frames_info.loc[(file, idx), 'DET SEQ1 DIT']
                sci_posang[file_idx] = frames_info.loc[(file, idx), 'INS4 DROT2 POSANG'] + 90

                # center 
                for field_idx, img in enumerate(cube):                    
                    # wavelength solution for this field
                    ciwave = iwave[:, field_idx]

                    if correct_mrs_chromatism and (filter_comb == 'S_MR'):
                        img = img.astype(np.float)
                        for wave_idx, widx in enumerate(ciwave):
                            cx = centers[widx, field_idx]
                            
                            line = img[widx, :]
                            nimg = imutils.shift(line, cc-cx, method=shift_method)
                            nimg = nimg / DIT
                            
                            sci_cube[field_idx, file_idx, wave_idx] = nimg[:science_dim]
                    else:
                        if skip_center:
                            cx = centers_default[field_idx]
                        else:
                            cx = centers[ciwave, field_idx].mean()
                    
                        img  = img.astype(np.float)
                        nimg = imutils.shift(img, (cc-cx, 0), method=shift_method)
                        nimg = nimg / DIT

                        sci_cube[field_idx, file_idx] = nimg[ciwave, :science_dim]
            
                    # neutral density
                    cwave  = final_wave[:, field_idx] 
                    ND = frames_info.loc[(file, idx), 'INS4 FILT2 NAME']
                    w, attenuation = transmission.transmission_nd(ND, wave=cwave)
                    sci_cube[field_idx, file_idx] = (sci_cube[field_idx, file_idx].T / attenuation).T
                    
            if split_posang:
                pas = np.unique(sci_posang)
                for pa in pas:
                    ii = np.where(sci_posang == pa)[0]
                    
                    # save metadata
                    object_files[(object_files['INS4 DROT2 POSANG'] + 90) == pa].to_csv(os.path.join(path.products, 'science_posang={:06.2f}_frames.csv'.format(pa)))
                    fits.writeto(os.path.join(path.products, 'science_posang={:06.2f}_posang.fits'.format(pa)), sci_posang[ii], overwrite=True)

                    # save final cubes
                    fits.writeto(os.path.join(path.products, 'science_posang={:06.2f}_cube.fits'.format(pa)), sci_cube[:, ii], overwrite=True)
            else:
                # save metadata
                object_files.to_csv(os.path.join(path.products, 'science_posang=all_frames.csv'))
                fits.writeto(os.path.join(path.products, 'science_posang=all_posang.fits'), sci_posang, overwrite=True)

                # save final cubes
                fits.writeto(os.path.join(path.products, 'science_posang=all_cube.fits'), sci_cube, overwrite=True)

            # delete big cubes
            del sci_cube

            print()        

        # update recipe execution
        self._recipe_execution['sph_ird_combine_data'] = True

    
    def sph_ird_clean(self, delete_raw=False, delete_products=False):
        '''
        Clean everything except for raw data and science products (by default)

        Parameters
        ----------
        delete_raw : bool
            Delete raw data. Default is False

        delete_products : bool
            Delete science products. Default is False
        '''

        # parameters
        path = self._path
                
        # tmp
        if os.path.exists(path.tmp):
            shutil.rmtree(path.tmp, ignore_errors=True)

        # sof
        if os.path.exists(path.sof):
            shutil.rmtree(path.sof, ignore_errors=True)

        # calib
        if os.path.exists(path.calib):
            shutil.rmtree(path.calib, ignore_errors=True)

        # preproc
        if os.path.exists(path.preproc):
            shutil.rmtree(path.preproc, ignore_errors=True)

        # raw
        if delete_raw:
            if os.path.exists(path.raw):
                shutil.rmtree(path.raw, ignore_errors=True)

        # products
        if delete_products:
            if os.path.exists(path.products):
                shutil.rmtree(path.products, ignore_errors=True)
