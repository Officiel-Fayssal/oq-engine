# -*- coding: utf-8 -*-
# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright (C) 2012-2018 GEM Foundation
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
"""
Module :mod:`~openquake.hazardlib.calc.filters` contain filter functions for
calculators.

Filters are functions (or other callable objects) that should take generators
and return generators. There are two different kinds of filter functions:

1. Source-site filters. Those functions take a generator of two-item tuples,
   each pair consists of seismic source object (that is, an instance of
   a subclass of :class:`~openquake.hazardlib.source.base.BaseSeismicSource`)
   and a site collection (instance of
   :class:`~openquake.hazardlib.site.SiteCollection`).
2. Rupture-site filters. Those also take a generator of pairs, but in this
   case the first item in the pair is a rupture object (instance of
   :class:`~openquake.hazardlib.source.rupture.Rupture`). The second element in
   generator items is still site collection.

The purpose of both kinds of filters is to limit the amount of calculation
to be done based on some criteria, like the distance between the source
and the site. So common design feature of all the filters is the loop over
pairs of the provided generator, filtering the sites collection, and if
there are no items left in it, skipping the pair and continuing to the next
one. If some sites need to be considered together with that source / rupture,
the pair gets generated out, with a (possibly) :meth:`limited
<openquake.hazardlib.site.SiteCollection.filter>` site collection.

Consistency of filters' input and output stream format allows several filters
(obviously, of the same kind) to be chained together.

Filter functions should not make assumptions about the ordering of items
in the original generator or draw more than one pair at once. Ideally, they
should also perform reasonably fast (filtering stage that takes longer than
the actual calculation on unfiltered collection only decreases performance).

Module :mod:`openquake.hazardlib.calc.filters` exports one distance-based
filter function as well as a "no operation" filter (`source_site_noop_filter`).
There is a class `SourceFilter` to determine the sites
affected by a given source: the second one uses an R-tree index and it is
faster if there are a lot of sources, i.e. if the initial time to prepare
the index can be compensed.
"""
import sys
import collections
from contextlib import contextmanager
import numpy
from scipy.interpolate import interp1d
import rtree
from openquake.baselib.python3compat import raise_
from openquake.hazardlib.geo.utils import (
    KM_TO_DEGREES, angular_distance, within, fix_lon, get_bounding_box)

MAX_DISTANCE = 2000  # km, ultra big distance used if there is no filter


@contextmanager
def context(src):
    """
    Used to add the source_id to the error message. To be used as

    with context(src):
        operation_with(src)

    Typically the operation is filtering a source, that can fail for
    tricky geometries.
    """
    try:
        yield
    except Exception:
        etype, err, tb = sys.exc_info()
        msg = 'An error occurred with source id=%s. Error: %s'
        msg %= (src.source_id, err)
        raise_(etype, msg, tb)


def getdefault(dic_with_default, key):
    """
    :param dic_with_default: a dictionary with a 'default' key
    :param key: a key that may be present in the dictionary or not
    :returns: the value associated to the key, or to 'default'
    """
    try:
        return dic_with_default[key]
    except KeyError:
        return dic_with_default['default']


def get_distances(rupture, mesh, param):
    """
    :param rupture: a rupture
    :param mesh: a mesh of points or a site collection
    :param param: the kind of distance to compute (default rjb)
    :returns: an array of distances from the given mesh
    """
    if param == 'rrup':
        dist = rupture.surface.get_min_distance(mesh)
    elif param == 'rx':
        dist = rupture.surface.get_rx_distance(mesh)
    elif param == 'ry0':
        dist = rupture.surface.get_ry0_distance(mesh)
    elif param == 'rjb':
        dist = rupture.surface.get_joyner_boore_distance(mesh)
    elif param == 'rhypo':
        dist = rupture.hypocenter.distance_to_mesh(mesh)
    elif param == 'repi':
        dist = rupture.hypocenter.distance_to_mesh(mesh, with_depths=False)
    elif param == 'rcdpp':
        dist = rupture.get_cdppvalue(mesh)
    elif param == 'azimuth':
        dist = rupture.surface.get_azimuth(mesh)
    elif param == "rvolc":
        # Volcanic distance not yet supported, defaulting to zero
        dist = numpy.zeros_like(mesh.lons)
    else:
        raise ValueError('Unknown distance measure %r' % param)
    return dist


class FarAwayRupture(Exception):
    """Raised if the rupture is outside the maximum distance for all sites"""


class Piecewise(object):
    """
    Given two arrays x and y of non-decreasing values, build a piecewise
    function associating to each x the corresponding y. If x is smaller
    then the minimum x, the minimum y is returned; if x is larger than the
    maximum x, the maximum y is returned.
    """
    def __init__(self, x, y):
        self.y = numpy.array(y)
        # interpolating from x values to indices in the range [0: len(x)]
        self.piecewise = interp1d(x, range(len(x)), bounds_error=False,
                                  fill_value=(0, len(x) - 1))

    def __call__(self, x):
        idx = numpy.int64(numpy.ceil(self.piecewise(x)))
        return self.y[idx]


class IntegrationDistance(collections.Mapping):
    """
    Pickleable object wrapping a dictionary of integration distances per
    tectonic region type. The integration distances can be scalars or
    list of pairs (magnitude, distance). Here is an example using 'default'
    as tectonic region type, so that the same values will be used for all
    tectonic region types:

    >>> maxdist = IntegrationDistance({'default': [
    ...          (3, 30), (4, 40), (5, 100), (6, 200), (7, 300), (8, 400)]})
    >>> maxdist('Some TRT', mag=2.5)
    30
    >>> maxdist('Some TRT', mag=3)
    30
    >>> maxdist('Some TRT', mag=3.1)
    40
    >>> maxdist('Some TRT', mag=8)
    400
    >>> maxdist('Some TRT', mag=8.5)  # 2000 km are used above the maximum
    2000

    It has also a method `.get_closest(sites, rupture)` returning the closest
    sites to the rupture and their distances. The integration distance can be
    missing if the sites have been already filtered (empty dictionary): in
    that case the method returns all the sites and all the distances.
    """
    def __init__(self, dic):
        self.dic = dic or {}  # TRT -> float or list of pairs
        self.magdist = {}  # TRT -> (magnitudes, distances)
        for trt, value in self.dic.items():
            if isinstance(value, list):  # assume a list of pairs (mag, dist)
                self.magdist[trt] = value
            else:
                self.dic[trt] = float(value)

    def __call__(self, trt, mag=None):
        value = getdefault(self.dic, trt)
        if isinstance(value, float):  # scalar maximum distance
            return value
        elif mag is None:  # get the maximum distance
            return MAX_DISTANCE
        elif not hasattr(self, 'piecewise'):
            self.piecewise = {}  # function cache
        try:
            md = self.piecewise[trt]  # retrieve from the cache
        except KeyError:  # fill the cache
            mags, dists = zip(*getdefault(self.magdist, trt))
            if mags[-1] < 11:  # use 2000 km for mag > mags[-1]
                mags = numpy.concatenate([mags, [11]])
                dists = numpy.concatenate([dists, [MAX_DISTANCE]])
            md = self.piecewise[trt] = Piecewise(mags, dists)
        return md(mag)

    def get_bounding_box(self, lon, lat, trt=None, mag=None):
        """
        Build a bounding box around the given lon, lat by computing the
        maximum_distance at the given tectonic region type and magnitude.

        :param lon: longitude
        :param lat: latitude
        :param trt: tectonic region type, possibly None
        :param mag: magnitude, possibly None
        :returns: min_lon, min_lat, max_lon, max_lat
        """
        if trt is None:  # take the greatest integration distance
            maxdist = max(self(trt, mag) for trt in self.dic)
        else:  # get the integration distance for the given TRT
            maxdist = self(trt, mag)
        a1 = min(maxdist * KM_TO_DEGREES, 90)
        a2 = min(angular_distance(maxdist, lat), 180)
        return lon - a2, lat - a1, lon + a2, lat + a1

    def __getstate__(self):
        # otherwise is not pickleable due to .piecewise
        return dict(dic=self.dic, magdist=self.magdist)

    def __getitem__(self, trt):
        return self(trt)

    def __iter__(self):
        return iter(self.dic)

    def __len__(self):
        return len(self.dic)

    def __repr__(self):
        return repr(self.dic)


def get_indices(sites):
    """
    :returns the indices from a SiteCollection
    """
    return (numpy.arange(len(sites), dtype=numpy.float32)
            if sites.indices is None else sites.indices)


class SourceFilter(object):
    """
    The SourceFilter uses the rtree library. The index is generated at
    instantiation time and kept in memory. The filter should be
    instantiated only once per calculation, after the site collection is
    known. It should be used as follows::

      ss_filter = SourceFilter(sitecol, integration_distance)
      for src, sites in ss_filter(sources):
         do_something(...)

    As a side effect, sets the `.nsites` attribute of the source, i.e. the
    number of sites within the integration distance. Notice that SourceFilter
    instances can be pickled, but when unpickled the index is lost: the reason
    is that libspatialindex indices cannot be properly pickled
    (https://github.com/Toblerity/rtree/issues/65).

    :param sitecol:
        :class:`openquake.hazardlib.site.SiteCollection` instance (or None)
    :param integration_distance:
        Integration distance dictionary (TRT -> distance in km)
    :param prefilter:
        by default "rtree", accepts also "numpy" and "no"
    """
    def __init__(self, sitecol, integration_distance, prefilter='rtree'):
        if sitecol is not None and len(sitecol) < len(sitecol.complete):
            raise ValueError('%s is not complete!' % sitecol)
        self.integration_distance = (
            IntegrationDistance(integration_distance)
            if isinstance(integration_distance, dict)
            else integration_distance)
        self.sitecol = sitecol
        if integration_distance and sitecol is not None:
            self.prefilter = prefilter
        else:
            self.prefilter = 'no'
        if self.prefilter == 'rtree':
            lonlats = zip(sitecol.lons, sitecol.lats)
            self.index = rtree.index.Index(
                (i, (lon, lat, lon, lat), None)
                for i, (lon, lat) in enumerate(lonlats))

    def get_affected_box(self, src):
        """
        Get the enlarged bounding box of a source.

        :param src: a source object
        :returns: a bounding box (min_lon, min_lat, max_lon, max_lat)
        """
        mag = src.get_min_max_mag()[1]
        maxdist = self.integration_distance(src.tectonic_region_type, mag)
        bbox = get_bounding_box(src, maxdist)
        return (fix_lon(bbox[0]), bbox[1], fix_lon(bbox[2]), bbox[3])

    def get_rectangle(self, src):
        """
        :param src: a source object
        :returns: ((min_lon, min_lat), width, height), useful for plotting
        """
        min_lon, min_lat, max_lon, max_lat = self.get_affected_box(src)
        return (min_lon, min_lat), (max_lon - min_lon) % 360, max_lat - min_lat

    def get_close_sites(self, source):
        """
        Returns the sites within the integration distance from the source,
        or None.
        """
        source_sites = list(self([source]))
        if source_sites:
            return source_sites[0][1]

    def get_bounding_boxes(self, trt=None, mag=None):
        """
        :param trt: a tectonic region type (used for the integration distance)
        :param mag: a magnitude (used for the integration distance)
        :returns: a list of bounding boxes, one per site
        """
        bbs = []
        for site in self.sitecol:
            bb = self.integration_distance.get_bounding_box(
                site.location.longitude, site.location.latitude, trt, mag)
            bbs.append(bb)
        return bbs

    def __call__(self, sources, sites=None):
        if sites is None:
            sites = self.sitecol
        for src in sources:
            if hasattr(src, 'indices'):  # already filtered
                yield src, sites.filtered(src.indices)
            elif self.prefilter == 'no':  # do not filter
                yield src, sites
            elif self.prefilter == 'rtree':
                indices = within(self.get_affected_box(src), self.index)
                if len(indices):
                    src.indices = indices
                    yield src, sites.filtered(src.indices)
            elif self.prefilter == 'numpy':
                s_sites = sites.within_bbox(self.get_affected_box(src))
                if s_sites is not None:
                    src.indices = get_indices(s_sites)
                    yield src, s_sites

    def __getstate__(self):
        # 'rtree' cannot be used on the workers, so we use 'numpy' instead
        pref = 'numpy' if self.prefilter == 'rtree' else self.prefilter
        return dict(integration_distance=self.integration_distance,
                    sitecol=self.sitecol, prefilter=pref)


source_site_noop_filter = SourceFilter(None, {})
