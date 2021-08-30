"""End-to-end tests for Keras Models."""

import os

from absl.testing import parameterized
import tensorflow as tf
import tensorflow_gnn as tfgnn  # Test user-visibe names.


as_tensor = tf.convert_to_tensor


class ExportedKerasNamesTest(tf.test.TestCase):
  """Tests symbols exist in tfgnn.keras.*."""

  def assertIsSubclass(self, first, second, msg=None):
    if msg is None:
      msg = f'{repr(first)} is not a subclass of {repr(second)}'
    self.assertTrue(issubclass(first, second), msg=msg)

  def assertCallable(self, expr, msg=None):
    if msg is None:
      msg = f'{repr(expr)} is not callable'
    self.assertTrue(callable(expr), msg=msg)

  def testLayers(self):
    Layer = tf.keras.layers.Layer  # pylint: disable=invalid-name
    self.assertIsSubclass(tfgnn.keras.layers.Broadcast, Layer)
    self.assertIsSubclass(tfgnn.keras.layers.Pool, Layer)
    self.assertIsSubclass(tfgnn.keras.layers.Readout, Layer)
    self.assertIsSubclass(tfgnn.keras.layers.EdgeSetUpdate, Layer)
    self.assertIsSubclass(tfgnn.keras.layers.NodeSetUpdate, Layer)
    self.assertIsSubclass(tfgnn.keras.layers.ContextUpdate, Layer)
    self.assertIsSubclass(tfgnn.keras.layers.GraphUpdateOptions, object)
    self.assertIsSubclass(tfgnn.keras.layers.GraphUpdateEdgeSetOptions, object)
    self.assertIsSubclass(tfgnn.keras.layers.GraphUpdateNodeSetOptions, object)
    self.assertIsSubclass(tfgnn.keras.layers.GraphUpdateContextOptions, object)
    # Test the `del` statements at the bottom of layers/__init__.py.
    self.assertFalse(hasattr(tfgnn.keras.layers, 'graph_ops'))
    self.assertFalse(hasattr(tfgnn.keras.layers, 'graph_update'))
    self.assertFalse(hasattr(tfgnn.keras.layers, 'graph_update_options'))

  def testUtils(self):
    self.assertCallable(tfgnn.keras.utils.get_fnn_factory)
    # Test the `del` statements at the bottom of utils/__init__.py.
    self.assertFalse(hasattr(tfgnn.keras.utils, 'fnn_factory'))


# An example of a custom Keras layer used by tests below.
class AddWeightedSwappedInEdges(tf.keras.layers.Layer):
  """Adds weighted sum of coordinate-swapped neighbor states to each node."""

  def __init__(self, supports_get_config=True, **kwargs):
    kwargs.setdefault('name', 'add_weighted_swapped_in_edges')
    super().__init__(**kwargs)
    self.supports_get_config = supports_get_config
    self.fnn = tf.keras.layers.Dense(
        units=2,
        name='swap_node_state_coordinates',
        use_bias=False,
        kernel_initializer=tf.keras.initializers.Constant([[0., 1.], [1., 0.]]))

  def get_config(self):
    if self.supports_get_config:
      return super().get_config()
    else:
      raise NotImplementedError('unsupported')

  def call(self, graph):
    weight = graph.edge_sets['edge']['edge_weight']
    node_state = graph.node_sets['node']['hidden_state']
    source_value = tf.gather(
        graph.node_sets['node']['hidden_state'],
        graph.edge_sets['edge'].adjacency[tfgnn.SOURCE])
    message = tf.multiply(weight, source_value)
    pooled_message = tf.math.unsorted_segment_sum(
        message, graph.edge_sets['edge'].adjacency[tfgnn.TARGET],
        graph.node_sets['node'].total_size)
    node_updates = self.fnn(pooled_message)
    node_state += node_updates
    return graph.replace_features(
        node_sets={'node': {'hidden_state': node_state}})


# A similar example of model building with tfgnn.keras.layers.*.
def add_weighted_swapped_in_edges(graph):
  graph = tfgnn.keras.layers.EdgeSetUpdate(
      'edge',
      input_fns=[tfgnn.keras.layers.Readout(feature_name='edge_weight'),
                 tfgnn.keras.layers.Broadcast(tfgnn.SOURCE)],
      combiner_fn='none',
      update_fn=tf.keras.layers.Lambda(lambda x: tf.multiply(x[0], x[1]))
  )(graph)
  graph = tfgnn.keras.layers.NodeSetUpdate(
      'node',
      input_fns=[
          tfgnn.keras.layers.Readout(),
          tfgnn.keras.layers.Pool(tfgnn.TARGET, 'sum', edge_set_name='edge')],
      update_fn=tf.keras.layers.Dense(
          units=2, name='add_swapped_message', use_bias=False,
          kernel_initializer=tf.keras.initializers.Constant([[1., 0., 0., 1.],
                                                             [0., 1., 1., 0.]]))
  )(graph)
  return graph


class GraphTensorKerasModelTest(tf.test.TestCase, parameterized.TestCase):

  def _create_graph_tensor(self, static_shapes, factor):
    """Returns a graph with one component, as depicted below.

            /--  0.5 -->>
     [10, 0]             [12, 0]
            <<-- -0.5 --/

    Args:
      static_shapes: If true, shape dimensions reflect the concrete values.
        If false, shape dimensions are set to None.
      factor: size multiplier.
    """
    factor = tf.cast(factor, tf.int32)

    def tile(tensor, factor):
      assert tensor.shape.rank in (1, 2)
      return tf.tile(tensor,
                     [factor] if tensor.shape.rank == 1 else [factor, 1])

    return tfgnn.GraphTensor.from_pieces(
        edge_sets={
            'edge':
                tfgnn.EdgeSet.from_fields(
                    features={
                        'edge_weight':
                            tile(
                                as_tensor([[0.5], [-0.5]], tf.float32),
                                factor)
                    },
                    sizes=as_tensor([2]) * factor,
                    adjacency=tfgnn.HyperAdjacency.from_indices(
                        indices={
                            tfgnn.SOURCE: (
                                'node', tile(as_tensor([0, 1]), factor)),
                            tfgnn.TARGET: (
                                'node', tile(as_tensor([1, 0]), factor)),
                        }))
        },
        node_sets={
            'node': tfgnn.NodeSet.from_fields(
                features={'hidden_state': tile(
                    as_tensor([[10, 0.], [12., 0.]], tf.float32),
                    factor)},
                sizes=as_tensor([2]) * factor)
        })

  def _get_input_spec(self, static_shapes):
    """Returns a GraphTensorSpec for a homogeneous scalar graph.

    The number of components is indeterminate ((suitable for model computations
    after merging a batch of inputs into components of a singe graph).
    Each node has a state of shape [2] and each edge has a weight of shape [1].

    Args:
      static_shapes: If true, shape dimensions reflect the concrete values.
        If false, shape dimensions are set to None.
    """
    if static_shapes:
      spec = self._create_graph_tensor(static_shapes, 1).spec
      # Check that dataset spec has static component dimensions.
      self.assertAllEqual(spec.edge_sets_spec['edge']['edge_weight'],
                          tf.TensorSpec(tf.TensorShape([2, 1]), tf.float32))
      return spec

    ds = tf.data.Dataset.range(1, 3).map(
        lambda factor: self._create_graph_tensor(static_shapes, factor))
    spec = ds.element_spec
    # Check that dataset spec has relaxed component dimensions.
    self.assertAllEqual(spec.edge_sets_spec['edge']['edge_weight'],
                        tf.TensorSpec(tf.TensorShape([None, 1]), tf.float32))
    return spec

  @parameterized.parameters([True, False])
  def testStdLayerModel(self, static_shapes):

    # A Keras Model build from tfgnn.keras.layers.*.
    inputs = tf.keras.layers.Input(
        type_spec=self._get_input_spec(static_shapes))
    graph = add_weighted_swapped_in_edges(inputs)
    outputs = tfgnn.keras.layers.Readout(node_set_name='node')(graph)
    model = tf.keras.Model(inputs, outputs)

    # Save and restore the model.
    export_dir = os.path.join(self.get_temp_dir(), 'stdlayer-tf')
    tf.saved_model.save(model, export_dir)
    restored_model = tf.saved_model.load(export_dir)

    expected_1 = as_tensor([[10., -6.], [12., 5.]], tf.float32)
    graph_1 = self._create_graph_tensor(static_shapes, factor=1)
    self.assertAllClose(model(graph_1), expected_1)
    self.assertAllClose(restored_model(graph_1), expected_1)

  @parameterized.parameters([True, False])
  def testCustomGraphToGraphModel(self, static_shapes):

    # A Keras Model that inputs and outputs a GraphTensor.
    inputs = tf.keras.layers.Input(
        type_spec=self._get_input_spec(static_shapes))
    outputs = AddWeightedSwappedInEdges(supports_get_config=False)(inputs)
    model = tf.keras.Model(inputs, outputs)
    # Save and restore the model.
    export_dir = os.path.join(self.get_temp_dir(), 'graph2graph-tf')
    tf.saved_model.save(model, export_dir)
    restored_model = tf.saved_model.load(export_dir)

    def readout(graph):
      return graph.node_sets['node']['hidden_state']

    expected_1 = as_tensor([[10., -6.], [12., 5.]], tf.float32)
    graph_1 = self._create_graph_tensor(static_shapes, factor=1)
    self.assertAllClose(readout(model(graph_1)), expected_1)
    self.assertAllClose(readout(restored_model(graph_1)), expected_1)

  def testCustomModelWithReadoutOp(self, static_shapes=True):

    # A Keras Model that maps a GraphTensor to a Tensor,
    # using subscripting provided by GraphKerasTensor.
    inputs = net = tf.keras.layers.Input(
        type_spec=self._get_input_spec(static_shapes))
    net = AddWeightedSwappedInEdges(supports_get_config=False)(net)
    net = net.node_sets['node']['hidden_state']
    model = tf.keras.Model(inputs, net)
    # Save and restore the model.
    export_dir = os.path.join(self.get_temp_dir(), 'graph2tensor-op-tf')
    tf.saved_model.save(model, export_dir)
    restored_model = tf.saved_model.load(export_dir)

    expected_1 = as_tensor([[10., -6.], [12., 5.]], tf.float32)
    graph_1 = self._create_graph_tensor(static_shapes, factor=1)
    self.assertAllClose(model(graph_1), expected_1)
    self.assertAllClose(restored_model(graph_1), expected_1)

  @parameterized.parameters([True, False])
  def testCustomModelKerasRestore(self, static_shapes):

    # A Keras Model that maps a GraphTensor to a Tensor.
    inputs = net = tf.keras.layers.Input(
        type_spec=self._get_input_spec(static_shapes))
    net = AddWeightedSwappedInEdges(supports_get_config=True)(net)
    net = tfgnn.keras.layers.Readout(node_set_name='node',
                                     feature_name='hidden_state')(net)
    model = tf.keras.Model(inputs, net)
    # Save and restore the model as a Keras model.
    export_dir = os.path.join(self.get_temp_dir(), 'graph2tensor-keras')
    model.save(export_dir)
    restored_model = tf.keras.models.load_model(
        export_dir, custom_objects=dict(
            AddWeightedSwappedInEdges=AddWeightedSwappedInEdges))
    self.assertIsInstance(restored_model, tf.keras.Model)
    self.assertIsInstance(restored_model.get_layer(index=1),
                          AddWeightedSwappedInEdges)
    self.assertIsInstance(restored_model.get_layer(index=2),
                          tfgnn.keras.layers.Readout)

    expected_1 = as_tensor([[10., -6.], [12., 5.]], tf.float32)
    graph_1 = self._create_graph_tensor(static_shapes, factor=1)
    self.assertAllClose(model(graph_1), expected_1)
    self.assertAllClose(restored_model(graph_1), expected_1)


if __name__ == '__main__':
  tf.test.main()