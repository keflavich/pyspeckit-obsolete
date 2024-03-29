"""
Cubes
=====

Tools to deal with spectroscopic data cubes.  


Many features in Cubes require additional packages:

   * smoothing - requires agpy_\'s smooth and parallel_map routines
   * `coords <http://stsdas.stsci.edu/astrolib/coords-0.37.tar.gz>`_ (`homepage <http://www.scipy.org/AstroLibCoordsHome>`_)
   * `pyregion <git://github.com/leejjoon/pyregion.git>`_
   * `pywcs <git://github.com/astropy/astropy.git>`_


In the near future, the coords_ requirement will be replaced with `astropy
<http://www.astropy.org>`_\'s coordinates module.
    
The 'grunt work' is performed by the :py:mod:`cubes` module


"""
# import parent package
import pyspeckit
from pyspeckit import spectrum
# import local things
import mapplot
import readers
import time
import numpy as np
from pyspeckit.parallel_map import parallel_map
import types
import copy
import itertools
from pyspeckit.spectrum import history
from astropy.io import fits

class Cube(spectrum.Spectrum):

    def __init__(self, filename=None, xarr=None, cube=None, errorcube=None,
                 header=None, x0=0, y0=0, maskfilename=None, maskmap=None,
                 **kwargs):
        """
        Initialize the Cube.  Accepts files in the following formats:
            
          * .fits

        Alternatively, you can specify the *xarr*, *cube*, and *header* kwargs.
        If nothing is specified, a blank :Cube: will be generated.

        x0,y0 - initial spectrum to use (defaults to lower-left corner)
        """

        if filename is not None:
            try:
                self.cube,self.xarr,self.header,self.fitsfile = readers.open_3d_fits(filename, **kwargs)
                self.errorcube = errorcube
                self.data = self.cube[:,y0,x0]
                self.error = None
                self.cubeheader = self.header
            except TypeError as inst:
                print "Failed to read fits file: wrong TYPE."
                print inst
                raise inst
        else:
            self.cube = cube
            self.errorcube = errorcube
            self.xarr = xarr
            self.header = header
            self.error = None
            if self.cube is not None:
                self.data = self.cube[:,y0,x0]

        if self.header is not None:
            self.parse_header(self.header)
        else:
            self.units = 'undefined'

        if maskmap is not None:
            self.maskmap = maskmap
        else:
            self.maskmap = np.ones(self.cube.shape[1:],dtype='bool')

        if isinstance(filename,str):
            self.fileprefix = filename.rsplit('.', 1)[0]    # Everything prior to .fits or .txt
        else:
            self.fileprefix = "pyfitsHDU"

        self.plotter = spectrum.plotters.Plotter(self)
        self._register_fitters()
        self.specfit = spectrum.fitters.Specfit(self,Registry=self.Registry)
        self.baseline = spectrum.baseline.Baseline(self)
        self.speclines = spectrum.speclines
        # Initialize writers
        self.writer = {}
        for writer in spectrum.writers.writers:
            self.writer[writer] = spectrum.writers.writers[writer](self)

        # Special.  This needs to be modified to be more flexible; for now I need it to work for nh3
        self.plot_special = None
        self.plot_special_kwargs = {}
        self._modelcube = None

        # TODO: improve this!!!
        self.system = 'galactic' if 'GLON' in self.header['CTYPE1'] else 'celestial'

        self.mapplot = mapplot.MapPlotter(self)

    def __repr__(self):
        return r'<Cube object over spectral range %6.5g : %6.5g %s and flux range = [%2.1f, %2.1f] %s with shape %r at %s>' % \
                (self.xarr.min(), self.xarr.max(), self.xarr.units,
                        self.data.min(), self.data.max(), self.units,
                        self.cube.shape, str(hex(self.__hash__())))


    def copy(self,deep=True):
        """
        Create a copy of the spectrum with its own plotter, fitter, etc.
        Useful for, e.g., comparing smoothed to unsmoothed data
        """

        newcube = copy.copy(self)
        if deep:
            newcube.xarr = copy.copy(self.xarr)
            newcube.data = copy.copy(self.data)
            if self.error is not None:
                newcube.error = copy.copy(self.error)

        newcube.header = copy.copy(self.header)
        newcube.plotter = self.plotter.copy(parent=newcube)
        newcube._register_fitters()
        newcube.specfit = self.specfit.copy(parent=newcube)
        newcube.specfit.Spectrum.plotter = newcube.plotter
        newcube.baseline = self.baseline.copy(parent=newcube)
        newcube.baseline.Spectrum.plotter = newcube.plotter

        newcube.mapplot = self.mapplot.copy(parent=newcube)
        newcube.mapplot.Cube = newcube

        return newcube

    def slice(self, start=None, stop=None, units='pixel', preserve_fits=False, copy=True):
        """Slicing the spectrum
        
        Parameters:
        -----------
        
        *start* [ numpy.float or int ]
            start of slice
        *stop*  [ numpy.float or int ]
            stop of slice
        *units* [ str ]
            allowed values are any supported physical unit, 'pixel'
        """
        
        x_in_units = self.xarr.as_unit(units)
        start_ind = x_in_units.x_to_pix(start)
        stop_ind  = x_in_units.x_to_pix(stop)
        spectrum_slice = slice(start_ind,stop_ind)

        if not copy:
            raise NotImplementedError("Must copy when slicing a cube.")
        newcube = self.copy()
        newcube.cube = newcube.cube[spectrum_slice,:,:]
        if hasattr(newcube,'errcube'):
            newcube.errcube = newcube.errcube[spectrum_slice,:,:]
        newcube.data = newcube.data[spectrum_slice]
        if newcube.error is not None:
            newcube.error = newcube.error[spectrum_slice]
        newcube.xarr = newcube.xarr[spectrum_slice]
        
        # create new specfit / baseline instances (otherwise they'll be the wrong length)
        newcube._register_fitters()
        newcube.baseline = spectrum.baseline.Baseline(newcube)
        newcube.specfit = spectrum.fitters.Specfit(newcube,Registry=newcube.Registry)

        if preserve_fits:
            newcube.specfit.modelpars = self.specfit.modelpars
            newcube.specfit.parinfo = self.specfit.parinfo
            newcube.baseline.baselinepars = self.baseline.baselinepars
            newcube.baseline.order = self.baseline.order

        return newcube
    

    def __getitem__(self, indx):
        """
        If [] is used on a cube, slice on the cube and use
        the first dimension to slice on the xarr and the data
        """

        newcube = copy.copy(self)
        newcube.cube = newcube.cube.__getitem__(indx)
        if hasattr(newcube,'errcube'):
            newcube.errcube = newcube.errcube.__getitem__(indx)
        newcube.data = newcube.data.__getitem__(indx[0])
        if newcube.error is not None:
            newcube.error = newcube.error.__getitem__(indx[0])
        newcube.xarr = newcube.xarr.__getitem__(indx[0])
        
        # this should be done by deepcopy, but deepcopy fails with current pyfits
        newcube.plotter = copy.copy(self.plotter)
        newcube.plotter.Spectrum = newcube
        newcube.specfit = copy.copy(self.specfit)
        newcube.specfit.Spectrum = newcube
        newcube.specfit.Spectrum.plotter = newcube.plotter
        newcube.baseline = copy.copy(self.baseline)
        newcube.baseline.Spectrum = newcube
        newcube.baseline.Spectrum.plotter = newcube.plotter
        newcube.mapplot = copy.copy(self.mapplot)
        newcube.mapplot.Cube = newcube

        return newcube

    def plot_spectrum(self, x, y, plot_fit=False, **kwargs):
        """
        Fill the .data array with a real spectrum and plot it
        """

        self.data = self.cube[:,y,x]
        if self.errorcube is not None:
            self.error = self.errorcube[:,y,x]
        if self.plot_special is None:
            self.plotter(**kwargs)
            if plot_fit: self.plot_fit(x,y)
            self.plotted_spectrum = self
        else:
            sp = self.get_spectrum(x,y)
            sp.plot_special = types.MethodType(self.plot_special, sp, sp.__class__)
            self._spdict = sp.plot_special(**dict(kwargs.items()+
                                                  self.plot_special_kwargs.items()))
            self.plotted_spectrum = sp
            self.plotter = sp.plotter
            self.plotter.refresh = lambda: [spi.plotter.refresh()
                                            for spi in self._spdict.values()]
            self.specfit.modelplot = [comp
                                      for spi in self._spdict.values()
                                      for comp in spi.specfit.modelplot]
            self.specfit._plotted_components = [comp
                                                for spi in self._spdict.values()
                                                for comp in spi.specfit._plotted_components]

    def plot_fit(self, x, y, silent=False, **kwargs):
        """
        If fiteach has been run, plot the best fit
        """
        if not hasattr(self,'parcube'):
            if not silent: print "Must run fiteach before plotting a fit.  If you want to fit a single spectrum, use plot_spectrum() and specfit() directly."
            return

        if self.plot_special is not None:
            # don't try to overplot a fit on a "special" plot
            return

        self.data = self.cube[:,y,x]
        if self.errorcube is not None:
            self.error = self.errorcube[:,y,x]

        self.specfit.modelpars = self.parcube[:,y,x]
        self.specfit.npeaks = self.specfit.fitter.npeaks
        self.specfit.model = self.specfit.fitter.n_modelfunc(self.specfit.modelpars, **self.specfit.fitter.modelfunc_kwargs)(self.xarr)

        # set the parinfo values correctly for annotations
        for pi,p,e in zip(self.specfit.parinfo, self.specfit.modelpars, self.errcube[:,y,x]):
            try:
                pi['value'] = p
                pi['error'] = e
            except ValueError:
                # likely to happen for failed fits
                pass

        self.specfit.plot_fit(**kwargs)

    def plot_apspec(self, aperture, coordsys=None, reset_ylimits=True,
                    method='mean', **kwargs):
        """
        Extract an aperture using cubes.extract_aperture
        (defaults to Cube coordinates)

        Parameters
        ----------
        aperture : list
            A list of aperture parameters, e.g. 
             * For a circular aperture, len(ap)=3:
               + ``ap = [xcen,ycen,radius]``
             * For an elliptical aperture, len(ap)=5:
               + ``ap = [xcen,ycen,height,width,PA]``
        coordsys : None or str
            The coordinate system of the aperture (e.g., galactic, fk5, None
            for pixel)
        method : 'mean' or 'sum'
            Either average over parellel spectra or sum them.
        """


        if self.plot_special is None:
            self.set_apspec(aperture, coordsys=coordsys, method=method)
            self.plotter(reset_ylimits=reset_ylimits, **kwargs)
        else:
            #self.plot_special(reset_ylimits=reset_ylimits, **dict(kwargs.items()+self.plot_special_kwargs.items()))

            sp = self.get_apspec(aperture, coordsys=coordsys, method=method)
            sp.plot_special = types.MethodType(self.plot_special, sp, sp.__class__)
            sp.plot_special(reset_ylimits=reset_ylimits, **dict(kwargs.items()+self.plot_special_kwargs.items()))

    def get_spectrum(self, x, y):
        """
        Very simple: get the spectrum at coordinates x,y

        (inherits fitter from self)

        Returns a SpectroscopicAxis instance
        """

        header = cubes.speccen_header(fits.Header(cards=[(k,v) for k,v in
                                                         self.header.iteritems()
                                                         if k != 'HISTORY']),
                                      lon=x, lat=y, system=self.system,
                                      proj=self.header['CTYPE1'][-3:])

        sp = pyspeckit.Spectrum( xarr=self.xarr.copy(), data=self.cube[:,y,x],
                header=header,
                error=self.errorcube[:,y,x] if self.errorcube is not None else None)

        sp.specfit = copy.copy(self.specfit)
        # explicitly re-do this (test)
        sp.specfit.includemask = self.specfit.includemask.copy()
        sp.specfit.Spectrum = sp

        if hasattr(self,'parcube'):
            sp.specfit.modelpars = self.parcube[:,y,x]
            sp.specfit.fitter.mpp = sp.specfit.modelpars # also for annotations (differs depending on which function... sigh... need to unify)
            if hasattr(self.specfit,'parinfo') and self.specfit.parinfo is not None:
                # set the parinfo values correctly for annotations
                for pi,p,e in zip(sp.specfit.parinfo, sp.specfit.modelpars, self.errcube[:,y,x]):
                    try:
                        pi['value'] = p
                        pi['error'] = e
                    except ValueError:
                        pass

            if hasattr(self.specfit,'fitter') and self.specfit.fitter is not None:
                sp.specfit.npeaks = self.specfit.fitter.npeaks
                sp.specfit.fitter.npeaks = len(sp.specfit.modelpars) / sp.specfit.fitter.npars
                sp.specfit.fitter.parinfo = sp.specfit.parinfo
                sp.specfit.model = sp.specfit.fitter.n_modelfunc(sp.specfit.modelpars,**sp.specfit.fitter.modelfunc_kwargs)(sp.xarr)

        return sp

    def get_apspec(self, aperture, coordsys=None, method='mean', **kwargs):
        """
        Extract an aperture using cubes.extract_aperture
        (defaults to Cube coordinates)

        *aperture* [tuple or list] (x, y, radius)
            The aperture to use when extracting the data

        *coordsys* [ 'celestial' | 'galactic' | None]
            the coordinate system the aperture is specified in
            None indicates pixel coordinates (default)

        """

        import cubes
        if coordsys is not None:
            wcs = self.mapplot.wcs
        else:
            wcs = None

        data = cubes.extract_aperture(self.cube, aperture,
                                      coordsys=coordsys,
                                      wcs=wcs,
                                      method=method,
                                      **kwargs)
        if self.errorcube is not None:
            error = cubes.extract_aperture(self.errorcube, aperture,
                                           coordsys=coordsys,
                                           wcs=self.mapplot.wcs,
                                           method='error', **kwargs)
        else:
            error = None

        header = cubes.speccen_header(fits.Header(cards=[(k,v) for k,v in
                                                         self.header.iteritems()
                                                         if k != 'HISTORY']),
                                      lon=aperture[0],
                                      lat=aperture[1],
                                      system=self.system,
                                      proj=self.header['CTYPE1'][-3:])
        header['APRADIUS'] = aperture[2]

        sp = pyspeckit.Spectrum(xarr=self.xarr.copy(),
                                data=data,
                                error=error,
                                header=header)

        sp.specfit = copy.copy(self.specfit)
        sp.specfit.includemask = self.specfit.includemask.copy()
        sp.specfit.Spectrum = sp

        return sp

    def set_apspec(self, aperture, coordsys=None, method='mean'):
        """
        Extract an aperture using cubes.extract_aperture
        (defaults to Cube coordinates)
        """

        import cubes
        if coordsys is not None:
            self.data = cubes.extract_aperture( self.cube, aperture , coordsys=coordsys , wcs=self.mapplot.wcs, method=method )
        else:
            self.data = cubes.extract_aperture( self.cube, aperture , coordsys=None, method=method)

    def get_modelcube(self, update=False):
        if self._modelcube is None or update:
            yy,xx = np.indices(self.mapplot.plane.shape)
            self._modelcube = np.zeros_like(self.cube)
            for x,y in zip(xx.flat,yy.flat):
                self._modelcube[:,y,x] = self.specfit.get_full_model(pars=self.parcube[:,y,x])

        return self._modelcube


    def fiteach(self, errspec=None, errmap=None, guesses=(), verbose=True,
                verbose_level=1, quiet=True, signal_cut=3, usemomentcube=False,
                blank_value=0, integral=True, direct=False, absorption=False,
                use_nearest_as_guess=False, start_from_point=(0,0), multicore=0,
                continuum_map=None, **fitkwargs):
        """
        Fit a spectrum to each valid pixel in the cube

        For guesses, priority is *use_nearest_as_guess*, *usemomentcube*,
        *guesses*, None

        Parameters
        ----------
        use_nearest_as_guess: bool
            Unless the fitted point is the first, it will find the nearest
            other point with a successful fit and use its best-fit parameters
            as the guess
        start_from_point: tuple(int,int)
            Either start from the center or from a point defined by a tuple.
            Work outward from that starting point.  
        guesses: tuple or ndarray[naxis=3]
            Either a tuple/list of guesses with len(guesses) = npars or a cube
            of guesses with shape [npars, ny, nx]
        signal_cut: float
            Minimum signal-to-noise ratio to "cut" on (i.e., if peak in a given
            spectrum has s/n less than this value, ignore it)
        blank_value: float
            Value to replace non-fitted locations with.  A good alternative is
            numpy.nan
        verbose: bool 
        verbose_level: int
            Controls how much is output.
            0,1 - only changes frequency of updates in loop
            2 - print out messages when skipping pixels
            3 - print out messages when fitting pixels
            4 - specfit will be verbose 
        multicore: int
            if >0, try to use multiprocessing via parallel_map to run on multiple cores
        continuum_map: np.ndarray
            Same shape as error map.  Subtract this from data before estimating noise.

        """

        if not hasattr(self.mapplot,'plane'):
            self.mapplot.makeplane()

        yy,xx = np.indices(self.mapplot.plane.shape)
        if isinstance(self.mapplot.plane, np.ma.core.MaskedArray): 
            OK = (True-self.mapplot.plane.mask) * self.maskmap
        else:
            OK = np.isfinite(self.mapplot.plane) * self.maskmap

        # NAN guesses rule out the model too
        if hasattr(guesses,'shape') and guesses.shape[1:] == self.cube.shape[1:]:
            bad = np.isnan(guesses).sum(axis=0)
            OK *= (True-bad)

        distance = ((xx)**2 + (yy)**2)**0.5
        if start_from_point == 'center':
            start_from_point = (xx.max()/2., yy.max/2.)
        d_from_start = np.roll( np.roll( distance, start_from_point[0], 0), start_from_point[1], 1)
        sort_distance = np.argsort(d_from_start.flat)

        valid_pixels = zip(xx.flat[sort_distance][OK.flat[sort_distance]], 
                yy.flat[sort_distance][OK.flat[sort_distance]])

        if verbose_level > 0:
            print "Number of valid pixels: %i" % len(valid_pixels)

        if usemomentcube:
            npars = self.momentcube.shape[0]
        else:
            npars = len(guesses)
            if npars == 0:
                raise ValueError("Parameter guesses are required.")

        self.parcube = np.zeros((npars,)+self.mapplot.plane.shape)
        self.errcube = np.zeros((npars,)+self.mapplot.plane.shape) 
        if integral: self.integralmap = np.zeros((2,)+self.mapplot.plane.shape)

        # newly needed as of March 27, 2012.  Don't know why.
        if 'fittype' in fitkwargs: self.specfit.fittype = fitkwargs['fittype']
        self.specfit.fitter = self.specfit.Registry.multifitters[self.specfit.fittype]

        # array to store whether pixels have fits
        self.has_fit = np.zeros(self.mapplot.plane.shape, dtype='bool')

        global counter
        counter = 0

        t0 = time.time()

        def fit_a_pixel(iixy):
            global counter
            ii,x,y = iixy
            sp = self.get_spectrum(x,y)

            # very annoying - cannot use min/max without checking type
            # maybe can use np.asarray here?
            if hasattr(sp.data,'mask'):
                sp.data[sp.data.mask] = np.nan
                sp.error[sp.data.mask] = np.nan
                sp.data = np.array(sp.data)
                sp.error = np.array(sp.error)

            if errspec is not None:
                sp.error = errspec
            elif errmap is not None:
                sp.error = np.ones(sp.data.shape) * errmap[y,x]
            else:
                if verbose_level > 1 and ii==0: print "WARNING: using data std() as error."
                sp.error[:] = sp.data[sp.data==sp.data].std()
            if sp.error is not None and signal_cut > 0:
                if continuum_map is not None:
                    snr = (sp.data-continuum_map[y,x]) / sp.error
                else:
                    snr = sp.data / sp.error
                if absorption:
                    max_sn = np.nanmax(-1*snr)
                else:
                    max_sn = np.nanmax(snr)
                if max_sn < signal_cut:
                    if verbose_level > 1:
                        print "Skipped %4i,%4i (s/n=%0.2g)" % (x,y,max_sn)
                    return
                elif np.isnan(max_sn):
                    if verbose_level > 1:
                        print "Skipped %4i,%4i (s/n is nan; max(data)=%0.2g, min(error)=%0.2g)" % (x,y,np.nanmax(sp.data),np.nanmin(sp.error))
                    return
                if verbose_level > 2:
                    print "Fitting %4i,%4i (s/n=%0.2g)" % (x,y,max_sn)
            else:
                max_sn = None
            sp.specfit.Registry = self.Registry # copy over fitter registry
            
            if use_nearest_as_guess and self.has_fit.sum() > 0:
                if verbose_level > 1 and ii == 0 or verbose_level > 4: print "Using nearest fit as guess"
                d = np.roll( np.roll( distance, x, 0), y, 1)
                # If there's no fit, set its distance to be unreasonably large
                nearest_ind = np.argmin(d+1e10*(True-self.has_fit))
                nearest_x, nearest_y = xx.flat[nearest_ind],yy.flat[nearest_ind]
                gg = self.parcube[:,nearest_y,nearest_x]
            elif usemomentcube:
                if verbose_level > 1 and ii == 0: print "Using moment cube"
                gg = self.momentcube[:,y,x]
            elif hasattr(guesses,'shape') and guesses.shape[1:] == self.cube.shape[1:]:
                if verbose_level > 1 and ii == 0: print "Using input guess cube"
                gg = guesses[:,y,x]
            else:
                if verbose_level > 1 and ii == 0: print "Using input guess"
                gg = guesses

            if np.all(np.isfinite(gg)):
                try:
                    sp.specfit(guesses=gg, quiet=verbose_level<=3, verbose=verbose_level>3, **fitkwargs)
                except Exception as ex:
                    print "Fit number %i at %i,%i failed on error " % (ii,x,y), ex
                    print "Guesses were: ",gg
                    print "Fitkwargs were: ",fitkwargs
                    if isinstance(ex,KeyboardInterrupt):
                        raise ex
                self.parcube[:,y,x] = sp.specfit.modelpars
                self.errcube[:,y,x] = sp.specfit.modelerrs
                if integral: self.integralmap[:,y,x] = sp.specfit.integral(direct=direct,return_error=True)
                self.has_fit[y,x] = True
            else:
                self.has_fit[y,x] = False
                self.parcube[:,y,x] = blank_value
                self.errcube[:,y,x] = blank_value
                if integral: self.integralmap[:,y,x] = blank_value

        
            if blank_value != 0:
                self.parcube[self.parcube == 0] = blank_value
                self.errcube[self.parcube == 0] = blank_value

            counter += 1
            if verbose:
                if ii % (min(10**(3-verbose_level),1)) == 0:
                    snmsg = " s/n=%5.1f" % (max_sn) if max_sn is not None else ""
                    npix = len(valid_pixels)
                    pct = 100 * counter/float(npix)
                    print "Finished fit %6i of %6i at (%4i,%4i)%s. Elapsed time is %0.1f seconds.  %%%01.f" % (counter, npix, x, y, snmsg, time.time()-t0, pct)

            if integral:
                return ((x,y), sp.specfit.modelpars, sp.specfit.modelerrs, self.integralmap[:,y,x])
            else:
                return ((x,y), sp.specfit.modelpars, sp.specfit.modelerrs)

        #### BEGIN TEST BLOCK ####
        # This test block is to make sure you don't run a 30 hour fitting
        # session that's just going to crash at the end.
        # try a first fit for exception-catching
        try0 = fit_a_pixel((0,valid_pixels[0][0],valid_pixels[0][1]))
        assert len(try0[1]) == len(guesses) == len(self.parcube) == len(self.errcube)
        assert len(try0[2]) == len(guesses) == len(self.parcube) == len(self.errcube)

        # This is a secondary test... I'm not sure it's necessary, but it
        # replicates what's inside the fit_a_pixel code and so should be a
        # useful sanity check
        x,y = valid_pixels[0]
        sp = self.get_spectrum(x,y)
        sp.specfit.Registry = self.Registry # copy over fitter registry
        # this reproduced code is needed because the functional wrapping
        # required for the multicore case prevents gg from being set earlier
        if usemomentcube:
            gg = self.momentcube[:,y,x]
        elif hasattr(guesses,'shape') and guesses.shape[1:] == self.cube.shape[1:]:
            gg = guesses[:,y,x]
        else:
            gg = guesses

        # This is NOT in a try/except block because we want to raise the
        # exception here if an exception is going to happen
        sp.specfit(guesses=gg, **fitkwargs)
        #### END TEST BLOCK ####


        if multicore > 0:
            sequence = [(ii,x,y) for ii,(x,y) in tuple(enumerate(valid_pixels))]
            result = parallel_map(fit_a_pixel, sequence, numcores=multicore)
            self._result = result # backup - don't want to lose data in the case of a failure
            # a lot of ugly hacking to deal with the way parallel_map returns
            # its results needs TWO levels of None-filtering, because any
            # individual result can be None (I guess?) but apparently (and this
            # part I don't believe) any individual *fit* result can be None as
            # well (apparently the x,y pairs can also be None?)
            merged_result = [core_result for core_result in result if core_result is not None ]
            # for some reason, every other time I run this code, merged_result
            # ends up with a different intrinsic shape.  This is an attempt to
            # force it to maintain a sensible shape.
            try:
                if integral:
                    ((x,y), m1, m2, intgl) = merged_result[0]
                else:
                    ((x,y), m1, m2) = merged_result[0]
            except ValueError:
                if verbose > 1:
                    print "ERROR: merged_result[0] is ",merged_result[0]," which has the wrong shape"
                merged_result = itertools.chain.from_iterable(merged_result)
            for TEMP in merged_result:
                if TEMP is None:
                    # this shouldn't be possible, but it appears to happen
                    # anyway.  parallel_map is great, up to a limit that was
                    # reached long before this level of complexity
                    continue
                try:
                    if integral:
                        ((x,y), modelpars, modelerrs, intgl) = TEMP
                    else:
                        ((x,y), modelpars, modelerrs) = TEMP
                except TypeError:
                    # implies that TEMP does not have the shape ((a,b),c,d)
                    # as above, shouldn't be possible, but it happens...
                    continue
                if ((len(modelpars) != len(modelerrs)) or
                    (len(modelpars) != len(self.parcube))):
                    raise ValueError("There was a serious problem; modelpar and error shape don't match that of the parameter cubes")
                if np.any(np.isnan(modelpars)) or np.any(np.isnan(modelerrs)):
                    self.parcube[:,y,x] = np.nan
                    self.errcube[:,y,x] = np.nan
                    self.has_fit[y,x] = False
                else:
                    self.parcube[:,y,x] = modelpars
                    self.errcube[:,y,x] = modelerrs
                    self.has_fit[y,x] = max(modelpars) > 0
                if integral:
                    self.integralmap[:,y,x] = intgl
        else:
            for ii,(x,y) in enumerate(valid_pixels):
                fit_a_pixel((ii,x,y))


        # March 27, 2014: This is EXTREMELY confusing.  This isn't in a loop...
        # make sure the fitter / fittype are set for the cube
        # this has to be done within the loop because skipped-over spectra
        # don't ever get their fittypes set
        self.specfit.fitter = sp.specfit.fitter
        self.specfit.fittype = sp.specfit.fittype
        self.specfit.parinfo = sp.specfit.parinfo

        if verbose:
            print "Finished final fit %i.  Elapsed time was %0.1f seconds" % (ii, time.time()-t0)


    def momenteach(self, verbose=True, verbose_level=1, multicore=0, **kwargs):
        """
        Return a cube of the moments of each pixel

        Parameters
        ----------
        multicore: int
            if >0, try to use multiprocessing via parallel_map to run on multiple cores
        """

        if not hasattr(self.mapplot,'plane'):
            self.mapplot.makeplane()

        yy,xx = np.indices(self.mapplot.plane.shape)
        if isinstance(self.mapplot.plane, np.ma.core.MaskedArray): 
            OK = (True-self.mapplot.plane.mask) * self.maskmap
        else:
            OK = np.isfinite(self.mapplot.plane) * self.maskmap
        valid_pixels = zip(xx[OK],yy[OK])

        # run the moment process to find out how many elements are in a moment
        _temp_moment = self.get_spectrum(yy[OK][0],xx[OK][0]).moments(**kwargs)

        self.momentcube = np.zeros((len(_temp_moment),)+self.mapplot.plane.shape)

        t0 = time.time()

        def moment_a_pixel(iixy):
            ii,x,y = iixy
            sp = self.get_spectrum(x,y)
            self.momentcube[:,y,x] = sp.moments(**kwargs)
            if verbose:
                if ii % 10**(3-verbose_level) == 0:
                    print "Finished moment %i.  Elapsed time is %0.1f seconds" % (ii, time.time()-t0)

            return ((x,y), self.momentcube[:,y,x])

        if multicore > 0:
            sequence = [(ii,x,y) for ii,(x,y) in tuple(enumerate(valid_pixels))]
            result = parallel_map(moment_a_pixel, sequence, numcores=multicore)
            merged_result = [core_result for core_result in result if core_result is not None]
            for mr in merged_result:
                for TEMP in mr:
                    ((x,y), moments) = TEMP
                    self.momentcube[:,y,x] = moments
        else:
            for ii,(x,y) in enumerate(valid_pixels):
                moment_a_pixel((ii,x,y))

        if verbose:
            print "Finished final moment %i.  Elapsed time was %0.1f seconds" % (ii, time.time()-t0)

    def show_moment(self, momentnumber, **kwargs):
        """
        If moments have been computed, display them in the mapplot window
        """

        if not hasattr(self,'momentcube'):
            print "Compute moments first"
            return

        self.mapplot.plane = self.momentcube[momentnumber,:,:].squeeze()

        self.mapplot(estimator=None, **kwargs)

    def show_fit_param(self, parnumber, **kwargs):
        """
        If pars have been computed, display them in the mapplot window
        """

        if not hasattr(self,'parcube'):
            print "Compute fit parameters first"
            return

        self.mapplot.plane = self.parcube[parnumber,:,:].squeeze()

        self.mapplot(estimator=None, **kwargs)


    def load_model_fit(self, fitsfilename, npars, npeaks=1, fittype=None, _temp_fit_loc=(0,0)):
        """
        Load a parameter + error cube into the .parcube and .errcube
        attributes.
        """
        try:
            import astropy.io.fits as pyfits
        except ImportError:
            import pyfits

        cubefile = pyfits.open(fitsfilename,ignore_missing_end=True)
        cube = cubefile[0].data

        # grab a spectrum and fit it however badly you want
        # this is just to __init__ the relevant data structures
        x,y = _temp_fit_loc
        sp = self.get_spectrum(x,y)
        if fittype is None:
            if cubefile[0].header.get('FITTYPE'):
                fittype = cubefile[0].header.get('FITTYPE')
            else:
                raise KeyError("Must specify FITTYPE or include it in cube header.")

        self.parcube = cube[:npars*npeaks,:,:]
        self.errcube = cube[npars*npeaks:npars*npeaks*2,:,:]

        # make sure params are within limits
        guesses,throwaway = self.specfit.Registry.multifitters[fittype]._make_parinfo(npeaks=npeaks)
        try:
            guesses.values = self.parcube[:,y,x]
        except ValueError:
            OKmask = (self.parcube != 0).sum(axis=0) > 0
            whereOK = np.where(OKmask)
            guesses.values = self.parcube[:,whereOK[0][0],whereOK[1][0]]

        sp.specfit(fittype=fittype, guesses=guesses.values)

        self.specfit.fitter = sp.specfit.fitter
        self.specfit.fittype = sp.specfit.fittype
        self.specfit.parinfo = sp.specfit.parinfo

    try:
        import cubes
        def smooth(self,smooth,**kwargs):
            """
            Smooth the spectrum by factor `smooth`.  

            Documentation from the :mod:`cubes.spectral_smooth` module:

            """
            import cubes

            smooth = round(smooth)
            self.cube = cubes.spectral_smooth(self.cube,smooth,**kwargs)
            self.xarr = self.xarr[::smooth]
            if hasattr(self,'data'):
                self.data = pyspeckit.smooth.smooth(self.data,smooth,**kwargs)
            if len(self.xarr) != self.cube.shape[0]:
                raise ValueError("Convolution resulted in different X and Y array lengths.  Convmode should be 'same'.")
            if self.errorcube is not None:
                self.errorcube = cubes.spectral_smooth(self.errorcube,smooth,**kwargs)
        
            self._smooth_header(smooth)
        __doc__ += "cubes.spectral_smooth doc: \n" + cubes.spectral_smooth.__doc__
    except ImportError:
        def smooth(self):
            raise ImportError("Can't import cubes: required for cube spectral smoothing")

    def _smooth_header(self,smooth):
        """
        Internal - correct the FITS header parameters when smoothing
        """
        if self.header.get('CDELT3') is not None and self.header.get('CRPIX3') is not None:
            self.header['CDELT3'] = self.header.get('CDELT3') * float(smooth)
            self.header['CRPIX3'] = self.header.get('CRPIX3') / float(smooth)

            history.write_history(self.header,"SMOOTH: Smoothed and downsampled spectrum by factor %i" % (smooth))
            history.write_history(self.header,"SMOOTH: Changed CRPIX3 from %f to %f" % (self.header.get('CRPIX3')*float(smooth),self.header.get('CRPIX3')))
            history.write_history(self.header,"SMOOTH: Changed CDELT3 from %f to %f" % (self.header.get('CRPIX3')/float(smooth),self.header.get('CRPIX3')))


    def write_fit(self, fitcubefilename, clobber=False):
        """
        Write out a fit cube using the information in the fit's parinfo to set the header keywords

        Parameters
        ----------
        fitcubefilename: string
            Filename to write to
        clobber: bool
            Overwrite file if it exists?
        """

        try:
            import astropy.io.fits as pyfits
        except ImportError:
            import pyfits
        
        try:
            fitcubefile = pyfits.PrimaryHDU(data=np.concatenate([self.parcube,self.errcube]), header=self.header)
            fitcubefile.header['FITTYPE'] = self.specfit.fittype

            for ii,par in enumerate(self.specfit.parinfo):
                kw = "PLANE%i" % ii
                parname = par['parname'].strip('0123456789')
                fitcubefile.header[kw] =  parname
            # set error parameters
            for jj,par in enumerate(self.specfit.parinfo):
                kw = "PLANE%i" % (ii+jj)
                parname = "e"+par['parname'].strip('0123456789')
                fitcubefile.header[kw] =  parname

            # overwrite the WCS
            fitcubefile.header['CDELT3'] = 1
            fitcubefile.header['CTYPE3'] = 'FITPAR'
            fitcubefile.header['CRVAL3'] = 0
            fitcubefile.header['CRPIX3'] = 1
        except AttributeError:
            print "Make sure you run the cube fitter first."
            return

        fitcubefile.writeto(fitcubefilename, clobber=clobber)

    def write_cube(self):
        raise NotImplementedError


class CubeStack(Cube):
    """
    The Cube equivalent of Spectra: for stitching multiple cubes with the same
    spatial grid but different frequencies together
    """

    def __init__(self, cubelist, xunits='GHz', x0=0, y0=0, maskmap=None, **kwargs):
        """
        Initialize the Cube.  Accepts FITS files.

        x0,y0 - initial spectrum to use (defaults to lower-left corner)
        """

        print "Creating Cube Stack"
        cubelist = list(cubelist)
        for ii,cube in enumerate(cubelist):
            if type(cube) is str:
                cube = Cube(cube)
                cubelist[ii] = cube
            if cube.xarr.units != xunits:
                # convert all inputs to same (non-velocity) units
                cube.xarr.convert_to_unit(xunits, **kwargs)

        self.cubelist = cubelist

        print "Concatenating data"
        self.xarr = spectrum.units.SpectroscopicAxes([sp.xarr for sp in cubelist])
        self.cube = np.ma.concatenate([cube.cube for cube in cubelist])

        if any([cube.errorcube is not None for cube in cubelist]):
            if all([cube.errorcube is not None for cube in cubelist]):
                self.errorcube = np.ma.concatenate([cube.errorcube for cube in cubelist])
            else:
                raise ValueError("Mismatched error cubes.")
        else:
            self.errorcube = None

        if hasattr(self.cube,'mask'):
            try:
                if self.cube.mask in (False,np.bool_(False)):
                    # mask causes major problems internally for numpy...
                    self.cube = np.array(self.cube)
            except ValueError:
                # this means that self.cube.mask is an array;
                # techically that's alright
                pass
        self._sort()
        self.data = self.cube[:,y0,x0]
        self.error = self.errorcube[:,y0,x0] if self.errorcube is not None else None

        self.header = cubelist[0].header
        for cube in cubelist:
            for key,value in cube.header.items():
                self.header[key] = value

        # TODO: Improve this!!!
        self.system = 'galactic' if 'GLON' in self.header['CTYPE1'] else 'celestial'
        
        self.units = cubelist[0].units
        for cube in cubelist: 
            if cube.units != self.units: 
                raise ValueError("Mismatched units")

        self.fileprefix = cubelist[0].fileprefix # first is the best?

        if maskmap is not None:
            self.maskmap = maskmap
        else:
            self.maskmap = np.ones(self.cube.shape[1:],dtype='bool')

        self._register_fitters()
        self.plotter = spectrum.plotters.Plotter(self)
        self.specfit = spectrum.fitters.Specfit(self,Registry=self.Registry)
        self.baseline = spectrum.baseline.Baseline(self)
        self.speclines = spectrum.speclines
        # Initialize writers TO DO: DO WRITERS WORK FOR CUBES?
        self.writer = {}
        for writer in spectrum.writers.writers:
            self.writer[writer] = spectrum.writers.writers[writer](self)
        
        # Special.  This needs to be modified to be more flexible; for now I need it to work for nh3
        self.plot_special = None
        self.plot_special_kwargs = {}

        self.mapplot = mapplot.MapPlotter(self)

    def _sort(self):
        """ Sort the data in order of increasing X (could be decreasing, but
        must be monotonic for plotting reasons) """

        indices = self.xarr.argsort()
        self.xarr = self.xarr[indices]
        self.cube = self.cube[indices,:,:]
        if self.errorcube is not None:
            self.errorcube = self.errorcube[indices,:,:]
