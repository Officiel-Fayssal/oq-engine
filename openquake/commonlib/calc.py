# -*- coding: utf-8 -*-
# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright (C) 2014-2016 GEM Foundation
#
# OpenQuake is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenQuake is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenQuake. If not, see <http://www.gnu.org/licenses/>.

from __future__ import division
import collections
import itertools
import operator
import logging

import numpy

from openquake.baselib.general import get_array, group_array
from openquake.hazardlib.imt import from_string
from openquake.hazardlib.calc import gmf, filters
from openquake.hazardlib.probability_map import (
    ProbabilityCurve, ProbabilityMap)
from openquake.hazardlib.site import SiteCollection
from openquake.commonlib import readinput, oqvalidation
from openquake.commonlib.readinput import \
    get_gsims, get_rupture, get_correl_model, get_imts


MAX_INT = 2 ** 31 - 1  # this is used in the random number generator
# in this way even on 32 bit machines Python will not have to convert
# the generated seed into a long integer

U8 = numpy.uint8
U16 = numpy.uint16
U32 = numpy.uint32
F32 = numpy.float32


# ############## utilities for the classical calculator ############### #

SourceRuptureSites = collections.namedtuple(
    'SourceRuptureSites',
    'source rupture sites')


def gen_ruptures(sources, site_coll, maximum_distance, monitor):
    """
    Yield (source, rupture, affected_sites) for each rupture
    generated by the given sources.

    :param sources: a sequence of sources
    :param site_coll: a SiteCollection instance
    :param maximum_distance: the maximum distance
    :param monitor: a Monitor object
    """
    filtsources_mon = monitor('filtering sources')
    genruptures_mon = monitor('generating ruptures')
    filtruptures_mon = monitor('filtering ruptures')
    for src in sources:
        with filtsources_mon:
            s_sites = src.filter_sites_by_distance_to_source(
                maximum_distance, site_coll)
            if s_sites is None:
                continue

        with genruptures_mon:
            ruptures = list(src.iter_ruptures())
        if not ruptures:
            continue

        for rupture in ruptures:
            with filtruptures_mon:
                r_sites = filters.filter_sites_by_distance_to_rupture(
                    rupture, maximum_distance, s_sites)
                if r_sites is None:
                    continue
            yield SourceRuptureSites(src, rupture, r_sites)


def gen_ruptures_for_site(site, sources, maximum_distance, monitor):
    """
    Yield source, <ruptures close to site>

    :param site: a Site object
    :param sources: a sequence of sources
    :param monitor: a Monitor object
    """
    source_rupture_sites = gen_ruptures(
        sources, SiteCollection([site]), maximum_distance, monitor)
    for src, rows in itertools.groupby(
            source_rupture_sites, key=operator.attrgetter('source')):
        yield src, [row.rupture for row in rows]


# ############## utilities for the scenario calculators ############### #

def calc_gmfs_fast(oqparam, sitecol):
    """
    Build all the ground motion fields for the whole site collection in
    a single step.
    """
    max_dist = oqparam.maximum_distance
    correl_model = get_correl_model(oqparam)
    seed = oqparam.random_seed
    imts = get_imts(oqparam)
    [gsim] = get_gsims(oqparam)
    trunc_level = oqparam.truncation_level
    n_gmfs = oqparam.number_of_ground_motion_fields
    rupture = get_rupture(oqparam)
    res = gmf.ground_motion_fields(
        rupture, sitecol, imts, gsim,
        trunc_level, n_gmfs, correl_model,
        filters.rupture_site_distance_filter(max_dist), seed)
    return {str(imt): matrix for imt, matrix in res.items()}

# ######################### hazard maps ################################### #

# cutoff value for the poe
EPSILON = 1E-30


def compute_hazard_maps(curves, imls, poes):
    """
    Given a set of hazard curve poes, interpolate a hazard map at the specified
    ``poe``.

    :param curves:
        2D array of floats. Each row represents a curve, where the values
        in the row are the PoEs (Probabilities of Exceedance) corresponding to
        ``imls``. Each curve corresponds to a geographical location.
    :param imls:
        Intensity Measure Levels associated with these hazard ``curves``. Type
        should be an array-like of floats.
    :param poes:
        Value(s) on which to interpolate a hazard map from the input
        ``curves``. Can be an array-like or scalar value (for a single PoE).
    :returns:
        An array of shape N x P, where N is the number of curves and P the
        number of poes.
    """
    curves = numpy.array(curves)
    poes = numpy.array(poes)

    if len(poes.shape) == 0:
        # `poes` was passed in as a scalar;
        # convert it to 1D array of 1 element
        poes = poes.reshape(1)

    if len(curves.shape) == 1:
        # `curves` was passed as 1 dimensional array, there is a single site
        curves = curves.reshape((1,) + curves.shape)  # 1 x L

    result = []
    imls = numpy.log(numpy.array(imls[::-1]))

    for curve in curves:
        # the hazard curve, having replaced the too small poes with EPSILON
        curve_cutoff = [max(poe, EPSILON) for poe in curve[::-1]]
        hmap_val = []
        for poe in poes:
            # special case when the interpolation poe is bigger than the
            # maximum, i.e the iml must be smaller than the minumum
            if poe > curve_cutoff[-1]:  # the greatest poes in the curve
                # extrapolate the iml to zero as per
                # https://bugs.launchpad.net/oq-engine/+bug/1292093
                # a consequence is that if all poes are zero any poe > 0
                # is big and the hmap goes automatically to zero
                hmap_val.append(0)
            else:
                # exp-log interpolation, to reduce numerical errors
                # see https://bugs.launchpad.net/oq-engine/+bug/1252770
                val = numpy.exp(
                    numpy.interp(
                        numpy.log(poe), numpy.log(curve_cutoff), imls))
                hmap_val.append(val)

        result.append(hmap_val)
    return numpy.array(result)


# #########################  GMF->curves #################################### #

# NB (MS): the approach used here will not work for non-poissonian models
def _gmvs_to_haz_curve(gmvs, imls, invest_time, duration):
    """
    Given a set of ground motion values (``gmvs``) and intensity measure levels
    (``imls``), compute hazard curve probabilities of exceedance.

    :param gmvs:
        A list of ground motion values, as floats.
    :param imls:
        A list of intensity measure levels, as floats.
    :param float invest_time:
        Investigation time, in years. It is with this time span that we compute
        probabilities of exceedance.

        Another way to put it is the following. When computing a hazard curve,
        we want to answer the question: What is the probability of ground
        motion meeting or exceeding the specified levels (``imls``) in a given
        time span (``invest_time``).
    :param float duration:
        Time window during which GMFs occur. Another was to say it is, the
        period of time over which we simulate ground motion occurrences.

        NOTE: Duration is computed as the calculation investigation time
        multiplied by the number of stochastic event sets.

    :returns:
        Numpy array of PoEs (probabilities of exceedance).
    """
    # convert to numpy array and redimension so that it can be broadcast with
    # the gmvs for computing PoE values; there is a gmv for each rupture
    # here is an example: imls = [0.03, 0.04, 0.05], gmvs=[0.04750576]
    # => num_exceeding = [1, 1, 0] coming from 0.04750576 > [0.03, 0.04, 0.05]
    imls = numpy.array(imls).reshape((len(imls), 1))
    num_exceeding = numpy.sum(numpy.array(gmvs) >= imls, axis=1)
    poes = 1 - numpy.exp(- (invest_time / duration) * num_exceeding)
    return poes


def gmvs_to_poe_map(gmvs_by_sid, imtls, invest_time, duration):
    """
    Convert a dictionary sid -> gmva into a ProbabilityMap
    """
    pmap = ProbabilityMap()
    for sid in gmvs_by_sid:
        data = []
        for imti, imt in enumerate(imtls):
            gmvs = get_array(gmvs_by_sid[sid], imti=imti)['gmv']
            data.append(
                _gmvs_to_haz_curve(gmvs, imtls[imt], invest_time, duration))
        # the array underlying the ProbabilityCurve has size (num_levels, 1)
        array = numpy.concatenate(data).reshape(-1, 1)
        pmap[sid] = ProbabilityCurve(array)
    return pmap


# ################## utilities for classical calculators ################ #

def get_imts_periods(imtls):
    """
    Returns a list of IMT strings and a list of periods. There is an element
    for each IMT of type Spectral Acceleration, including PGA which is
    considered an alias for SA(0.0). The lists are sorted by period.

    :param imtls: a set of intensity measure type strings
    :returns: a list of IMT strings and a list of periods
    """
    getperiod = operator.itemgetter(1)
    imts = sorted((from_string(imt) for imt in imtls
                   if imt.startswith('SA') or imt == 'PGA'), key=getperiod)
    return map(str, imts), [imt[1] or 0.0 for imt in imts]


def make_uhs(maps, imtls, poes):
    """
    Make Uniform Hazard Spectra curves for each location.

    It is assumed that the `lons` and `lats` for each of the `maps` are
    uniform.

    :param maps:
        a composite array with shape N x P, where N is the number of
        sites and P is the number of poes in the hazard maps
    :param imtls:
        a dictionary of intensity measure types and levels
    :param poes:
        a sequence of PoEs for the underlying hazard maps
    :returns:
        an composite array containing N uniform hazard maps
    """
    imts, _ = get_imts_periods(imtls)
    imts_dt = numpy.dtype([(imt, F32) for imt in imts])
    uhs_dt = numpy.dtype([(str(poe), imts_dt) for poe in poes])
    N = len(maps)
    uhs = numpy.zeros(N, uhs_dt)
    for poe in poes:
        for imt in imts:
            uhs[str(poe)][imt] = maps['%s-%s' % (imt, poe)]
    return uhs


def get_gmfs(dstore):
    """
    :param dstore: a datastore
    :returns: a dictionary trt_id, gsid -> gmfa
    """
    oq = dstore['oqparam']
    if 'gmfs' in oq.inputs:  # from file
        logging.info('Reading gmfs from file')
        sitecol, etags, gmfs_by_imt = readinput.get_gmfs(oq)

        # reduce the gmfs matrices to the filtered sites
        for imt in oq.imtls:
            gmfs_by_imt[imt] = gmfs_by_imt[imt][sitecol.indices]

        logging.info('Preparing the risk input')
        return etags, {(0, 'FromFile'): gmfs_by_imt}

    # else from datastore
    rlzs_assoc = dstore['csm_info'].get_rlzs_assoc()
    rlzs = rlzs_assoc.realizations
    sitecol = dstore['sitecol']
    # NB: if the hazard site collection has N sites, the hazard
    # filtered site collection for the nonzero GMFs has N' <= N sites
    # whereas the risk site collection associated to the assets
    # has N'' <= N' sites
    if dstore.parent:
        haz_sitecol = dstore.parent['sitecol']  # N' values
    else:
        haz_sitecol = sitecol
    risk_indices = set(sitecol.indices)  # N'' values
    N = len(haz_sitecol.complete)
    imt_dt = numpy.dtype([(bytes(imt), F32) for imt in oq.imtls])
    E = oq.number_of_ground_motion_fields
    # build a matrix N x E for each GSIM realization
    gmfs = {(trt_id, gsim): numpy.zeros((N, E), imt_dt)
            for trt_id, gsim in rlzs_assoc}
    for i, rlz in enumerate(rlzs):
        data = group_array(dstore['gmf_data/%04d' % i], 'sid')
        for sid, array in data.items():
            if sid in risk_indices:
                for imti, imt in enumerate(oq.imtls):
                    a = get_array(array, imti=imti)
                    gs = str(rlz.gsim_rlz)
                    gmfs[0, gs][imt][sid, a['eid']] = a['gmv']
    return dstore['etags'].value, gmfs


def fix_minimum_intensity(min_iml, imts):
    """
    :param min_iml: a dictionary, possibly with a 'default' key
    :param imts: an ordered list of IMTs
    :returns: a numpy array of intensities, one per IMT

    Make sure the dictionary minimum_intensity (provided by the user in the
    job.ini file) is filled for all intensity measure types and has no key
    named 'default'. Here is how it works:

    >>> min_iml = {'PGA': 0.1, 'default': 0.05}
    >>> fix_minimum_intensity(min_iml, ['PGA', 'PGV'])
    array([ 0.1 ,  0.05], dtype=float32)
    >>> sorted(min_iml.items())
    [('PGA', 0.1), ('PGV', 0.05)]
    """
    if min_iml:
        for imt in imts:
            try:
                min_iml[imt] = oqvalidation.getdefault(min_iml, imt)
            except KeyError:
                raise ValueError(
                    'The parameter `minimum_intensity` in the job.ini '
                    'file is missing the IMT %r' % imt)
    if 'default' in min_iml:
        del min_iml['default']
    return F32([min_iml.get(imt, 0) for imt in imts])


class GmfColl(object):
    """
    A class to collect GMFs in memory, with methods .save and .by_rlzi
    returning a dictionary rlzi -> gmv_dt
    """
    def __init__(self, imts, rlzs):
        self.data = collections.defaultdict(list)  # rlzi -> data

    def save(self, eid, imti, rlz, gmf, sids):
        rlzi = rlz.ordinal
        for gmv, sid in zip(gmf, sids):
            self.data[rlzi].append((sid, eid, imti, gmv))

    def by_rlzi(self):
        return {rlzi: numpy.array(self.data[rlzi], gmv_dt)
                for rlzi in self.data}


gmv_dt = numpy.dtype([('sid', U16), ('eid', U32), ('imti', U8), ('gmv', F32)])


def check_overflow(calc):
    """
    :param calc: an event based calculator

    Raise a ValueError if the number of sites is larger than 65,536 or the
    number of IMTs is larger than 256 or the number of ruptures is larger
    than 4,294,967,296. The limits are due to the numpy dtype used to
    store the GMFs (gmv_dt). They could be relaxed in the future.
    """
    max_ = dict(sites=2**16, events=2**32, imts=2**8)
    num_ = dict(sites=len(calc.sitecol),
                events=len(calc.datastore['etags']),
                imts=len(calc.oqparam.imtls))
    for var in max_:
        if num_[var] > max_[var]:
            raise ValueError(
                'The event based calculator is restricted to '
                '%d %s, got %d' % (max_[var], var, num_[var]))
