# Licensed to Modin Development Team under one or more contributor license agreements.
# See the NOTICE file distributed with this work for additional information regarding
# copyright ownership.  The Modin Development Team licenses this file to you under the
# Apache License, Version 2.0 (the "License"); you may not use this file except in
# compliance with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software distributed under
# the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific language
# governing permissions and limitations under the License.

import abc
from .calcite_algebra import *
from .expr import *
from collections import OrderedDict


class TransformMapper:
    def __init__(self, op):
        self._op = op

    def translate(self, col):
        return self._op.exprs[col]


class InputMapper:
    def __init__(self):
        self._mappers = {}

    def add_mapper(self, frame, mapper):
        self._mappers[frame] = mapper

    def translate(self, ref):
        if ref.modin_frame in self._mappers:
            return self._mappers[ref.modin_frame].translate(ref.column)
        return ref


class DFAlgNode(abc.ABC):
    """Base class for all DataFrame Algebra nodes"""

    @abc.abstractmethod
    def copy(self):
        pass

    def walk_dfs(self, cb, *args, **kwargs):
        if hasattr(self, "input"):
            for i in self.input:
                i._op.walk_dfs(cb, *args, **kwargs)
        cb(self, *args, **kwargs)

    def collect_partitions(self):
        partitions = []
        self.walk_dfs(lambda a, b: a._append_partitions(b), partitions)
        return partitions

    def collect_frames(self):
        frames = []
        self.walk_dfs(lambda a, b: a._append_frames(b), frames)
        return frames

    def _append_partitions(self, partitions):
        pass

    def _append_frames(self, frames):
        pass

    def __repr__(self):
        return self.dumps()

    def dump(self, prefix=""):
        print(self.dumps(prefix))

    def dumps(self, prefix=""):
        return self._prints(prefix)

    @abc.abstractmethod
    def _prints(self, prefix):
        pass

    def _prints_input(self, prefix):
        res = ""
        if hasattr(self, "input"):
            for i, node in enumerate(self.input):
                if isinstance(node._op, FrameNode):
                    res += f"{prefix}input[{i}]: {node._op}\n"
                else:
                    res += f"{prefix}input[{i}]:\n" + node._op._prints(prefix + "  ")
        return res


class FrameNode(DFAlgNode):
    """FrameNode holds a list of Ray object ids for frame partitions"""

    def __init__(self, modin_frame):
        self.modin_frame = modin_frame

    def copy(self):
        return FrameNode(self.modin_frame)

    def _append_partitions(self, partitions):
        partitions += self.modin_frame._partitions.flatten()

    def _append_frames(self, frames):
        frames.append(self.modin_frame)

    def _prints(self, prefix):
        return f"{prefix}{self.modin_frame.id_str()}"


class MaskNode(DFAlgNode):
    def __init__(
        self, base, row_indices=None, row_numeric_idx=None,
    ):
        self.input = [base]
        self.row_indices = row_indices
        self.row_numeric_idx = row_numeric_idx

    def copy(self):
        return MaskNode(self.input[0], self.row_indices, self.row_numeric_idx,)

    def _prints(self, prefix):
        return (
            f"{prefix}MaskNode:\n"
            f"{prefix}  row_indices: {self.row_indices}\n"
            f"{prefix}  row_numeric_idx: {self.row_numeric_idx}\n"
            + self._prints_input(prefix + "  ")
        )


class GroupbyAggNode(DFAlgNode):
    def __init__(self, base, by, agg, groupby_opts):
        self.by = by
        self.agg = agg
        self.groupby_opts = groupby_opts
        self.input = [base]

    def copy(self):
        return GroupbyAggNode(self.input[0], self.by, self.agg, self.groupby_opts)

    def _prints(self, prefix):
        return (
            f"{prefix}AggNode:\n"
            f"{prefix}  by: {self.by}\n"
            f"{prefix}  agg: {self.agg}\n"
            f"{prefix}  groupby_opts: {self.groupby_opts}\n"
            + self._prints_input(prefix + "  ")
        )


class TransformNode(DFAlgNode):
    """Make simple column transformations.

    Args:
        base - frame to transform
        exprs - dictionary with new column names mapped to expressions
        keep_index - if True then keep all index columns (if any),
            otherwise drop them
    """

    def __init__(self, base, exprs, fold=False):
        self.exprs = exprs
        self.input = [base]
        if fold:
            self._fold()

    def _fold(self):
        if isinstance(self.input[0]._op, TransformNode):
            self.input[0] = self.input[0]._op.input[0]
            self.exprs = translate_exprs_to_base(self.exprs, self.input[0])

    def copy(self):
        return TransformNode(self.input[0], self.exprs, self.keep_index)

    def _prints(self, prefix):
        res = f"{prefix}TransformNode:\n"
        for k, v in self.exprs.items():
            res += f"{prefix}  {k}: {v}\n"
        res += self._prints_input(prefix + "  ")
        return res


class JoinNode(DFAlgNode):
    def __init__(
        self, left, right, how="inner", on=None, sort=False, suffixes=("_x", "_y")
    ):
        self.input = [left, right]
        self.how = how
        self.on = on
        self.sort = sort
        self.suffixes = suffixes

    def copy(self):
        return JoinNode(self.input[0], self.input[1], self.how, self.on, self.sort,)

    def _prints(self, prefix):
        return (
            f"{prefix}JoinNode:\n"
            f"{prefix}  How: {self.how}\n"
            f"{prefix}  On: {self.on}\n"
            f"{prefix}  Sorting: {self.sort}\n"
            f"{prefix}  Suffixes: {self.suffixes}\n" + self._prints_input(prefix + "  ")
        )


class UnionNode(DFAlgNode):
    """Concat frames by axis=0, all frames should be aligned."""

    def __init__(self, frames):
        self.input = frames

    def copy(self):
        return UnionNode(self.input)

    def _prints(self, prefix):
        return f"{prefix}UnionNode:\n" + self._prints_input(prefix + "  ")


def translate_exprs_to_base(exprs, base):
    new_exprs = dict(exprs)

    frames = set()
    for k, v in new_exprs.items():
        v.collect_frames(frames)
    frames.discard(base)

    while len(frames) > 0:
        mapper = InputMapper()
        new_frames = set()
        for frame in frames:
            frame_base = frame._op.input[0]
            if frame_base != base:
                new_frames.add(frame_base)
            assert isinstance(frame._op, TransformNode)
            mapper.add_mapper(frame, TransformMapper(frame._op))

        for k, v in new_exprs.items():
            new_expr = new_exprs[k].translate_input(mapper)
            new_expr.collect_frames(new_frames)
            new_exprs[k] = new_expr

        new_frames.discard(base)
        frames = new_frames

    res = OrderedDict()
    for col in exprs.keys():
        res[col] = new_exprs[col]
    return res
