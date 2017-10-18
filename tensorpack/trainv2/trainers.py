#!/usr/bin/env python
# -*- coding: utf-8 -*-
# File: trainers.py
import os

from ..callbacks.graph import RunOp
from ..tfutils.sesscreate import NewSessionCreator
from ..graph_builder.training import (
    SimpleBuilder,
    SyncMultiGPUParameterServerBuilder,
    SyncMultiGPUReplicatedBuilder,
    AsyncMultiGPUBuilder,
    DistributedReplicatedBuilder)
from ..graph_builder.utils import override_to_local_variable
from ..utils import logger
from ..tfutils import get_global_step_var
from ..tfutils.distributed import get_distributed_session_creator
from ..input_source import QueueInput

from .base import Trainer, SingleCostTrainer

__all__ = ['SimpleTrainer',
           'QueueInputTrainer',
           'SyncMultiGPUTrainerReplicated',
           'SyncMultiGPUTrainerParameterServer',
           'AsyncMultiGPUTrainer',
           'DistributedTrainerReplicated']


class SimpleTrainer(SingleCostTrainer):
    """
    Single-GPU single-cost single-tower trainer.
    """
    def _setup_graph(self, input, get_cost_fn, get_opt_fn):
        self.train_op = SimpleBuilder().build(
            input, get_cost_fn, get_opt_fn)
        return []


# Only works for type check
class QueueInputTrainer(SimpleTrainer):
    def _setup_graph(self, input, get_cost_fn, get_opt_fn):
        assert isinstance(input, QueueInput)
        return super(QueueInputTrainer, self)._setup_graph(input, get_cost_fn, get_opt_fn)


class SyncMultiGPUTrainerParameterServer(SingleCostTrainer):

    __doc__ = SyncMultiGPUParameterServerBuilder.__doc__

    def __init__(self, towers, ps_device='gpu'):
        """
        Args:
            towers ([int]): list of GPU ids.
            ps_device: either 'gpu' or 'cpu', where variables are stored.  Setting to 'cpu' might help when #gpu>=4
        """
        self._builder = SyncMultiGPUParameterServerBuilder(towers, ps_device)
        super(SyncMultiGPUTrainerParameterServer, self).__init__()

    def _setup_graph(self, input, get_cost_fn, get_opt_fn):
        self.train_op = self._builder.build(input, get_cost_fn, get_opt_fn)
        return []


class AsyncMultiGPUTrainer(SingleCostTrainer):

    __doc__ = AsyncMultiGPUBuilder.__doc__

    def __init__(self, towers, scale_gradient=True):
        """
        Args:
            towers ([int]): list of GPU ids.
            scale_gradient (bool): if True, will scale each gradient by ``1.0/nr_gpu``.
        """
        self._builder = AsyncMultiGPUBuilder(towers, scale_gradient)
        super(AsyncMultiGPUTrainer, self).__init__()

    def _setup_graph(self, input, get_cost_fn, get_opt_fn):
        self.train_op = self._builder.build(input, get_cost_fn, get_opt_fn)
        return []


class SyncMultiGPUTrainerReplicated(SingleCostTrainer):

    __doc__ = SyncMultiGPUReplicatedBuilder.__doc__

    def __init__(self, towers):
        """
        Args:
            towers ([int]): list of GPU ids.
        """
        self._builder = SyncMultiGPUReplicatedBuilder(towers)
        super(SyncMultiGPUTrainerReplicated, self).__init__()

    def _setup_graph(self, input, get_cost_fn, get_opt_fn):
        self.train_op, post_init_op = self._builder.build(
            input, get_cost_fn, get_opt_fn)

        cb = RunOp(
            post_init_op,
            run_before=True, run_as_trigger=True, verbose=True)
        return [cb]


class DistributedTrainerReplicated(SingleCostTrainer):

    __doc__ = DistributedReplicatedBuilder.__doc__

    def __init__(self, towers, server):
        """
        Args:
            towers (list[int]): list of GPU ids.
            server (tf.train.Server): the server with ps and workers.
                The job_name must be 'worker' because 'ps' job doesn't need to
                build any graph.
        """
        self.server = server
        self.job_name = server.server_def.job_name
        assert self.job_name in ['ps', 'worker'], self.job_name

        if self.job_name == 'worker':
            # ps doesn't build any graph
            self._builder = DistributedReplicatedBuilder(towers, server)
            self.is_chief = self._builder.is_chief
        else:
            self.is_chief = False
        logger.info("Distributed training on cluster:\n" + str(server.server_def.cluster))

    def train(self,
              inputs_desc, input, get_cost_fn, get_opt_fn,
              callbacks, monitors,
              session_creator, session_init,
              steps_per_epoch, starting_epoch, max_epoch):

        if self.job_name == 'ps':
            logger.info("Running ps {}".format(self.server.server_def.task_index))
            logger.info("Kill me with 'kill {}'".format(os.getpid()))
            self.server.join()  # this will never return tensorflow#4713
            return

        with override_to_local_variable():
            get_global_step_var()  # gs should be local
            # input source may create variable (queue size summary)
            # TODO This is not good because we don't know from here
            # whether something should be global or local. We now assume
            # they should be local.
            input_callbacks = input.setup(inputs_desc)

        train_callbacks = self.setup_graph(input, get_cost_fn, get_opt_fn)
        Trainer.train(
            self,
            callbacks + input_callbacks + train_callbacks, monitors,
            session_creator, session_init,
            steps_per_epoch, starting_epoch, max_epoch)

    def _setup_graph(self, input, get_cost_fn, get_opt_fn):
        self.train_op, initial_sync_op, model_sync_op = self._builder.build(
            input, get_cost_fn, get_opt_fn)

        callbacks = []
        # initial local_vars syncing
        cb = RunOp(lambda: initial_sync_op,
                   run_before=True, run_as_trigger=False, verbose=True)
        cb.chief_only = False
        callbacks.append(cb)

        # model_variables syncing
        if model_sync_op:
            cb = RunOp(lambda: model_sync_op,
                       run_before=False, run_as_trigger=True, verbose=True)
            logger.warn("For efficiency, local MODEL_VARIABLES are only synced to PS once "
                        "every epoch. Be careful if you save the model more frequently than this.")
            callbacks.append(cb)
        return callbacks

    def initialize(self, session_creator, session_init):
        if not isinstance(session_creator, NewSessionCreator):
            raise ValueError(
                "Cannot set session_creator for distributed training! "
                "To use a custom session config, pass it to tf.train.Server.")
        super(DistributedTrainerReplicated, self).initialize(
            get_distributed_session_creator(), session_init)