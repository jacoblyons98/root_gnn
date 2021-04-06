import tensorflow as tf
from tensorflow.compat.v1 import logging
logging.set_verbosity("INFO")
logging.info("TF Version:{}".format(tf.__version__))
try:
    import horovod.tensorflow as hvd
    no_horovod = False
except ModuleNotFoundError:
    logging.warning("No horvod module, cannot perform distributed training")
    no_horovod = True


import os
import six
from types import SimpleNamespace
import pprint
import time
import functools
import argparse

import numpy as np

from tensorflow.python.profiler import profiler_v2 as profiler

from graph_nets import utils_tf
from graph_nets import utils_np
import sonnet as snt

from root_gnn.utils import load_yaml
from root_gnn.src.datasets import graph
from root_gnn import model as all_models
from root_gnn import losses

verbosities = ['DEBUG','ERROR', "FATAL", "INFO", "WARN"]
printer = pprint.PrettyPrinter(indent=2)

def read_dataset(filenames):
    """
    Read dataset...
    """
    AUTO = tf.data.experimental.AUTOTUNE
    tr_filenames = tf.io.gfile.glob(filenames)
    n_files = len(tr_filenames)

    dataset = tf.data.TFRecordDataset(tr_filenames)
    dataset = dataset.map(graph.parse_tfrec_function, num_parallel_calls=AUTO)
    n_graphs = sum([1 for _ in dataset])
    return dataset, n_graphs


def loop_dataset(datasets, batch_size):
    if batch_size > 0:
        in_list = []
        target_list = []
        for dataset in datasets:
            inputs_tr, targets_tr = dataset
            in_list.append(inputs_tr)
            target_list.append(targets_tr)
            if len(in_list) == batch_size:
                inputs_tr = utils_tf.concat(in_list, axis=0)
                targets_tr = utils_tf.concat(target_list, axis=0)
                yield (inputs_tr, targets_tr)
                in_list = []
                target_list = []
    else:
        for dataset in datasets:
            yield dataset


def get_signature(
        dataset, batch_size, with_bool=False,
        dynamic_num_nodes=True,
        dynamic_num_edges=True,
        ):
    """
    Get signature of inputs for the training loop.
    The signature is used by the tf.function
    """
    with_batch_dim = False
    input_list = []
    target_list = []
    for dd in dataset.take(batch_size).as_numpy_iterator():
        input_list.append(dd[0])
        target_list.append(dd[1])

    inputs = utils_tf.concat(input_list, axis=0)
    targets = utils_tf.concat(target_list, axis=0)
    input_signature = (
        graph.specs_from_graphs_tuple(
            inputs, with_batch_dim,
            dynamic_num_nodes=dynamic_num_nodes,
            dynamic_num_edges=dynamic_num_edges
        ),
        graph.specs_from_graphs_tuple(
            targets, with_batch_dim,
            dynamic_num_nodes=dynamic_num_nodes,
            dynamic_num_edges=dynamic_num_edges
        )
    )
    if with_bool:
        input_signature = input_signature + (tf.TensorSpec(shape=[], dtype=tf.bool), )
        
    return input_signature


class TrainerBase(object):

    def __init__(self, train_dir=None, val_dir=None, output_dir=None, 
                model=None, loss_fcn=None, lr=0.0005,
                optimizer=None,
                batch_size=50, num_epochs=1, num_iters=10,
                metric_mode=None,
                early_stop=None, max_attempts=1,
                shuffle_size=1,
                decay_lr=True, # if to use decay learning rate...
                decay_lr_start_epoch=10,
                patterns='*', distributed=False, verbose="INFO", 
                **extra_configs):
        self.train_dir = train_dir
        self.val_dir = val_dir
        self.output_dir = output_dir
        self.shuffle_size = shuffle_size

        self.num_iters = num_iters
        self.batch_size = batch_size
        self.num_epochs = tf.constant(num_epochs, dtype=tf.int32)
        self.epoch_count = tf.Variable(0, trainable=False, name='epoch_count', dtype=tf.int64)
        self.lr = tf.Variable(lr, trainable=False, name='lr', dtype=tf.float32)

        self.model = model
        self.loss_fcn = loss_fcn
        if optimizer:
            self.optimizer = optimizer(learning_rate=self.lr)
        else:
            self.optimizer = snt.optimizers.Adam(learning_rate=self.lr)
        
        self.init_metrics(metric_mode)
        if metric_mode = "clf":
            if early_stop:
                self.early_stop = early_stop
            else:
                self.early_stop = "auc"
            self.max_attempts = max_attempts
            self.attempts = 0
            self.best_metric = 0.0
        else:
            print("Note: early stop metric currently only supported with metric_mode 'clf'")

        # self.decay_lr_start_epoch = tf.constant(decay_lr_start_epoch, dtype=tf.int32)
        # self.decay_lr = decay_lr # if use de
        # perform distributed training
        self.distributed = read_args.distributed
        self.extra_configs = extra_configs

    # Preprocessing
    # --------------------
    @staticmethod
    def get_arg_parser():
        parser = argparse.ArgumentParser()
        add_arg = parser.add_argument
        add_arg("--train-dir", help="name of dir for training", default="tfrec/train_*.tfrec")
        add_arg("--val-dir", help="name of dir for validation", default="tfrec/val_*.tfrec")
        add_arg("--output-dir", help="name of dir for output", default="trained")
        add_arg("--prod-name", help="inner dir for output", default="noedge_fullevts") # TODO: move to tensorboard
        add_arg("--batch-size", type=int, help="training/evaluation batch size", default=50)
        add_arg("--max-epochs", type=int, help="number of epochs", default=1)
        add_arg("--num-iters", type=int, help="level of message passing", default=10)
        add_arg("--shuffle-size", type=int, help="number for shuffling", default=-1)
        return parser

    def init_metrics(self, mode=None):
        if mode is None:
            self.metric_dict = {}
        elif mode == "clf":
            self.metric_dict = {
                "auc": 0.0, "acc": 0.0, "prec": 0.0, "rec": 0.0, "loss": 0.0
            }
        elif mode == "rgr":
            self.metric_dict = {
                "loss": 0.0, "pull": 0.0
            }
        else:
            raise ValueError("Unsupported metric mode: must either be 'clf', 'rgr', or None")
        time_stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime())
        self.metric_writer = tf.summary.create_file_writer(os.path.join(self.output_dir, "logs/{}/metrics".format(time_stamp)))
    
    # User can override
    def early_stop_condition(self):
        current_metric = self.metric_dict[self.early_stop]
        if current_metric < self.best_metric:
            print("Current metric {} {:.4f} is lower than best {:.4f}.".format(
                self.early_stop, current_metric, self.best_metric))
            if self.attempts >= self.max_attempts:
                print("Reached maximum failed attempts: {} attempts. Stopping training".format(self.max_attempts))
                return True
            self.attempts += 1
        else:
            self.best_metric = current_metric
        return False

    def get_shuffle_size(self, shuffle_size):
        # default behavior for train_classifier.py, can override for other models
        return self.ngraphs_train if shuffle_size < 0 else shuffle_size

    def load_training_data(self, filenames=None, shuffle=False, shuffle_size=self.shuffle_size, repeat=1):
        if not filenames:
            filenames = tf.io.gfile.glob(self.train_dir)
        self.data_train, self.ngraphs_train = read_dataset(filenames)
        if shuffle:
            self.data_train = self.data_train.shuffle(self.get_shuffle_size(shuffle_size), seed=12345, reshuffle_each_iteration=False)
        if repeat > 1:
            self.data_train = self.data_train.repeat(repeat)
        self.data_train = self.data_train.prefetch(AUTO)
        return self.ngraphs_train

    def load_validating_data(self, filenames=None, shuffle=False, shuffle_size=self.shuffle_size, repeat=1):
        if not filenames:
            filenames = tf.io.gfile.glob(self.val_dir)
        self.data_val, self.ngraphs_val = read_dataset(filenames)
        if shuffle:
            self.data_val = self.data_val.shuffle(self.get_shuffle_size(shuffle_size), seed=12345, reshuffle_each_iteration=False)
        if repeat > 1:
            self.data_val = self.data_val.repeat(repeat)
        self.data_val = self.data_val.prefetch(AUTO)
        return self.ngraphs_val

    def load_testing_data(self, filenames, shuffle=False, shuffle_size=self.shuffle_size, repeat=1):
        self.data_test, self.ngraphs_test = read_dataset(filenames)
        if shuffle:
            self.data_test = self.data_test.shuffle(self.get_shuffle_size(shuffle_size), seed=12345, reshuffle_each_iteration=False)
        if repeat > 1:
            self.data_test = self.data_test.repeat(repeat)
        self.data_test = self.data_test.prefetch(AUTO)
        return self.ngraphs_test

    def setup_training_loop(self):
        input_signature = self.input_signature()

        def update_step(inputs, targets):
            print("Tracing update_step")
            with tf.GradientTape() as tape:
                output_ops = self.model(inputs, self.num_iters)
                loss_ops_tr = self.loss_fcn(targets, output_ops)
                loss_op_tr = tf.math.reduce_sum(loss_ops_tr) / tf.constant(self.num_iters, dtype=tf.float32)

            gradients = tape.gradient(loss_op_tr, self.model.trainable_variables)
            self.optimizer.apply(gradients, self.model.trainable_variables)
            return loss_op_tr

        self.training_step = tf.function(update_step, input_signature=input_signature)

    def input_signature(self):
        return get_signature(self.data_train, self.train_batch_size)

    def optimizer(self, lr):
        self.optimizer = snt.optimizers.Adam(lr)

    def set_model_and_loss(self, model=None, loss_fcn=None):
        if model:
            self.model = model
        if loss_fcn:
            self.loss_fcn = loss_fcn

    # Training
    # --------------------        

    def train_one_epoch(self, train_data=self.data_train):
        num_batches = 0
        total_loss = 0.
        for inputs in loop_dataset(train_data):
            inputs_tr, targets_tr = inputs
            total_loss += self.training_step(inputs_tr, targets_tr).numpy()
            num_batches += 1
        return total_loss, num_batches

    def validate_one_epoch(self, val_data=self.data_val):
        total_loss = 0.
        num_batches = 0
        predictions, truth_info = [], []
        inputs_list, target_list = [], []
        for data in loop_dataset(val_data):
            inputs, targets = data
            inputs_list.append(inputs)
            target_list.append(targets)
            if len(input_list) == self.batch_size:
                inputs = utils_tf.concat(inputs_list, axis=0)
                targets = utils_tf.concat(target_list, axis=0)
                outputs = self.model(inputs, self.num_iters)
                total_loss += (tf.math.reduce_sum(
                    self.loss_fcn(targets, outputs))/tf.constant(
                        self.num_iters, dtype=tf.float32)).numpy()
                if len(outputs) > 1:
                    outputs = outputs[-1]
                if self.loss_fcn == GlobalLoss:
                    predictions.append(outputs.globals)
                    truth_info.append(targets.globals)
                else:
                    predictions.append(outputs.edges)
                    truth_info.append(targets.edges)
                inputs_list, target_list = [], []
                num_batches += 1
        return total_loss, num_batches, np.array(predictions), np.array(truth_info)

    def update_metrics(self, predictions, truth_info, loss, threshold=0.5):
        if self.metric_mode == "clf":
            y_true, y_pred = (truth_info > threshold), (predictions > threshold)
            fpr, tpr, _ = sklearn.metrics.roc_curve(y_true, predictions)
            metric_dict['auc'] = sklearn.metrics.auc(fpr, tpr)
            metric_dict['acc'] = sklearn.metrics.accuracy_score(y_true, y_pred)
            metric_dict['pre'] = sklearn.metrics.precision_score(y_true, y_pred)
            metric_dict['rec'] = sklearn.metrics.recall_score(y_true, y_pred)
        elif self.metric_mode == "rgr":
            metric_dict['pull'] = np.mean((predictions - truth_info) / truth_info)
        else:
            raise ValueError("currently " + self.metric_mode + " is not supported")
        metric_dict['loss'] = loss
        with self.metric_writer.as_default():
            for key,val in metric_dict.items():
                tf.summary.scalar(key, val, step=self.epoch_count)
            writer.flush()
        self.epoch_count.assign_add(1)

    def update_step(self, model=None, loss_fcn=None):
        set_model_and_loss(model, loss_fcn)
        self.setup_training_loop()
        loss_tr, num_batches_tr = self.train_one_epoch()
        loss_val, num_batches_val = self.validate_one_epoch()
        return loss_tr / num_batches_tr, loss_val / num_batches_val

    # Trains the dataset. If train_data and val_data are specified, it uses those as the dataset.
    # Otherwise, it uses self.data_train and self.data_val attributes of the TrainerBase object.
    def train(self, train_data=None, val_data=None):
        self.setup_training_loop()
        for epoch in range(self.num_epochs):
            loss_tr, num_batches_tr = self.train_one_epoch(train_data)
            loss_val, num_batches_val, predictions, truth_info = self.validate_one_epoch(val_data)
            if metric_mode:
                self.update_metrics(predictions, truth_info, loss_val / num_batches_val)
            if self.early_stop_condition():
                break

    def eval(self, model):
        raise NotImplementedError