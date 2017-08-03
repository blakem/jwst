from __future__ import (division, print_function, unicode_literals,
    absolute_import)

import time
import numpy as np
from collections import OrderedDict

from .. import datamodels

from . import gwcs_drizzle
from . import bitmask
from . import resample_utils
from . import blend

import logging
log = logging.getLogger(__name__)
log.setLevel(logging.DEBUG)


class ResampleData(object):
    """
    This is the controlling routine for the resampling process.
    It loads and sets the various input data and parameters needed by
    the drizzle function and then calls the C-based cdriz.tdriz function
    to do the actual resampling.

    Notes
    -----
    This routine performs the following operations::

      1. Extracts parameter settings from input model, such as pixfrac,
         weight type, exposure time (if relevant), and kernel, and merges
         them with any user-provided values.
      2. Creates output WCS based on input images and define mapping function
         between all input arrays and the output array.
      3. Initializes all output arrays, including WHT and CTX arrays.
      4. Passes all information for each input chip to drizzle function.
      5. Updates output data model with output arrays from drizzle, including
         (eventually) a record of metadata from all input models.
    """
    drizpars = {'single': False,
                'kernel': 'square',
                'pixfrac': 1.0,
                'good_bits': 4,
                'fillval': 'INDEF',
                'wht_type': 'exptime',
                'blendheaders': True}

    def __init__(self, input_models, output=None, ref_filename=None, **pars):
        """
        Parameters
        ----------
        input_models : list of objects
            list of data models, one for each input image

        output : str
            filename for output
        """
        self.input_models = input_models
        if output is None:
            output = input_models.meta.resample.output
        self.output_filename = output
        self.ref_filename = ref_filename


        # If user specifies use of drizpars ref file (default for pipeline use)
        # update input parameters with default values from ref file
        if self.ref_filename is not None:
            self.get_drizpars()
        self.drizpars.update(pars)

        # Define output WCS based on all inputs, including a reference WCS
        self.output_wcs = resample_utils.make_output_wcs(self.input_models)
        log.debug('Output mosaic size: {}'.format(self.output_wcs.data_size))
        self.blank_output = datamodels.DrizProductModel(self.output_wcs.data_size)
        self.blank_output.meta.wcs = self.output_wcs

        self.blank_output.meta = self.input_models[0].meta
        self.output_models = datamodels.ModelContainer()


    def get_drizpars(self):
        """ Extract drizzle parameters from reference file
        """
        # start by interpreting input data models to define selection criteria
        num_groups = len(self.input_models.group_names)
        input_dm = self.input_models[0]
        filtname = input_dm.meta.instrument.filter

        # Create a data model for the reference file
        ref_model = datamodels.DrizParsModel(self.ref_filename)
        # look for row that applies to this set of input data models
        # NOTE:
        #  This logic could be replaced by a method added to the DrizParsModel object
        #  to select the correct row based on a set of selection parameters
        row = None
        drizpars = ref_model.drizpars_table

        filter_match = False # flag to support wild-card rows in drizpars table
        for n, filt, num in zip(range(0, len(drizpars)), drizpars.filter,
            drizpars.numimages):
            # only remember this row if no exact match has already been made for
            # the filter. This allows the wild-card row to be anywhere in the
            # table; since it may be placed at beginning or end of table.

            if filt == b"ANY" and not filter_match and num_groups >= num:
                row = n
            # always go for an exact match if present, though...
            if filtname == filt and num_groups >= num:
                row = n
                filter_match = True

        # With presence of wild-card rows, code should never trigger this logic
        if row is None:
            log.error("No row found in %s matching input data.", self.ref_filename)
            raise ValueError

        # read in values from that row for each parameter
        for kw in self.drizpars:
            if kw in drizpars.names:
                self.drizpars[kw] = getattr(drizpars, kw)[row]

    def update_driz_outputs(self):
        """ Define output arrays for use with drizzle operations.
        """
        numchips = len(self.input_models) # assuming 1 chip per model
        numplanes = (numchips // 32) + 1

        # Replace CONTEXT array with full set of planes needed for all inputs
        outcon = np.zeros((numplanes, self.output_wcs.data_size[0],
                                    self.output_wcs.data_size[1]), dtype=np.int32)
        self.blank_output.con = outcon


    def blend_output_metadata(self, output_model):
        """ Create new output metadata based on blending all input metadata
        """
        # Run fitsblender on output product
        input_list = [i.meta.filename for i in self.input_models]
        log.debug('Blending metadata for {}'.format(output_model.meta.filename))
        blend.blendfitsdata(input_list, output_model)


    def do_drizzle(self, **pars):
        """ Perform drizzling operation on input images's to create a new output
        """
        # Set up information about what outputs we need to create: single or final
        # Key: value from metadata for output/observation name
        # Value: full filename for output file
        driz_outputs = OrderedDict()

        # Look for input configuration parameter telling the code to run
        # in single-drizzle mode (mosaic all detectors in a single observation)
        if self.drizpars['single']:
            driz_outputs = self.input_models.group_names
            exposures = self.input_models.models_grouped
            group_exptime = []
            for exposure in exposures:
                group_exptime.append(exposure[0].meta.exposure.exposure_time)
        else:
            driz_outputs = [self.output_filename]
            exposures = [self.input_models]

            total_exposure_time = 0.0
            for exposure in exposures:
                total_exposure_time += exposure[0].meta.exposure.exposure_time
            group_exptime = [total_exposure_time]
        pointings = len(self.input_models.group_names)

        for obs_product, exposure, texptime in zip(driz_outputs, exposures,
            group_exptime):
            output_model = self.blank_output.copy()
            output_model.meta.filename = obs_product

            if self.drizpars['blendheaders']:
                self.blend_output_metadata(output_model)

            # Following 2 lines can probably be removed once ASN dicts
            # are handled properly
            output_model.meta.asn.pool_name = self.input_models.meta.pool_name
            output_model.meta.asn.table_name = self.input_models.meta.table_name

            exposure_times = {'start': [], 'end': []}

            # Initialize the output with the wcs
            driz = gwcs_drizzle.GWCSDrizzle(output_model,
                                single=self.drizpars['single'],
                                pixfrac=self.drizpars['pixfrac'],
                                kernel=self.drizpars['kernel'],
                                fillval=self.drizpars['fillval'])

            for n, img in enumerate(exposure):
                exposure_times['start'].append(img.meta.exposure.start_time)
                exposure_times['end'].append(img.meta.exposure.end_time)

                # apply sky subtraction 
                if 'skybg' in img.meta._instance:
                    img.data -= img.meta.skybg
                    
                outwcs_pscale = output_model.meta.wcsinfo.cdelt1
                wcslin_pscale = img.meta.wcsinfo.cdelt1

                inwht = build_driz_weight(img, wht_type=self.drizpars['wht_type'],
                                    good_bits=self.drizpars['good_bits'])
                driz.add_image(img.data, img.meta.wcs, inwht=inwht,
                        expin=img.meta.exposure.exposure_time,
                        pscale_ratio=outwcs_pscale / wcslin_pscale)

            # Update some basic exposure time values based on all the inputs
            output_model.meta.exposure.exposure_time = texptime
            output_model.meta.exposure.start_time = min(exposure_times['start'])
            output_model.meta.exposure.end_time = max(exposure_times['end'])
            output_model.meta.resample.product_exposure_time = texptime
            output_model.meta.resample.product_data_extname = driz.sciext
            output_model.meta.resample.product_context_extname = driz.conext
            output_model.meta.resample.product_weight_extname = driz.whtext
            output_model.meta.resample.drizzle_fill_value = str(driz.fillval)
            output_model.meta.resample.drizzle_pixel_fraction = driz.pixfrac
            output_model.meta.resample.drizzle_kernel = driz.kernel
            output_model.meta.resample.drizzle_output_units = driz.out_units
            output_model.meta.resample.drizzle_weight_scale = driz.wt_scl
            output_model.meta.resample.resample_bits = self.drizpars['good_bits']
            output_model.meta.resample.weight_type = self.drizpars['wht_type']
            output_model.meta.resample.pointings = pointings

            self.output_models.append(output_model)


def _buildMask(dqarr, bitvalue):
    """ Builds a bit-mask from an input DQ array and a bitvalue flag"""

    bitvalue = bitmask.interpret_bits_value(bitvalue)

    if bitvalue is None:
        return (np.ones(dqarr.shape, dtype=np.uint8))
    return np.logical_not(np.bitwise_and(dqarr, ~bitvalue)).astype(np.uint8)


def build_driz_weight(model, wht_type=None, good_bits=None):
    """ Create input weighting image based on user inputs

    """
    if good_bits is not None and good_bits < 0: good_bits = None
    dqmask = _buildMask(model.dq, good_bits)
    exptime = model.meta.exposure.exposure_time

    if wht_type.lower()[:3] == 'err':
        # Multiply the scaled ERR file by the input mask in place.
        inwht = (exptime / model.err)**2 * dqmask
    #elif wht_type == 'IVM':
    #    _inwht = img.buildIVMmask(chip._chip,dqarr,pix_ratio)
    elif wht_type.lower()[:3] == 'exp':# or wht_type is None, as used for single=True
        inwht = exptime * dqmask
    else:
        # Create an identity input weight map
        inwht = np.ones(model.data.shape, dtype=model.data.dtype)
    return inwht
