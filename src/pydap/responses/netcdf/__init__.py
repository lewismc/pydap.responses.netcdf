from pydap.model import *
from pydap.lib import walk, get_var
from pydap.responses.lib import BaseResponse
from itertools import chain, ifilter
from numpy.compat import asbytes
from collections import Iterator
from logging import debug
from datetime import datetime
import time

from pupynere import netcdf_file, nc_generator
import numpy as np

class NCResponse(BaseResponse):
    def __init__(self, dataset):
        BaseResponse.__init__(self, dataset)

        self.nc = netcdf_file(None)
        self.nc._attributes.update(self.dataset.attributes['NC_GLOBAL'])

        dimensions = [var.dimensions for var in walk(self.dataset) if isinstance(var, BaseType)]
        dimensions = set(reduce(lambda x, y: x+y, dimensions))
        try:
            unlim_dim = self.dataset.attributes['DODS_EXTRA']['Unlimited_Dimension']
        except:
            unlim_dim = None

        # GridType
        for grid in walk(dataset, GridType):

            # add dimensions
            for dim, map_ in grid.maps.items():
                if dim in self.nc.dimensions:
                    continue

                n = None if dim == unlim_dim else grid[dim].data.shape[0]
                self.nc.createDimension(dim, n)
                if not n:
                    self.nc.set_numrecs(grid[dim].data.shape[0])
                var = grid[dim]

                # and add dimension variable
                self.nc.createVariable(dim, var.dtype.char, (dim,), attributes=var.attributes)

            # finally add the grid variable itself
            base_var = grid[grid.name]
            var = self.nc.createVariable(base_var.name, base_var.dtype.char, base_var.dimensions, attributes=base_var.attributes)

        # Sequence types!
        for seq in walk(dataset, SequenceType):

            self.nc.createDimension(seq.name, None)
            self.nc.set_numrecs(len(seq))

            dim = seq.name,

            for child in seq.children():
                dtype = child.dtype
                # netcdf does not have a date type, so remap to float
                if dtype == np.dtype('datetime64'):
                    dtype = np.dtype('float32')
                elif dtype == np.dtype('object'):
                    raise TypeError("Don't know how to handle numpy type {0}".format(dtype))
                        
                var = self.nc.createVariable(child.name, dtype.char, dim, attributes=child.attributes)

        self.headers.extend([
            ('Content-type', 'application/x-netcdf')
        ])
        # Optionally set the filesize header if possible
        try:
            self.headers.extend([('Content-length', self.nc.filesize)])
        except ValueError:
            pass

    def __iter__(self):
        nc = self.nc

        # Hack to find the variables if they're nested in the tree
        var2id = {}
        for recvar in nc.variables.keys():
            for dstvar in walk(self.dataset, BaseType):
                if recvar == dstvar.name:
                    var2id[recvar] = dstvar.id
                    continue

        def type_generator(input):
            # is this a "scalar" (i.e. a standard python object)
            # if so, it needs to be a numpy array, or at least have 'dtype' and 'byteswap' attributes
            for value in input:
                if isinstance(value, (type(None), str, int, float, bool, datetime)):
                    # special case datetimes, since dates aren't supported by NetCDF3
                    if type(value) == datetime:
                        yield np.array(time.mktime(value.timetuple()) / 3600. / 24., dtype='Float32') # days since epoch
                    else:
                        yield np.array(value)
                else:
                    yield value
            
        def nonrecord_input():
            for varname in nc.non_recvars.keys():
                debug("Iterator for %s", varname)
                dst_var = get_var(self.dataset, var2id[varname]).data
                # skip 0-d variables
                if not dst_var.shape:
                    continue

                # Make sure that all elements of the list are iterators
                for x in dst_var:
                    yield x
            debug("Done with nonrecord input")

        # Create a generator for the record variables
        recvars = nc.recvars.keys()
        def record_generator(nc, dst, table):
            debug("record_generator() for dataset %s", dst)
            vars = [ iter(get_var(dst, table[varname])) for varname in nc.recvars.keys() ]
            while True:
                for var in vars:
                    try:
                        yield var.next()
                    except StopIteration:
                        raise
                    
        more_input = type_generator(record_generator(nc, self.dataset, var2id))

        # Create a single pipeline which includes the non-record and record variables
        pipeline = nc_generator(nc, chain(type_generator(nonrecord_input()), more_input))

        # Generate the netcdf stream
        for block in pipeline:
            yield block
