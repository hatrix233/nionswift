"""
    Provide symbolic math services.

    The goal is to provide a module (namespace) where users can be provided with variables representing
    data items (directly or indirectly via reference to workspace panels).

    DataNodes represent data items, operations, numpy arrays, and constants.
"""

# standard libraries
import copy
import datetime
import logging
import numbers
import operator
import threading
import uuid

# typing
from typing import List

# third party libraries
import numpy
import scipy
import scipy.fftpack
import scipy.ndimage
import scipy.ndimage.filters
import scipy.ndimage.fourier
import scipy.signal

# local libraries
from nion.swift.model import Calibration
from nion.swift.model import DataAndMetadata
from nion.swift.model import Image
from nion.ui import Event
from nion.ui import Geometry
from nion.ui import Observable
from nion.ui import Persistence


def arange(data):
    return numpy.amax(data) - numpy.amin(data)

def column(data, start, stop):
    start_0 = start if start is not None else 0
    stop_0 = stop if stop is not None else data_shape(data)[0]
    start_1 = start if start is not None else 0
    stop_1 = stop if stop is not None else data_shape(data)[1]
    return numpy.meshgrid(numpy.linspace(start_1, stop_1, data_shape(data)[1]), numpy.linspace(start_0, stop_0, data_shape(data)[0]), sparse=True)[0]

def row(data, start, stop):
    start_0 = start if start is not None else 0
    stop_0 = stop if stop is not None else data_shape(data)[0]
    start_1 = start if start is not None else 0
    stop_1 = stop if stop is not None else data_shape(data)[1]
    return numpy.meshgrid(numpy.linspace(start_1, stop_1, data_shape(data)[1]), numpy.linspace(start_0, stop_0, data_shape(data)[0]), sparse=True)[1]

def radius(data, normalize):
    start_0 = -1 if normalize else -data_shape(data)[0] * 0.5
    stop_0 = -start_0
    start_1 = -1 if normalize else -data_shape(data)[1] * 0.5
    stop_1 = -start_1
    icol, irow = numpy.meshgrid(numpy.linspace(start_1, stop_1, data_shape(data)[1]), numpy.linspace(start_0, stop_0, data_shape(data)[0]), sparse=True)
    return numpy.sqrt(icol * icol + irow * irow)

def take_item(data, key):
    return data[key]

def data_shape(data):
    return Image.spatial_shape_from_data(data)

def astype(data, dtype):
    return data.astype(str_to_dtype(dtype))


def function_fft(data_and_metadata):
    data_shape = data_and_metadata.data_shape
    data_dtype = data_and_metadata.data_dtype

    def calculate_data():
        data = data_and_metadata.data
        if data is None or not Image.is_data_valid(data):
            return None
        # scaling: numpy.sqrt(numpy.mean(numpy.absolute(data_copy)**2)) == numpy.sqrt(numpy.mean(numpy.absolute(data_copy_fft)**2))
        # see https://gist.github.com/endolith/1257010
        if Image.is_data_1d(data):
            scaling = 1.0 / numpy.sqrt(data_shape[0])
            return scipy.fftpack.fftshift(scipy.fftpack.fft(data) * scaling)
        elif Image.is_data_2d(data):
            data_copy = data.copy()  # let other threads use data while we're processing
            scaling = 1.0 / numpy.sqrt(data_shape[1] * data_shape[0])
            return scipy.fftpack.fftshift(scipy.fftpack.fft2(data_copy) * scaling)
        else:
            raise NotImplementedError()

    src_dimensional_calibrations = data_and_metadata.dimensional_calibrations

    if not Image.is_shape_and_dtype_valid(data_shape, data_dtype) or src_dimensional_calibrations is None:
        return None

    assert len(src_dimensional_calibrations) == len(
        Image.dimensional_shape_from_shape_and_dtype(data_shape, data_dtype))

    data_shape_and_dtype = data_shape, numpy.dtype(numpy.complex128)

    dimensional_calibrations = [Calibration.Calibration(0.0, 1.0 / (dimensional_calibration.scale * data_shape_n),
                                                        "1/" + dimensional_calibration.units) for
        dimensional_calibration, data_shape_n in zip(src_dimensional_calibrations, data_shape)]

    return DataAndMetadata.DataAndMetadata(calculate_data, data_shape_and_dtype, Calibration.Calibration(),
                                           dimensional_calibrations, dict(), datetime.datetime.utcnow())


def function_ifft(data_and_metadata):
    data_shape = data_and_metadata.data_shape
    data_dtype = data_and_metadata.data_dtype

    def calculate_data():
        data = data_and_metadata.data
        if data is None or not Image.is_data_valid(data):
            return None
        # scaling: numpy.sqrt(numpy.mean(numpy.absolute(data_copy)**2)) == numpy.sqrt(numpy.mean(numpy.absolute(data_copy_fft)**2))
        # see https://gist.github.com/endolith/1257010
        if Image.is_data_1d(data):
            scaling = numpy.sqrt(data_shape[0])
            return scipy.fftpack.fftshift(scipy.fftpack.ifft(data) * scaling)
        elif Image.is_data_2d(data):
            data_copy = data.copy()  # let other threads use data while we're processing
            scaling = numpy.sqrt(data_shape[1] * data_shape[0])
            return scipy.fftpack.ifft2(scipy.fftpack.ifftshift(data_copy) * scaling)
        else:
            raise NotImplementedError()

    src_dimensional_calibrations = data_and_metadata.dimensional_calibrations

    if not Image.is_shape_and_dtype_valid(data_shape, data_dtype) or src_dimensional_calibrations is None:
        return None

    assert len(src_dimensional_calibrations) == len(
        Image.dimensional_shape_from_shape_and_dtype(data_shape, data_dtype))

    data_shape_and_dtype = data_shape, data_dtype

    dimensional_calibrations = [Calibration.Calibration(0.0, 1.0 / (dimensional_calibration.scale * data_shape_n),
                                                        "1/" + dimensional_calibration.units) for
        dimensional_calibration, data_shape_n in zip(src_dimensional_calibrations, data_shape)]

    return DataAndMetadata.DataAndMetadata(calculate_data, data_shape_and_dtype, Calibration.Calibration(),
                                           dimensional_calibrations, dict(), datetime.datetime.utcnow())


def function_autocorrelate(data_and_metadata):
    def calculate_data():
        data = data_and_metadata.data
        if data is None or not Image.is_data_valid(data):
            return None
        if Image.is_data_2d(data):
            data_copy = data.copy()  # let other threads use data while we're processing
            data_std = data_copy.std(dtype=numpy.float64)
            if data_std != 0.0:
                data_norm = (data_copy - data_copy.mean(dtype=numpy.float64)) / data_std
            else:
                data_norm = data_copy
            scaling = 1.0 / (data_norm.shape[0] * data_norm.shape[1])
            data_norm = numpy.fft.rfft2(data_norm)
            return numpy.fft.fftshift(numpy.fft.irfft2(data_norm * numpy.conj(data_norm))) * scaling
            # this gives different results. why? because for some reason scipy pads out to 1023 and does calculation.
            # see https://github.com/scipy/scipy/blob/master/scipy/signal/signaltools.py
            # return scipy.signal.fftconvolve(data_copy, numpy.conj(data_copy), mode='same')
        return None

    if data_and_metadata is None:
        return None

    dimensional_calibrations = [Calibration.Calibration() for _ in data_and_metadata.data_shape]

    return DataAndMetadata.DataAndMetadata(calculate_data, data_and_metadata.data_shape_and_dtype,
                                           Calibration.Calibration(), dimensional_calibrations, dict(),
                                           datetime.datetime.utcnow())


def function_crosscorrelate(*args):
    if len(args) != 2:
        return None

    data_and_metadata1, data_and_metadata2 = args[0], args[1]

    def calculate_data():
        data1 = data_and_metadata1.data
        data2 = data_and_metadata2.data
        if data1 is None or data2 is None:
            return None
        if Image.is_data_2d(data1) and Image.is_data_2d(data2):
            data_std1 = data1.std(dtype=numpy.float64)
            if data_std1 != 0.0:
                norm1 = (data1 - data1.mean(dtype=numpy.float64)) / data_std1
            else:
                norm1 = data1
            data_std2 = data2.std(dtype=numpy.float64)
            if data_std2 != 0.0:
                norm2 = (data2 - data2.mean(dtype=numpy.float64)) / data_std2
            else:
                norm2 = data2
            scaling = 1.0 / (norm1.shape[0] * norm1.shape[1])
            return numpy.fft.fftshift(numpy.fft.irfft2(numpy.fft.rfft2(norm1) * numpy.conj(numpy.fft.rfft2(norm2)))) * scaling
            # this gives different results. why? because for some reason scipy pads out to 1023 and does calculation.
            # see https://github.com/scipy/scipy/blob/master/scipy/signal/signaltools.py
            # return scipy.signal.fftconvolve(data1.copy(), numpy.conj(data2.copy()), mode='same')
        return None

    if data_and_metadata1 is None or data_and_metadata2 is None:
        return None

    dimensional_calibrations = [Calibration.Calibration() for _ in data_and_metadata1.data_shape]

    return DataAndMetadata.DataAndMetadata(calculate_data, data_and_metadata1.data_shape_and_dtype,
                                           Calibration.Calibration(), dimensional_calibrations, dict(),
                                           datetime.datetime.utcnow())


def function_sobel(data_and_metadata):
    def calculate_data():
        data = data_and_metadata.data
        if not Image.is_data_valid(data):
            return None
        if Image.is_shape_and_dtype_rgb(data.shape, data.dtype):
            rgb = numpy.empty(data.shape[:-1] + (3,), numpy.uint8)
            rgb[..., 0] = scipy.ndimage.sobel(data[..., 0])
            rgb[..., 1] = scipy.ndimage.sobel(data[..., 1])
            rgb[..., 2] = scipy.ndimage.sobel(data[..., 2])
            return rgb
        elif Image.is_shape_and_dtype_rgba(data.shape, data.dtype):
            rgba = numpy.empty(data.shape[:-1] + (4,), numpy.uint8)
            rgba[..., 0] = scipy.ndimage.sobel(data[..., 0])
            rgba[..., 1] = scipy.ndimage.sobel(data[..., 1])
            rgba[..., 2] = scipy.ndimage.sobel(data[..., 2])
            rgba[..., 3] = data[..., 3]
            return rgba
        else:
            return scipy.ndimage.sobel(data)

    return DataAndMetadata.DataAndMetadata(calculate_data, data_and_metadata.data_shape_and_dtype,
                                           data_and_metadata.intensity_calibration,
                                           data_and_metadata.dimensional_calibrations, data_and_metadata.metadata,
                                           datetime.datetime.utcnow())


def function_laplace(data_and_metadata):
    def calculate_data():
        data = data_and_metadata.data
        if not Image.is_data_valid(data):
            return None
        if Image.is_shape_and_dtype_rgb(data.shape, data.dtype):
            rgb = numpy.empty(data.shape[:-1] + (3,), numpy.uint8)
            rgb[..., 0] = scipy.ndimage.laplace(data[..., 0])
            rgb[..., 1] = scipy.ndimage.laplace(data[..., 1])
            rgb[..., 2] = scipy.ndimage.laplace(data[..., 2])
            return rgb
        elif Image.is_shape_and_dtype_rgba(data.shape, data.dtype):
            rgba = numpy.empty(data.shape[:-1] + (4,), numpy.uint8)
            rgba[..., 0] = scipy.ndimage.laplace(data[..., 0])
            rgba[..., 1] = scipy.ndimage.laplace(data[..., 1])
            rgba[..., 2] = scipy.ndimage.laplace(data[..., 2])
            rgba[..., 3] = data[..., 3]
            return rgba
        else:
            return scipy.ndimage.laplace(data)

    return DataAndMetadata.DataAndMetadata(calculate_data, data_and_metadata.data_shape_and_dtype,
                                           data_and_metadata.intensity_calibration,
                                           data_and_metadata.dimensional_calibrations, data_and_metadata.metadata,
                                           datetime.datetime.utcnow())


def function_gaussian_blur(data_and_metadata, sigma):
    sigma = float(sigma)

    def calculate_data():
        data = data_and_metadata.data
        if not Image.is_data_valid(data):
            return None
        return scipy.ndimage.gaussian_filter(data, sigma=sigma)

    return DataAndMetadata.DataAndMetadata(calculate_data, data_and_metadata.data_shape_and_dtype,
                                           data_and_metadata.intensity_calibration,
                                           data_and_metadata.dimensional_calibrations, data_and_metadata.metadata,
                                           datetime.datetime.utcnow())


def function_median_filter(data_and_metadata, size):
    size = max(min(int(size), 999), 1)

    def calculate_data():
        data = data_and_metadata.data
        if not Image.is_data_valid(data):
            return None
        if Image.is_shape_and_dtype_rgb(data.shape, data.dtype):
            rgb = numpy.empty(data.shape[:-1] + (3,), numpy.uint8)
            rgb[..., 0] = scipy.ndimage.median_filter(data[..., 0], size=size)
            rgb[..., 1] = scipy.ndimage.median_filter(data[..., 1], size=size)
            rgb[..., 2] = scipy.ndimage.median_filter(data[..., 2], size=size)
            return rgb
        elif Image.is_shape_and_dtype_rgba(data.shape, data.dtype):
            rgba = numpy.empty(data.shape[:-1] + (4,), numpy.uint8)
            rgba[..., 0] = scipy.ndimage.median_filter(data[..., 0], size=size)
            rgba[..., 1] = scipy.ndimage.median_filter(data[..., 1], size=size)
            rgba[..., 2] = scipy.ndimage.median_filter(data[..., 2], size=size)
            rgba[..., 3] = data[..., 3]
            return rgba
        else:
            return scipy.ndimage.median_filter(data, size=size)

    return DataAndMetadata.DataAndMetadata(calculate_data, data_and_metadata.data_shape_and_dtype,
                                           data_and_metadata.intensity_calibration,
                                           data_and_metadata.dimensional_calibrations, data_and_metadata.metadata,
                                           datetime.datetime.utcnow())


def function_uniform_filter(data_and_metadata, size):
    size = max(min(int(size), 999), 1)

    def calculate_data():
        data = data_and_metadata.data
        if not Image.is_data_valid(data):
            return None
        if Image.is_shape_and_dtype_rgb(data.shape, data.dtype):
            rgb = numpy.empty(data.shape[:-1] + (3,), numpy.uint8)
            rgb[..., 0] = scipy.ndimage.uniform_filter(data[..., 0], size=size)
            rgb[..., 1] = scipy.ndimage.uniform_filter(data[..., 1], size=size)
            rgb[..., 2] = scipy.ndimage.uniform_filter(data[..., 2], size=size)
            return rgb
        elif Image.is_shape_and_dtype_rgba(data.shape, data.dtype):
            rgba = numpy.empty(data.shape[:-1] + (4,), numpy.uint8)
            rgba[..., 0] = scipy.ndimage.uniform_filter(data[..., 0], size=size)
            rgba[..., 1] = scipy.ndimage.uniform_filter(data[..., 1], size=size)
            rgba[..., 2] = scipy.ndimage.uniform_filter(data[..., 2], size=size)
            rgba[..., 3] = data[..., 3]
            return rgba
        else:
            return scipy.ndimage.uniform_filter(data, size=size)

    return DataAndMetadata.DataAndMetadata(calculate_data, data_and_metadata.data_shape_and_dtype,
                                           data_and_metadata.intensity_calibration,
                                           data_and_metadata.dimensional_calibrations, data_and_metadata.metadata,
                                           datetime.datetime.utcnow())


def function_transpose_flip(data_and_metadata, transpose=False, flip_v=False, flip_h=False):
    def calculate_data():
        data = data_and_metadata.data
        data_id = id(data)
        if not Image.is_data_valid(data):
            return None
        if transpose:
            if Image.is_shape_and_dtype_rgb_type(data.shape, data.dtype):
                data = numpy.transpose(data, [1, 0, 2])
            else:
                data = numpy.transpose(data, [1, 0])
        if flip_h:
            data = numpy.fliplr(data)
        if flip_v:
            data = numpy.flipud(data)
        if id(data) == data_id:  # ensure real data, not a view
            data = data.copy()
        return data

    data_shape = data_and_metadata.data_shape
    data_dtype = data_and_metadata.data_dtype

    if not Image.is_shape_and_dtype_valid(data_shape, data_dtype):
        return None

    if transpose:
        dimensional_calibrations = list(reversed(data_and_metadata.dimensional_calibrations))
    else:
        dimensional_calibrations = data_and_metadata.dimensional_calibrations

    if transpose:
        if Image.is_shape_and_dtype_rgb_type(data_shape, data_dtype):
            data_shape = list(reversed(data_shape[0:2])) + [data_shape[-1], ]
        else:
            data_shape = list(reversed(data_shape))

    return DataAndMetadata.DataAndMetadata(calculate_data, (data_shape, data_dtype),
                                           data_and_metadata.intensity_calibration, dimensional_calibrations,
                                           data_and_metadata.metadata, datetime.datetime.utcnow())


def function_crop(data_and_metadata, bounds):
    data_shape = data_and_metadata.data_shape
    data_dtype = data_and_metadata.data_dtype

    def calculate_data():
        data = data_and_metadata.data
        if not Image.is_data_valid(data):
            return None
        data_shape = data_and_metadata.data_shape
        bounds_int = ((int(data_shape[0] * bounds[0][0]), int(data_shape[1] * bounds[0][1])),
            (int(data_shape[0] * bounds[1][0]), int(data_shape[1] * bounds[1][1])))
        return data[bounds_int[0][0]:bounds_int[0][0] + bounds_int[1][0],
            bounds_int[0][1]:bounds_int[0][1] + bounds_int[1][1]].copy()

    dimensional_calibrations = data_and_metadata.dimensional_calibrations

    if not Image.is_shape_and_dtype_valid(data_shape, data_dtype) or dimensional_calibrations is None:
        return None

    bounds_int = ((int(data_shape[0] * bounds[0][0]), int(data_shape[1] * bounds[0][1])),
        (int(data_shape[0] * bounds[1][0]), int(data_shape[1] * bounds[1][1])))

    if Image.is_shape_and_dtype_rgb_type(data_shape, data_dtype):
        data_shape_and_dtype = bounds_int[1] + (data_shape[-1], ), data_dtype
    else:
        data_shape_and_dtype = bounds_int[1], data_dtype

    cropped_dimensional_calibrations = list()
    for index, dimensional_calibration in enumerate(dimensional_calibrations):
        cropped_calibration = Calibration.Calibration(
            dimensional_calibration.offset + data_shape[index] * bounds[0][index] * dimensional_calibration.scale,
            dimensional_calibration.scale, dimensional_calibration.units)
        cropped_dimensional_calibrations.append(cropped_calibration)

    return DataAndMetadata.DataAndMetadata(calculate_data, data_shape_and_dtype,
                                           data_and_metadata.intensity_calibration, cropped_dimensional_calibrations,
                                           data_and_metadata.metadata, datetime.datetime.utcnow())


def function_slice_sum(data_and_metadata, slice_center, slice_width):
    slice_center = int(slice_center)
    slice_width = int(slice_width)

    data_shape = data_and_metadata.data_shape
    data_dtype = data_and_metadata.data_dtype

    def calculate_data():
        data = data_and_metadata.data
        if not Image.is_data_valid(data):
            return None
        shape = data.shape
        slice_start = int(slice_center - slice_width * 0.5 + 0.5)
        slice_start = max(slice_start, 0)
        slice_end = slice_start + slice_width
        slice_end = min(shape[0], slice_end)
        return numpy.sum(data[slice_start:slice_end,:], 0)

    dimensional_calibrations = data_and_metadata.dimensional_calibrations

    if not Image.is_shape_and_dtype_valid(data_shape, data_dtype) or dimensional_calibrations is None:
        return None

    data_shape_and_dtype = data_shape[1:], data_dtype

    dimensional_calibrations = dimensional_calibrations[1:]

    return DataAndMetadata.DataAndMetadata(calculate_data, data_shape_and_dtype,
                                           data_and_metadata.intensity_calibration, dimensional_calibrations,
                                           data_and_metadata.metadata, datetime.datetime.utcnow())


def function_pick(data_and_metadata, position):
    data_shape = data_and_metadata.data_shape
    data_dtype = data_and_metadata.data_dtype

    def calculate_data():
        data = data_and_metadata.data
        if not Image.is_data_valid(data):
            return None
        data_shape = data_and_metadata.data_shape
        position_f = Geometry.FloatPoint.make(position)
        position_i = Geometry.IntPoint(y=position_f.y * data_shape[1], x=position_f.x * data_shape[2])
        if position_i.y >= 0 and position_i.y < data_shape[1] and position_i.x >= 0 and position_i.x < data_shape[2]:
            return data[:, position_i[0], position_i[1]].copy()
        else:
            return numpy.zeros((data_shape[:-2], ), dtype=data.dtype)

    dimensional_calibrations = data_and_metadata.dimensional_calibrations

    if not Image.is_shape_and_dtype_valid(data_shape, data_dtype) or dimensional_calibrations is None:
        return None

    data_shape_and_dtype = data_shape[:-2], data_dtype

    dimensional_calibrations = dimensional_calibrations[0:-2]

    return DataAndMetadata.DataAndMetadata(calculate_data, data_shape_and_dtype,
                                           data_and_metadata.intensity_calibration, dimensional_calibrations,
                                           data_and_metadata.metadata, datetime.datetime.utcnow())


def function_data_slice(data_and_metadata, key):
    """Slice data.

    a[2, :]

    Keeps calibrations.
    """

    # (4, 8, 8)[:, 4, 4]
    # (4, 8, 8)[:, :, 4]
    # (4, 8, 8)[:, 4:4, 4]
    # (4, 8, 8)[:, 4:5, 4]
    # (4, 8, 8)[2, ...]
    # (4, 8, 8)[..., 2]
    # (4, 8, 8)[2, ..., 2]

    slices = list_to_key(key)

    def calculate_data():
        data = data_and_metadata.data
        return data[slices].copy()

    if data_and_metadata is None:
        return None

    def non_ellipses_count(slices):
        return sum(1 if isinstance(slice, type(Ellipsis)) else 0 for slice in slices)

    def normalize_slice(index: int, s: slice, shape: List[int], ellipse_count: int):
        size = shape[index]
        collapsible = False
        if isinstance(s, type(Ellipsis)):
            # for the ellipse, return a full slice for each ellipse dimension
            slices = list()
            for ellipse_index in range(ellipse_count):
                slices.append((False, slice(0, shape[index + ellipse_index], 1)))
            return slices
        elif isinstance(s, numbers.Integral):
            s = slice(s, s + 1, 1)
            collapsible = True
        elif s is None:
            s = slice(0, size, 1)
        s_start = s.start
        s_stop = s.stop
        s_step = s.step
        s_start = s_start if s_start is not None else 0
        s_start = size + s_start if s_start < 0 else s_start
        s_stop = s_stop if s_stop is not None else size
        s_stop = size + s_stop if s_stop < 0 else s_stop
        s_step = s_step if s_step is not None else 1
        return [(collapsible, slice(s_start, s_stop, s_step))]

    ellipse_count = len(data_and_metadata.data_shape) - non_ellipses_count(slices)
    normalized_slices = list()  # type: List[(bool, slice)]
    for index, s in enumerate(slices):
        normalized_slices.extend(normalize_slice(index, s, data_and_metadata.data_shape, ellipse_count))

    if any(s.start >= s.stop for c, s in normalized_slices):
        return None

    data_shape = [abs(s.start - s.stop) // s.step for c, s in normalized_slices if not c]

    uncollapsed_data_shape = [abs(s.start - s.stop) // s.step for c, s in normalized_slices]

    cropped_dimensional_calibrations = list()

    for index, dimensional_calibration in enumerate(data_and_metadata.dimensional_calibrations):
        if not normalized_slices[index][0]:  # not collapsible
            cropped_calibration = Calibration.Calibration(
                dimensional_calibration.offset + uncollapsed_data_shape[index] * normalized_slices[index][1].start * dimensional_calibration.scale,
                dimensional_calibration.scale / normalized_slices[index][1].step, dimensional_calibration.units)
            cropped_dimensional_calibrations.append(cropped_calibration)

    return DataAndMetadata.DataAndMetadata(calculate_data, (data_shape, data_and_metadata.data_dtype),
                                           data_and_metadata.intensity_calibration, cropped_dimensional_calibrations,
                                           data_and_metadata.metadata, datetime.datetime.utcnow())


def function_concatenate(*args, axis=0):
    """Concatenate multiple data_and_metadatas.

    concatenate((a, b, c), 1)

    Function is called by passing a tuple of the list of source items, which matches the
    form of the numpy function of the same name.

    Keeps intensity calibration of first source item.

    Keeps dimensional calibration in axis dimension.
    """
    data_and_metadata_list = tuple(args)

    partial_shape = data_and_metadata_list[0].data_shape

    def calculate_data():
        if any([data_and_metadata.data is None for data_and_metadata in data_and_metadata_list]):
            return None
        if all([data_and_metadata.data_shape[1:] == partial_shape[1:] for data_and_metadata in data_and_metadata_list]):
            data_list = list(data_and_metadata.data for data_and_metadata in data_and_metadata_list)
            return numpy.concatenate(data_list, axis)
        return None

    if len(data_and_metadata_list) < 1:
        return None

    if any([data_and_metadata.data is None for data_and_metadata in data_and_metadata_list]):
        return None

    if any([data_and_metadata.data_shape != partial_shape[1:] is None for data_and_metadata in data_and_metadata_list]):
        return None

    dimensional_calibrations = list()
    for index, dimensional_calibration in enumerate(data_and_metadata_list[0].dimensional_calibrations):
        if index != axis:
            dimensional_calibrations.append(Calibration.Calibration())
        else:
            dimensional_calibrations.append(dimensional_calibration)

    new_shape = [sum([data_and_metadata.data_shape[0] for data_and_metadata in data_and_metadata_list]), ] + list(partial_shape[1:])

    intensity_calibration = data_and_metadata_list[0].intensity_calibration

    return DataAndMetadata.DataAndMetadata(calculate_data, (new_shape, data_and_metadata_list[0].data_dtype), intensity_calibration, dimensional_calibrations, dict(),
                                           datetime.datetime.utcnow())


def function_project(data_and_metadata):
    data_shape = data_and_metadata.data_shape
    data_dtype = data_and_metadata.data_dtype

    def calculate_data():
        data = data_and_metadata.data
        if not Image.is_data_valid(data):
            return None
        if Image.is_shape_and_dtype_rgb_type(data.shape, data.dtype):
            if Image.is_shape_and_dtype_rgb(data.shape, data.dtype):
                rgb_image = numpy.empty(data.shape[1:], numpy.uint8)
                rgb_image[:,0] = numpy.average(data[...,0], 0)
                rgb_image[:,1] = numpy.average(data[...,1], 0)
                rgb_image[:,2] = numpy.average(data[...,2], 0)
                return rgb_image
            else:
                rgba_image = numpy.empty(data.shape[1:], numpy.uint8)
                rgba_image[:,0] = numpy.average(data[...,0], 0)
                rgba_image[:,1] = numpy.average(data[...,1], 0)
                rgba_image[:,2] = numpy.average(data[...,2], 0)
                rgba_image[:,3] = numpy.average(data[...,3], 0)
                return rgba_image
        else:
            return numpy.sum(data, 0)

    dimensional_calibrations = data_and_metadata.dimensional_calibrations

    if not Image.is_shape_and_dtype_valid(data_shape, data_dtype) or dimensional_calibrations is None:
        return None

    data_shape_and_dtype = data_shape[1:], data_dtype

    dimensional_calibrations = dimensional_calibrations[1:]

    return DataAndMetadata.DataAndMetadata(calculate_data, data_shape_and_dtype,
                                           data_and_metadata.intensity_calibration, dimensional_calibrations,
                                           data_and_metadata.metadata, datetime.datetime.utcnow())


def function_reshape(data_and_metadata, shape):
    """Reshape a data and metadata to shape.

    reshape(a, shape(4, 5))
    reshape(a, data_shape(b))

    Handles special cases when going to one extra dimension and when going to one fewer
    dimension -- namely to keep the calibrations intact.

    When increasing dimension, a -1 can be passed for the new dimension and this function
    will calculate the missing value.
    """
    data_shape = data_and_metadata.data_shape
    data_dtype = data_and_metadata.data_dtype

    def calculate_data():
        data = data_and_metadata.data
        if not Image.is_data_valid(data):
            return None
        return numpy.reshape(data, shape)

    dimensional_calibrations = data_and_metadata.dimensional_calibrations

    if not Image.is_shape_and_dtype_valid(data_shape, data_dtype) or dimensional_calibrations is None:
        return None

    total_old_pixels = 1
    for dimension in data_shape:
        total_old_pixels *= dimension
    total_new_pixels = 1
    for dimension in shape:
        total_new_pixels *= dimension if dimension > 0 else 1
    new_dimensional_calibrations = list()
    new_dimensions = list()
    if len(data_shape) + 1 == len(shape) and -1 in shape:
        # special case going to one more dimension
        index = 0
        for dimension in shape:
            if dimension == -1:
                new_dimensional_calibrations.append(Calibration.Calibration())
                new_dimensions.append(total_old_pixels / total_new_pixels)
            else:
                new_dimensional_calibrations.append(dimensional_calibrations[index])
                new_dimensions.append(dimension)
                index += 1
    elif len(data_shape) - 1 == len(shape) and 1 in data_shape:
        # special case going to one fewer dimension
        for dimension, dimensional_calibration in zip(data_shape, dimensional_calibrations):
            if dimension == 1:
                continue
            else:
                new_dimensional_calibrations.append(dimensional_calibration)
                new_dimensions.append(dimension)
    else:
        for _ in range(len(shape)):
            new_dimensional_calibrations.append(Calibration.Calibration())

    data_shape_and_dtype = new_dimensions, data_dtype

    return DataAndMetadata.DataAndMetadata(calculate_data, data_shape_and_dtype,
                                           data_and_metadata.intensity_calibration, new_dimensional_calibrations,
                                           data_and_metadata.metadata, datetime.datetime.utcnow())


def function_resample_2d(data_and_metadata, shape):
    height = int(shape[0])
    width = int(shape[1])

    data_shape = data_and_metadata.data_shape
    data_dtype = data_and_metadata.data_dtype

    def calculate_data():
        data = data_and_metadata.data
        if not Image.is_data_valid(data):
            return None
        if not Image.is_data_2d(data):
            return None
        if data.shape[0] == height and data.shape[1] == width:
            return data.copy()
        return Image.scaled(data, (height, width))

    dimensional_calibrations = data_and_metadata.dimensional_calibrations

    if not Image.is_shape_and_dtype_valid(data_shape, data_dtype) or dimensional_calibrations is None:
        return None

    if not Image.is_shape_and_dtype_2d(data_shape, data_dtype):
        return None

    if Image.is_shape_and_dtype_rgb_type(data_shape, data_dtype):
        data_shape_and_dtype = (height, width, data_shape[-1]), data_dtype
    else:
        data_shape_and_dtype = (height, width), data_dtype

    dimensions = height, width
    resampled_dimensional_calibrations = [Calibration.Calibration(dimensional_calibrations[i].offset, dimensional_calibrations[i].scale * data_shape[i] / dimensions[i], dimensional_calibrations[i].units) for i in range(len(dimensional_calibrations))]

    return DataAndMetadata.DataAndMetadata(calculate_data, data_shape_and_dtype,
                                           data_and_metadata.intensity_calibration, resampled_dimensional_calibrations,
                                           data_and_metadata.metadata, datetime.datetime.utcnow())


def function_histogram(data_and_metadata, bins):
    bins = int(bins)

    data_shape = data_and_metadata.data_shape
    data_dtype = data_and_metadata.data_dtype

    def calculate_data():
        data = data_and_metadata.data
        if not Image.is_data_valid(data):
            return None
        histogram_data = numpy.histogram(data, bins=bins)
        return histogram_data[0].astype(numpy.int)

    dimensional_calibrations = data_and_metadata.dimensional_calibrations

    if not Image.is_shape_and_dtype_valid(data_shape, data_dtype) or dimensional_calibrations is None:
        return None

    data_shape_and_dtype = (bins, ), numpy.dtype(numpy.int)

    dimensional_calibrations = [Calibration.Calibration()]

    return DataAndMetadata.DataAndMetadata(calculate_data, data_shape_and_dtype,
                                           data_and_metadata.intensity_calibration, dimensional_calibrations,
                                           data_and_metadata.metadata, datetime.datetime.utcnow())


def function_line_profile(data_and_metadata, vector, integration_width):
    integration_width = int(integration_width)

    data_shape = data_and_metadata.data_shape
    data_dtype = data_and_metadata.data_dtype

    # calculate grid of coordinates. returns n coordinate arrays for each row.
    # start and end are in data coordinates.
    # n is a positive integer, not zero
    def get_coordinates(start, end, n):
        assert n > 0 and int(n) == n
        # n=1 => 0
        # n=2 => -0.5, 0.5
        # n=3 => -1, 0, 1
        # n=4 => -1.5, -0.5, 0.5, 1.5
        length = math.sqrt(math.pow(end[0] - start[0], 2) + math.pow(end[1] - start[1], 2))
        l = math.floor(length)
        a = numpy.linspace(0, length, l)  # along
        t = numpy.linspace(-(n-1)*0.5, (n-1)*0.5, n)  # transverse
        dy = (end[0] - start[0]) / length
        dx = (end[1] - start[1]) / length
        ix, iy = numpy.meshgrid(a, t)
        yy = start[0] + dy * ix + dx * iy
        xx = start[1] + dx * ix - dy * iy
        return xx, yy

    # xx, yy = __coordinates(None, (4,4), (8,4), 3)

    def calculate_data():
        data = data_and_metadata.data
        if not Image.is_data_valid(data):
            return None
        if Image.is_data_rgb_type(data):
            data = Image.convert_to_grayscale(data, numpy.double)
        start, end = vector
        shape = data.shape
        actual_integration_width = min(max(shape[0], shape[1]), integration_width)  # limit integration width to sensible value
        start_data = (int(shape[0]*start[0]), int(shape[1]*start[1]))
        end_data = (int(shape[0]*end[0]), int(shape[1]*end[1]))
        length = math.sqrt(math.pow(end_data[1] - start_data[1], 2) + math.pow(end_data[0] - start_data[0], 2))
        if length > 1.0:
            spline_order_lookup = { "nearest": 0, "linear": 1, "quadratic": 2, "cubic": 3 }
            method = "nearest"
            spline_order = spline_order_lookup[method]
            xx, yy = get_coordinates(start_data, end_data, actual_integration_width)
            samples = scipy.ndimage.map_coordinates(data, (yy, xx), order=spline_order)
            if len(samples.shape) > 1:
                return numpy.sum(samples, 0) / actual_integration_width
            else:
                return samples
        return numpy.zeros((1))

    dimensional_calibrations = data_and_metadata.dimensional_calibrations

    if not Image.is_shape_and_dtype_valid(data_shape, data_dtype) or dimensional_calibrations is None:
        return None

    if dimensional_calibrations is None or len(dimensional_calibrations) != 2:
        return None

    import math

    start, end = vector
    shape = data_shape
    start_int = (int(shape[0]*start[0]), int(shape[1]*start[1]))
    end_int = (int(shape[0]*end[0]), int(shape[1]*end[1]))
    length = int(math.sqrt((end_int[1] - start_int[1])**2 + (end_int[0] - start_int[0])**2))
    length = max(length, 1)
    data_shape_and_dtype = (length, ), numpy.dtype(numpy.double)

    dimensional_calibrations = [Calibration.Calibration(0.0, dimensional_calibrations[1].scale, dimensional_calibrations[1].units)]

    return DataAndMetadata.DataAndMetadata(calculate_data, data_shape_and_dtype,
                                           data_and_metadata.intensity_calibration, dimensional_calibrations,
                                           data_and_metadata.metadata, datetime.datetime.utcnow())

def function_make_point(y, x):
    return y, x

def function_make_size(height, width):
    return height, width

def function_make_vector(start, end):
    return start, end

def function_make_rectangle_origin_size(origin, size):
    return tuple(Geometry.FloatRect(origin, size))

def function_make_rectangle_center_size(center, size):
    return tuple(Geometry.FloatRect.from_center_and_size(center, size))

def function_make_interval(start, end):
    return start, end

def function_make_shape(*args):
    return tuple(args)


_function2_map = {
    "fft": function_fft,
    "ifft": function_ifft,
    "autocorrelate": function_autocorrelate,
    "crosscorrelate": function_crosscorrelate,
    "sobel": function_sobel,
    "laplace": function_laplace,
    "gaussian_blur": function_gaussian_blur,
    "median_filter": function_median_filter,
    "uniform_filter": function_uniform_filter,
    "transpose_flip": function_transpose_flip,
    "crop": function_crop,
    "slice_sum": function_slice_sum,
    "pick": function_pick,
    "project": function_project,
    "resample_image": function_resample_2d,
    "histogram": function_histogram,
    "line_profile": function_line_profile,
    "data_slice": function_data_slice,
    "concatenate": function_concatenate,
    "reshape": function_reshape,
}

_operator_map = {
    "pow": ["**", 9],
    "neg": ["-", 8],
    "pos": ["+", 8],
    "add": ["+", 6],
    "sub": ["-", 6],
    "mul": ["*", 7],
    "div": ["/", 7],
    "truediv": ["/", 7],
    "floordiv": ["//", 7],
    "mod": ["%", 7],
}

_function_map = {
    "abs": operator.abs,
    "neg": operator.neg,
    "pos": operator.pos,
    "add": operator.add,
    "sub": operator.sub,
    "mul": operator.mul,
    "div": operator.truediv,
    "truediv": operator.truediv,
    "floordiv": operator.floordiv,
    "mod": operator.mod,
    "pow": operator.pow,
    "column": column,
    "row": row,
    "radius": radius,
    "item": take_item,
    "amin": numpy.amin,
    "amax": numpy.amax,
    "arange": arange,
    "median": numpy.median,
    "average": numpy.average,
    "mean": numpy.mean,
    "std": numpy.std,
    "var": numpy.var,
    # trig functions
    "sin": numpy.sin,
    "cos": numpy.cos,
    "tan": numpy.tan,
    "arcsin": numpy.arcsin,
    "arccos": numpy.arccos,
    "arctan": numpy.arctan,
    "hypot": numpy.hypot,
    "arctan2": numpy.arctan2,
    "degrees": numpy.degrees,
    "radians": numpy.radians,
    "rad2deg": numpy.rad2deg,
    "deg2rad": numpy.deg2rad,
    # rounding
    "around": numpy.around,
    "round": numpy.round,
    "rint": numpy.rint,
    "fix": numpy.fix,
    "floor": numpy.floor,
    "ceil": numpy.ceil,
    "trunc": numpy.trunc,
    # exponents and logarithms
    "exp": numpy.exp,
    "expm1": numpy.expm1,
    "exp2": numpy.exp2,
    "log": numpy.log,
    "log10": numpy.log10,
    "log2": numpy.log2,
    "log1p": numpy.log1p,
    # other functions
    "reciprocal": numpy.reciprocal,
    "clip": numpy.clip,
    "sqrt": numpy.sqrt,
    "square": numpy.square,
    "nan_to_num": numpy.nan_to_num,
    # complex numbers
    "angle": numpy.angle,
    "real": numpy.real,
    "imag": numpy.imag,
    "conj": numpy.conj,
    # conversions
    "astype": astype,
    # data functions
    "data_shape": data_shape,
    "shape": function_make_shape,
    "vector": function_make_vector,
    "rectangle_from_origin_size": function_make_rectangle_origin_size,
    "rectangle_from_center_size": function_make_rectangle_center_size,
    "normalized_point": function_make_point,
    "normalized_size": function_make_size,
    "normalized_interval": function_make_interval,
}

def reconstruct_inputs(variable_map, inputs):
    input_texts = list()
    for input in inputs:
        text, precedence = input.reconstruct(variable_map)
        input_texts.append((text, precedence))
    return input_texts


def extract_data(evaluated_input):
    if isinstance(evaluated_input, DataAndMetadata.DataAndMetadata):
        return evaluated_input.data
    return evaluated_input


def key_to_list(key):
    if not isinstance(key, tuple):
        key = (key, )
    l = list()
    for k in key:
        if isinstance(k, slice):
            d = dict()
            if k.start is not None:
                d["start"] = k.start
            if k.stop is not None:
                d["stop"] = k.stop
            if k.step is not None:
                d["step"] = k.step
            l.append(d)
        elif isinstance(k, numbers.Integral):
            l.append({"index": k})
        elif isinstance(k, type(Ellipsis)):
            l.append({"ellipses": True})
        elif k is None:
            l.append({"newaxis": True})
        else:
            print(type(k))
            assert False
    return l


def list_to_key(l):
    key = list()
    for d in l:
        if "index" in d:
            key.append(d.get("index"))
        elif d.get("ellipses", False):
            key.append(Ellipsis)
        elif d.get("newaxis", False):
            key.append(None)
        else:
            key.append(slice(d.get("start"), d.get("stop"), d.get("step")))
    if len(key) == 1:
        return key[0]
    return key


dtype_map = {int: "int", float: "float", complex: "complex", numpy.int16: "int16", numpy.int32: "int32",
    numpy.int64: "int64", numpy.uint8: "uint8", numpy.uint16: "uint16", numpy.uint32: "uint32", numpy.uint64: "uint64",
    numpy.float32: "float32", numpy.float64: "float64", numpy.complex64: "complex64", numpy.complex128: "complex128"}

dtype_inverse_map = {dtype_map[k]: k for k in dtype_map}

def str_to_dtype(str: str) -> numpy.dtype:
    return dtype_inverse_map.get(str, float)

def dtype_to_str(dtype: numpy.dtype) -> str:
    return dtype_map.get(dtype, "float")


class DataNode(object):

    def __init__(self, inputs=None):
        self.uuid = uuid.uuid4()
        self.inputs = inputs if inputs is not None else list()

    def __deepcopy__(self, memo):
        new = self.__class__()
        new.deepcopy_from(self, memo)
        memo[id(self)] = new
        return new

    def deepcopy_from(self, node, memo):
        self.uuid = node.uuid
        self.inputs = [copy.deepcopy(input, memo) for input in node.inputs]

    @classmethod
    def factory(cls, d):
        data_node_type = d["data_node_type"]
        assert data_node_type in _node_map
        node = _node_map[data_node_type]()
        node.read(d)
        return node

    def read(self, d):
        self.uuid = uuid.UUID(d["uuid"])
        inputs = list()
        input_dicts = d.get("inputs", list())
        for input_dict in input_dicts:
            node = DataNode.factory(input_dict)
            node.read(input_dict)
            inputs.append(node)
        self.inputs = inputs
        return d

    def write(self):
        d = dict()
        d["uuid"] = str(self.uuid)
        input_dicts = list()
        for input in self.inputs:
            input_dicts.append(input.write())
        if len(input_dicts) > 0:
            d["inputs"] = input_dicts
        return d

    @classmethod
    def make(cls, value):
        if isinstance(value, ScalarOperationDataNode):
            return value
        elif isinstance(value, DataNode):
            return value
        elif isinstance(value, numbers.Integral):
            return ConstantDataNode(value)
        elif isinstance(value, numbers.Rational):
            return ConstantDataNode(value)
        elif isinstance(value, numbers.Real):
            return ConstantDataNode(value)
        elif isinstance(value, numbers.Complex):
            return ConstantDataNode(value)
        elif isinstance(value, DataItemDataNode):
            return value
        assert False
        return None

    def evaluate(self, context):
        evaluated_inputs = list()
        for input in self.inputs:
            evaluated_input = input.evaluate(context)
            evaluated_inputs.append(evaluated_input)
        return self._evaluate_inputs(evaluated_inputs, context)

    def _evaluate_inputs(self, evaluated_inputs, context):
        raise NotImplementedError()

    def bind(self, context, bound_items):
        for input in self.inputs:
            input.bind(context, bound_items)

    def unbind(self):
        for input in self.inputs:
            input.unbind()

    def reconstruct(self, variable_map):
        raise NotImplemented()

    def print_mapping(self, context):
        for input in self.inputs:
            input.print_mapping(context)

    def __abs__(self):
        return UnaryOperationDataNode([self], "abs")

    def __neg__(self):
        return UnaryOperationDataNode([self], "neg")

    def __pos__(self):
        return UnaryOperationDataNode([self], "pos")

    def __add__(self, other):
        return BinaryOperationDataNode([self, DataNode.make(other)], "add")

    def __radd__(self, other):
        return BinaryOperationDataNode([DataNode.make(other), self], "add")

    def __sub__(self, other):
        return BinaryOperationDataNode([self, DataNode.make(other)], "sub")

    def __rsub__(self, other):
        return BinaryOperationDataNode([DataNode.make(other), self], "sub")

    def __mul__(self, other):
        return BinaryOperationDataNode([self, DataNode.make(other)], "mul")

    def __rmul__(self, other):
        return BinaryOperationDataNode([DataNode.make(other), self], "mul")

    def __div__(self, other):
        return BinaryOperationDataNode([self, DataNode.make(other)], "div")

    def __rdiv__(self, other):
        return BinaryOperationDataNode([DataNode.make(other), self], "div")

    def __truediv__(self, other):
        return BinaryOperationDataNode([self, DataNode.make(other)], "truediv")

    def __rtruediv__(self, other):
        return BinaryOperationDataNode([DataNode.make(other), self], "truediv")

    def __floordiv__(self, other):
        return BinaryOperationDataNode([self, DataNode.make(other)], "floordiv")

    def __rfloordiv__(self, other):
        return BinaryOperationDataNode([DataNode.make(other), self], "floordiv")

    def __mod__(self, other):
        return BinaryOperationDataNode([self, DataNode.make(other)], "mod")

    def __rmod__(self, other):
        return BinaryOperationDataNode([DataNode.make(other), self], "mod")

    def __pow__(self, other):
        return BinaryOperationDataNode([self, DataNode.make(other)], "pow")

    def __rpow__(self, other):
        return BinaryOperationDataNode([DataNode.make(other), self], "pow")

    def __complex__(self):
        raise Exception("Use astype(data, complex128) instead.")

    def __int__(self):
        raise Exception("Use astype(data, int) instead.")

    def __long__(self):
        raise Exception("Use astype(data, int64) instead.")

    def __float__(self):
        raise Exception("Use astype(data, float64) instead.")

    def __getitem__(self, key):
        key = key_to_list(key)
        return FunctionOperationDataNode([self], "data_slice", {"key": key})


class ConstantDataNode(DataNode):
    """Represent a constant value."""

    def __init__(self, value=None):
        super(ConstantDataNode, self).__init__()
        self.__scalar = numpy.array(value)
        if isinstance(value, numbers.Integral):
            self.__scalar_type = "integral"
        elif isinstance(value, numbers.Rational):
            self.__scalar_type = "rational"
        elif isinstance(value, numbers.Real):
            self.__scalar_type = "real"
        elif isinstance(value, numbers.Complex):
            self.__scalar_type = "complex"
        # else:
        #     raise Exception("Invalid constant type [{}].".format(type(value)))

    def deepcopy_from(self, node, memo):
        super(ConstantDataNode, self).deepcopy_from(node, memo)
        self.__scalar = copy.deepcopy(node.__scalar)

    def read(self, d):
        super(ConstantDataNode, self).read(d)
        scalar_type = d.get("scalar_type")
        if scalar_type == "integral":
            self.__scalar = numpy.array(int(d["value"]))
        elif scalar_type == "real":
            self.__scalar = numpy.array(float(d["value"]))
        elif scalar_type == "complex":
            self.__scalar = numpy.array(complex(*d["value"]))

    def write(self):
        d = super(ConstantDataNode, self).write()
        d["data_node_type"] = "constant"
        d["scalar_type"] = self.__scalar_type
        value = self.__scalar
        if self.__scalar_type == "integral":
            d["value"] = int(value)
        elif isinstance(value, numbers.Rational):
            pass
        elif self.__scalar_type == "real":
            d["value"] = float(value)
        elif self.__scalar_type == "complex":
            d["value"] = complex(float(value.real), float(value.imag))
        return d

    def _evaluate_inputs(self, evaluated_inputs, context):
        return self.__scalar

    def reconstruct(self, variable_map):
        return str(self.__scalar), 10

    def __str__(self):
        return "{0} ({1})".format(self.__repr__(), self.__scalar)


class ScalarOperationDataNode(DataNode):
    """Take a set of inputs and produce a scalar value output.

    For example, mean(src) will produce the mean value of the data item src.
    """

    def __init__(self, inputs=None, function_id=None, args=None):
        super(ScalarOperationDataNode, self).__init__(inputs=inputs)
        self.__function_id = function_id
        self.__args = copy.copy(args if args is not None else dict())

    def deepcopy_from(self, node, memo):
        super(ScalarOperationDataNode, self).deepcopy_from(node, memo)
        self.__function_id = node.__function_id
        self.__args = [copy.deepcopy(arg, memo) for arg in node.__args]

    def read(self, d):
        super(ScalarOperationDataNode, self).read(d)
        function_id = d.get("function_id")
        self.__function_id = function_id
        args = d.get("args")
        self.__args = copy.copy(args if args is not None else dict())

    def write(self):
        d = super(ScalarOperationDataNode, self).write()
        d["data_node_type"] = "scalar"
        d["function_id"] = self.__function_id
        if self.__args:
            d["args"] = self.__args
        return d

    def _evaluate_inputs(self, evaluated_inputs, context):
        if self.__function_id in _function_map and all(evaluated_input is not None for evaluated_input in evaluated_inputs):
            return _function_map[self.__function_id](*[extract_data(evaluated_input) for evaluated_input in evaluated_inputs], **self.__args)
        return None

    def reconstruct(self, variable_map):
        inputs = reconstruct_inputs(variable_map, self.inputs)
        input_texts = [input[0] for input in inputs]
        if self.__function_id == "item":
            return "{0}[{1}]".format(input_texts[0], self.__args["key"]), 10
        args_str = ", ".join([k + "=" + str(v) for k, v in self.__args.items()])
        if len(self.__args) > 0:
            args_str = ", " + args_str
        return "{0}({1}{2})".format(self.__function_id, ", ".join(input_texts), args_str), 10

    def __getitem__(self, key):
        return ScalarOperationDataNode([self], "item", {"key": key})

    def __str__(self):
        return "{0} {1}({2})".format(self.__repr__(), self.__function_id, self.inputs[0])


class UnaryOperationDataNode(DataNode):

    def __init__(self, inputs=None, function_id=None, args=None):
        super(UnaryOperationDataNode, self).__init__(inputs=inputs)
        self.__function_id = function_id
        self.__args = copy.copy(args if args is not None else dict())

    def deepcopy_from(self, node, memo):
        super(UnaryOperationDataNode, self).deepcopy_from(node, memo)
        self.__function_id = node.__function_id
        self.__args = [copy.deepcopy(arg, memo) for arg in node.__args]

    def read(self, d):
        super(UnaryOperationDataNode, self).read(d)
        function_id = d.get("function_id")
        self.__function_id = function_id
        args = d.get("args")
        self.__args = copy.copy(args if args is not None else dict())
        # TODO: fix this special case by providing default arguments
        # the issue is that JSON is not able to store dict's with None
        # values. this is OK in most cases, but in this case, it prevents
        # the argument from being passed to column/row.
        if self.__function_id in ("column", "row"):
            self.__args.setdefault("start", None)
            self.__args.setdefault("stop", None)

    def write(self):
        d = super(UnaryOperationDataNode, self).write()
        d["data_node_type"] = "unary"
        d["function_id"] = self.__function_id
        if self.__args:
            d["args"] = self.__args
        return d

    def _evaluate_inputs(self, evaluated_inputs, context):
        def calculate_data():
            return _function_map[self.__function_id](extract_data(evaluated_inputs[0]), **self.__args)

        if self.__function_id in _function_map and all(evaluated_input is not None for evaluated_input in evaluated_inputs):
            if isinstance(evaluated_inputs[0], DataAndMetadata.DataAndMetadata):
                return DataAndMetadata.DataAndMetadata(calculate_data, evaluated_inputs[0].data_shape_and_dtype,
                                                       evaluated_inputs[0].intensity_calibration,
                                                       evaluated_inputs[0].dimensional_calibrations,
                                                       evaluated_inputs[0].metadata, datetime.datetime.utcnow())
            else:
                return numpy.array(_function_map[self.__function_id](evaluated_inputs[0]))
        return None

    def reconstruct(self, variable_map):
        inputs = reconstruct_inputs(variable_map, self.inputs)
        input_texts = [input[0] for input in inputs]
        operator_arg = input_texts[0]
        if self.__function_id in _operator_map:
            operator_text, precedence = _operator_map[self.__function_id]
            if precedence >= inputs[0][1]:
                operator_arg = "({0})".format(operator_arg)
            return "{0}{1}".format(operator_text, operator_arg), precedence
        if self.__function_id == "astype":
            return "{0}({1}, {2})".format(self.__function_id, operator_arg, self.__args["dtype"]), 10
        if self.__function_id in ("column", "row"):
            if self.__args.get("start") is None and self.__args.get("stop") is None:
                return "{0}({1})".format(self.__function_id, operator_arg), 10
        if self.__function_id == "radius":
            if self.__args.get("normalize", True) is True:
                return "{0}({1})".format(self.__function_id, operator_arg), 10
        args_str = ", ".join([k + "=" + str(v) for k, v in self.__args.items()])
        if len(self.__args) > 0:
            args_str = ", " + args_str
        return "{0}({1}{2})".format(self.__function_id, operator_arg, args_str), 10

    def __str__(self):
        return "{0} {1}({2})".format(self.__repr__(), self.__function_id, self.inputs[0])


class BinaryOperationDataNode(DataNode):

    def __init__(self, inputs=None, function_id=None, args=None):
        super(BinaryOperationDataNode, self).__init__(inputs=inputs)
        self.__function_id = function_id
        self.__args = copy.copy(args if args is not None else dict())

    def deepcopy_from(self, node, memo):
        super(BinaryOperationDataNode, self).deepcopy_from(node, memo)
        self.__function_id = node.__function_id
        self.__args = [copy.deepcopy(arg, memo) for arg in node.__args]

    def read(self, d):
        super(BinaryOperationDataNode, self).read(d)
        function_id = d.get("function_id")
        self.__function_id = function_id
        args = d.get("args")
        self.__args = copy.copy(args if args is not None else dict())

    def write(self):
        d = super(BinaryOperationDataNode, self).write()
        d["data_node_type"] = "binary"
        d["function_id"] = self.__function_id
        if self.__args:
            d["args"] = self.__args
        return d

    def _evaluate_inputs(self, evaluated_inputs, context):
        def calculate_data():
            return _function_map[self.__function_id](extract_data(evaluated_inputs[0]), extract_data(evaluated_inputs[1]), **self.__args)

        # if the first input is not a data_and_metadata, use the second input
        src_evaluated_input = evaluated_inputs[0] if isinstance(evaluated_inputs[0], DataAndMetadata.DataAndMetadata) else evaluated_inputs[1]

        if self.__function_id in _function_map and all(evaluated_input is not None for evaluated_input in evaluated_inputs):
            if isinstance(src_evaluated_input, DataAndMetadata.DataAndMetadata):
                return DataAndMetadata.DataAndMetadata(calculate_data, src_evaluated_input.data_shape_and_dtype,
                                                       src_evaluated_input.intensity_calibration,
                                                       src_evaluated_input.dimensional_calibrations,
                                                       src_evaluated_input.metadata, datetime.datetime.utcnow())
            else:
                return numpy.array(_function_map[self.__function_id](evaluated_inputs[0], evaluated_inputs[1]))
        return None

    def reconstruct(self, variable_map):
        inputs = reconstruct_inputs(variable_map, self.inputs)
        input_texts = [input[0] for input in inputs]
        operator_left = input_texts[0]
        operator_right = input_texts[1]
        if self.__function_id in _operator_map:
            operator_text, precedence = _operator_map[self.__function_id]
            if precedence > inputs[0][1]:
                operator_left = "({0})".format(operator_left)
            if precedence > inputs[1][1]:
                operator_right = "({0})".format(operator_right)
            return "{1} {0} {2}".format(operator_text, operator_left, operator_right), precedence
        return "{0}({1}, {2})".format(self.__function_id, operator_left, operator_right), 10

    def __str__(self):
        return "{0} {1}({2}, {3})".format(self.__repr__(), self.__function_id, self.inputs[0], self.inputs[1])


class FunctionOperationDataNode(DataNode):

    def __init__(self, inputs=None, function_id=None, args=None):
        super(FunctionOperationDataNode, self).__init__(inputs=inputs)
        self.__function_id = function_id
        self.__args = copy.copy(args if args is not None else dict())

    def deepcopy_from(self, node, memo):
        super(FunctionOperationDataNode, self).deepcopy_from(node, memo)
        self.__function_id = node.__function_id
        self.__args = [copy.deepcopy(arg, memo) for arg in node.__args]

    def read(self, d):
        super(FunctionOperationDataNode, self).read(d)
        function_id = d.get("function_id")
        self.__function_id = function_id
        args = d.get("args")
        self.__args = copy.copy(args if args is not None else dict())

    def write(self):
        d = super(FunctionOperationDataNode, self).write()
        d["data_node_type"] = "function"
        d["function_id"] = self.__function_id
        if self.__args:
            d["args"] = self.__args
        return d

    def _evaluate_inputs(self, evaluated_inputs, context):
        # don't pass the data; the functions are responsible for extracting the data correctly
        if self.__function_id in _function2_map and all(evaluated_input is not None for evaluated_input in evaluated_inputs):
            return _function2_map[self.__function_id](*evaluated_inputs, **self.__args)
        return None

    def reconstruct(self, variable_map):
        inputs = reconstruct_inputs(variable_map, self.inputs)
        input_texts = [input[0] for input in inputs]
        if self.__function_id == "data_slice":
            operator_arg = input_texts[0]
            slice_strs = list()
            for slice_or_index in list_to_key(self.__args["key"]):
                if isinstance(slice_or_index, slice):
                    slice_str = str(slice_or_index.start) if slice_or_index.start is not None else ""
                    slice_str += ":" + str(slice_or_index.stop) if slice_or_index.stop is not None else ":"
                    slice_str += ":" + str(slice_or_index.step) if slice_or_index.step is not None else ""
                    slice_strs.append(slice_str)
                elif isinstance(slice_or_index, numbers.Integral):
                    slice_str += str(slice_or_index)
                    slice_strs.append(slice_str)
            return "{0}[{1}]".format(operator_arg, ", ".join(slice_strs)), 10
        if self.__function_id == "concatenate":
            axis = self.__args.get("axis", 0)
            axis_str = (", " + str(axis)) if axis != 0 else str()
            return "{0}(({1}){2})".format(self.__function_id, ", ".join(input_texts), axis_str), 10
        if self.__function_id == "reshape":
            shape = self.__args.get("shape", 0)
            shape_str = (", " + str(shape)) if shape != 0 else str()
            return "{0}({1}{2})".format(self.__function_id, ", ".join(input_texts), shape_str), 10
        return "{0}({1})".format(self.__function_id, ", ".join(input_texts)), 10

    def __str__(self):
        return "{0} {1}({2}, {3})".format(self.__repr__(), self.__function_id, [str(input) for input in self.inputs], list(self.__args))


class DataItemDataNode(DataNode):

    def __init__(self, object_specifier=None):
        super(DataItemDataNode, self).__init__()
        self.__object_specifier = object_specifier
        self.__bound_item = None

    def deepcopy_from(self, node, memo):
        super(DataItemDataNode, self).deepcopy_from(node, memo)
        self.__object_specifier = copy.deepcopy(node.__object_specifier, memo)
        self.__bound_item = None

    @property
    def _bound_item_for_test(self):
        return self.__bound_item

    def read(self, d):
        super(DataItemDataNode, self).read(d)
        self.__object_specifier = d["object_specifier"]

    def write(self):
        d = super(DataItemDataNode, self).write()
        d["data_node_type"] = "data"
        d["object_specifier"] = copy.deepcopy(self.__object_specifier)
        return d

    def _evaluate_inputs(self, evaluated_inputs, context):
        if self.__bound_item:
            return self.__bound_item.value
        return None

    def print_mapping(self, context):
        logging.debug("%s: %s", self.__data_reference_uuid, self.__object_specifier)

    def bind(self, context, bound_items):
        self.__bound_item = context.resolve_object_specifier(self.__object_specifier)
        if self.__bound_item is not None:
            bound_items[self.uuid] = self.__bound_item

    def unbind(self):
        self.__bound_item = None

    def reconstruct(self, variable_map):
        variable_index = -1
        prefix = "d"
        for variable, object_specifier in variable_map.items():
            if object_specifier == self.__object_specifier:
                return variable, 10
            suffix = variable[len(prefix):]
            if suffix.isdigit():
                variable_index = max(variable_index, int(suffix) + 1)
        variable_index = max(variable_index, 0)
        variable_name = "{0}{1}".format(prefix, variable_index)
        variable_map[variable_name] = copy.deepcopy(self.__object_specifier)
        return variable_name, 10

    def __getattr__(self, name):
        return PropertyDataNode(self.__object_specifier, name)

    def __str__(self):
        return "{0} ({1})".format(self.__repr__(), self.__object_specifier)


class ReferenceDataNode(DataNode):

    def __init__(self, object_specifier=None):
        super(ReferenceDataNode, self).__init__()
        self.__object_specifier = object_specifier

    def deepcopy_from(self, node, memo):
        # should only be used as intermediate node, but here for error handling
        super(ReferenceDataNode, self).deepcopy_from(node, memo)
        self.__object_specifier = copy.deepcopy(node.__object_specifier, memo)

    def read(self, d):
        # should only be used as intermediate node, but here for error handling
        super(ReferenceDataNode, self).read(d)
        self.__object_specifier = d.get("object_specifier", {"type": "reference", "version": 1, "uuid": str(uuid.uuid4())})

    def write(self):
        # should only be used as intermediate node, but here for error handling
        d = super(ReferenceDataNode, self).write()
        d["data_node_type"] = "reference"
        d["object_specifier"] = copy.deepcopy(self.__object_specifier)
        return d

    def _evaluate_inputs(self, evaluated_inputs, context):
        return None

    def print_mapping(self, context):
        # should only be used as intermediate node, but here for error handling
        logging.debug("%s", self.__object_specifier)

    def bind(self, context, bound_items):
        pass  # should only be used as intermediate node

    def unbind(self):
        raise NotImplemented()  # should only be used as intermediate node

    def reconstruct(self, variable_map):
        variable_index = -1
        prefix = "ref"
        for variable, object_specifier in variable_map.items():
            if object_specifier == self.__object_specifier:
                return variable, 10
            suffix = variable[len(prefix):]
            if suffix.isdigit():
                variable_index = max(variable_index, int(suffix) + 1)
        variable_index = max(variable_index, 0)
        variable_name = "{0}{1}".format(prefix, variable_index)
        variable_map[variable_name] = copy.deepcopy(self.__object_specifier)
        return variable_name, 10

    def __getattr__(self, name):
        return PropertyDataNode(self.__object_specifier, name)

    def __str__(self):
        return "{0} ({1})".format(self.__repr__(), self.__reference_uuid)


class VariableDataNode(DataNode):

    def __init__(self, object_specifier=None):
        super(VariableDataNode, self).__init__()
        self.__object_specifier = object_specifier  # type: dict
        self.__bound_item = None

    def deepcopy_from(self, node, memo):
        super(VariableDataNode, self).deepcopy_from(node, memo)
        self.__object_specifier = copy.deepcopy(node.__object_specifier, memo)
        self.__bound_item = None

    def read(self, d):
        super(VariableDataNode, self).read(d)
        self.__object_specifier = d["object_specifier"]

    def write(self):
        d = super(VariableDataNode, self).write()
        d["data_node_type"] = "variable"
        d["object_specifier"] = copy.deepcopy(self.__object_specifier)
        return d

    def _evaluate_inputs(self, evaluated_inputs, context):
        if self.__bound_item:
            return self.__bound_item.value
        return None

    def print_mapping(self, context):
        logging.debug("%s", self.__object_specifier)

    def bind(self, context, bound_items):
        self.__bound_item = context.resolve_object_specifier(self.__object_specifier)
        if self.__bound_item is not None:
            bound_items[self.uuid] = self.__bound_item

    def unbind(self):
        self.__bound_item = None

    def reconstruct(self, variable_map):
        variable_index = -1
        object_specifier_type = self.__object_specifier["type"]
        if object_specifier_type == "variable":
            prefix = "x"
        for variable, object_specifier in sorted(variable_map.items()):
            if object_specifier == self.__object_specifier:
                return variable, 10
            if variable.startswith(prefix):
                suffix = variable[len(prefix):]
                if suffix.isdigit():
                    variable_index = max(variable_index, int(suffix) + 1)
        variable_index = max(variable_index, 0)
        variable_name = "{0}{1}".format(prefix, variable_index)
        variable_map[variable_name] = copy.deepcopy(self.__object_specifier)
        return variable_name, 10

    def __getattr__(self, name):
        return PropertyDataNode(self.__object_specifier, name)

    def __str__(self):
        return "{0} ({1})".format(self.__repr__(), self.__object_specifier)


class PropertyDataNode(DataNode):

    def __init__(self, object_specifier=None, property=None):
        super(PropertyDataNode, self).__init__()
        self.__object_specifier = object_specifier
        self.__property = str(property)
        self.__bound_item = None

    def deepcopy_from(self, node, memo):
        super(PropertyDataNode, self).deepcopy_from(node, memo)
        self.__object_specifier = copy.deepcopy(node.__object_specifier, memo)
        self.__property = node.__property
        self.__bound_item = None

    def read(self, d):
        super(PropertyDataNode, self).read(d)
        self.__object_specifier = d["object_specifier"]
        self.__property = d["property"]

    def write(self):
        d = super(PropertyDataNode, self).write()
        d["data_node_type"] = "property"
        d["object_specifier"] = copy.deepcopy(self.__object_specifier)
        d["property"] = self.__property
        return d

    def _evaluate_inputs(self, evaluated_inputs, resolve):
        if self.__bound_item:
            return self.__bound_item.value
        return None

    def print_mapping(self, context):
        logging.debug("%s.%s: %s", self.__property, self.__object_specifier)

    def bind(self, context, bound_items):
        self.__bound_item = context.resolve_object_specifier(self.__object_specifier, self.__property)
        if self.__bound_item is not None:
            bound_items[self.uuid] = self.__bound_item

    def unbind(self):
        self.__bound_item = None

    def reconstruct(self, variable_map):
        variable_index = -1
        object_specifier_type = self.__object_specifier["type"]
        if object_specifier_type == "data_item":
            prefix = "d"
        elif object_specifier_type == "region":
            prefix = "region"
        else:
            prefix = "object"
        for variable, object_specifier in sorted(variable_map.items()):
            if object_specifier == self.__object_specifier:
                return "{0}.{1}".format(variable, self.__property), 10
            if variable.startswith(prefix):
                suffix = variable[len(prefix):]
                if suffix.isdigit():
                    variable_index = max(variable_index, int(suffix) + 1)
        variable_index = max(variable_index, 0)
        variable_name = "{0}{1}".format(prefix, variable_index)
        variable_map[variable_name] = copy.deepcopy(self.__object_specifier)
        return "{0}.{1}".format(variable_name, self.__property), 10

    def __str__(self):
        return "{0} ({1}.{2})".format(self.__repr__(), self.__object_specifier, self.__property)


def data_by_uuid(context, data_uuid):
    object_specifier = context.get_data_item_specifier(data_uuid)
    return DataItemDataNode(object_specifier)


def region_by_uuid(context, region_uuid):
    object_specifier = context.get_region_specifier(region_uuid)
    if object_specifier:
        return ReferenceDataNode(object_specifier)
    return None


_node_map = {
    "constant": ConstantDataNode,
    "scalar": ScalarOperationDataNode,
    "unary": UnaryOperationDataNode,
    "binary": BinaryOperationDataNode,
    "function": FunctionOperationDataNode,
    "property": PropertyDataNode,
    "reference": ReferenceDataNode,
    "variable": VariableDataNode,
    "data": DataItemDataNode,  # TODO: file format: Rename symbolic node 'data' to 'dataitem'
}

def transpose_flip(data_node, transpose=False, flip_v=False, flip_h=False):
    return FunctionOperationDataNode([data_node, DataNode.make(transpose), DataNode.make(flip_v), DataNode.make(flip_h)], "transpose_flip")

def parse_expression(expression_lines, variable_map, context):
    code_lines = []
    code_lines.append("import uuid")
    g = dict()
    g["int16"] = numpy.int16
    g["int32"] = numpy.int32
    g["int64"] = numpy.int64
    g["uint8"] = numpy.uint8
    g["uint16"] = numpy.uint16
    g["uint32"] = numpy.uint32
    g["uint64"] = numpy.uint64
    g["float32"] = numpy.float32
    g["float64"] = numpy.float64
    g["complex64"] = numpy.complex64
    g["complex128"] = numpy.complex128
    g["astype"] = lambda data_node, dtype: UnaryOperationDataNode([data_node], "astype", {"dtype": dtype_to_str(dtype)})
    g["concatenate"] = lambda data_nodes, axis=0: FunctionOperationDataNode(tuple(data_nodes), "concatenate", {"axis": axis})
    g["reshape"] = lambda data_node, shape: FunctionOperationDataNode([data_node, DataNode.make(shape)], "reshape")
    g["data_slice"] = lambda data_node, key: FunctionOperationDataNode([data_node], "data_slice", {"key": key})
    g["item"] = lambda data_node, key: ScalarOperationDataNode([data_node], "item", {"key": key})
    g["column"] = lambda data_node, start=None, stop=None: UnaryOperationDataNode([data_node], "column", {"start": start, "stop": stop})
    g["row"] = lambda data_node, start=None, stop=None: UnaryOperationDataNode([data_node], "row", {"start": start, "stop": stop})
    g["radius"] = lambda data_node, normalize=True: UnaryOperationDataNode([data_node], "radius", {"normalize": normalize})
    g["amin"] = lambda data_node: ScalarOperationDataNode([data_node], "amin")
    g["amax"] = lambda data_node: ScalarOperationDataNode([data_node], "amax")
    g["arange"] = lambda data_node: ScalarOperationDataNode([data_node], "arange")
    g["median"] = lambda data_node: ScalarOperationDataNode([data_node], "median")
    g["average"] = lambda data_node: ScalarOperationDataNode([data_node], "average")
    g["mean"] = lambda data_node: ScalarOperationDataNode([data_node], "mean")
    g["std"] = lambda data_node: ScalarOperationDataNode([data_node], "std")
    g["var"] = lambda data_node: ScalarOperationDataNode([data_node], "var")
    g["sin"] = lambda data_node: UnaryOperationDataNode([data_node], "sin")
    g["cos"] = lambda data_node: UnaryOperationDataNode([data_node], "cos")
    g["tan"] = lambda data_node: UnaryOperationDataNode([data_node], "tan")
    g["arcsin"] = lambda data_node: UnaryOperationDataNode([data_node], "arcsin")
    g["arccos"] = lambda data_node: UnaryOperationDataNode([data_node], "arccos")
    g["arctan"] = lambda data_node: UnaryOperationDataNode([data_node], "arctan")
    g["hypot"] = lambda data_node: UnaryOperationDataNode([data_node], "hypot")
    g["arctan2"] = lambda data_node: UnaryOperationDataNode([data_node], "arctan2")
    g["degrees"] = lambda data_node: UnaryOperationDataNode([data_node], "degrees")
    g["radians"] = lambda data_node: UnaryOperationDataNode([data_node], "radians")
    g["rad2deg"] = lambda data_node: UnaryOperationDataNode([data_node], "rad2deg")
    g["deg2rad"] = lambda data_node: UnaryOperationDataNode([data_node], "deg2rad")
    g["around"] = lambda data_node: UnaryOperationDataNode([data_node], "around")
    g["round"] = lambda data_node: UnaryOperationDataNode([data_node], "round")
    g["rint"] = lambda data_node: UnaryOperationDataNode([data_node], "rint")
    g["fix"] = lambda data_node: UnaryOperationDataNode([data_node], "fix")
    g["floor"] = lambda data_node: UnaryOperationDataNode([data_node], "floor")
    g["ceil"] = lambda data_node: UnaryOperationDataNode([data_node], "ceil")
    g["trunc"] = lambda data_node: UnaryOperationDataNode([data_node], "trunc")
    g["exp"] = lambda data_node: UnaryOperationDataNode([data_node], "exp")
    g["expm1"] = lambda data_node: UnaryOperationDataNode([data_node], "expm1")
    g["exp2"] = lambda data_node: UnaryOperationDataNode([data_node], "exp2")
    g["log"] = lambda data_node: UnaryOperationDataNode([data_node], "log")
    g["log10"] = lambda data_node: UnaryOperationDataNode([data_node], "log10")
    g["log2"] = lambda data_node: UnaryOperationDataNode([data_node], "log2")
    g["log1p"] = lambda data_node: UnaryOperationDataNode([data_node], "log1p")
    g["reciprocal"] = lambda data_node: UnaryOperationDataNode([data_node], "reciprocal")
    g["clip"] = lambda data_node: UnaryOperationDataNode([data_node], "clip")
    g["sqrt"] = lambda data_node: UnaryOperationDataNode([data_node], "sqrt")
    g["square"] = lambda data_node: UnaryOperationDataNode([data_node], "square")
    g["nan_to_num"] = lambda data_node: UnaryOperationDataNode([data_node], "nan_to_num")
    g["angle"] = lambda data_node: UnaryOperationDataNode([data_node], "angle")
    g["real"] = lambda data_node: UnaryOperationDataNode([data_node], "real")
    g["imag"] = lambda data_node: UnaryOperationDataNode([data_node], "imag")
    g["conj"] = lambda data_node: UnaryOperationDataNode([data_node], "conj")
    g["fft"] = lambda data_node: FunctionOperationDataNode([data_node], "fft")
    g["ifft"] = lambda data_node: FunctionOperationDataNode([data_node], "ifft")
    g["autocorrelate"] = lambda data_node: FunctionOperationDataNode([data_node], "autocorrelate")
    g["crosscorrelate"] = lambda data_node1, data_node2: FunctionOperationDataNode([data_node1, data_node2], "crosscorrelate")
    g["sobel"] = lambda data_node: FunctionOperationDataNode([data_node], "sobel")
    g["laplace"] = lambda data_node: FunctionOperationDataNode([data_node], "laplace")
    g["gaussian_blur"] = lambda data_node, scalar_node: FunctionOperationDataNode([data_node, DataNode.make(scalar_node)], "gaussian_blur")
    g["median_filter"] = lambda data_node, scalar_node: FunctionOperationDataNode([data_node, DataNode.make(scalar_node)], "median_filter")
    g["uniform_filter"] = lambda data_node, scalar_node: FunctionOperationDataNode([data_node, DataNode.make(scalar_node)], "uniform_filter")
    g["transpose_flip"] = transpose_flip
    g["crop"] = lambda data_node, bounds_node: FunctionOperationDataNode([data_node, DataNode.make(bounds_node)], "crop")
    g["slice_sum"] = lambda data_node, scalar_node1, scalar_node2: FunctionOperationDataNode([data_node, DataNode.make(scalar_node1), DataNode.make(scalar_node2)], "slice_sum")
    g["pick"] = lambda data_node, position_node: FunctionOperationDataNode([data_node, DataNode.make(position_node)], "pick")
    g["project"] = lambda data_node: FunctionOperationDataNode([data_node], "project")
    g["resample_image"] = lambda data_node, shape: FunctionOperationDataNode([data_node, DataNode.make(shape)], "resample_image")
    g["histogram"] = lambda data_node, bins_node: FunctionOperationDataNode([data_node, DataNode.make(bins_node)], "histogram")
    g["line_profile"] = lambda data_node, vector_node, width_node: FunctionOperationDataNode([data_node, DataNode.make(vector_node), DataNode.make(width_node)], "line_profile")
    g["data_by_uuid"] = lambda data_uuid: data_by_uuid(context, data_uuid)
    g["region_by_uuid"] = lambda region_uuid: region_by_uuid(context, region_uuid)
    g["data_shape"] = lambda data_node: ScalarOperationDataNode([data_node], "data_shape")
    g["shape"] = lambda *args: ScalarOperationDataNode([DataNode.make(arg) for arg in args], "shape")
    g["rectangle_from_origin_size"] = lambda origin, size: ScalarOperationDataNode([DataNode.make(origin), DataNode.make(size)], "rectangle_from_origin_size")
    g["rectangle_from_center_size"] = lambda center, size: ScalarOperationDataNode([DataNode.make(center), DataNode.make(size)], "rectangle_from_center_size")
    g["vector"] = lambda start, end: ScalarOperationDataNode([DataNode.make(start), DataNode.make(end)], "vector")
    g["normalized_point"] = lambda y, x: ScalarOperationDataNode([DataNode.make(y), DataNode.make(x)], "normalized_point")
    g["normalized_size"] = lambda height, width: ScalarOperationDataNode([DataNode.make(height), DataNode.make(width)], "normalized_size")
    g["normalized_interval"] = lambda start, end: ScalarOperationDataNode([DataNode.make(start), DataNode.make(end)], "normalized_interval")
    l = dict()
    for variable_name, object_specifier in variable_map.items():
        if object_specifier["type"] == "data_item":
            reference_node = DataItemDataNode(object_specifier=object_specifier)
        elif object_specifier["type"] == "variable":
            reference_node = VariableDataNode(object_specifier=object_specifier)
        else:
            reference_node = ReferenceDataNode(object_specifier=object_specifier)
        g[variable_name] = reference_node
    g["newaxis"] = numpy.newaxis
    expression_lines = expression_lines[:-1] + ["result = {0}".format(expression_lines[-1]), ]
    code_lines.extend(expression_lines)
    code = "\n".join(code_lines)
    try:
        exec(code, g, l)
    except Exception as e:
        return None, str(e)
    return l["result"], None


class ComputationVariable(Observable.Observable, Persistence.PersistentObject):
    def __init__(self, name: str=None, value_type: str=None, value=None, value_default=None, value_min=None, value_max=None, control_type: str=None, specifier: dict=None):  # defaults are None for factory
        super(ComputationVariable, self).__init__()
        self.define_type("variable")
        self.define_property("name", name, changed=self.__property_changed)
        self.define_property("label", name, changed=self.__property_changed)
        self.define_property("value_type", value_type, changed=self.__property_changed)
        self.define_property("value", value, changed=self.__property_changed, reader=self.__value_reader, writer=self.__value_writer)
        self.define_property("value_default", value_default, changed=self.__property_changed, reader=self.__value_reader, writer=self.__value_writer)
        self.define_property("value_min", value_min, changed=self.__property_changed, reader=self.__value_reader, writer=self.__value_writer)
        self.define_property("value_max", value_max, changed=self.__property_changed, reader=self.__value_reader, writer=self.__value_writer)
        self.define_property("specifier", specifier, changed=self.__property_changed)
        self.define_property("control_type", control_type, changed=self.__property_changed)
        self.variable_type_changed_event = Event.Event()

    def read_from_dict(self, properties: dict) -> None:
        # ensure that value_type is read first
        value_type_property = self._get_persistent_property("value_type")
        value_type_property.read_from_dict(properties)
        super(ComputationVariable, self).read_from_dict(properties)

    def write_to_dict(self) -> dict:
        return super(ComputationVariable, self).write_to_dict()

    def __value_reader(self, persistent_property, properties):
        value_type = self.value_type
        raw_value = properties.get(persistent_property.key)
        if raw_value is not None:
            if value_type == "boolean":
                return bool(raw_value)
            elif value_type == "integral":
                return int(raw_value)
            elif value_type == "real":
                return float(raw_value)
            elif value_type == "complex":
                return complex(*raw_value)
            elif value_type == "string":
                return str(raw_value)
        return None

    def __value_writer(self, persistent_property, properties, value):
        value_type = self.value_type
        if value is not None:
            if value_type == "boolean":
                properties[persistent_property.key] = bool(value)
            if value_type == "integral":
                properties[persistent_property.key] = int(value)
            if value_type == "real":
                properties[persistent_property.key] = float(value)
            if value_type == "complex":
                properties[persistent_property.key] = complex(value).real, complex(value).imag
            if value_type == "string":
                properties[persistent_property.key] = str(value)

    @property
    def variable_specifier(self) -> dict:
        return {"type": "variable", "version": 1, "uuid": str(self.uuid)}

    @property
    def bound_variable(self):
        class BoundVariable(object):
            def __init__(self, variable):
                self.__variable = variable
                self.changed_event = Event.Event()
                def property_changed(key, value):
                    if key == "value":
                        self.changed_event.fire()
                self.__variable_property_changed_listener = variable.property_changed_event.listen(property_changed)
            @property
            def value(self):
                return self.__variable.value
            def close(self):
                self.__variable_property_changed_listener.close()
                self.__variable_property_changed_listener = None
        return BoundVariable(self)

    def __property_changed(self, name, value):
        self.notify_set_property(name, value)
        if name in ["name", "label"]:
            self.notify_set_property("display_label", self.display_label)

    def control_type_default(self, value_type: str) -> None:
        mapping = {"boolean": "checkbox", "integral": "slider", "real": "field", "complex": "field", "string": "field"}
        return mapping.get(value_type)

    @property
    def variable_type(self) -> str:
        if self.value_type is not None:
            return self.value_type
        elif self.specifier is not None:
            return self.specifier.get("type")
        return None

    @variable_type.setter
    def variable_type(self, value_type: str) -> None:
        if value_type != self.variable_type:
            if value_type in ("boolean", "integral", "real", "complex", "string"):
                self.specifier = None
                self.value_type = value_type
                self.control_type = self.control_type_default(value_type)
                if value_type == "boolean":
                    self.value_default = True
                elif value_type == "integral":
                    self.value_default = 0
                elif value_type == "real":
                    self.value_default = 0.0
                elif value_type == "complex":
                    self.value_default = 0 + 0j
                else:
                    self.value_default = None
                self.value_min = None
                self.value_max = None
            elif value_type in ("data_item", "region"):
                self.value_type = None
                self.control_type = None
                self.value_default = None
                self.value_min = None
                self.value_max = None
                self.specifier = {"type": value_type, "version": 1}
            self.variable_type_changed_event.fire()

    @property
    def display_label(self):
        return self.label or self.name

    @property
    def has_range(self):
        return self.value_type is not None and self.value_min is not None and self.value_max is not None


def variable_factory(lookup_id):
    build_map = {
        "variable": ComputationVariable,
    }
    type = lookup_id("type")
    return build_map[type]() if type in build_map else None


class ComputationContext(object):
    def __init__(self, computation, context):
        self.__computation = computation
        self.__context = context

    def get_data_item_specifier(self, data_item_uuid):
        """Supports data item lookup by uuid."""
        return self.__context.get_object_specifier(self.__context.get_data_item_by_uuid(data_item_uuid))

    def get_region_specifier(self, region_uuid):
        """Supports region lookup by uuid."""
        for data_item in self.__context.data_items:
            for data_source in data_item.data_sources:
                for region in data_source.regions:
                    if region.uuid == region_uuid:
                        return self.__context.get_object_specifier(region)
        return None

    def resolve_object_specifier(self, object_specifier, property_name=None):
        """Resolve the object specifier, returning a bound variable.

        Ask the computation for the variable associated with the object specifier. If it doesn't exist, let the
        enclosing context handle it. Otherwise, check to see if the variable directly includes a value (i.e. has no
        specifier). If so, let the variable return the bound variable directly. Otherwise (again) let the enclosing
        context resolve, but use the specifier in the variable.

        Structuring this method this way allows the variable to provide a second level of indirection. The computation
        can store variable specifiers only. The variable specifiers can hold values directly or specifiers to the
        enclosing context. This isolates the computation further from the enclosing context.
        """
        variable = self.__computation.resolve_variable(object_specifier)
        if not variable:
            return self.__context.resolve_object_specifier(object_specifier, property_name)
        elif variable.specifier is None:
            return variable.bound_variable
        else:
            # BoundVariable is used here to watch for changes to the variable in addition to watching for changes
            # to the context of the variable. Fire changed_event for either type of change.
            class BoundVariable:
                def __init__(self, variable, context, property_name):
                    self.__bound_object_changed_listener = None
                    self.__variable = variable
                    self.changed_event = Event.Event()
                    def update_bound_object():
                        if self.__bound_object_changed_listener:
                            self.__bound_object_changed_listener.close()
                            self.__bound_object_changed_listener = None
                        self.__bound_object = context.resolve_object_specifier(self.__variable.specifier, property_name)
                        if self.__bound_object:
                            def bound_object_changed():
                                self.changed_event.fire()
                            self.__bound_object_changed_listener = self.__bound_object.changed_event.listen(bound_object_changed)
                    def property_changed(key, value):
                        if key == "specifier":
                            update_bound_object()
                            self.changed_event.fire()
                    self.__variable_property_changed_listener = variable.property_changed_event.listen(property_changed)
                    update_bound_object()
                @property
                def value(self):
                    return self.__bound_object.value if self.__bound_object else None
                def close(self):
                    self.__variable_property_changed_listener.close()
                    self.__variable_property_changed_listener = None
                    if self.__bound_object_changed_listener:
                        self.__bound_object_changed_listener.close()
                        self.__bound_object_changed_listener = None
            return BoundVariable(variable, self.__context, property_name)


class Computation(Observable.Observable, Persistence.PersistentObject):
    """A computation on data and other inputs using symbolic nodes.

    Watches for changes to the sources and fires a needs_update_event
    when a new computation needs to occur.

    Call parse_expression first to establish the computation. Bind will be automatically called.

    Call bind to establish connections after reloading. Call unbind to release connections.

    Listen to needs_update_event and call evaluate in response to perform
    computation (on thread).

    The computation will listen to any bound items established in the bind method. When those
    items signal a change, the needs_update_event will be fired.
    """

    def __init__(self):
        super(Computation, self).__init__()
        self.define_type("computation")
        self.define_property("node")
        self.define_property("original_expression")
        self.define_property("error_text")
        self.define_property("label", changed=self.__label_changed)
        self.define_relationship("variables", variable_factory)
        self.__bound_items = dict()
        self.__bound_item_listeners = dict()
        self.__data_node = None
        self.__evaluate_lock = threading.RLock()
        self.__evaluating = False
        self.needs_update = False
        self.needs_update_event = Event.Event()
        self.computation_mutated_event = Event.Event()
        self.variable_inserted_event = Event.Event()
        self.variable_removed_event = Event.Event()
        self._evaluation_count_for_test = 0

    def deepcopy_from(self, item, memo):
        super(Computation, self).deepcopy_from(item, memo)
        self.__data_node = DataNode.factory(self.node)

    @property
    def _data_node_for_test(self):
        return self.__data_node

    def read_from_dict(self, properties):
        super(Computation, self).read_from_dict(properties)
        self.__data_node = DataNode.factory(self.node)

    def __label_changed(self, name, value):
        self.notify_set_property(name, value)
        self.computation_mutated_event.fire()

    def add_variable(self, variable: ComputationVariable) -> None:
        count = self.item_count("variables")
        self.append_item("variables", variable)
        self.variable_inserted_event.fire(count, variable)
        self.computation_mutated_event.fire()

    def remove_variable(self, variable: ComputationVariable) -> None:
        index = self.item_index("variables", variable)
        self.remove_item("variables", variable)
        self.variable_removed_event.fire(index, variable)
        self.computation_mutated_event.fire()

    def create_variable(self, name: str=None, value_type: str=None, value=None, value_default=None, value_min=None, value_max=None, control_type: str=None, specifier: dict=None) -> ComputationVariable:
        variable = ComputationVariable(name, value_type, value, value_default, value_min, value_max, control_type, specifier)
        self.add_variable(variable)
        return variable

    def create_object(self, name: str, object_specifier: dict) -> ComputationVariable:
        variable = ComputationVariable(name, specifier=object_specifier)
        self.add_variable(variable)
        return variable

    def resolve_variable(self, object_specifier: dict) -> ComputationVariable:
        uuid_str = object_specifier.get("uuid")
        uuid_ = uuid.UUID(uuid_str) if uuid_str else None
        if uuid_:
            for variable in self.variables:
                if variable.uuid == uuid_:
                    return variable
        return None

    def parse_expression(self, context, expression, variable_map):
        self.unbind()
        old_data_node = copy.deepcopy(self.__data_node)
        old_error_text = self.error_text
        self.original_expression = expression
        computation_context = ComputationContext(self, context)
        computation_variable_map = copy.copy(variable_map)
        for variable in self.variables:
            computation_variable_map[variable.name] = variable.variable_specifier
        self.__data_node, self.error_text = parse_expression(expression.split("\n"), computation_variable_map, computation_context)
        if self.__data_node:
            self.node = self.__data_node.write()
            self.bind(context)
        if self.__data_node != old_data_node or old_error_text != self.error_text:
            self.needs_update = True
            self.needs_update_event.fire()
            self.computation_mutated_event.fire()

    def begin_evaluate(self):
        with self.__evaluate_lock:
            evaluating = self.__evaluating
            self.__evaluating = True
            return not evaluating

    def end_evaluate(self):
        self.__evaluating = False

    def evaluate(self):
        """Evaluate the computation and return data and metadata."""
        self._evaluation_count_for_test += 1
        def resolve(uuid):
            bound_item = self.__bound_items[uuid]
            return bound_item.value
        result = None
        if self.__data_node:
            result = self.__data_node.evaluate(resolve)
        self.needs_update = False
        return result

    def bind(self, context) -> None:
        """Ask the data node for all bound items, then watch each for changes."""

        # make a computation context based on the enclosing context.
        computation_context = ComputationContext(self, context)

        # normally I would think re-bind should not be valid; but for testing, the expression
        # is often evaluated and bound. it also needs to be bound a new data item is added to a document
        # model. so special case to see if it already exists. this may prove troublesome down the road.
        if len(self.__bound_items) == 0:  # check if already bound
            if self.__data_node:  # error condition
                self.__data_node.bind(computation_context, self.__bound_items)

                def needs_update():
                    self.needs_update = True
                    self.needs_update_event.fire()

                for bound_item_uuid, bound_item in self.__bound_items.items():
                    self.__bound_item_listeners[bound_item_uuid] = bound_item.changed_event.listen(needs_update)

    def unbind(self):
        """Unlisten and close each bound item."""
        for bound_item, bound_item_listener in zip(self.__bound_items.values(), self.__bound_item_listeners.values()):
            bound_item.close()
            bound_item_listener.close()
        self.__bound_items = dict()
        self.__bound_item_listeners = dict()

    def __get_object_specifier_expression(self, specifier):
        if specifier.get("version") == 1:
            specifier_type = specifier["type"]
            if specifier_type == "data_item":
                object_uuid = uuid.UUID(specifier["uuid"])
                return "data_by_uuid(uuid.UUID('{0}'))".format(object_uuid)
            elif specifier_type == "region":
                object_uuid = uuid.UUID(specifier["uuid"])
                return "region_by_uuid(uuid.UUID('{0}'))".format(object_uuid)
        return None

    def reconstruct(self, variable_map):
        if self.__data_node:
            lines = list()
            # construct the variable map, which maps variables used in the text expression
            # to specifiers that can be resolved to specific objects and values.
            computation_variable_map = copy.copy(variable_map)
            for variable in self.variables:
                computation_variable_map[variable.name] = variable.variable_specifier
            variable_map_copy = copy.deepcopy(computation_variable_map)
            expression, precedence = self.__data_node.reconstruct(variable_map_copy)
            for variable, object_specifier in variable_map_copy.items():
                if not variable in computation_variable_map:
                    lines.append("{0} = {1}".format(variable, self.__get_object_specifier_expression(object_specifier)))
            lines.append(expression)
            return "\n".join(lines)
        return None