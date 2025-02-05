# coding=utf-8
# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import torch

from .initialize import get_tensor_model_parallel_group
from .initialize import get_tensor_model_parallel_rank
from .initialize import get_tensor_model_parallel_src_rank
from .utils import get_use_hpu
from megatron.global_vars import get_current_device
import functools


_MAX_DATA_DIM = 5


def _check_data_types(keys, data, target_dtype):
    """Check that all the keys have the same target data type."""
    for key in keys:
        assert data[key].dtype == target_dtype, '{} has data type {} which '\
            'is different than {}'.format(key, data[key].dtype, target_dtype)

def callonce(func):
    result = []
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if not result:
            result.append(func(*args, **kwargs))
        return result[0]
    return wrapper

@callonce
def broadcast_sizes(sizes):
    # Move to GPU and broadcast.
    async_op = get_use_hpu()
    sizes_cuda = torch.IntTensor(sizes).to(get_current_device())
    torch.distributed.broadcast(sizes_cuda, get_tensor_model_parallel_src_rank(),
                                group=get_tensor_model_parallel_group(), async_op=async_op)
    # Move back to cpu and unpack.
    sizes_cpu = sizes_cuda.tolist()
    return sizes_cpu

def _build_key_size_numel_dictionaries(keys, data):
    """Build the size on rank 0 and broadcast."""
    max_dim = _MAX_DATA_DIM
    sizes = [0 for _ in range(max_dim) for _ in keys]

    # Pack the sizes on rank zero.
    if get_tensor_model_parallel_rank() == 0:
        offset = 0
        for key in keys:
            assert data[key].dim() < max_dim, 'you should increase MAX_DATA_DIM'
            size = data[key].size()
            for i, s in enumerate(size):
                sizes[i + offset] = s
            offset += max_dim

    sizes_cpu = broadcast_sizes(sizes)
    if get_tensor_model_parallel_rank() == 0:
        assert sizes_cpu == sizes, "sizes have changed and not broadcast to other ranks"
    key_size = {}
    key_numel = {}
    total_numel = 0
    offset = 0
    for key in keys:
        i = 0
        size = []
        numel = 1
        while sizes_cpu[offset + i] > 0:
            this_size = sizes_cpu[offset + i]
            size.append(this_size)
            numel *= this_size
            i += 1
        key_size[key] = size
        key_numel[key] = numel
        total_numel += numel
        offset += max_dim

    return key_size, key_numel, total_numel


def broadcast_data(keys, data, datatype):
    """Broadcast data from rank zero of each model parallel group to the
    members of the same model parallel group.

    Arguments:
        keys: list of keys in the data disctionary to be broadcasted
        data: data dictionary of string keys and cpu tensor values.
        datatype: torch data type of all tensors in data associated
                  with keys.
    """
    # Build (key, size) and (key, number of elements) dictionaries along
    # with the total number of elements on all ranks.
    key_size, key_numel, total_numel = _build_key_size_numel_dictionaries(keys,
                                                                          data)

    # Pack on rank zero.
    if get_tensor_model_parallel_rank() == 0:
        # Check that all keys have the same data type.
        _check_data_types(keys, data, datatype)
        # Flatten the data associated with the keys
        flatten_data = torch.cat(
            [data[key].contiguous().view(-1) for key in keys], dim=0).to(get_current_device())
    else:
        flatten_data = torch.empty(total_numel,
                                   device=get_current_device(),
                                   dtype=datatype)

    # Broadcast
    async_op = get_use_hpu()
    torch.distributed.broadcast(flatten_data, get_tensor_model_parallel_src_rank(),
                                group=get_tensor_model_parallel_group(), async_op=async_op)

    # Unpack
    output = {}
    offset = 0
    for key in keys:
        size = key_size[key]
        numel = key_numel[key]
        output[key] = flatten_data.narrow(0, offset, numel).view(size)
        offset += numel

    return output
