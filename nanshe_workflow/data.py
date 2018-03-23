__author__ = "John Kirkham <kirkhamj@janelia.hhmi.org>"
__date__ = "$Nov 05, 2015 13:54$"


import collections
from contextlib import contextmanager
import errno
import itertools
import glob
import numbers
import os
import shutil
import tempfile
import zipfile

import scandir

import h5py
import numpy
import tifffile
import zarr

import dask
import dask.array
import dask.delayed

import kenjutsu.format

from kenjutsu.measure import len_slices
from kenjutsu.blocks import num_blocks, split_blocks

from builtins import (
    map as imap,
    range as irange,
    zip as izip,
)
from past.builtins import unicode

from nanshe_workflow.ipy import display, FloatProgress


def io_remove(name):
    if not os.path.exists(name):
        return
    elif os.path.isfile(name):
        os.remove(name)
    elif os.path.isdir(name):
        shutil.rmtree(name)
    else:
        raise ValueError("Unable to remove path, '%s'." % name)


@dask.delayed
def dask_rm_file(fname):
    try:
        os.remove(fname)
    except IOError as e:
        if e.errno != errno.ENOENT:
            raise
    return fname


@dask.delayed
def dask_rm_dir(dname, *deps):
    try:
        shutil.rmtree(dname)
    except IOError as e:
        if e.errno != errno.ENOENT:
            raise
    return dname


def dask_rm_tree(dirname):
    dirname = os.path.abspath(dirname)

    def _int_dask_rm_tree(dname):
        deps = []
        for pobj in scandir.scandir(dname):
            if pobj.is_file(follow_symlinks=False):
                deps.append(dask_rm_file(pobj.path))
            if pobj.is_dir(follow_symlinks=False):
                deps.append(dask_rm_dir(
                    pobj.path, *_int_dask_rm_tree(pobj.path)
                ))
        return deps

    return dask_rm_dir(dirname, *_int_dask_rm_tree(dirname))


def dask_io_remove(name, executor=None):
    name = os.path.abspath(name)

    tmp_dir = "tmp_nanshe_workflow_{0}_".format(
        os.path.splitext(os.path.basename(name))[0]
    )
    tmp_dir = tempfile.mkdtemp(prefix=tmp_dir)

    if os.path.exists(name):
        os.rename(name, os.path.join(tmp_dir, name))

    rm_task = dask_rm_tree(tmp_dir)
    rm_task = dask.delayed(io_remove)(rm_task)

    if executor is None:
        return rm_task

    return executor.compute(rm_task)


def zip_dir(dirname, compression=zipfile.ZIP_STORED, allowZip64=True):
    dirname = os.path.abspath(dirname)
    zipname = dirname + os.extsep + "zip"

    if os.path.exists(zipname):
        os.remove(zipname)

    num_files = sum([len(fns) for _1, _2, fns in scandir.walk(dirname)])

    progress_bar = FloatProgress(min=0.0, max=float(num_files))
    display(progress_bar)
    with zipfile.ZipFile(zipname,
                         mode="w",
                         compression=compression,
                         allowZip64=allowZip64) as fh:
        for path, dnames, fnames in scandir.walk(dirname):
            fnames = sorted(fnames)
            for each_fname in fnames:
                each_fname = os.path.join(path, each_fname)
                each_fname_rel = os.path.relpath(each_fname, dirname)
                fh.write(each_fname, each_fname_rel)
                progress_bar.value += 1

    return zipname


def concat_dask(dask_arr):
    n_blocks = dask_arr.shape

    result = dask_arr.copy()
    for i in irange(-1, -1 - len(n_blocks), -1):
        result2 = result[..., 0]
        for j in itertools.product(*[
                irange(e) for e in n_blocks[:i]
            ]):
            result2[j] = dask.array.concatenate(
                result[j].tolist(),
                axis=i
            )
        result = result2
    result = result[()]

    return result


def dask_load_hdf5(fn, dn, chunks=None):
    with h5py.File(fn) as fh:
        shape = fh[dn].shape
        dtype = fh[dn].dtype
        if chunks is None:
            chunks = fh[dn].chunks

    def _read_chunk(fn, dn, idx):
        with h5py.File(fn) as fh:
            return fh[dn][idx]

    a = numpy.empty(
        num_blocks(shape, chunks),
        dtype=object
    )
    for i, s in izip(*split_blocks(shape, chunks, index=True)[:2]):
        a[i] = dask.array.from_delayed(
            dask.delayed(_read_chunk)(fn, dn, s),
            len_slices(s),
            dtype
        )
    a = concat_dask(a)

    return a


def dask_store_zarr(filename, datasetnames, datasets, executor):
    if len(datasetnames) != len(datasets):
        raise ValueError(
            "Need `datasetnames` and `datasets` to have the same length."
        )

    with open_zarr(filename, "w") as fh:
        status = None

        dask_arrays = []
        zarr_arrays = []
        for each_datasetname, each_dataset in izip(datasetnames, datasets):
            each_dask_array = dask.array.asarray(each_dataset)

            each_zarr_array = fh.create_dataset(
                each_datasetname,
                shape=each_dask_array.shape,
                dtype=each_dask_array.dtype,
                chunks=True
            )

            each_dask_array = each_dask_array.rechunk(each_zarr_array.chunks)

            dask_arrays.append(each_dask_array)
            zarr_arrays.append(each_zarr_array)

        status = executor.compute(dask.array.store(
            dask_arrays, zarr_arrays, lock=False, compute=False
        ))

        dask.distributed.progress(status, notebook=False)
        print("")


def save_tiff(fn, a):
    if os.path.exists(fn):
        os.remove(fn)
    with tifffile.TiffWriter(fn, bigtiff=True) as tif:
        for i in irange(a.shape[0]):
            tif.save(numpy.asarray(a[i]))


class DistributedDirectoryStore(zarr.DirectoryStore):
    def __delitem__(self, key):
        path = os.path.join(self.path, key)
        if os.path.exists(path):
            dask.distributed.fire_and_forget(dask_io_remove(path))
        else:
            raise KeyError(key)

    def __setitem__(self, key, value):
        # Delete in parallel, asynchronously.
        # Immediately makes room for new key-value pair.
        try:
            del self[key]
        except KeyError:
            pass

        super(DistributedDirectoryStore, self).__setitem__(key, value)


@contextmanager
def open_zarr(name, mode="r"):
    if not os.path.exists(name) and mode in ["a", "w"]:
        store = DistributedDirectoryStore(name)
        yield zarr.open_group(store, mode)
    elif os.path.isdir(name):
        store = DistributedDirectoryStore(name)
        yield zarr.open_group(store, mode)
    elif zipfile.is_zipfile(name):
        with zarr.ZipStore(name, mode=mode, compression=0, allowZip64=True) as store:
            yield zarr.open_group(store, mode)
    else:
        raise NotImplementedError("Unable to open '%s'." % name)


def zip_zarr(name, executor=None):
    name_z = zip_dir(name)

    name_rm = os.extsep + name
    os.rename(name, name_rm)

    shutil.move(name_z, name)

    if executor is None:
        io_remove(name_rm)
    else:
        dask_io_remove(name_rm, executor=executor)


def hdf5_to_zarr(hdf5_file, zarr_file):
    def copy(name, h5py_obj):
        if isinstance(h5py_obj, h5py.Group):
            zarr_obj = zarr_file.create_group(name)
        elif isinstance(h5py_obj, h5py.Dataset):
            zarr_obj = zarr_file.create_dataset(
                name,
                data=h5py_obj,
                chunks=h5py_obj.chunks
            )
        else:
            raise NotImplementedError(
                "No Zarr type analogue for HDF5 type,"
                " '%s'." % str(type(h5py_obj))
            )

        zarr_obj.attrs.update(h5py_obj.attrs)

    hdf5_file.visititems(copy)


def _zarr_visitvalues(group, func):
    def _visit(obj):
        yield obj

        keys = sorted(getattr(obj, "keys", lambda: [])())
        for each_key in keys:
            for each_obj in _visit(obj[each_key]):
                yield each_obj

    for each_obj in itertools.islice(_visit(group), 1, None):
        value = func(each_obj)
        if value is not None:
            return value


def _zarr_visititems(group, func):
    base_len = len(group.name)
    return _zarr_visitvalues(
        group, lambda o: func(o.name[base_len:].lstrip("/"), o)
    )


def zarr_to_hdf5(zarr_file, hdf5_file):
    def copy(name, zarr_obj):
        if isinstance(zarr_obj, zarr.Group):
            h5py_obj = hdf5_file.create_group(name)
        elif isinstance(zarr_obj, zarr.Array):
            h5py_obj = hdf5_file.create_dataset(
                name,
                data=zarr_obj,
                chunks=zarr_obj.chunks
            )
        else:
            raise NotImplementedError(
                "No HDF5 type analogue for Zarr type,"
                " '%s'." % str(type(zarr_obj))
            )

        h5py_obj.attrs.update(zarr_obj.attrs)

    _zarr_visititems(zarr_file, copy)


class DataBlocks(object):
    def __init__(self, data, data_blocks):
        self.data = data
        self.data_blocks = data_blocks

    def __iter__(self):
        for each_data_block in self.data_blocks:
            yield self.data[each_data_block]

    def __len__(self):
        return(len(self.data_blocks))


class LazyDataset(object):
    class LazyDatasetSelection(object):
        def __init__(self, filename, datasetname, key, shape, dtype, size):
            self.filename = filename
            self.datasetname = datasetname
            self.key = key
            self.shape = shape
            self.dtype = dtype
            self.size = size

        def __getitem__(self, key):
            pass

    def __init__(self, filename, datasetname):
        pass

    def __getitem__(self, key):
        pass

    def __len__(self):
        return(self.shape[0])

    @contextmanager
    def astype(self, dtype):
        yield None


class LazyHDF5Dataset(LazyDataset):
    class LazyHDF5DatasetSelection(LazyDataset.LazyDatasetSelection):
        def __getitem__(self, key):
            with h5py.File(self.filename, "r") as filehandle:
                dataset = filehandle[self.datasetname]
                with dataset.astype(self.dtype):
                    try:
                        return(dataset[self.key][key])
                    except TypeError:
                        key_sort = tuple()
                        key_rsort = tuple()
                        for each_key in self.key:
                            if (
                                isinstance(each_key, slice) or
                                isinstance(each_key, int) or
                                isinstance(each_key, str) or
                                isinstance(each_key, unicode)
                            ):
                                key_sort += (each_key,)
                                key_rsort += (slice(None),)
                                continue

                            each_key = numpy.asarray(each_key)
                            each_key_sort = numpy.argsort(each_key)
                            each_key_rsort = numpy.concatenate([
                                each_key_sort[None],
                                numpy.arange(len(each_key_sort))[None]
                            ]).T
                            each_key_rsort = numpy.array(
                                list(tuple(_) for _ in each_key_rsort),
                                dtype=[("sort", int), ("rsort", int)]
                            )
                            each_key_rsort.sort(order="sort")
                            each_key_rsort = each_key_rsort["rsort"].copy()

                            each_key_sort = each_key[each_key_sort]

                            key_sort += (each_key_sort,)
                            key_rsort += (each_key_rsort,)

                        return(dataset[key_sort][key_rsort][key])

    def __init__(self, filename, datasetname):
        self.filename = filename
        self.datasetname = datasetname

        with h5py.File(self.filename, "r") as filehandle:
            dataset = filehandle[self.datasetname]

            self.shape = dataset.shape
            self.dtype = dataset.dtype

        self.size = numpy.prod(self.shape)

    def __getitem__(self, key):
        return(
            LazyHDF5Dataset.LazyHDF5DatasetSelection(
                self.filename,
                self.datasetname,
                key,
                self.shape,
                self.dtype,
                self.size
            )
        )

    @contextmanager
    def astype(self, dtype):
        self_astype = LazyHDF5Dataset(self.filename, self.datasetname)
        self_astype.dtype = numpy.dtype(dtype)

        yield(self_astype)


class LazyZarrDataset(LazyDataset):
    class LazyZarrDatasetSelection(LazyDataset.LazyDatasetSelection):
        def __getitem__(self, key):
            with open_zarr(self.filename, "r") as filehandle:
                dataset = filehandle[self.datasetname]

                try:
                    return(dataset[self.key][key].astype(self.dtype))
                except TypeError:
                    ref_key = list()
                    for i in irange(len(self.key)):
                        each_key = self.key[i]
                        try:
                            each_key = list(each_key)
                        except TypeError:
                            pass
                        ref_key.append(each_key)
                    ref_key = tuple(ref_key)

                    ref_key = kenjutsu.format.reformat_slices(ref_key, self.shape)

                    # Verify there is only one sequence
                    num_seqs = sum(imap(
                        lambda i: isinstance(i, collections.Sequence),
                        ref_key
                    ))
                    if num_seqs > 1:
                        raise ValueError(
                            "Cannot take more than one sequence of integers."
                            " Got %i instead." % num_seqs
                        )
                    elif num_seqs == 1:
                        if not isinstance(ref_key[0], collections.Sequence):
                            raise ValueError(
                                "Sequence of integers must be first."
                            )

                    result = []
                    for each_key in kenjutsu.format.split_indices(ref_key):
                        result.append([dataset[each_key]])
                    result = numpy.concatenate(result)

                    return(result[key].astype(self.dtype))

    def __init__(self, filename, datasetname):
        self.filename = filename
        self.datasetname = datasetname

        with open_zarr(self.filename, "r") as filehandle:
            dataset = filehandle[self.datasetname]

            self.shape = dataset.shape
            self.dtype = dataset.dtype

        self.size = numpy.prod(self.shape)

    def __getitem__(self, key):
        return(
            LazyZarrDataset.LazyZarrDatasetSelection(
                self.filename,
                self.datasetname,
                key,
                self.shape,
                self.dtype,
                self.size
            )
        )

    @contextmanager
    def astype(self, dtype):
        self_astype = LazyZarrDataset(self.filename, self.datasetname)
        self_astype.dtype = numpy.dtype(dtype)

        yield(self_astype)
