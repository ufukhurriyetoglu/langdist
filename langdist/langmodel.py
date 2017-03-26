# -*- coding: UTF-8 -*-
"""
This module implements language modeling algorithms.
"""
from copy import copy
import os
import pickle
from math import ceil

import numpy as np
import tensorflow as tf
from sklearn.model_selection import train_test_split
from tensorflow.contrib.rnn import DropoutWrapper, LSTMCell, MultiRNNCell
from tensorflow.contrib.rnn import LSTMStateTuple
from tensorflow.contrib.seq2seq import sequence_loss

from langdist.batch import BatchGenerator
from langdist.encoder import CharEncoder
from langdist.util import get_logger

__author__ = 'kensk8er1017@gmail.com'

_LOGGER = get_logger(__name__, write_file=True)


class CharLSTM(object):
    """Character-based language modeling using LSTM."""
    _padding_id = 0  # TODO: 0 is used for actual character as well, which is a bit confusing...
    _random_state = 0  # this is to make train/test split always return the same split
    _checkpoint_file_name = 'model.ckpt'
    _instance_file_name = 'instance.pkl'
    _tensorboard_dir = 'tensorboard.log'

    def __init__(self, embedding_size=128, rnn_size=256, num_rnn_layers=2, learning_rate=0.003,
                 rnn_dropouts=None, final_dropout=0.5, encoder=None):
        # in order to avoid using mutable object as a default argument
        if rnn_dropouts is None:
            # default is 1.0, which means no dropout
            rnn_dropouts = [1.0 for _ in range(num_rnn_layers)]
        assert len(rnn_dropouts) == num_rnn_layers, 'len(rnn_dropouts) != num_rnn_layers'

        self._embedding_size = embedding_size
        self._rnn_size = rnn_size
        self._num_rnn_layers = num_rnn_layers
        self._learning_rate = learning_rate
        self._rnn_dropouts = rnn_dropouts
        self._final_dropout = final_dropout
        self._nodes = None
        self._graph = None
        self._vocab_size = encoder.vocab_size if encoder else None
        self._encoder = encoder if encoder else CharEncoder()
        self._num_params = None
        self._paragraph_border = encoder.paragraph_border if encoder else None
        self._paragraph_border_id = encoder.paragraph_border_id if encoder else None
        self._session = None

    def train(self, paragraphs, model_path, batch_size=64, patience=30000, max_iteration=1000000,
              stat_interval=50, valid_intervals=None, summary_interval=100, valid_size=0.1,
              valid_batch_num=10):
        """Train a language model on the paragraphs of word IDs."""

        def add_metric_summary(summary_writer, mode, iteration, perplexity):
            """Add summary for metric."""
            metric_summary = tf.Summary()
            metric_summary.value.add(tag='{}_perplexity'.format(mode), simple_value=perplexity)
            summary_writer.add_summary(metric_summary, global_step=iteration)

        def validate(X_valid, Y_valid, seq_lens_valid, batch_id, best_perplexity, summary_writer):
            """Validate the model on validation set."""
            valid_losses = list()
            batch_size = ceil(len(X_valid) / valid_batch_num)
            for index in range(valid_batch_num):
                X_valid_batch = X_valid[index * batch_size: (index + 1) * batch_size]
                Y_valid_batch = Y_valid[index * batch_size: (index + 1) * batch_size]
                seq_lens_valid_batch = seq_lens_valid[index * batch_size: (index + 1) * batch_size]

                valid_loss = session.run(
                    nodes['loss'],
                    feed_dict={nodes['X']: X_valid_batch, nodes['Y']: Y_valid_batch,
                               nodes['seq_lens']: seq_lens_valid_batch, nodes['is_train']: False})
                valid_losses.append(valid_loss)

            valid_loss = np.mean(valid_losses, dtype=np.float64)
            perplexity = np.exp(np.mean(valid_loss))  # cross entropy is log-perplexity
            _LOGGER.info('Epoch={}, Iter={:,}, Mean Perplexity (Validation set)= {:.3f}'
                         .format(epoch, iteration, perplexity))
            add_metric_summary(summary_writer, 'valid', iteration, perplexity)

            if perplexity < best_perplexity:
                _LOGGER.info('Best perplexity so far, save the model.')
                self._save(model_path, session)
                best_perplexity = perplexity

            return best_perplexity

        X = self._encode_chars(paragraphs, fit=True)
        # in order to avoid using mutable object as a default argument
        if valid_intervals is None:
            # make the interval trice longer up to 2**9 = 512
            valid_intervals = [2**i for i in range(10)]

        X_train, X_valid = train_test_split(
            X, random_state=self._random_state, test_size=valid_size)

        X_valid, Y_valid = self._create_Y(X_valid)
        X_valid, Y_valid, seq_lens_valid = self._add_padding(X_valid, Y_valid)

        self._build_graph()
        nodes = self._nodes
        train_size = len(X_train)
        train_batch_generator = BatchGenerator(X_train, batch_size)
        best_perplexity = np.float64('inf')

        # Launch the graph
        with tf.Session(graph=self._graph) as session:
            summary_writer = tf.summary.FileWriter(
                os.path.join(model_path, self._tensorboard_dir), session.graph)
            _LOGGER.info('Start fitting a model...')
            session.run(nodes['init'])
            losses = list()
            iteration = 0
            valid_interval = valid_intervals.pop(0)

            for batch_id, X_batch in enumerate(train_batch_generator):
                epoch = 1 + iteration // train_size

                if batch_id % valid_interval == 0:
                    best_perplexity = validate(
                        X_valid, Y_valid, seq_lens_valid, batch_id, best_perplexity, summary_writer)
                    self._sample(session)
                    valid_interval = valid_intervals.pop(0) if valid_intervals else valid_interval

                if batch_id % summary_interval == 0:
                    summaries = session.run(nodes['summaries'])
                    summary_writer.add_summary(summaries, global_step=iteration)

                X_batch, Y_batch = self._create_Y(X_batch)
                X_batch, Y_batch, seq_lens = self._add_padding(X_batch, Y_batch)

                # Predict labels and update the parameters
                _, loss = session.run(
                    [nodes['optimizer'], nodes['loss']],
                    feed_dict={nodes['X']: X_batch, nodes['Y']: Y_batch,
                               nodes['seq_lens']: seq_lens, nodes['is_train']: True})
                losses.append(loss)
                iteration += batch_size

                if batch_id % stat_interval == 0:
                    perplexity = np.exp(np.mean(losses))  # cross entropy is log-perplexity
                    _LOGGER.info('Epoch={}, Iter={:,}, Mean Perplexity (Training batch)= {:.3f}'
                                 .format(epoch, iteration, perplexity))
                    losses = list()
                    add_metric_summary(summary_writer, 'train', iteration, perplexity)

                if iteration > max_iteration or iteration > patience:
                    _LOGGER.info('Iteration is more than max_iteration/patience, finish training.')
                    break

        _LOGGER.info('Finished fitting the model.')
        _LOGGER.info('Best perplexity: {:.3f}'.format(best_perplexity))

    @classmethod
    def load(cls, model_path):
        """
        Load the model from the saved model directory.

        :param model_path: path to the model directory you want to load the model from.
        :return: instance of the model
        """
        # load the instance, set _model_path appropriately
        with open(os.path.join(model_path, cls._instance_file_name), 'rb') as model_file:
            instance = pickle.load(model_file)

        # build the graph and restore the session
        instance._build_graph()
        instance._session = tf.Session(graph=instance._graph)
        instance._session.run(instance._nodes['init'])
        instance._nodes['saver'].restore(
            instance._session, os.path.join(model_path, instance._checkpoint_file_name))

        return instance

    def sample(self, sample_num=10, prompts=None, pick_top_k=10, max_char_len=300, log=False):
        """Sample paragraphs using a trained model running on the given session."""
        return self._sample(self._session, sample_num, prompts, pick_top_k, max_char_len, log)

    def _save(self, model_path, session):
        """Save the tensorflow session and the instance object of this Python class."""
        if not os.path.exists(model_path):
            os.makedirs(model_path)

        # save the session
        self._nodes['saver'].save(session, os.path.join(model_path, self._checkpoint_file_name))

        # save the instance
        instance = copy(self)
        instance._graph = None  # _graph is not picklable
        instance._nodes = None  # _nodes is not pciklable
        with open(os.path.join(model_path, self._instance_file_name), 'wb') as pickle_file:
            pickle.dump(instance, pickle_file)

    def _build_graph(self):
        """Build computational graph."""

        def get_num_params():
            """Count the number of trainable parameters."""
            num_params = 0
            for variable in tf.trainable_variables():
                shape = variable.get_shape()
                var_num_params = 1
                for dimension in shape:
                    var_num_params *= dimension.value
                num_params += var_num_params
            return num_params

        graph = tf.Graph()
        nodes = dict()

        with graph.as_default():
            with tf.name_scope('inputs'):
                nodes['X'] = tf.placeholder(tf.int32, [None, None], name='X')
                nodes['Y'] = tf.placeholder(tf.int32, [None, None], name='Y')
                nodes['seq_lens'] = tf.placeholder(tf.int32, [None], name='seq_lens')
                nodes['is_train'] = tf.placeholder(tf.bool, shape=[], name='is_train')
                rnn_dropouts = tf.where(nodes['is_train'], tf.constant(self._rnn_dropouts),
                                        tf.ones([self._num_rnn_layers]))
                final_dropout = tf.where(
                    nodes['is_train'], tf.constant(self._final_dropout), tf.constant(1.0))

                # get the shape of the input
                X_shape = tf.shape(nodes['X'])
                batch_size = X_shape[0]
                max_seq_len = X_shape[1]

                nodes['initial_states'] = tf.placeholder_with_default(
                    tf.zeros([self._num_rnn_layers, 2, batch_size, self._rnn_size], tf.float32),
                    [self._num_rnn_layers, 2, None, self._rnn_size], 'initial_states')
                initial_states = tf.unstack(nodes['initial_states'], axis=0)
                initial_states = tuple(
                    [LSTMStateTuple(initial_states[layer_id][0], initial_states[layer_id][1])
                     for layer_id in range(self._num_rnn_layers)])

            with tf.name_scope('embedding_layer'):
                nodes['embeddings'] = tf.Variable(
                    tf.random_uniform([self._vocab_size, self._embedding_size], -1.0, 1.0),
                    trainable=True, name='embeddings')
                embedded = tf.nn.embedding_lookup(nodes['embeddings'], nodes['X'])

            with tf.name_scope('rnn_layer'):
                cells = list()
                for layer_id in range(self._num_rnn_layers):
                    cell = LSTMCell(num_units=self._rnn_size)
                    cell = DropoutWrapper(cell, input_keep_prob=rnn_dropouts[layer_id])
                    cells.append(cell)

                rnn_cell = MultiRNNCell(cells)
                rnn_outputs, nodes['states'] = tf.nn.dynamic_rnn(
                    rnn_cell, embedded, nodes['seq_lens'], initial_states, dtype=tf.float32)

                # reshape rnn_outputs so we can compute activations for all the time steps at once
                rnn_outputs = tf.reshape(rnn_outputs, [-1, self._rnn_size])
                rnn_outputs = tf.nn.dropout(rnn_outputs, final_dropout, name='final_dropout')

            with tf.variable_scope('softmax_layer'):
                nodes['W_s'] = tf.Variable(
                    tf.random_normal([self._rnn_size, self._vocab_size]), name='weight')
                nodes['b_s'] = tf.Variable(tf.random_normal([self._vocab_size]), name='bias')
                logits = tf.matmul(rnn_outputs, nodes['W_s']) + nodes['b_s']

            with tf.variable_scope('optimizer'):
                # reshape the logits back to batch_size * seq_lens such that we can compute mean
                # loss after masking padding inputs easily by using sequence_loss
                logits = tf.reshape(logits, [batch_size, max_seq_len, -1])
                weights = tf.cast(tf.sequence_mask(nodes['seq_lens'], max_seq_len), tf.float32)
                nodes['loss'] = sequence_loss(logits=logits, targets=nodes['Y'], weights=weights)
                nodes['optimizer'] = tf.train.AdamOptimizer(self._learning_rate).minimize(
                    nodes['loss'])

            with tf.variable_scope('outputs'):
                nodes['Y_prob'] = tf.nn.softmax(logits)
                nodes['y_pred'] = tf.argmax(nodes['Y_prob'], axis=2)

            # initialize the variables
            nodes['init'] = tf.global_variables_initializer()

            # count the number of parameters
            self._num_params = get_num_params()
            _LOGGER.info('Total number of parameters = {:,}'.format(self._num_params))

            # generate summaries
            for variable in tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES):
                # having ":" in the name is illegal, so replace to "/"
                tf.summary.histogram(variable.name.replace(':', '/'), variable)
            nodes['summaries'] = tf.summary.merge_all()

            # save the model to checkpoint
            nodes['saver'] = tf.train.Saver()

        self._graph = graph
        self._nodes = nodes

    def _add_padding(self, X, Y=None):
        """
        Add paddings to X and Y in order to align the sequence lengths.

        :param X: list of sequences of word IDs
        :param Y: list of sequences of word IDs
        :return: padded list of sequences of word IDs and list of sequence length before padding
        """
        max_len = max(len(x) for x in X)
        seq_lens = list()

        if not Y:
            for x in X:
                seq_lens.append(len(x))
                pad_len = max_len - len(x)
                x.extend([self._padding_id for _ in range(pad_len)])
            return X, seq_lens

        for x, y in zip(X, Y):
            seq_lens.append(len(x))
            pad_len = max_len - len(x)
            x.extend([self._padding_id for _ in range(pad_len)])
            y.extend([self._padding_id for _ in range(pad_len)])
        return X, Y, seq_lens

    def _create_Y(self, X):
        """
        Create Y (correct character sequences) based on X (input character sequences). Also prepend
        the paragraph border character to X (in order to learn the beginning of a paragraph).
        """
        Y = list()
        for x in X:
            y = copy(x)
            y.append(self._paragraph_border_id)
            Y.append(y)
            x.insert(0, self._paragraph_border_id)
            assert len(x) == len(y), 'len(x) != len(y)'
        return X, Y

    def _encode_chars(self, paragraphs, fit):
        """Convert paragraphs of characters into encoded characters (character IDs)."""
        if fit:
            if self._encoder.is_fit():
                encoded_paragraphs = self._encoder.encode(paragraphs)
            else:
                encoded_paragraphs = self._encoder.fit_encode(paragraphs)
            self._vocab_size = self._encoder.vocab_size
            self._paragraph_border = self._encoder.paragraph_border
            self._paragraph_border_id = self._encoder.paragraph_border_id
        else:
            encoded_paragraphs = self._encoder.encode(paragraphs)
        return encoded_paragraphs

    def _decode_chars(self, paragraphs):
        """Convert paragraphs of encoded character IDs into decoded characters."""
        return self._encoder.decode(paragraphs)

    def _sample(self, session, sample_num=10, prompts=None, pick_top_k=10, max_char_len=300,
                log=True):
        """Sample paragraphs using a trained model running on the given session."""

        def sample_chars_from_probs(Y_prob):
            """Sample a character for each paragraph based on the predicted probabilities."""
            Y_prob = np.squeeze(Y_prob, axis=1)
            chars = list()
            for y_prob in Y_prob:
                if pick_top_k:
                    y_prob[np.argsort(y_prob)[:-pick_top_k]] = 0
                    y_prob /= np.sum(y_prob)
                chars.append(np.random.choice(self._vocab_size, 1, p=y_prob)[0])
            return chars

        paragraphs = [[self._paragraph_border_id] for _ in range(sample_num)]
        if prompts:
            assert sample_num == len(prompts), 'sample_num != len(prompts)'
            for paragraph_id, prompt in enumerate(prompts):
                paragraphs[paragraph_id].append(self._encode_chars([prompt], fit=False)[0])

        X, seq_lens = self._add_padding(paragraphs)
        paragraph_ids = list(range(sample_num))  # IDs of paragraphs to still sample
        nodes = self._nodes
        initial_states = np.zeros((self._num_rnn_layers, 2, sample_num, self._rnn_size))

        while len(max(paragraphs, key=len)) < max_char_len:
            Y_prob, states = session.run(
                [nodes['Y_prob'], nodes['states']],
                feed_dict={nodes['X']: X, nodes['seq_lens']: seq_lens, nodes['is_train']: False,
                           nodes['initial_states']: initial_states})
            sampled_char_ids = sample_chars_from_probs(Y_prob)
            next_sample_ids = list()

            for sample_id, paragraph_id in enumerate(paragraph_ids):
                sampled_char_id = sampled_char_ids[sample_id]

                # don't process paragraphs that already finish sampling
                if sampled_char_id == self._paragraph_border_id:
                    continue

                paragraphs[paragraph_id].append(sampled_char_id)
                next_sample_ids.append(sample_id)

            # finish the loop when there's no more paragraph to sample
            if not next_sample_ids:
                break

            # prepare next input
            # don't process paragraphs that already finish sampling
            paragraph_ids = [paragraph_ids[sample_id] for sample_id in next_sample_ids]
            initial_states = np.array(states)[:, :, np.array(next_sample_ids)]
            X = list()
            for paragraph_id in paragraph_ids:
                X.append([paragraphs[paragraph_id][-1]])
            X, seq_lens = self._add_padding(X)

        paragraphs = self._decode_chars(paragraphs)
        paragraphs = [paragraph.strip() for paragraph in paragraphs]

        if log:
            _LOGGER.info('Sampled paragraphs: \n{}'.format('\n'.join(paragraphs)))

        return paragraphs
