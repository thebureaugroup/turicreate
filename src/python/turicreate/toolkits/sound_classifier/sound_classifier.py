# -*- coding: utf-8 -*-
# Copyright © 2017 Apple Inc. All rights reserved.
#
# Use of this source code is governed by a BSD-3-clause license that can
# be found in the LICENSE.txt file or at https://opensource.org/licenses/BSD-3-Clause
"""
Class definition and utilities for the sound classification toolkit.
"""
from __future__ import print_function as _
from __future__ import division as _
from __future__ import absolute_import as _

import logging as _logging
import numpy as _np

import turicreate as _tc
import turicreate.toolkits._internal_utils as _tk_utils
from turicreate.toolkits._main import ToolkitError as _ToolkitError
from turicreate.toolkits._model import CustomModel as _CustomModel
from turicreate.toolkits._model import PythonProxy as _PythonProxy


def _is_deep_feature_sarray(sa):
    if not isinstance(sa, _tc.SArray):
        return False
    if sa.dtype != list:
        return False
    if not isinstance(sa[0][0], _np.ndarray):
        return False
    if sa[0][0].dtype != _np.float64:
        return False
    if len(sa[0][0]) != 12288:
        return False
    return True

def _is_audio_data_sarray(sa):
    if not isinstance(sa, _tc.SArray):
        return False
    if sa.dtype != dict:
        return False
    if set(sa[0].keys()) != {'sample_rate', 'data'}:
        return False
    return True

def get_deep_features(audio_data, verbose=True):
    '''
    Calculates the deep features used by the Sound Classifier.

    Internally the Sound Classifier calculates deep features for both model
    creation and predictions. If the same data will be used multiple times,
    calculating the deep features just once will result in a significant speed
    up.

    Parameters
    ----------
    audio_data : SArray
        Audio data is represented as dicts with key 'data' and 'sample_rate',
        see `turicreate.load_audio(...)`.

    Examples
    --------
    >>> my_audio_data['deep_features'] = get_deep_features(my_audio_data['audio'])
    >>> train, test = my_audio_data.random_split(.8)
    >>> model = tc.sound_classifier.create(train, 'label', 'deep_features')
    >>> predictions = model.predict(test)
    '''
    from ._audio_feature_extractor import _get_feature_extractor

    if not _is_audio_data_sarray(audio_data):
        raise TypeError("Input must be audio data")

    feature_extractor_name = 'VGGish'
    feature_extractor = _get_feature_extractor(feature_extractor_name)

    return feature_extractor.get_deep_features(audio_data, verbose=verbose)


def create(dataset, target, feature, max_iterations=10,
           custom_layer_sizes=[100, 100], verbose=True,
           validation_set='auto', batch_size=64):
    '''
    Creates a :class:`SoundClassifier` model.

    Parameters
    ----------
    dataset : SFrame
        Input data. The column named by the 'feature' parameter will be
        extracted for modeling.

    target : string or int
        Name of the column containing the target variable. The values in this
        column must be of string or integer type.

    feature : string
        Name of the column containing the feature column. This column must
        contain audio data or deep audio features.
        Audio data is represented as dicts with key 'data' and 'sample_rate',
        see `turicreate.load_audio(...)`.
        Deep audio features are represented as a list of numpy arrays, each of
        size 12288, see `turicreate.sound_classifier.get_deep_features(...)`.

    max_iterations : int, optional
        The maximum number of allowed passes through the data. More passes over
        the data can result in a more accurately trained model. Consider
        increasing this (the default value is 10) if the training accuracy is
        low.

    custom_layer_sizes : list of ints
        Specifies the architecture of the custom neural network. This neural
        network is made up of a series of dense layers. This parameter allows
        you to specify how many layers and the number of units in each layer.
        The custom neural network will always have one more layer than the
        length of this list. The last layer is always a soft max with units
        equal to the number of classes.

    verbose : bool, optional
        If True, prints progress updates and model details.

    validation_set : SFrame, optional
        A dataset for monitoring the model's generalization performance. The
        format of this SFrame must be the same as the training dataset. By
        default, a validation set is automatically sampled. If `validation_set`
        is set to None, no validataion is used. You can also pass a validation
        set you have constructed yourself.

    batch_size : int, optional
        If you are getting memory errors, try decreasing this value. If you
        have a powerful computer, increasing this value may improve performance.
    '''
    import time
    from .._mxnet import _mxnet_utils
    import mxnet as mx

    from ._audio_feature_extractor import _get_feature_extractor

    start_time = time.time()

    # check parameters
    if len(dataset) == 0:
        raise _ToolkitError('Unable to train on empty dataset')
    if feature not in dataset.column_names():
        raise _ToolkitError("Audio feature column '%s' does not exist" % feature)
    if not _is_deep_feature_sarray(dataset[feature]) and not _is_audio_data_sarray(dataset[feature]):
        raise _ToolkitError("'%s' column is not audio data." % feature)
    if target not in dataset.column_names():
        raise _ToolkitError("Target column '%s' does not exist" % target)
    if not _tc.util._is_non_string_iterable(custom_layer_sizes) or len(custom_layer_sizes) == 0:
        raise _ToolkitError("'custom_layer_sizes' must be a non-empty list.")
    for i in custom_layer_sizes:
        if not isinstance(i, int):
            raise _ToolkitError("'custom_layer_sizes' must contain only integers.")
    if not (isinstance(validation_set, _tc.SFrame) or validation_set == 'auto' or validation_set is None):
        raise TypeError("Unrecognized value for 'validation_set'")
    if isinstance(validation_set, _tc.SFrame):
        if feature not in validation_set.column_names() or target not in validation_set.column_names():
            raise ValueError("The 'validation_set' SFrame must be in the same format as the 'dataset'")
    if batch_size < 1:
        raise ValueError('\'batch_size\' must be greater than or equal to 1')

    classes = list(dataset[target].unique().sort())
    num_labels = len(classes)
    if num_labels <= 1:
        raise ValueError('The number of classes must be greater than one.')
    feature_extractor_name = 'VGGish'
    feature_extractor = _get_feature_extractor(feature_extractor_name)
    class_label_to_id = {l: i for i, l in enumerate(classes)}

    # create the validation set
    if not isinstance(validation_set, _tc.SFrame) and validation_set == 'auto':
        if len(dataset) >= 100:
            print ( "Creating a validation set from 5 percent of training data. This may take a while.\n"
                    "\tYou can set ``validation_set=None`` to disable validation tracking.\n")
            dataset, validation_set = dataset.random_split(0.95, exact=True)
        else:
            validation_set = None

    encoded_target = dataset[target].apply(lambda x: class_label_to_id[x])

    if _is_deep_feature_sarray(dataset[feature]):
        train_deep_features = dataset[feature]
    else:
        # do the preprocess and VGGish feature extraction
        train_deep_features = get_deep_features(dataset[feature], verbose=verbose)

    train_data = _tc.SFrame({'deep features': train_deep_features, 'labels': encoded_target})
    train_data = train_data.stack('deep features', new_column_name='deep features')
    train_data, missing_ids = train_data.dropna_split(columns=['deep features'])

    if len(missing_ids) > 0:
        _logging.warning("Dropping %d examples which are less than 975ms in length." % len(missing_ids))

    if validation_set is not None:
        if verbose:
            print("Preparing validataion set")
        validation_encoded_target = validation_set[target].apply(lambda x: class_label_to_id[x])

        if _is_deep_feature_sarray(validation_set[feature]):
            validation_deep_features = validation_set[feature]
        else:
            validation_deep_features = get_deep_features(validation_set[feature], verbose=verbose)

        validation_data = _tc.SFrame({'deep features': validation_deep_features, 'labels': validation_encoded_target})
        validation_data = validation_data.stack('deep features', new_column_name='deep features')
        validation_data = validation_data.dropna(columns=['deep features'])

        validation_batch_size = min(len(validation_data), batch_size)
        validation_data = mx.io.NDArrayIter(validation_data['deep features'].to_numpy(),
                                             label=validation_data['labels'].to_numpy(),
                                             batch_size=validation_batch_size)
    else:
        validation_data = []

    if verbose:
        print("\nTraining a custom neural network -")

    training_batch_size = min(len(train_data), batch_size)
    train_data = mx.io.NDArrayIter(train_data['deep features'].to_numpy(),
                                    label=train_data['labels'].to_numpy(),
                                    batch_size=training_batch_size, shuffle=True)

    custom_NN = SoundClassifier._build_custom_neural_network(feature_extractor.output_length, num_labels, custom_layer_sizes)
    ctx = _mxnet_utils.get_mxnet_context()
    custom_NN.initialize(mx.init.Xavier(), ctx=ctx)

    trainer = mx.gluon.Trainer(custom_NN.collect_params(), 'nag', {'learning_rate': 0.01, 'momentum': 0.9})

    if verbose:
        # Setup progress table
        row_ids = ['iteration', 'train_accuracy', 'time']
        row_display_names = ['Iteration', 'Training Accuracy', 'Elapsed Time']
        if validation_data:
            row_ids.insert(2, 'validation_accuracy')
            row_display_names.insert(2, 'Validation Accuracy (%)')
        table_printer = _tc.util._ProgressTablePrinter(row_ids, row_display_names)

    train_metric = mx.metric.Accuracy()
    if validation_data:
        validation_metric = mx.metric.Accuracy()
    softmax_cross_entropy_loss = mx.gluon.loss.SoftmaxCrossEntropyLoss()
    for i in range(max_iterations):
        # TODO: early stopping

        for batch in train_data:
            data = mx.gluon.utils.split_and_load(batch.data[0], ctx_list=ctx, batch_axis=0, even_split=False)
            label = mx.gluon.utils.split_and_load(batch.label[0], ctx_list=ctx, batch_axis=0, even_split=False)

            # Inside training scope
            with mx.autograd.record():
                for x, y in zip(data, label):
                    z = custom_NN(x)
                    # Computes softmax cross entropy loss.
                    loss = softmax_cross_entropy_loss(z, y)
                    # Backpropagate the error for one iteration.
                    loss.backward()
            # Make one step of parameter update. Trainer needs to know the
            # batch size of data to normalize the gradient by 1/batch_size.
            trainer.step(batch.data[0].shape[0])
        train_data.reset()

        # Calculate training metric
        for batch in train_data:
            data = mx.gluon.utils.split_and_load(batch.data[0], ctx_list=ctx, batch_axis=0, even_split=False)
            label = mx.gluon.utils.split_and_load(batch.label[0], ctx_list=ctx, batch_axis=0, even_split=False)
            outputs = [custom_NN(x) for x in data]
            train_metric.update(label, outputs)
        train_data.reset()

        # Calculate validataion metric
        for batch in validation_data:
            data = mx.gluon.utils.split_and_load(batch.data[0], ctx_list=ctx, batch_axis=0, even_split=False)
            label = mx.gluon.utils.split_and_load(batch.label[0], ctx_list=ctx, batch_axis=0, even_split=False)
            outputs = [custom_NN(x) for x in data]
            validation_metric.update(label, outputs)

        # Get metrics, print progress table
        _, train_accuracy = train_metric.get()
        train_metric.reset()
        printed_row_values = {'iteration': i+1, 'train_accuracy': train_accuracy}
        if validation_data:
            _, validataion_accuracy = validation_metric.get()
            printed_row_values['validation_accuracy'] = validataion_accuracy
            validation_metric.reset()
            validation_data.reset()
        if verbose:
            printed_row_values['time'] = time.time()-start_time
            table_printer.print_row(**printed_row_values)


    state = {
        '_class_label_to_id': class_label_to_id,
        '_custom_classifier': custom_NN,
        '_feature_extractor': feature_extractor,
        '_id_to_class_label': {v: k for k, v in class_label_to_id.items()},
        'classes': classes,
        'custom_layer_sizes': custom_layer_sizes,
        'feature': feature,
        'feature_extractor_name': feature_extractor.name,
        'num_classes': num_labels,
        'num_examples': len(dataset),
        'target': target,
        'training_accuracy': train_accuracy,
        'training_time': time.time() - start_time,
        'validation_accuracy': validataion_accuracy if validation_data else None,
    }
    return SoundClassifier(state)


class SoundClassifier(_CustomModel):
    """
    A trained model that is ready to use for sound classification or to export to CoreML.

    This model should not be constructed directly.

    See Also
    ----------
    create
    """
    _PYTHON_SOUND_CLASSIFIER_VERSION = 1

    @staticmethod
    def _build_custom_neural_network(num_inputs, num_labels, layer_sizes):
        from mxnet.gluon import nn

        net = nn.Sequential(prefix='custom_')
        with net.name_scope():
            for i, cur_layer_size in enumerate(layer_sizes):
                prefix = "dense%d_" % i
                if i == 0:
                    in_units = num_inputs
                else:
                    in_units = layer_sizes[i-1]

                net.add(nn.Dense(cur_layer_size, in_units=in_units, activation='relu', prefix=prefix))

            prefix = 'dense%d_' % len(layer_sizes)
            net.add(nn.Dense(num_labels, prefix=prefix))
        return net

    def __init__(self, state):
        self.__proxy__ = _PythonProxy(state)

    @classmethod
    def _native_name(cls):
        return "sound_classifier"

    def _get_version(self):
        return self._PYTHON_SOUND_CLASSIFIER_VERSION

    def _get_native_state(self):
        """
        Save the model as a dictionary, which can be loaded with the
        :py:func:`~turicreate.load_model` method.
        """
        from .._mxnet import _mxnet_utils
        state = self.__proxy__.get_state()

        del state['_feature_extractor']

        mxnet_params = state['_custom_classifier'].collect_params()
        state['_custom_classifier'] = _mxnet_utils.get_gluon_net_params_state(mxnet_params)

        return state

    @classmethod
    def _load_version(cls, state, version):
        """
        A function to load a previously saved SoundClassifier instance.
        """
        from ._audio_feature_extractor import _get_feature_extractor
        from .._mxnet import _mxnet_utils

        state['_feature_extractor'] = _get_feature_extractor(state['feature_extractor_name'])

        # Load the custom nerual network
        num_classes = state['num_classes']
        num_inputs = state['_feature_extractor'].output_length
        if 'custom_layer_sizes' in state:
            # These are deserialized as floats
            custom_layer_sizes = list(map(int, state['custom_layer_sizes']))
        else:
            # Default value, was not part of state for only Turi Create 5.4
            custom_layer_sizes = [100, 100]
        state['custom_layer_sizes'] = custom_layer_sizes
        net = SoundClassifier._build_custom_neural_network(num_inputs, num_classes, custom_layer_sizes)
        net_params = net.collect_params()
        ctx = _mxnet_utils.get_mxnet_context()
        _mxnet_utils.load_net_params_from_state(net_params, state['_custom_classifier'], ctx=ctx)
        state['_custom_classifier'] = net

        return SoundClassifier(state)

    def __str__(self):
        """
        Return a string description of the model to the ``print`` method.

        Returns
        -------
        out : string
            A description of the SoundClassifier.
        """
        return self.__repr__()

    def __repr__(self):
        """
        Print a string description of the model when the model name is entered
        in the terminal.
        """
        import turicreate.toolkits._internal_utils as tkutl

        width = 40

        sections, section_titles = self._get_summary_struct()
        out = tkutl._toolkit_repr_print(self, sections, section_titles,
                                        width=width)
        return out

    def _get_summary_struct(self):
        """
        Returns a structured description of the model, including the
        schema of the training data, description of the training
        data, training statistics, and model hyperparameters.

        Returns
        -------
        sections : list (of list of tuples)
            A list of summary sections.
              Each section is a list.
                Each item in a section list is a tuple of the form:
                  ('<label>','<field>')
        section_titles: list
            A list of section titles.
              The order matches that of the 'sections' object.
        """
        model_fields = [
            ('Number of classes', 'num_classes'),
            ('Number of training examples', 'num_examples'),
            ('Custom layer sizes', 'custom_layer_sizes'),
        ]
        training_fields = [
            ('Number of examples', 'num_examples'),
            ("Training accuracy", 'training_accuracy'),
            ("Validation accuracy", 'validation_accuracy'),
            ("Training time (sec)", 'training_time'),
        ]

        section_titles = ['Schema', 'Training Summary']
        return([model_fields, training_fields], section_titles)

    def classify(self, dataset, verbose=True, batch_size=64):
        """
        Return the classification for each examples in the ``dataset``.
        The output SFrame contains predicted class labels and its probability.

        Parameters
        ----------
        dataset : SFrame | SArray | dict
            The audio data to be classified.
            If dataset is an SFrame, it must have a column with the same name as
            the feature used for model training, but does not require a target
            column. Additional columns are ignored.

        verbose : bool, optional
            If True, prints progress updates and model details.

        batch_size : int, optional
            If you are getting memory errors, try decreasing this value. If you
            have a powerful computer, increasing this value may improve performance.

        Returns
        -------
        out : SFrame
            An SFrame with model predictions, both class labels and probabilities.

        See Also
        ----------
        create, evaluate, predict

        Examples
        ----------
        >>> classes = model.classify(data)
        """
        prob_vector = self.predict(dataset, output_type='probability_vector',
                                   verbose=verbose, batch_size=batch_size)
        id_to_label = self._id_to_class_label

        return _tc.SFrame({
            'class': prob_vector.apply(lambda v: id_to_label[_np.argmax(v)]),
            'probability': prob_vector.apply(_np.max)
        })

    def evaluate(self, dataset, metric='auto', verbose=True, batch_size=64):
        """
        Evaluate the model by making predictions of target values and comparing
        these to actual values.

        Parameters
        ----------
        dataset : SFrame
            Dataset to use for evaluation, must include a column with the same
            name as the features used for model training. Additional columns
            are ignored.

        metric : str, optional
            Name of the evaluation metric.  Possible values are:

            - 'auto'             : Returns all available metrics.
            - 'accuracy'         : Classification accuracy (micro average).
            - 'auc'              : Area under the ROC curve (macro average)
            - 'precision'        : Precision score (macro average)
            - 'recall'           : Recall score (macro average)
            - 'f1_score'         : F1 score (macro average)
            - 'log_loss'         : Log loss
            - 'confusion_matrix' : An SFrame with counts of possible
                                   prediction/true label combinations.
            - 'roc_curve'        : An SFrame containing information needed for an
                                   ROC curve

        verbose : bool, optional
            If True, prints progress updates and model details.

        batch_size : int, optional
            If you are getting memory errors, try decreasing this value. If you
            have a powerful computer, increasing this value may improve performance.

        Returns
        -------
        out : dict
            Dictionary of evaluation results where the key is the name of the
            evaluation metric (e.g. `accuracy`) and the value is the evaluation
            score.

        See Also
        ----------
        classify, predict

        Examples
        ----------
        .. sourcecode:: python

          >>> results = model.evaluate(data)
          >>> print results['accuracy']
        """
        from turicreate.toolkits import evaluation

        # parameter checking
        if not isinstance(dataset, _tc.SFrame):
            raise TypeError('\'dataset\' parameter must be an SFrame')

        avail_metrics = ['accuracy', 'auc', 'precision', 'recall',
                         'f1_score', 'log_loss', 'confusion_matrix', 'roc_curve']
        _tk_utils._check_categorical_option_type(
            'metric', metric, avail_metrics + ['auto'])

        if metric == 'auto':
            metrics = avail_metrics
        else:
            metrics = [metric]

        if _is_deep_feature_sarray(dataset[self.feature]):
            deep_features = dataset[self.feature]
        else:
            deep_features = get_deep_features(dataset[self.feature], verbose=verbose)
        data = _tc.SFrame({'deep features': deep_features})
        data = data.add_row_number()
        missing_ids = data.filter_by([[]], 'deep features')['id']

        if len(missing_ids) > 0:
            data = data.filter_by([[]], 'deep features', exclude=True)
            # Remove the labels for entries without deep features
            _logging.warning("Dropping %d examples which are less than 975ms in length." % len(missing_ids))
            labels = dataset[[self.target]].add_row_number()
            labels = data.join(labels, how='left')[self.target]
        else:
            labels = dataset[self.target]
        assert(len(labels) == len(data))

        if any([m in metrics for m in ('roc_curve', 'log_loss', 'auc')]):
            probs = self.predict(data['deep features'], output_type='probability_vector',
                                 verbose=verbose, batch_size=batch_size)
        if any([m in metrics for m in ('accuracy', 'precision', 'recall', 'f1_score', 'confusion_matrix')]):
            classes = self.predict(data['deep features'], output_type='class',
                                   verbose=verbose, batch_size=batch_size)

        ret = {}
        if 'accuracy' in metrics:
            ret['accuracy'] = evaluation.accuracy(labels, classes)
        if 'auc' in metrics:
            ret['auc'] = evaluation.auc(labels, probs, index_map=self._class_label_to_id)
        if 'precision' in metrics:
            ret['precision'] = evaluation.precision(labels, classes)
        if 'recall' in metrics:
            ret['recall'] = evaluation.recall(labels, classes)
        if 'f1_score' in metrics:
            ret['f1_score'] = evaluation.f1_score(labels, classes)
        if 'log_loss' in metrics:
            ret['log_loss'] = evaluation.log_loss(labels, probs, index_map=self._class_label_to_id)
        if 'confusion_matrix' in metrics:
            ret['confusion_matrix'] = evaluation.confusion_matrix(labels, classes)
        if 'roc_curve' in metrics:
            ret['roc_curve'] = evaluation.roc_curve(labels, probs, index_map=self._class_label_to_id)

        return ret

    def export_coreml(self, filename):
        """
        Save the model in Core ML format.

        See Also
        --------
        save

        Examples
        --------
        >>> model.export_coreml('./myModel.mlmodel')
        """
        import coremltools
        from coremltools.proto.FeatureTypes_pb2 import ArrayFeatureType
        from .._mxnet import _mxnet_utils

        prob_name = self.target + 'Probability'

        def get_custom_model_spec():
            from coremltools.models.neural_network import NeuralNetworkBuilder
            from coremltools.models.datatypes import Array, Dictionary, String

            input_name = 'output1'
            input_length = self._feature_extractor.output_length
            builder = NeuralNetworkBuilder([(input_name, Array(input_length,))],
                                           [(prob_name, Dictionary(String))],
                                           'classifier')

            ctx = _mxnet_utils.get_mxnet_context()[0]
            input_name, output_name = input_name, 0
            for i, cur_layer in enumerate(self._custom_classifier):
                W = cur_layer.weight.data(ctx).asnumpy()
                nC, nB = W.shape
                Wb = cur_layer.bias.data(ctx).asnumpy()

                builder.add_inner_product(name="inner_product_"+str(i),
                                          W=W,
                                          b=Wb,
                                          input_channels=nB,
                                          output_channels=nC,
                                          has_bias=True,
                                          input_name=str(input_name),
                                          output_name='inner_product_'+str(output_name))

                if cur_layer.act:
                    builder.add_activation("activation"+str(i), 'RELU', 'inner_product_'+str(output_name), str(output_name))

                input_name = i
                output_name = i + 1

            last_output = builder.spec.neuralNetworkClassifier.layers[-1].output[0]
            builder.add_softmax('softmax', last_output, self.target)

            builder.set_class_labels(self.classes)
            builder.set_input([input_name], [(input_length,)])
            builder.set_output([self.target], [(self.num_classes,)])

            return builder.spec


        top_level_spec = coremltools.proto.Model_pb2.Model()
        top_level_spec.specificationVersion = 3

        # Set input
        desc = top_level_spec.description
        input = desc.input.add()
        input.name = self.feature
        input.type.multiArrayType.dataType = ArrayFeatureType.ArrayDataType.Value('FLOAT32')
        input.type.multiArrayType.shape.append(15600)

        # Set outputs
        prob_output = desc.output.add()
        prob_output.name = prob_name
        label_output = desc.output.add()
        label_output.name = 'classLabel'
        desc.predictedFeatureName = 'classLabel'
        desc.predictedProbabilitiesName = prob_name
        if type(self.classes[0]) == int:
            # Class labels are ints
            prob_output.type.dictionaryType.int64KeyType.MergeFromString(b'')
            label_output.type.int64Type.MergeFromString(b'')
        else:     # Class are strings
            prob_output.type.dictionaryType.stringKeyType.MergeFromString(b'')
            label_output.type.stringType.MergeFromString(b'')

        # Set metadata
        user_metadata = desc.metadata.userDefined
        user_metadata['sampleRate'] = str(self._feature_extractor.input_sample_rate)

        pipeline = top_level_spec.pipelineClassifier.pipeline

        # Add the preprocessing model
        preprocessing_model = pipeline.models.add()
        preprocessing_model.customModel.className = 'TCSoundClassifierPreprocessing'
        preprocessing_model.specificationVersion = 3
        preprocessing_input = preprocessing_model.description.input.add()
        preprocessing_input.CopyFrom(input)

        preprocessed_output = preprocessing_model.description.output.add()
        preprocessed_output.name = 'preprocessed_data'
        preprocessed_output.type.multiArrayType.dataType = ArrayFeatureType.ArrayDataType.Value('DOUBLE')
        preprocessed_output.type.multiArrayType.shape.append(1)
        preprocessed_output.type.multiArrayType.shape.append(96)
        preprocessed_output.type.multiArrayType.shape.append(64)

        # Add the feature extractor, updating its input name
        feature_extractor_spec = self._feature_extractor.get_spec()
        pipeline.models.add().CopyFrom(feature_extractor_spec)
        pipeline.models[-1].description.input[0].name = preprocessed_output.name
        pipeline.models[-1].neuralNetwork.layers[0].input[0] = preprocessed_output.name

        # Add the custom neural network
        pipeline.models.add().CopyFrom(get_custom_model_spec())

        # Set key type for the probability dictionary
        prob_output_type = pipeline.models[-1].description.output[0].type.dictionaryType
        if type(self.classes[0]) == int:
            prob_output_type.int64KeyType.MergeFromString(b'')
        else:    # String labels
            prob_output_type.stringKeyType.MergeFromString(b'')

        mlmodel = coremltools.models.MLModel(top_level_spec)
        mlmodel.save(filename)

    def predict(self, dataset, output_type='class', verbose=True, batch_size=64):
        """
        Return predictions for ``dataset``. Predictions can be generated
        as class labels or probabilities.

        Parameters
        ----------
        dataset : SFrame | SArray | dict
            The audio data to be classified.
            If dataset is an SFrame, it must have a column with the same name as
            the feature used for model training, but does not require a target
            column. Additional columns are ignored.

        output_type : {'probability', 'class', 'probability_vector'}, optional
            Form of the predictions which are one of:

            - 'class': Class prediction. For multi-class classification, this
              returns the class with maximum probability.
            - 'probability': Prediction probability associated with the True
              class (not applicable for multi-class classification)
            - 'probability_vector': Prediction probability associated with each
              class as a vector. Label ordering is dictated by the ``classes``
              member variable.

        verbose : bool, optional
            If True, prints progress updates and model details.

        batch_size : int, optional
            If you are getting memory errors, try decreasing this value. If you
            have a powerful computer, increasing this value may improve performance.

        Returns
        -------
        out : SArray
            An SArray with the predictions.

        See Also
        ----------
        evaluate, classify

        Examples
        ----------
        >>> probability_predictions = model.predict(data, output_type='probability')
        >>> prediction_vector = model.predict(data, output_type='probability_vector')
        >>> class_predictions = model.predict(data, output_type='class')

        """
        from .._mxnet import _mxnet_utils
        import mxnet as mx

        if not isinstance(dataset, (_tc.SFrame, _tc.SArray, dict)):
            raise TypeError('\'dataset\' parameter must be either an SFrame, SArray or dictionary')

        if isinstance(dataset, dict):
            if(set(dataset.keys()) != {'sample_rate', 'data'}):
                raise ValueError('\'dataset\' parameter is a dictionary but does not appear to be audio data.')
            dataset = _tc.SArray([dataset])
        elif isinstance(dataset, _tc.SFrame):
            dataset = dataset[self.feature]

        if not _is_deep_feature_sarray(dataset) and not _is_audio_data_sarray(dataset):
            raise ValueError('\'dataset\' must be either audio data or audio deep features.')

        if output_type not in ('probability', 'probability_vector', 'class'):
            raise ValueError('\'dataset\' parameter must be either an SFrame, SArray or dictionary')
        if output_type == 'probability' and self.num_classes != 2:
            raise _ToolkitError('Output type \'probability\' is only supported for binary'
                                ' classification. For multi-class classification, use'
                                ' predict_topk() instead.')
        if(batch_size < 1):
            raise ValueError("'batch_size' must be greater than or equal to 1")

        if _is_deep_feature_sarray(dataset):
            deep_features = dataset
        else:
            deep_features = get_deep_features(dataset, verbose=verbose)

        deep_features = _tc.SFrame({'deep features': deep_features})
        deep_features = deep_features.add_row_number()
        deep_features = deep_features.stack('deep features', new_column_name='deep features')
        deep_features, missing_ids = deep_features.dropna_split(columns=['deep features'])

        if len(missing_ids) > 0:
            _logging.warning("Unable to make predictions for %d examples because they are less than 975ms in length."
                             % len(missing_ids))

        if batch_size > len(deep_features):
            batch_size = len(deep_features)

        y = []
        for batch in mx.io.NDArrayIter(deep_features['deep features'].to_numpy(), batch_size=batch_size):
            ctx = _mxnet_utils.get_mxnet_context()
            if(len(batch.data[0]) < len(ctx)):
                ctx = ctx[:len(batch.data[0])]

            batch_data = batch.data[0]
            if batch.pad != 0:
                batch_data = batch_data[:-batch.pad]    # prevent batches looping back

            batch_data = mx.gluon.utils.split_and_load(batch_data, ctx_list=ctx, batch_axis=0, even_split=False)

            for x in batch_data:
                forward_output = self._custom_classifier.forward(x)
                y += mx.nd.softmax(forward_output).asnumpy().tolist()
        assert(len(y) == len(deep_features))

        # Combine predictions from multiple frames
        sf = _tc.SFrame({'predictions': y, 'id': deep_features['id']})
        probabilities_sum = sf.groupby('id', {'prob_sum': _tc.aggregate.SUM('predictions')})

        if output_type == 'class':
            predicted_ids = probabilities_sum['prob_sum'].apply(lambda x: _np.argmax(x))
            mappings = self._id_to_class_label
            probabilities_sum['results'] = predicted_ids.apply(lambda x: mappings[x])
        else:
            assert output_type in ('probability', 'probability_vector')
            frame_per_example_count = sf.groupby('id', _tc.aggregate.COUNT())
            probabilities_sum = probabilities_sum.join(frame_per_example_count)
            probabilities_sum['results'] = probabilities_sum.apply(lambda row: [i / row['Count'] for i in row['prob_sum']])

        if len(missing_ids) > 0:
            output_type = probabilities_sum['results'].dtype
            missing_predictions = _tc.SFrame({'id': missing_ids['id'],
                                              'results': _tc.SArray([ None ] * len(missing_ids), dtype=output_type)
                                              })
            probabilities_sum = probabilities_sum[['id', 'results']].append(missing_predictions)

        probabilities_sum = probabilities_sum.sort('id')
        return probabilities_sum['results']

    def predict_topk(self, dataset, output_type='probability', k=3, verbose=True, batch_size=64):
        """
        Return top-k predictions for the ``dataset``.
        Predictions are returned as an SFrame with three columns: `id`,
        `class`, and `probability` or `rank` depending on the ``output_type``
        parameter.

        Parameters
        ----------
        dataset : SFrame | SArray | dict
            The audio data to be classified.
            If dataset is an SFrame, it must have a column with the same name as
            the feature used for model training, but does not require a target
            column. Additional columns are ignored.

        output_type : {'probability', 'rank'}, optional
            Choose the return type of the prediction:
            - `probability`: Probability associated with each label in the prediction.
            - `rank`       : Rank associated with each label in the prediction.

        k : int, optional
            Number of classes to return for each input example.

        verbose : bool, optional
            If True, prints progress updates and model details.

        batch_size : int, optional
            If you are getting memory errors, try decreasing this value. If you
            have a powerful computer, increasing this value may improve performance.

        Returns
        -------
        out : SFrame
            An SFrame with model predictions.

        See Also
        --------
        predict, classify, evaluate

        Examples
        --------
        >>> pred = m.predict_topk(validation_data, k=3)
        >>> pred
        +------+-------+-------------------+
        |  id  | class |    probability    |
        +------+-------+-------------------+
        |  0   |   4   |   0.995623886585  |
        |  0   |   9   |  0.0038311756216  |
        |  0   |   7   | 0.000301006948575 |
        |  1   |   1   |   0.928708016872  |
        |  1   |   3   |  0.0440889261663  |
        |  1   |   2   |  0.0176190119237  |
        |  2   |   3   |   0.996967732906  |
        |  2   |   2   |  0.00151345680933 |
        |  2   |   7   | 0.000637513934635 |
        |  3   |   1   |   0.998070061207  |
        | ...  |  ...  |        ...        |
        +------+-------+-------------------+
        """
        prob_vector = self.predict(dataset, output_type='probability_vector',
                                   verbose=verbose, batch_size=64)
        id_to_label = self._id_to_class_label

        if output_type == 'probability':
            results = prob_vector.apply(lambda p: [
                {'class': id_to_label[i], 'probability': p[i]}
                for i in reversed(_np.argsort(p)[-k:])]
            )
        else:
            assert(output_type == 'rank')
            results = prob_vector.apply(lambda p: [
                {'class': id_to_label[i], 'rank': rank}
                for rank, i in enumerate(reversed(_np.argsort(p)[-k:]))]
            )

        results = _tc.SFrame({'X': results})
        results = results.add_row_number()
        results = results.stack('X', new_column_name='X')
        results = results.unpack('X', column_name_prefix='')
        return results
