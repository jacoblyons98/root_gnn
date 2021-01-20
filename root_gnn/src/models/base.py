"""
The implementation of Graph Networks are mostly inspired by the ones in deepmind/graphs_nets
https://github.com/deepmind/graph_nets
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf
from graph_nets import modules
from graph_nets import utils_tf
from graph_nets import blocks

import sonnet as snt

from root_gnn.src.types import ActivationFn

NUM_LAYERS = 2    # Hard-code number of layers in the edge/node/global models.
LATENT_SIZE = 64  # Hard-code latent layer sizes for demos.
DROPOUT_RATE = 0.2  # 0 means no dropout

def make_mlp_model():
  """Instantiates a new MLP, followed by LayerNorm.

  The parameters of each new MLP are not shared with others generated by
  this function.

  Returns:
    A Sonnet module which contains the MLP and LayerNorm.
  """
  # the activation function choices:
  # swish, relu, relu6, leaky_relu
  return snt.Sequential([
      snt.nets.MLP([128, 64]*NUM_LAYERS,
                    activation=tf.nn.relu,
                    activate_final=True, 
                  #  dropout_rate=DROPOUT_RATE
        ),
      snt.LayerNorm(axis=-1, create_scale=True, create_offset=False)
  ])

def mlp_model_with_dropout(
    latent_size: int = 128,
    num_layers:int = 2, 
    dropout_rate: float = 0.30,
    activations: ActivationFn = tf.nn.relu,
    activate_final: bool =True,
    name: str = 'MLP', *args, **kwargs):
  create_scale = True if not "create_scale" in kwargs else kwargs['create_scale']
  create_offset = True if not "create_offset" in kwargs else kwargs['create_offset']
  return snt.Sequential([
      snt.nets.MLP([latent_size]*num_layers,
                    activation=activations,
                    activate_final=activate_final, 
                    dropout_rate=dropout_rate
        ),
      snt.LayerNorm(axis=-1, create_scale=create_scale, create_offset=create_offset)
  ], name=name)

class MLPGraphIndependent(snt.Module):
  """GraphIndependent with MLP edge, node, and global models."""

  def __init__(self, name="MLPGraphIndependent"):
    super(MLPGraphIndependent, self).__init__(name=name)
    self._network = modules.GraphIndependent(
        edge_model_fn=make_mlp_model,
        node_model_fn=make_mlp_model,
        global_model_fn=make_mlp_model)

  def __call__(self, inputs,
            edge_model_kwargs=None,
            node_model_kwargs=None,
            global_model_kwargs=None):
    return self._network(
      inputs,
      edge_model_kwargs=edge_model_kwargs,
      node_model_kwargs=node_model_kwargs,
      global_model_kwargs=global_model_kwargs
      )


class MLPGraphNetwork(snt.Module):
    """GraphIndependent with MLP edge, node, and global models."""
    def __init__(self, name="MLPGraphNetwork"):
        super(MLPGraphNetwork, self).__init__(name=name)
        self._network = modules.GraphNetwork(
            edge_model_fn=make_mlp_model,
            node_model_fn=make_mlp_model,
            global_model_fn=make_mlp_model
            )

    def __call__(self, inputs,
            edge_model_kwargs=None,
            node_model_kwargs=None,
            global_model_kwargs=None):
        return self._network(inputs,
                      edge_model_kwargs=edge_model_kwargs,
                      node_model_kwargs=node_model_kwargs,
                      global_model_kwargs=global_model_kwargs)



class InteractionNetwork(snt.Module):
  """Implementation of an Interaction Network, similarly to
  https://arxiv.org/abs/1612.00222, except that it does not require input 
  edge features.
  """

  def __init__(self,
               edge_model_fn,
               node_model_fn,
               reducer=tf.math.unsorted_segment_sum,
               name="interaction_network"):
    super(InteractionNetwork, self).__init__(name=name)
    self._edge_block = blocks.EdgeBlock(
        edge_model_fn=edge_model_fn, use_globals=False)
    self._node_block = blocks.NodeBlock(
        node_model_fn=node_model_fn,
        use_received_edges=True,
        use_sent_edges=True,
        use_globals=False,
        received_edges_reducer=reducer)

  def __call__(self,
    graph,
    edge_model_kwargs=None,
    node_model_kwargs=None
  ):
    return self._edge_block(self._node_block(graph, node_model_kwargs), edge_model_kwargs)