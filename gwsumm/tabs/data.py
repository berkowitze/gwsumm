# -*- coding: utf-8 -*-
# Copyright (C) Duncan Macleod (2013)
#
# This file is part of GWSumm.
#
# GWSumm is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# GWSumm is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with GWSumm.  If not, see <http://www.gnu.org/licenses/>.

"""This module defines tabs for generating plots from data on-the-fly.
"""

from __future__ import print_function

import abc
import os.path
import getpass
import re

from copy import copy
from multiprocessing import (Process, Queue)
from multiprocessing.queues import Empty
from time import sleep
from StringIO import StringIO
from datetime import timedelta

from numpy import isclose

from astropy.time import Time

from gwpy.segments import DataQualityFlag

from .. import (globalv, html)
from ..channels import (re_channel,
                        split_combination as split_channel_combination)
from ..config import *
from ..mode import (get_mode, MODE_ENUM)
from ..data import (get_channel, get_timeseries_dict, get_spectrograms,
                    get_coherence_spectrograms, get_spectrum, FRAMETYPE_REGEX)
from ..plot import get_plot
from ..segments import get_segments
from ..state import (generate_all_state, ALLSTATE, SummaryState, get_state)
from ..triggers import get_triggers
from ..utils import (re_cchar, re_flagdiv, vprint, count_free_cores, safe_eval)

from .registry import (get_tab, register_tab)

__author__ = 'Duncan Macleod <duncan.macleod@ligo.org>'


class DataTabBase(get_tab('archived-state')):
    """Abstract base class to detect necessity to run Tab.process()
    """
    __metaclass__ = abc.ABCMeta
    type = 'data-abc'

    @abc.abstractmethod
    def process(self):
        """This method must be overridden by all subclasses
        """
        pass


class DataTab(DataTabBase):
    """A tab where plots and data summaries are built upon request

    This is the 'default' tab for the command-line gw_summary executable.

    All ``*args`` and ``**kwargs`` are passed up-stream to the base
    class constructor, excepting the following:

    Parameters
    ----------
    name : `str`
        name of this tab (required)
    start : `LIGOTimeGPS`, `str`
        start time of this `DataTab`, anything that can be parsed by
        `~gwpy.time.to_gps` is fine
    end : `LIGOTimeGPS`, `str`
        end time of this `DataTab`, format as for `start`
    states : `list` of `states <gwsumm.state.SummaryState>`
        the `list` of states (`~gwsumm.state.SummaryState`) over which
        this `DataTab` should be processed. More states can be added
        later (but before running :meth:`~DataTab.process`) via
        :meth:`~DataTab.add_state`.
    ismeta : `bool`, optional, default: `False`
        indicates that this tab only contains data already by others
        and so doesn't need to be processed.
    noplots : `bool`, optional, default: `False`
        indicates that this tab only exists to trigger data access, and
        shouldn't actually generate any figures
    **kwargs
        other keyword arguments

    See Also
    --------
    gwsumm.tabs.StateTab
        for details on the other keyword arguments (``**kwargs``)
        accepted by the constructor for the `DataTab`.
    """
    type = 'archived-data'

    def __init__(self, name, start, end, states=list([ALLSTATE]),
                 ismeta=False, noplots=False, **kwargs):
        """Initialise a new `DataTab`.
        """
        super(DataTab, self).__init__(name, start, end, states=states, **kwargs)
        self.ismeta = ismeta
        self.noplots = noplots
        self.subplots = []

    # -------------------------------------------
    # SummaryTab configuration parser

    @classmethod
    def from_ini(cls, cp, section, plotdir='plots', **kwargs):
        """Define a new `SummaryTab` from the given section of the
        `ConfigParser`.

        Parameters
        ----------
        cp : :class:`~gwsumm.config.GWConfigParser`
            customised configuration parser containing given section
        section : `str`
            name of section to parse
        plotdir : `str`, optional, default: ``'plots'``
            output path for plots, relative to current directory

        Returns
        -------
        tab : `DataTab`
            a new `DataTab` defined from the configuration
        """
        kwargs.setdefault('plots', [])

        # get meta tags
        try:
            ismeta = cp.get(section, 'meta-tab')
        except NoOptionError:
            pass
        else:
            if ismeta is None:
                kwargs.setdefault('ismeta', True)
            else:
                kwargs.setdefault('ismeta', bool(ismeta.title()))
        try:
            noplots = cp.get(section, 'no-plots')
        except NoOptionError:
            pass
        else:
            if noplots is None:
                kwargs.setdefault('noplots', True)
            else:
                kwargs.setdefault('noplots', bool(noplots.title()))

        job = super(DataTab, cls).from_ini(cp, section, **kwargs)
        job._config = cp._sections[section]

        # -------------------
        # parse plot requests
        #    All config entries whose key is a single integer is
        #    interpreted as a requested plot.

        start, end = job.span

        # parse subplot request
        try:
            subidx = cp.getint(section, 'subplot')
        except NoOptionError:
            subidx = None
        else:
            job.subplots = []
            subplots = []
            try:
                subdelta = timedelta(seconds=cp.getfloat(
                    section, 'subplot-duration'))
            except NoOptionError:
                mode = get_mode()
                if mode == MODE_ENUM['DAY']:
                    subdelta = timedelta(hours=1)
                elif mode == MODE_ENUM['WEEK']:
                    subdelta = timedelta(days=1)
                elif mode == MODE_ENUM['MONTH']:
                    subdelta = timedelta(weeks=1)
                elif mode == MODE_ENUM['YEAR']:
                    subdelta = timedelta(months=1)
                else:
                    d = int(end - start)
                    if d <= 601:
                        subdelta = timedelta(minutes=1)
                    elif d <= 7201:
                        subdelta = timedelta(minutes=10)
                    elif d <= 86401:
                        subdelta = timedelta(hours=1)
                    elif d <= 259201:
                        subdelta = timedelta(hours=6)
                    else:
                        subdelta = timedelta(days=1)
            startd = Time(float(start), format='gps', scale='utc').datetime
            endd = Time(float(end), format='gps', scale='utc').datetime
            while startd < endd:
                e = min(endd, startd + subdelta)
                sps = int(Time(startd, format='datetime', scale='utc').gps)
                spe = int(Time(e, format='datetime', scale='utc').gps)
                subplots.append((sps, spe))
                startd += subdelta

        # find and order the plots
        requests = sorted([(int(opt), val) for (opt, val) in
                           cp.nditems(section) if opt.isdigit()],
                          key=lambda a: a[0])

        # parse plot definition
        for index, definition in requests:
            # find plot customisations within this section
            mods = {}
            for key, val in cp.nditems(section):
                if key.startswith('%d-' % index):
                    opt = key.split('-', 1)[1]
                    mods[opt] = safe_eval(val)

            # parse definition for section references
            try:
                pdef, sources = [s[::-1] for s in
                                 re.split('[\s,]', definition[::-1], 1)]
            except ValueError:
                pdef = definition
                sources = []
            else:
                if not re_channel.match(sources) and cp.has_section(sources):
                    try:
                        sources = cp.get(sources, 'channels')
                    except NoOptionError:
                        pass

            # if pdef refers to another config section, it must have a type
            if cp.has_section(pdef):
                type_ = cp.get(pdef, 'type')
                PlotClass = get_plot(type_)
            elif (pdef not in ['range-histogram', 'segment-histogram'] and
                    pdef.endswith('-histogram')):
                type_ = None
                etg, column = pdef.rsplit('-', 2)[:2]
                mods.setdefault('etg', etg)
                mods.setdefault('column', column)
                PlotClass = get_plot('trigger-histogram')
            elif re.search('-rate', pdef):
                type_ = None
                etg = pdef.rsplit('-', 1)[0]
                mods.setdefault('etg', etg)
                PlotClass = get_plot('trigger-rate')
            else:
                type_ = None
                PlotClass = get_plot(pdef)
            # if the plot definition declares multiple states
            if 'all-states' in mods:
                mods.setdefault('all-data', True)
                if type_:
                    plot = PlotClass.from_ini(cp, pdef, start, end, sources,
                                              state=None, outdir=plotdir,
                                              **mods)
                else:
                    plot = PlotClass(sources, start, end, state=None,
                                     outdir=plotdir, **mods)
                job.plots.append(plot)
                if subidx == index:
                    for span in subplots:
                        subplot = copy(plot)
                        subplot.pargs = plot.pargs.copy()
                        subplot.span = span
                        job.subplots.append(subplot)
            # otherwise define individually for multiple states
            else:
                for state in job.states:
                    if type_:
                        plot = PlotClass.from_ini(cp, pdef, start, end,
                                                  sources, state=state,
                                                  outdir=plotdir, **mods)
                    else:
                        plot = PlotClass(sources, start, end, state=state,
                                         outdir=plotdir, **mods)
                    job.plots.append(plot)
                    if subidx == index:
                        for span in subplots:
                            subplot = copy(plot)
                            subplot.pargs = plot.pargs.copy()
                            subplot.span = span
                            job.subplots.append(subplot)

        return job

    # -------------------------------------------
    # SummaryTab processing

    def finalize_states(self, config=ConfigParser(), segdb_error='raise',
                        **kwargs):
        """Fetch the segments for each state for this `SummaryTab`
        """
        # finalize all-state
        try:
            allstate = get_state(ALLSTATE)
        except ValueError:
            allstate = generate_all_state(self.start, self.end)
        allstate.fetch(config=config)
        # shortcut segment query for each state
        #alldefs = {}
        #for state in self.states:
        #    if state.name == ALLSTATE or state.ready or not state.definition:
        #        continue
        #    try:
        #        alldefs[state.url].append(state)
        #    except KeyError:
        #        alldefs[state.url] = [state]
        #for url,states_ in alldefs.iteritems():
        #    allvalid = reduce(operator.or_,
        #                      [state.known for state in states_])
        #    defs = [s.definition for s in states_]
        #    get_segments(defs, allvalid, config=config, url=url#,
        #                 segdb_error=segdb_error, return_=False, **kwargs)
        # individually double-check, set ready condition
        for state in self.states:
            state.fetch(config=config, segdb_error=segdb_error, **kwargs)

    def process(self, config=ConfigParser(), multiprocess=True, **stateargs):
        """Process data for this `StateTab`.

        Parameters
        ----------
        config : `ConfigParser.ConfigParser`, optional
            job configuration to pass to :math:`~StateTab.finalize_states`
        **stateargs
            all other keyword arguments are passed directly onto the
            :meth:`~StateTab.process_state` method.
        """
        if self.ismeta:
            return
        config = GWSummConfigParser.from_configparser(config)
        # load state segments
        self.finalize_states(
            config=config, segdb_error=stateargs.get('segdb_error', 'raise'),
            datafind_error=stateargs.get('datafind_error', 'raise'))
        vprint("States finalised [%d total]\n" % len(self.states))
        vprint("    Default state: %r\n" % str(self.defaultstate))

        # pre-process requests for 'all-data' plots
        all_data = any([(p.all_data & p.new) for p in self.plots])
        if all_data:
            self.process_state(None, config=config, multiprocess=multiprocess,
                               **stateargs)
        # process each state
        for state in sorted(self.states, key=lambda s: abs(s.active),
                            reverse=True):
            if state:
                vprint("Processing '%s' state\n" % state.name)
            else:
                vprint("Pre-processing all-data requests\n")
            self.process_state(state, config=config, multiprocess=multiprocess,
                               **stateargs)


    def process_state(self, state, nds=None, multiprocess=True,
                      config=GWSummConfigParser(), datacache=None,
                      trigcache=None, segmentcache=None,
                      segdb_error='raise', datafind_error='raise'):
        """Process data for this tab in a given state

        Parameters
        ----------
        state : `~gwsumm.state.SummaryState`
            the state to process. Can give `None` to process ALLSTATE with
            no plots, useful to load all data for other states
        nds : `bool`, optional
            `True` to use NDS to read data, otherwise read from frames.
            Use `None` to read from frames if possible, otherwise
            using NDS.
        multiprocess : `bool`, `int`, optional
            use multiple processes to read data and make plots. If `True`
            use all cores on host, otherwise give an `int` to manually
            select the number of cores to use.
        config : `ConfigParser`, optional
            configuration for this analysis
        datacache : `~glue.lal.Cache`, optional
            `Cache` of files from which to read time-series data
        trigcache : `~glue.lal.Cache`, optional
            `Cache` of files from which to read event triggers
        segmentcache : `~glue.lal.Cache`, optional
            `Cache` of files from which to read segments
        segdb_error : `str`, optional
            if ``'raise'``: raise exceptions when the segment database
            reports exceptions, if ``'warn''`, print warnings but continue,
            otherwise ``'ignore'`` them completely and carry on.
        """
        if state:
            all_data = False
        else:
            all_data = True
            state = get_state(ALLSTATE)

        # flag those plots that were already written by this process
        for p in self.plots + self.subplots:
            if p.outputfile in globalv.WRITTEN_PLOTS:
                p.new = False

        # --------------------------------------------------------------------
        # process time-series

        # find channels that need a TimeSeries
        tschannels = self.get_channels('timeseries',
                                       all_data=all_data, read=True)
        if len(tschannels):
            vprint("    %d channels identified for TimeSeries\n"
                   % len(tschannels))
            get_timeseries_dict(tschannels, state, config=config, nds=nds,
                                multiprocess=multiprocess, cache=datacache,
                                datafind_error=datafind_error, return_=False)
            vprint("    All time-series data loaded\n")

        # find channels that need a StateVector
        svchannels = set(self.get_channels('statevector', all_data=all_data,
                                           read=True))
        odcchannels = self.get_channels('odc', all_data=all_data, read=True)
        svchannels.update(odcchannels)
        svchannels = list(svchannels)
        if len(svchannels):
            vprint("    %d channels identified as StateVectors\n"
                   % (len(svchannels) - len(odcchannels)))
            get_timeseries_dict(svchannels, state, config=config, nds=nds,
                                multiprocess=multiprocess, statevector=True,
                                cache=datacache, return_=False,
                                datafind_error=datafind_error, dtype='uint32')
            vprint("    All state-vector data loaded\n")

        # --------------------------------------------------------------------
        # process spectrograms

        # find FFT parameters
        try:
            fftparams = dict(config.nditems('fft'))
        except NoSectionError:
            fftparams = {}
        for key, val in fftparams.iteritems():
            try:
                fftparams[key] = eval(val)
            except (NameError, SyntaxError):
                pass

        sgchannels = self.get_channels('spectrogram', 'spectrum',
                                       all_data=all_data, read=True)
        raychannels = self.get_channels('rayleigh-spectrogram',
                                        'rayleigh-spectrum',
                                        all_data=all_data, read=True)
        # for coherence spectrograms, we need all pairs of channels,
        # not just the unique ones
        csgchannels = self.get_channels('coherence-spectrogram',
                                        all_data=all_data, read=True,
                                        unique=False, state=state)

        if len(sgchannels):
            vprint("    %d channels identified for Spectrogram\n"
                   % len(sgchannels))
            get_spectrograms(sgchannels, state, config=config, nds=nds,
                             multiprocess=multiprocess, return_=False,
                             cache=datacache, datafind_error=datafind_error,
                             **fftparams)
        if len(raychannels):
            fp2 = fftparams.copy()
            fp2['method'] = fp2['format'] = 'rayleigh'
            get_spectrograms(raychannels, state, config=config, return_=False,
                             multiprocess=multiprocess, **fp2)

        if len(csgchannels):
            if (len(csgchannels)%2 != 0):
                raise ValueError("Error processing coherence spectrograms: "
                                 "you must supply exactly 2 channels for "
                                 "each spectrogram.")
            vprint("    %d channel pairs identified for Coherence "
                   "Spectrogram\n" % (len(csgchannels)/2))
            fp2 = fftparams.copy()
            fp2['method'] = 'welch'
            get_coherence_spectrograms(
                csgchannels, state, config=config, nds=nds,
                multiprocess=multiprocess, return_=False, cache=datacache,
                datafind_error=datafind_error, **fp2)

        # --------------------------------------------------------------------
        # process spectra

        for channel in self.get_channels('spectrum', all_data=all_data,
                                         read=True):
            get_spectrum(channel, state, config=config, return_=False,
                         query=False, **fftparams)

        for channel in self.get_channels(
                'rayleigh-spectrum', all_data=all_data, read=True):
            fp2 = fftparams.copy()
            fp2['method'] = fp2['format'] = 'rayleigh'
            get_spectrum(channel, state, config=config, return_=False, **fp2)

        # --------------------------------------------------------------------
        # process segments

        # find flags that need a DataQualityFlag
        dqflags = set(self.get_flags('segments', all_data=all_data))
        dqflags.update(self.get_flags('timeseries', all_data=all_data,
                                      type='time-volume'))
        dqflags.update(self.get_flags('spectrogram', all_data=all_data,
                                      type='strain-time-volume'))
        if len(dqflags):
            vprint("    %d data-quality flags identified for segments\n"
                   % len(dqflags))
            get_segments(dqflags, state, config=config, segdb_error=segdb_error,
                         cache=segmentcache)

        # --------------------------------------------------------------------
        # process triggers

        for etg, channel in self.get_triggers('triggers',
                                              'trigger-timeseries',
                                              'trigger-rate',
                                              'trigger-histogram',
                                              all_data=all_data, state=state):
            get_triggers(channel, etg, state.active, config=config,
                         cache=trigcache, multiprocess=multiprocess,
                         return_=False)

        # --------------------------------------------------------------------
        # make plots

        if all_data or self.noplots:
            vprint("    Done.\n")
            return

        vprint("    Plotting... \n")

        # filter out plots that aren't for this state
        new_plots = [p for p in self.plots + self.subplots if
                     p.new and (p.state is None or p.state.name == state.name)]

        # setup plotting queue
        if multiprocess:
            queue = Queue()
        else:
            queue = None

        # setup plotting processes
        if queue:
            def process_image(q):
                while True:
                    try:
                        plot = q.get(block=False)
                    except Empty:
                        break
                    else:
                        plot.process()
        # process each one
        nproc = 0
        for plot in sorted(new_plots, key=lambda p: p._threadsafe and 1 or 2):
            # in case a single tab requests the same plot twice, check again:
            if plot.outputfile in globalv.WRITTEN_PLOTS:
                continue
            globalv.WRITTEN_PLOTS.append(plot.outputfile)
            # queue plot for multiprocessing
            if queue and plot._threadsafe:
                queue.put(plot)
                nproc += 1
            # process plot now
            else:
                plot.process()

        # if a single multi-processed figure, just run it in this process
        if nproc == 1:
            queue.get().process()
        # otherwise execute all processes and wait
        elif nproc > 1:
            # actually execute all processes
            procs = []
            for i in range(min(nproc, multiprocess)):
                procs.append(Process(target=process_image, args=(queue,)))
                procs[-1].daemon = True
                procs[-1].start()
            vprint("        %d plot processes executed in %d processes.\n"
                   % (nproc, len(procs)))
            vprint("        Waiting for plotting to complete... ")
            queue.close()
            sleep(2)
            # wait for children to finish
            for p in procs:
                p.join()
            vprint("Done.\n")

    # -------------------------------------------------------------------------
    # HTML operations

    def build_html_content(self, frame):
        """Build the #main div for this tab.

        In this construction, the <div id="id\_"> is empty, with a
        javascript hook to load the given frame into the div when ready.
        """
        page = html.markup.page()
        page.div(id_='main')
        page.div('', id_='content')
        page.add(str(html.load(frame, id_='content')))
        if globalv.HTML_COMMENTS_NAME:
            if globalv.IFO:
                id_ = '/%s/%s/%s' % (getpass.getuser(), globalv.IFO, self.path)
            else:
                id_ = '/%s/%s' % (getpass.getuser(), self.path)
            page.hr(class_='row-divider')
            page.h1('Comments')
            page.add(str(html.comments_box(
                globalv.HTML_COMMENTS_NAME, identifier=id_)))
        page.div.close()
        return page

    def write_html(self, *args, **kwargs):
        writedata = kwargs.pop('writedata', True)
        vprint("Writing HTML:\n")
        for state, frame in zip(self.states, self.frames):
            idx = self.states.index(state)
            if writedata:
                self.write_state_html(state)
                vprint("    %s written\n" % frame)
            elif not os.path.isfile(self.frames[idx]):
                self.write_state_placeholder(state)
                vprint("    %s placeholder written\n" % frame)
        writehtml = kwargs.pop('writehtml', True)
        if writehtml:
            # work out whether to print comments
            comments = False
            for frame in self.frames:
                if not ('These data have not been generated yet' in
                        open(frame).read()):
                    comments = True
                    break
            if not comments:
                c = globalv.HTML_COMMENTS_NAME
                globalv.HTML_COMMENTS_NAME = None
            super(DataTab, self).write_html(*args, **kwargs)
            if not comments:
                globalv.HTML_COMMENTS_NAME = c
            vprint("    %s written\n" % self.index)

    def write_state_placeholder(self, state):
        """Write a placeholder '#main' content for this tab
        """
        email = html.markup.oneliner.a('the DetChar group',
                                       class_='alert-link',
                                       href='mailto:detchar+code@ligo.org')
        page = html.markup.page()
        page.div(class_='row')
        page.div(class_='col-md-12')
        page.div(class_='alert alert-info')
        page.p("These data have not been generated yet, please check back "
               "later.")
        page.p("If this state persists for more than three or four hours, "
               "please contact %s." % email)
        page.div.close()
        page.div.close()
        page.div.close()

        # write to file
        idx = self.states.index(state)
        with open(self.frames[idx], 'w') as fobj:
            fobj.write(str(page))
        return self.frames[idx]

    def write_state_html(self, state):
        """Write the '#main' HTML content for this tab.

        For now, this function just links all the plots in a 2-column
        format.
        """
        page = html.markup.page()

        # link data
        if self.subplots:
            page.hr(class_='row-divider')
            page.h1('Sub-plots')
            layout = get_mode() == MODE_ENUM['WEEK'] and [7] or [4]
            plist = [p for p in self.subplots if p.state in [state, None]]
            page.add(str(self.scaffold_plots(plots=plist, state=state,
                                             layout=layout)))

        page.hr(class_='row-divider')
        page.div(class_='row')
        page.div(class_='col-md-12')
        channels = self.get_channels('timeseries', 'statevector', 'spectrum',
                                     'spectrogram', 'odc', new=False)
        if len(channels):
            page.h1('Channel information')
            page.add("The following channels were used to generate the above "
                     "data")
            headers = ['Channel', 'Type', 'Frametype', 'Sample rate', 'Units']
            data = []
            for channel in sorted(channels, key=lambda x: str(x)):
                channel = get_channel(channel)
                # don't write combination meta-channels
                parts = split_channel_combination(channel.ndsname)
                if len(parts) != 1 or len(parts[0]) != len(channel.ndsname):
                    continue
                # format CIS url and type
                ftype = channel.frametype or 'Unknown'
                for desc, regex in FRAMETYPE_REGEX.iteritems():
                    if regex.match(ftype):
                        ftype += ' <small>[%s]</small>' % desc
                        break
                if re.search('\.[a-z]+\Z', channel.name):
                    name, ctype = channel.name.rsplit('.', 1)
                    c2 = get_channel(name)
                    ctype = ctype in ['rms'] and ctype.upper() or ctype.title()
                else:
                    c2 = channel
                    ctype = 'Raw'
                if c2.url:
                    link = html.markup.oneliner.a(str(channel), href=c2.url,
                                                  target='_blank')
                else:
                    link = str(channel)

                # format sameple rate
                if channel.sample_rate is None:
                    rate = 'Unknown'
                elif isclose(channel.sample_rate.value, 1/60.):
                    rate = '1/60 %s' % channel.sample_rate.unit
                elif channel.sample_rate.value.is_integer():
                    rate = '{0}{1:s}'.format(int(channel.sample_rate.value),
                                             channel.sample_rate._unitstr)
                else:
                    rate = str(channel.sample_rate)
                # format unit
                if channel.unit:
                    try:
                        unit = channel.unit.to_string(
                            'unicode').encode('ascii', 'xmlcharrefreplace')
                    except ValueError:
                        try:
                            unit = channel.unit.long_names[0]
                        except IndexError:
                            unit = str(channel.unit)
                else:
                    unit = 'Unknown'
                data.append([link, ctype, ftype, rate, unit])
            page.add(str(html.data_table(headers, data)))

        allflags = sorted(set([
            (f, p) for plot in filter(lambda p: p.data == 'segments' and
                                                p.type != 'guardian',
                                      self.plots)
            for (f, p) in plot.padding.iteritems()]), key=lambda x: x[0])
        if len(allflags):
            page.h1('Segment information')
            page.add("The following flags were used in "
                     "the above data. This list does not include state "
                     "information or combinations of flags")
            # make summary table
            headers = ['Name', 'Defined duration', 'Active duration',
                       'Padding', 'Description']
            data = []
            pc = float(abs(self.span) / 100.)
            for flag, padding in allflags:
                if padding == (0, 0):
                    padding = None
                flag = get_segments(flag, [self.span], query=False,
                                    padding={flag: padding})
                v = flag.version and str(flag.version) or ''
                try:
                    valid = '%.2f (%.2f%%)' % (abs(flag.known),
                                               abs(flag.known) / pc)
                except ZeroDivisionError:
                    valid = '0.00 (0.00%)'
                    active = '0.00 (0.00%)'
                else:
                    active = '%.2f (%.2f%%)' % (abs(flag.active),
                                                abs(flag.active) / pc)
                data.append([flag.name, valid, active,
                             padding and str(padding) or '-',
                             flag.description or ''])
            page.add(str(html.data_table(headers, data)))
            # print segment lists
            page.div(class_='panel-group', id="accordion")
            for i, (flag, padding) in enumerate(allflags):
                flag = get_segments(flag, state.active, query=False,
                                    padding={flag: padding})
                page.div(class_='panel well panel-primary')
                page.div(class_='panel-heading')
                page.a(href='#flag%d' % i, **{'data-toggle': 'collapse',
                                              'data-parent': '#accordion'})
                page.h4(flag.name, class_='panel-title')
                page.a.close()
                page.div.close()
                page.div(id_='flag%d' % i, class_='panel-collapse collapse')
                page.div(class_='panel-body')
                # write segment summary
                page.p('This flag was defined and had a known state during '
                       'the following segments:')
                page.add(self.print_segments(flag.known))
                # write segment table
                page.p('This flag was active during the following segments:')
                page.add(self.print_segments(flag.active))

                page.div.close()
                page.div.close()
                page.div.close()
            page.div.close()
        page.div.close()
        page.div.close()

        return super(DataTab, self).write_state_html(state, plots=True,
                                                     pre=self.foreword,
                                                     post=page)

    @staticmethod
    def print_segments(flag):
        """Print the contents of a `SegmentList` in HTML
        """
        if isinstance(flag, DataQualityFlag):
            flag = flag.active
        dtype = float(abs(flag)).is_integer() and int or float
        segwizard = StringIO()
        flag.write(segwizard, format='segwizard', coltype=dtype)
        return html.markup.oneliner.pre(segwizard.getvalue())

    # -------------------------------------------------------------------------
    # methods

    def get_channels(self, *types, **kwargs):
        """Return the `set` of data channels required for plots of the
        given ``types``.

        Parameters
        ----------
        *types : `list` of `str`
            `list` of plot data type strings whose channel sets to return
        new : `bool`, default: `True`
            only include plots whose 'new' attribute is True

        Returns
        -------
        channels : `list`
            an alphabetically-sorted `list` of channels
        """
        isnew = kwargs.pop('new', True)
        if kwargs.pop('unique', True):
            out = set()
            add = out.update
        else:
            out = list()
            add = out.extend
        for plot in self.plots:
            if not plot.data in types:
                continue
            if isnew and not plot.new:
                continue
            skip = False
            for key, val in kwargs.iteritems():
                if getattr(plot, key) != val:
                    skip = True
                    break
            if skip:
                continue
            add(plot.channels)
            if plot.data == 'odc':
                add(plot.get_bitmask_channels())
        return out

    def get_flags(self, *types, **kwargs):
        """Return the `set` of data-quality flags required for plots of the
        given ``types``.

        Parameters
        ----------
        *types : `list` of `str`
            `list` of plot type strings whose flag sets to return

        Returns
        -------
        flags : `list`
            an alphabetically-sorted `list` of flags
        """
        isnew = kwargs.pop('new', True)
        uniq = kwargs.pop('unique', True)
        out = set()
        for plot in self.plots:
            if not plot.data in types:
                continue
            if isnew and not plot.new:
                continue
            skip = False
            for key, val in kwargs.iteritems():
                if getattr(plot, key) != val:
                    skip = True
                    break
            if skip:
                continue
            if uniq:
                out.update([f for cflag in plot.flags for f in
                            re_flagdiv.split(cflag)[::2] if f])
            else:
                out.update(plot.flags)
        return sorted(out, key=lambda dqf: str(dqf))

    def get_triggers(self, *types, **kwargs):
        """Return the `set` of data-quality flags required for plots of the
        given ``types``.

        Parameters
        ----------
        *types : `list` of `str`
            `list` of plot type strings whose flag sets to return

        Returns
        -------
        flags : `list`
            an alphabetically-sorted `list` of flags
        """
        isnew = kwargs.pop('new', True)
        out = set()
        for plot in self.plots:
            if not plot.type in types:
                continue
            if isnew and not plot.new:
                continue
            skip = False
            for key, val in kwargs.iteritems():
                if getattr(plot, key) != val:
                    skip = True
                    break
            if skip:
                continue
            for channel in plot.channels:
                out.add((plot.etg, channel))
        return sorted(out, key=lambda ch: ch[1].name)

register_tab(DataTab)
register_tab(DataTab, name='default')
