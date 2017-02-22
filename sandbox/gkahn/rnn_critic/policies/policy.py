import os
from collections import defaultdict
import itertools

import numpy as np
import scipy.linalg
import tensorflow as tf
from sklearn.utils.extmath import cartesian

from rllab.core.serializable import Serializable
import rllab.misc.logger as logger

from sandbox.gkahn.tf.policies.base import Policy

class RNNCriticPolicy(Policy, Serializable):
    def __init__(self,
                 env_spec,
                 H,
                 gamma,
                 weight_decay,
                 learning_rate,
                 get_action_params,
                 gpu_device=None,
                 gpu_frac=None,
                 log_history_len=100,
                 **kwargs):
        """
        :param H: critic horizon length
        :param gamma: reward decay
        :param weight_decay
        :param learning_rate
        :param reset_every_train: reset parameters every time train is called?
        :param train_steps: how many calls to optimizer each time train is called
        :param batch_size
        :param get_action_params: dictionary specifying how to choose actions
        """
        Serializable.quick_init(self, locals())

        self._env_spec = env_spec
        self._H = H
        self._gamma = gamma
        self._weight_decay = weight_decay
        self._learning_rate = learning_rate
        self._get_action_params = get_action_params
        self._gpu_device = gpu_device
        self._gpu_frac = gpu_frac
        self._log_history_len = log_history_len
        self._exploration_strategy = None # don't set in init b/c will then be Serialized

        self._tf_graph, self._tf_sess, self._d_preprocess, \
            self._tf_obs_ph, self._tf_actions_ph, self._tf_rewards_ph, self._tf_rewards, self._tf_cost, self._tf_opt, \
            self._tf_obs_target_ph, self._tf_actions_target_ph, self._tf_target_rewards, self._update_target_fn = \
            self._graph_setup()

        self._get_action_preprocess = self._get_action_setup()

        ### saving/loading
        self._match_dict = dict()

        ### logging
        self._log_stats = defaultdict(list)

        Policy.__init__(self, env_spec, sess=self._tf_sess)

    ##################
    ### Properties ###
    ##################

    @property
    def H(self):
        return self._H

    @property
    def session(self):
        return self._tf_sess

    ###########################
    ### TF graph operations ###
    ###########################

    @staticmethod
    def create_session_and_graph(gpu_device=None, gpu_frac=None):
        if gpu_device is None:
            gpu_device = 0
        if gpu_frac is None:
            gpu_frac = 0.3

        tf_graph = tf.Graph()
        os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu_device)
        gpu_options = tf.GPUOptions(per_process_gpu_memory_fraction=gpu_frac)
        config = tf.ConfigProto(gpu_options=gpu_options,
                                log_device_placement=False,
                                allow_soft_placement=True)
        # config = tf.ConfigProto(
        #     device_count={'GPU': 0}
        # )
        tf_sess = tf.Session(graph=tf_graph, config=config)
        return tf_sess, tf_graph

    def _graph_inputs_outputs_from_placeholders(self):
        with tf.variable_scope('feed_input'):
            tf_obs_ph = tf.placeholder('float', [None, self._env_spec.observation_space.flat_dim], name='tf_obs_ph')
            tf_actions_ph = tf.placeholder('float', [None, self._env_spec.action_space.flat_dim * self._H], name='tf_actions_ph')
            tf_rewards_ph = tf.placeholder('float', [None, self._H], name='tf_rewards_ph')

        return tf_obs_ph, tf_actions_ph, tf_rewards_ph

    def _graph_preprocess_from_placeholders(self):
        d_preprocess = dict()

        with tf.variable_scope('preprocess'):
            for name, dim in (('observations', self._env_spec.observation_space.flat_dim),
                              ('actions', self._env_spec.action_space.flat_dim * self._H),
                              ('rewards', self._H)):
                d_preprocess[name+'_mean_ph'] = tf.placeholder(tf.float32, shape=(1, dim), name=name+'_mean_ph')
                d_preprocess[name+'_mean_var'] = tf.get_variable(name+'_mean_var', shape=[1, dim],
                                                                 trainable=False, dtype=tf.float32,
                                                                 initializer=tf.constant_initializer(np.zeros((1, dim))))
                d_preprocess[name+'_mean_assign'] = tf.assign(d_preprocess[name+'_mean_var'],
                                                              d_preprocess[name+'_mean_ph'])

                d_preprocess[name+'_orth_ph'] = tf.placeholder(tf.float32, shape=(dim, dim), name=name+'_orth_ph')
                d_preprocess[name+'_orth_var'] = tf.get_variable(name+'_orth_var',
                                                                 shape=(dim, dim),
                                                                 trainable=False, dtype=tf.float32,
                                                                 initializer=tf.constant_initializer(np.eye(dim)))
                d_preprocess[name+'_orth_assign'] = tf.assign(d_preprocess[name+'_orth_var'],
                                                              d_preprocess[name+'_orth_ph'])

        return d_preprocess

    def _graph_preprocess_inputs(self, tf_obs_ph, tf_actions_ph, d_preprocess):
        ### whiten inputs
        tf_obs_whitened = tf.matmul(tf_obs_ph - d_preprocess['observations_mean_var'],
                                    d_preprocess['observations_orth_var'])
        tf_actions_whitened = tf.matmul(tf_actions_ph - d_preprocess['actions_mean_var'],
                                        d_preprocess['actions_orth_var'])

        num_obs = tf.shape(tf_obs_whitened)[0]
        num_action = tf.shape(tf_actions_whitened)[0]

        def tf_repeat_2d(x, reps):
            """ Repeats x on axis=0 reps times """
            x_shape = tf.shape(x)
            x_repeat = tf.reshape(tf.tile(x, [1, reps]), (x_shape[0] * reps, x_shape[1]))
            x_repeat.set_shape((None, x.get_shape()[1]))
            return x_repeat

        ### replicate observation for each action
        def replicate_observation():
            tf_obs_whitened_rep = tf_repeat_2d(tf_obs_whitened, num_action // num_obs)
            # tf_obs_whitened_tiled = tf.tile(tf_obs_whitened, tf.pack([batch_size, 1]))
            # tf_obs_whitened_tiled.set_shape([None, tf_obs_whitened.get_shape()[1]])
            return tf_obs_whitened_rep

        # assumes num_action is a multiple of num_obs
        tf_obs_whitened_cond = tf.cond(tf.not_equal(num_obs, num_action), replicate_observation, lambda: tf_obs_whitened)

        return tf_obs_whitened_cond, tf_actions_whitened

    def _graph_preprocess_outputs(self, tf_rewards, d_preprocess):
        return tf.add(tf.matmul(tf_rewards, tf.transpose(d_preprocess['rewards_orth_var'])),
                      d_preprocess['rewards_mean_var'])

    def _graph_inference(self, tf_obs_ph, tf_actions_ph, d_preprocess):
        raise NotImplementedError

    def _graph_calculate_values(self, tf_rewards):
        gammas = np.power(self._gamma * np.ones(self._H), np.arange(self._H))
        tf_values = tf.reduce_sum(gammas * tf_rewards, reduction_indices=1)
        return tf_values

    def _graph_cost(self, tf_rewards_ph, tf_rewards, tf_target_rewards):
        gammas = np.power(self._gamma * np.ones(self._H), np.arange(self._H))

        # for training, len(tf_obs_ph) == len(tf_actions_ph)
        # but len(tf_actions_target_ph) == N * len(tf_action_ph),
        # so need to be selective about what to take the max over
        tf_target_values = self._graph_calculate_values(tf_target_rewards)
        batch_size = tf.shape(tf_rewards_ph)[0]
        tf_target_values_flat = tf.reshape(tf_target_values, (batch_size, -1))
        tf_target_values_max = tf.reduce_max(tf_target_values_flat, reduction_indices=1)

        tf_rewards_ph_sum = tf.reduce_sum(gammas * tf_rewards_ph, reduction_indices=1)
        tf_rewards_sum = tf.reduce_sum(gammas * tf_rewards, reduction_indices=1)

        mse = tf.reduce_mean(tf.square(tf_rewards_ph_sum + np.power(self._gamma, self._H)*tf_target_values_max -
                                       tf_rewards_sum))
        weight_decay = self._weight_decay * tf.add_n(tf.get_collection('weight_decays'))
        cost = mse + weight_decay
        return cost, mse

    def _graph_optimize(self, tf_cost):
        return tf.train.AdamOptimizer(learning_rate=self._learning_rate).minimize(tf_cost)

    def _graph_init_vars(self, tf_sess):
        tf_sess.run([tf.global_variables_initializer()])

    def _graph_setup(self):
        tf_sess = tf.get_default_session()
        if tf_sess is None:
            tf_sess, tf_graph = RNNCriticPolicy.create_session_and_graph(gpu_device=self._gpu_device,
                                                                         gpu_frac=self._gpu_frac)
        else:
            tf_graph = tf_sess.graph

        with tf_sess.as_default(), tf_graph.as_default():
            d_preprocess = self._graph_preprocess_from_placeholders()

            ### policy
            with tf.variable_scope('policy'):
                tf_obs_ph, tf_actions_ph, tf_rewards_ph = self._graph_inputs_outputs_from_placeholders()
                tf_rewards = self._graph_inference(tf_obs_ph, tf_actions_ph, d_preprocess)

            ### target network
            with tf.variable_scope('target_network'):
                tf_obs_target_ph, tf_actions_target_ph, _ = self._graph_inputs_outputs_from_placeholders()
                tf_target_rewards = self._graph_inference(tf_obs_target_ph, tf_actions_target_ph, d_preprocess)

            policy_vars = sorted(tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope='policy'), key=lambda v: v.name)
            target_network_vars = sorted(tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope='target_network'), key=lambda v: v.name)
            update_target_fn = []
            for var, var_target in zip(policy_vars, target_network_vars):
                update_target_fn.append(var_target.assign(var))
            update_target_fn = tf.group(*update_target_fn)

            ### optimization
            tf_cost, tf_mse = self._graph_cost(tf_rewards_ph, tf_rewards, tf_target_rewards)
            tf_opt = self._graph_optimize(tf_cost)

            self._graph_init_vars(tf_sess)

            # merged = tf.merge_all_summaries()
            # writer = tf.train.SummaryWriter('/tmp', graph_def=tf_sess.graph_def)

        return tf_graph, tf_sess, d_preprocess, \
               tf_obs_ph, tf_actions_ph, tf_rewards_ph, tf_rewards, tf_cost, tf_opt, \
               tf_obs_target_ph, tf_actions_target_ph, tf_target_rewards, update_target_fn

    ################
    ### Training ###
    ################

    def update_preprocess(self, preprocess_stats):
        obs_mean, obs_orth, actions_mean, actions_orth, rewards_mean, rewards_orth = \
            preprocess_stats['observations_mean'], \
            preprocess_stats['observations_orth'], \
            preprocess_stats['actions_mean'], \
            preprocess_stats['actions_orth'], \
            preprocess_stats['rewards_mean'], \
            preprocess_stats['rewards_orth']
        self._tf_sess.run([
            self._d_preprocess['observations_mean_assign'],
            # self._d_preprocess['observations_orth_assign'],
            self._d_preprocess['actions_mean_assign'],
            # self._d_preprocess['actions_orth_assign'],
            self._d_preprocess['rewards_mean_assign'],
            # self._d_preprocess['rewards_orth_assign']
        ],
          feed_dict={
              self._d_preprocess['observations_mean_ph']: obs_mean,
              self._d_preprocess['observations_orth_ph']: obs_orth,
              self._d_preprocess['actions_mean_ph']: np.tile(actions_mean, self._H),
              self._d_preprocess['actions_orth_ph']: scipy.linalg.block_diag(*([actions_orth] * self._H)),
              self._d_preprocess['rewards_mean_ph']: np.expand_dims(np.tile(rewards_mean, self._H), 0),
              self._d_preprocess['rewards_orth_ph']: scipy.linalg.block_diag(*([rewards_orth] * self._H))
          })

    def update_target(self):
        self._tf_sess.run(self._update_target_fn)

    def train_step(self, observations, actions, rewards):
        batch_size = len(observations)
        action_dim = self._env_spec.action_space.flat_dim

        action_lower, action_upper = self._env_spec.action_space.bounds
        if self._get_action_params['type'] == 'random':
            N = self._get_action_params['N']
            target_actions = np.random.uniform(action_lower.tolist(), action_upper.tolist(),
                                               size=(N, self._H, self._env_spec.action_space.flat_dim))
            target_actions = target_actions.reshape(N, self._H * self._env_spec.action_space.flat_dim)
        else:
            raise NotImplementedError('get_action type {0} not implemented'.format(self._get_action_params['type']))

        cost, _ = self._tf_sess.run([self._tf_cost, self._tf_opt],
                                    feed_dict={
                                        ### policy
                                        self._tf_obs_ph: observations[:, 0, :],
                                        self._tf_actions_ph: actions.reshape((batch_size, self._H * action_dim)),
                                        self._tf_rewards_ph: rewards,
                                        ### target network
                                        self._tf_obs_target_ph: observations[:, -1, :],
                                        self._tf_actions_target_ph: np.tile(target_actions, (len(observations), 1)),
                                    })

        # policy_vars = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope='policy')
        # target_network_vars = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope='target_network')
        # pvars_eval = self._tf_sess.run(policy_vars)
        # tn_vars_eval = self._tf_sess.run(target_network_vars)
        # if not np.all([np.isfinite(v).all() for v in pvars_eval + tn_vars_eval]) or not np.isfinite(cost):
        #     import IPython; IPython.embed()
        assert(np.isfinite(cost))

        self._log_stats['Cost'].append(cost)
        for k, v in self._log_stats.items():
            if len(v) > self._log_history_len:
                self._log_stats[k] = v[1:]

    ######################
    ### Policy methods ###
    ######################

    def _get_action_setup(self):
        get_action_preprocess = dict()

        if self._get_action_params['type'] == 'random':
            pass
        elif self._get_action_params['type'] == 'lattice':
            N = self._get_action_params['N']
            action_lower, action_upper = self._env_spec.action_space.bounds
            single_actions = cartesian([np.linspace(l, u, N) for l, u in zip(action_lower, action_upper)])
            actions = np.asarray(list(itertools.combinations(single_actions, self._H)))
            get_action_preprocess['actions'] = actions.reshape((len(actions), self._H * self._env_spec.action_space.flat_dim))
        else:
            raise NotImplementedError('get_action type {0} not implemented'.format(self._get_action_params['type']))

        return get_action_preprocess

    def set_exploration_strategy(self, exploration_strategy):
        self._exploration_strategy = exploration_strategy

    def get_action(self, observation):
        action_lower, action_upper = self._env_spec.action_space.bounds

        if self._get_action_params['type'] == 'random':
            N = self._get_action_params['N']
            actions = np.random.uniform(action_lower.tolist(), action_upper.tolist(),
                                        size=(N, self._H, self._env_spec.action_space.flat_dim))
            actions = actions.reshape(N, self._H * self._env_spec.action_space.flat_dim)
        elif self._get_action_params['type'] == 'lattice':
            actions = self._get_action_preprocess['actions']
        else:
            raise NotImplementedError('get_action type {0} not implemented'.format(self._get_action_params['type']))

        pred_rewards = self._tf_sess.run([self._tf_rewards],
                                         feed_dict={self._tf_obs_ph: [observation],
                                                    self._tf_actions_ph: actions})[0]

        chosen_action = actions[pred_rewards.sum(axis=1).argmax()][:self._env_spec.action_space.flat_dim]

        if self._exploration_strategy is not None:
            exploration_func = lambda: None
            exploration_func.get_action = lambda _: (chosen_action, dict())
            chosen_action = self._exploration_strategy.get_action(0, observation, exploration_func)

        return chosen_action, dict()

    def get_actions(self, observations):
        num_obs = len(observations)
        action_lower, action_upper = self._env_spec.action_space.bounds

        if self._get_action_params['type'] == 'random':
            N = self._get_action_params['N']
            actions = np.random.uniform(action_lower.tolist(), action_upper.tolist(),
                                        size=(N * num_obs, self._H, self._env_spec.action_space.flat_dim))
            actions = actions.reshape(N * num_obs, self._H * self._env_spec.action_space.flat_dim)
        else:
            raise NotImplementedError('get_actions type {0} not implemented'.format(self._get_action_params['type']))

        pred_rewards = self._tf_sess.run([self._tf_rewards],
                                         feed_dict={self._tf_obs_ph: observations,
                                                    self._tf_actions_ph: actions})[0]

        chosen_actions = []
        for observation_i, pred_rewards_i, actions_i in zip(observations,
                                                           np.split(pred_rewards, num_obs, axis=0),
                                                           np.split(actions, num_obs, axis=0)):
            chosen_action_i = actions_i[pred_rewards_i.sum(axis=1).argmax()][:self._env_spec.action_space.flat_dim]
            if self._exploration_strategy is not None:
                exploration_func = lambda: None
                exploration_func.get_action = lambda _: (chosen_action_i, dict())
                chosen_action_i = self._exploration_strategy.get_action(0, observation_i, exploration_func)
            chosen_actions.append(chosen_action_i)

        return chosen_actions, dict()

    @property
    def recurrent(self):
        raise NotImplementedError

    def terminate(self):
        self._tf_sess.close()

    ######################
    ### Saving/loading ###
    ######################

    def get_params_internal(self, **tags):
        with self._tf_graph.as_default():
            # vars = sorted(tf.all_variables(), key=lambda v: v.name)
            # for v in vars:
            #     print(v.name)
            #     print(v.get_shape())
            #     if len(v.get_shape()) == 0:
            #         import IPython; IPython.embed()

            return sorted(tf.global_variables(), key=lambda v: v.name)

    ###############
    ### Logging ###
    ###############

    def log(self):
        for k in sorted(self._log_stats.keys()):
            logger.record_tabular(k, np.mean(self._log_stats[k]))
