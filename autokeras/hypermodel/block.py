import kerastuner
import tensorflow as tf
from kerastuner.applications import resnet
from kerastuner.applications import xception
from tensorflow.python.util import nest

from autokeras import const
from autokeras import utils
from autokeras.hypermodel import node


class HyperBlock(kerastuner.HyperModel):
    """The base class for different HyperBlock.

    The HyperBlock can be connected together to build the search space
    for an AutoModel. Notably, many args in the __init__ function are defaults to
    be a tunable variable when not specified by the user.

    Attributes:
        inputs: A list of input node(s) for the HyperBlock.
        outputs: A list of output node(s) for the HyperBlock.
    """

    def __init__(self, **kwargs):
        super(HyperBlock, self).__init__(**kwargs)
        self.inputs = None
        self.outputs = None
        self._num_output_node = 1

    def __call__(self, inputs):
        """Functional API.

        Args:
            inputs: A list of input node(s) or a single input node for the block.

        Returns:
            list: A list of output node(s) of the HyperBlock.
        """
        self.inputs = nest.flatten(inputs)
        for input_node in self.inputs:
            input_node.add_out_hypermodel(self)
        self.outputs = []
        for _ in range(self._num_output_node):
            output_node = node.Node()
            output_node.add_in_hypermodel(self)
            self.outputs.append(output_node)
        return self.outputs

    def build(self, hp, inputs=None):
        """Build the HyperBlock into a real Keras Model.

        The subclasses should overide this function and return the output node.

        Args:
            hp: Hyperparameters. The hyperparameters for building the model.
            inputs: A list of input node(s).
        """
        return super(HyperBlock, self).build(hp)


class DenseBlock(HyperBlock):
    """HyperBlock for Dense layers.

    Attributes:
        num_layers: Int. The number of Dense layers in the block.
            Defaults to tunable.
        use_bn: Boolean. Whether to use BatchNormalization layers.
            Defaults to tunable.
        dropout_rate: Float. The dropout rate for the layers.
            Defaults to tunable.
    """

    def __init__(self,
                 num_layers=None,
                 use_bn=None,
                 dropout_rate=None,
                 **kwargs):
        super().__init__(**kwargs)
        self.num_layers = num_layers
        self.use_bn = use_bn
        self.dropout_rate = dropout_rate

    def build(self, hp, inputs=None):
        inputs = nest.flatten(inputs)
        utils.validate_num_inputs(inputs, 1)
        input_node = inputs[0]
        output_node = input_node
        output_node = Flatten().build(hp, output_node)

        num_layers = self.num_layers or hp.Choice('num_layers', [1, 2, 3], default=2)
        use_bn = self.use_bn or hp.Choice('use_bn', [True, False], default=False)
        dropout_rate = self.dropout_rate or hp.Choice('dropout_rate',
                                                      [0, 0.25, 0.5],
                                                      default=0)

        for i in range(num_layers):
            units = hp.Choice(
                'units_{i}'.format(i=i),
                [16, 32, 64, 128, 256, 512, 1024],
                default=32)
            output_node = tf.keras.layers.Dense(units)(output_node)
            if use_bn:
                output_node = tf.keras.layers.BatchNormalization()(output_node)
            output_node = tf.keras.layers.ReLU()(output_node)
            output_node = tf.keras.layers.Dropout(dropout_rate)(output_node)
        return output_node


class RNNBlock(HyperBlock):
    """ An RNN HyperBlock.

    Attributes:
        return_sequences: Boolean. Whether to return the last output in the
            output sequence, or the full sequence. Defaults to False.
        bidirectional: Boolean. Bidirectional RNN. Defaults to tunable.
    """

    def __init__(self,
                 return_sequences=False,
                 bidirectional=None,
                 attention=None,
                 num_layers=None,
                 layer_type=None,
                 **kwargs):
        super().__init__(**kwargs)
        self.return_sequences = return_sequences
        self.bidirectional = bidirectional
        self.attention = attention
        self.num_layers = num_layers
        self.layer_type = layer_type

    @staticmethod
    def attention_block(inputs):
        time_steps = int(inputs.shape[1])
        attention_out = tf.keras.layers.Permute((2, 1))(inputs)
        attention_out = tf.keras.layers.Dense(time_steps,
                                              activation='softmax')(attention_out)
        attention_out = tf.keras.layers.Permute((2, 1))(attention_out)
        mul_attention_out = tf.keras.layers.Multiply()([inputs, attention_out])
        return mul_attention_out

    def build(self, hp, inputs=None):
        inputs = nest.flatten(inputs)
        utils.validate_num_inputs(inputs, 1)
        input_node = inputs[0]
        shape = input_node.shape.as_list()
        if len(shape) != 3:
            raise ValueError(
                'Expect the input tensor to have '
                'at least 3 dimensions for rnn models, '
                'but got {shape}'.format(shape=input_node.shape))

        feature_size = shape[-1]
        output_node = input_node

        attention_choices = ['pre', 'post', 'none'] if self.return_sequences \
            else ['pre', 'none']
        bidirectional = self.bidirectional or hp.Choice('bidirectional',
                                                        [True, False],
                                                        default=True)
        attention = self.attention or hp.Choice('attention',
                                                attention_choices,
                                                default='none')
        layer_type = self.layer_type or hp.Choice('layer_type',
                                                  ['gru', 'lstm'],
                                                  default='lstm')
        num_layers = self.num_layers or hp.Choice('num_layers',
                                                  [1, 2, 3],
                                                  default=2)

        in_layer = const.Constant.RNN_LAYERS[layer_type]
        output_node = self.attention_block(output_node) \
            if attention == 'pre' else output_node
        for i in range(num_layers):
            return_sequences = True
            if i == num_layers - 1:
                return_sequences = self.return_sequences
            if bidirectional:
                output_node = tf.keras.layers.Bidirectional(
                    in_layer(feature_size,
                             return_sequences=return_sequences))(output_node)
            else:
                output_node = in_layer(
                    feature_size,
                    return_sequences=return_sequences)(output_node)
        output_node = self.attention_block(output_node) \
            if attention == 'post' else output_node
        return output_node


class ImageBlock(HyperBlock):
    """HyperBlock for image data.

    Attributes:
        block_type: Str. 'resnet', 'xception', 'vanilla'. The type of HyperBlock to
            use. Defaults to tunable.
    """

    def __init__(self, block_type=None, **kwargs):
        super().__init__(**kwargs)
        self.block_type = block_type

    def build(self, hp, inputs=None):
        inputs = nest.flatten(inputs)
        utils.validate_num_inputs(inputs, 1)
        input_node = inputs[0]
        output_node = input_node

        block_type = self.block_type or hp.Choice('block_type',
                                                  ['resnet', 'xception', 'vanilla'],
                                                  default='resnet')

        if block_type == 'resnet':
            output_node = ResNetBlock().build(hp, output_node)
        elif block_type == 'xception':
            output_node = XceptionBlock().build(hp, output_node)
        elif block_type == 'vanilla':
            output_node = ConvBlock().build(hp, output_node)
        return output_node


class ConvBlock(HyperBlock):
    """HyperBlock for vanilla ConvNets.

    Attributes:
        kernel_size: Int. Defaults to Tunable.
        dropout_rate: Float. Defaults to Tunable.
        num_blocks: Int. The number of conv blocks. Defaults to Tunable.
        separable: Boolean. Whether to use separable conv layers.
            Defaults to Tunable.
    """

    def __init__(self,
                 kernel_size=None,
                 dropout_rate=None,
                 num_blocks=None,
                 separable=None,
                 **kwargs):
        super().__init__(**kwargs)
        self.kernel_size = kernel_size
        self.dropout_rate = dropout_rate
        self.num_blocks = num_blocks
        self.separable = separable

    def build(self, hp, inputs=None):
        inputs = nest.flatten(inputs)
        utils.validate_num_inputs(inputs, 1)
        input_node = inputs[0]
        output_node = input_node

        kernel_size = self.kernel_size or hp.Choice('kernel_size',
                                                    [3, 5, 7],
                                                    default=3)
        dropout_rate = self.dropout_rate or hp.Choice('dropout_rate',
                                                      [0, 0.25, 0.5],
                                                      default=0.5)
        num_blocks = self.num_blocks or hp.Choice('num_blocks',
                                                  [1, 2, 3],
                                                  default=2)
        separable = self.separable or hp.Choice('separable',
                                                [True, False],
                                                default=False)

        if separable:
            conv = utils.get_sep_conv(input_node.shape)
        else:
            conv = utils.get_conv(input_node.shape)
        pool = utils.get_max_pooling(input_node.shape)
        dropout = utils.get_dropout(input_node.shape)

        for i in range(num_blocks):
            if dropout_rate > 0:
                output_node = dropout(dropout_rate)(output_node)
            output_node = conv(
                hp.Choice('filters_{i}_1'.format(i=i),
                          [16, 32, 64],
                          default=32),
                kernel_size,
                padding=self._get_padding(kernel_size, output_node))(output_node)
            output_node = conv(
                hp.Choice('filters_{i}_2'.format(i=i),
                          [16, 32, 64],
                          default=32),
                kernel_size,
                padding=self._get_padding(kernel_size, output_node))(output_node)
            output_node = pool(
                kernel_size - 1,
                padding=self._get_padding(kernel_size - 1, output_node))(output_node)
        return output_node

    @staticmethod
    def _get_padding(kernel_size, output_node):
        if (kernel_size * 2 <= output_node.shape[1] and
                kernel_size * 2 <= output_node.shape[2]):
            return 'valid'
        return 'same'


class ResNetBlock(HyperBlock, resnet.HyperResNet):
    """HyperBlock for ResNet.

    Attributes:
        version: Str. 'v1', 'v2' or 'next'. The type of ResNet to use.
            Defaults to tunable.
        pooling: Str. 'avg', 'max'. The type of pooling layer to use.
            Defaults to tunable.
    """

    def __init__(self,
                 version=None,
                 pooling=None,
                 **kwargs):
        super().__init__(include_top=False, input_shape=(10,), **kwargs)
        self.version = version
        self.pooling = pooling

    def build(self, hp, inputs=None):
        self.input_tensor = nest.flatten(inputs)[0]
        self.input_shape = None

        hp.Choice('version', ['v1', 'v2', 'next'], default='v2')
        hp.Choice('pooling', ['avg', 'max'], default='avg')

        hp.values['version'] = self.version or hp.values['version']
        hp.values['pooling'] = self.pooling or hp.values['pooling']

        model = super(ResNetBlock, self).build(hp)
        return model.outputs


class XceptionBlock(HyperBlock, xception.HyperXception):
    """XceptionBlock.

    An Xception structure, used for specifying your model with specific datasets.

    The original Xception architecture is from https://arxiv.org/abs/1610.02357.
    The data first goes through the entry flow, then through the middle flow which
    is repeated eight times, and finally through the exit flow.

    This XceptionBlock returns a similar architecture as Xception except without
    the last (optional) fully connected layer(s) and logistic regression.
    The size of this architecture could be decided by `HyperParameters`, to get an
    architecture with a half, an identical, or a double size of the original one.

    Attributes:
        activation: Str. 'selu' or 'relu'. Defaults to tunable.
        conv2d_num_filters: Int. Defaults to tunable.
        kernel_size: Int. Defaults to tunable.
        initial_strides: Int. Defaults to tunable.
        num_residual_blocks: Int. Defaults to tunable.
        pooling: Str. 'ave', 'flatten', or 'max'. Defaults to tunable.
    """

    def __init__(self,
                 activation=None,
                 conv2d_num_filters=None,
                 kernel_size=None,
                 initial_strides=None,
                 num_residual_blocks=None,
                 pooling=None,
                 **kwargs):
        super().__init__(include_top=False, input_shape=(10,), **kwargs)
        self.activation = activation
        self.conv2d_num_filters = conv2d_num_filters
        self.kernel_size = kernel_size
        self.initial_strides = initial_strides
        self.num_residual_blocks = num_residual_blocks
        self.pooling = pooling

    def build(self, hp, inputs=None):
        self.input_tensor = nest.flatten(inputs)[0]
        self.input_shape = None

        hp.Choice('activation', ['relu', 'selu'])
        hp.Choice('conv2d_num_filters', [32, 64, 128], default=64)
        hp.Choice('kernel_size', [3, 5])
        hp.Choice('initial_strides', [2])
        hp.Range('num_residual_blocks', 2, 8, default=4)
        hp.Choice('pooling', ['avg', 'flatten', 'max'])

        hp.values['activation'] = self.activation or hp.values['activation']
        hp.values['conv2d_num_filters'] = \
            self.conv2d_num_filters or hp.values['conv2d_num_filters']
        hp.values['kernel_size'] = self.kernel_size or hp.values['kernel_size']
        hp.values['initial_strides'] = \
            self.initial_strides or hp.values['initial_strides']
        hp.values['num_residual_blocks'] = \
            self.num_residual_blocks or hp.values['num_residual_blocks']
        hp.values['pooling'] = self.pooling or hp.values['pooling']

        model = super(XceptionBlock, self).build(hp)
        return model.outputs


def shape_compatible(shape1, shape2):
    if len(shape1) != len(shape2):
        return False
    # TODO: If they can be the same after passing through any layer,
    #  they are compatible. e.g. (32, 32, 3), (16, 16, 2) are compatible
    return shape1[:-1] == shape2[:-1]


class Merge(HyperBlock):

    def __init__(self, merge_type=None, **kwargs):
        super().__init__(**kwargs)
        self.merge_type = merge_type

    def build(self, hp, inputs=None):
        inputs = nest.flatten(inputs)
        if len(inputs) == 1:
            return inputs

        merge_type = self.merge_type or hp.Choice("merge_type",
                                                  ['add', 'concatenate'],
                                                  default='add')

        if not all([shape_compatible(input_node.shape, inputs[0].shape) for
                    input_node in inputs]):
            new_inputs = []
            for input_node in inputs:
                new_inputs.append(Flatten().build(hp, input_node))
            inputs = new_inputs

        # TODO: Even inputs have different shape[-1], they can still be Add(
        #  ) after another layer. Check if the inputs are all of the same
        #  shape
        if all([input_node.shape == inputs[0].shape for input_node in inputs]):
            if merge_type == 'add':
                return tf.keras.layers.Add(inputs)

        return tf.keras.layers.Concatenate()(inputs)


class Flatten(HyperBlock):

    def build(self, hp, inputs=None):
        inputs = nest.flatten(inputs)
        utils.validate_num_inputs(inputs, 1)
        input_node = inputs[0]
        if len(input_node.shape) > 2:
            return tf.keras.layers.Flatten()(input_node)
        return input_node


class SpatialReduction(HyperBlock):
    """Reduce the dimension of a spatial tensor, e.g. image, to a vector.

    Attributes:
        reduction_type: Str. 'flatten', 'global_max' or 'global_ave'.
            Defaults to tunable.
    """

    def __init__(self, reduction_type, **kwargs):
        super().__init__(**kwargs)
        self.reduction_type = reduction_type

    def build(self, hp, inputs=None):
        inputs = nest.flatten(inputs)
        utils.validate_num_inputs(inputs, 1)
        input_node = inputs[0]
        output_node = input_node

        # No need to reduce.
        if len(output_node.shape) <= 2:
            return output_node

        reduction_type = self.reduction_type or hp.Choice('reduction_type',
                                                          ['flatten',
                                                           'global_max',
                                                           'global_ave'],
                                                          default='global_ave')
        if reduction_type == 'flatten':
            output_node = Flatten().build(hp, output_node)
        elif reduction_type == 'global_max':
            output_node = utils.get_global_max_pooling(
                output_node.shape)()(output_node)
        elif reduction_type == 'global_ave':
            output_node = utils.get_global_average_pooling(
                output_node.shape)()(output_node)
        return output_node


class TemporalReduction(HyperBlock):
    """Reduce the dimension of a temporal tensor, e.g. output of RNN, to a vector.

    Attributes:
        reduction_type: Str. 'flatten', 'global_max', 'global_ave', 'global_min'.
            Defaults to tunable.
    """

    def __init__(self, reduction_type, **kwargs):
        super().__init__(**kwargs)
        self.reduction_type = reduction_type

    def build(self, hp, inputs=None):
        inputs = nest.flatten(inputs)
        utils.validate_num_inputs(inputs, 1)
        input_node = inputs[0]
        output_node = input_node

        # No need to reduce.
        if len(output_node.shape) <= 2:
            return output_node

        reduction_type = self.reduction_type or hp.Choice('reduction_type',
                                                          ['flatten',
                                                           'global_max',
                                                           'global_ave',
                                                           'global_min'],
                                                          default='global_ave')

        if reduction_type == 'flatten':
            output_node = Flatten().build(hp, output_node)
        elif reduction_type == 'global_max':
            output_node = tf.math.reduce_max(output_node, axis=-2)
        elif reduction_type == 'global_ave':
            output_node = tf.math.reduce_mean(output_node, axis=-2)
        elif reduction_type == 'global_min':
            output_node = tf.math.reduce_min(output_node, axis=-2)

        return output_node


class EmbeddingBlock(HyperBlock):
    """Word embedding block for sequences.

    The input should be tokenized sequences with the same length, where each element
    of a sequence should be the index of the word.

    Attributes:
        pretrained: Boolean. Use pretrained word embedding. Defaults to tunable.
        is_embedding_trainable: Boolean. Defaults to tunable.
        embedding_dim: Int. Defaults to None.
    """

    def __init__(self,
                 pretrained=None,
                 is_embedding_trainable=None,
                 embedding_dim=None,
                 **kwargs):
        super().__init__(**kwargs)
        self.pretrained = pretrained
        self.is_embedding_trainable = is_embedding_trainable
        self.embedding_dim = embedding_dim

    def build(self, hp, inputs=None):
        input_node = nest.flatten(inputs)[0]
        pretrained = self.pretrained or hp.Choice('pretrained',
                                                  [True, False],
                                                  default=False)
        is_embedding_trainable = self.is_embedding_trainable or hp.Choice(
            'is_embedding_trainable',
            [True, False],
            default=False)
        embedding_dim = self.embedding_dim or hp.Choice('embedding_dim',
                                                        [32, 64, 128, 256, 512],
                                                        default=128)
        if pretrained:
            # TODO: load from pretrained weights
            layer = tf.keras.layers.Embedding(
                input_dim=input_node.shape[1],
                output_dim=embedding_dim,
                input_length=const.Constant.VOCABULARY_SIZE,
                trainable=is_embedding_trainable)
            # weights=[embedding_matrix])
        else:
            layer = tf.keras.layers.Embedding(
                input_dim=input_node.shape[1],
                output_dim=embedding_dim,
                input_length=const.Constant.VOCABULARY_SIZE,
                trainable=is_embedding_trainable)
        return layer(input_node)


class TextBlock(RNNBlock):
    pass


class StructuredBlock(HyperBlock):

    def build(self, hp, inputs=None):
        pass


class TimeSeriesBlock(HyperBlock):

    def build(self, hp, inputs=None):
        pass


class GeneralBlock(HyperBlock):

    def build(self, hp, inputs=None):
        pass