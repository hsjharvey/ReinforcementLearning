# -*- coding:utf-8 -*-
from utils import policies, config, replay_fn
from network import neural_network
import numpy as np
import tensorflow as tf
from collections import deque
import gym
import time


class CategoricalDQNAgent:
    def __init__(self, config):
        self.config = config

        self.input_dim = config.input_dim  # neural network input dimension
        self.n_atoms = config.Categorical_n_atoms
        self.vmin = config.Categorical_Vmin
        self.vmax = config.Categorical_Vmax

        self.atoms = np.linspace(
            config.Categorical_Vmin,
            config.Categorical_Vmax,
            config.Categorical_n_atoms,
        )  # Z

        self.envs = None
        self.actor_network = None
        self.target_network = None

        self.total_steps = 0
        self.episodes = config.episodes
        self.steps = config.steps
        self.batch_size = config.batch_size

        self.replay_buffer_size = config.replay_buffer_size
        self.replay_buffer = deque()

        self.delta_z = (config.Categorical_Vmax - config.Categorical_Vmin) / float(config.Categorical_n_atoms - 1)

        # June 17, 2019: there seems to be a problem with checkpoint implementation
        # do not pass it to model.fit()
        self.check = tf.keras.callbacks.ModelCheckpoint('../saved_network_models/harvey.model',
                                                        monitor='loss',
                                                        save_best_only=True,
                                                        save_weights_only=False,
                                                        mode='auto')

    def train_by_replay(self):
        """
        TD update by replay the history.
        Implementation of algorithm 1 in the paper.
        :return: loss generated by the network
        """
        # step 1: generate replay samples (size = self.batch_size) from the replay buffer
        # e.g. prioritize experience replay
        current_states, actions, next_states, rewards, terminals = \
            replay_fn.uniform_random_replay(self.replay_buffer, self.batch_size)

        # step 2:
        # generate next state probability, size = (batch_size, action_dimension, number_of_atoms)
        # e.g. (32, 2, 51) where batch_size =  32, each batch contains 2 actions,
        # each action distribution has 51 bins.
        prob_next = self.target_network.predict(next_states)

        # step 3:
        # calculate next state Q values, size = (batch_size, action_dimension, 1).
        # e.g. (32, 2, 1), each action has one Q value.
        # then choose the higher value out of the 2 for each of the 32 batches.
        q_next = np.dot(np.array(prob_next), self.atoms)
        action_next = np.argmax(q_next, axis=1)

        # step 4:
        # use the optimal actions as index, pick out the probabilities of the optimal action
        prob_next = prob_next[np.arange(self.batch_size), action_next, :]

        # match the rewards from the memory to the same size as the prob_next
        rewards = np.tile(rewards.reshape(self.batch_size, 1), (1, self.n_atoms))

        # TD update
        discount_rate = self.config.discount_rate * (1 - terminals)
        atoms_next = rewards + np.dot(discount_rate.reshape(self.batch_size, 1),
                                      self.atoms.reshape(1, self.n_atoms))

        # constrain atoms next to be within Vmin and Vmax
        atoms_next = np.clip(atoms_next, self.vmin, self.vmax)

        # calculate the floors and ceilings of atom next
        b = (atoms_next - self.config.Categorical_Vmin) / self.delta_z
        l, u = np.floor(b).astype(int), np.ceil(b).astype(int)

        # it is important to check l == u, to avoid histogram collapsing.
        d_m_l = (u + (l == u) - b) * prob_next
        d_m_u = (b - l) * prob_next

        # step 5: redistribute the target probability histogram (calculation of m)
        # Note that there is an implementation issue
        # The loss function requires current histogram and target histogram to have the same size
        # Generally, the loss function should be the categorical cross entropy loss between
        # P(x, a*): size = (32, 1, 51) and P(x(t+1), a*): size = (32, 1, 51), i.e. only for optimal actions
        # However, the network generates P(x, a): size = (32, 2, 51), i.e. for all actions
        # Therefore, I create a tensor with zeros (size = (32, 2, 51)) and update only the probability histogram
        # so that the calculated cross entropy loss is accurate
        target_histo = np.zeros(shape=(self.batch_size, self.config.action_dim, self.n_atoms))

        for i in range(self.batch_size):
            target_histo[i][action_next[i]] = 0.0  # clear the histogram that needs to be updated
            np.add.at(target_histo[i][action_next[i]], l[i], d_m_l[i])  # update d_m_l
            np.add.at(target_histo[i][action_next[i]], l[i], d_m_u[i])  # update d_m_u

        loss = self.actor_network.fit(x=current_states, y=target_histo, verbose=2)  # update actor network weights
        return loss

    def transition(self):
        """
        At this stage, the agent simply play and record
        [current_state, action, reward, next_state, done]
        Updating the weights of the neural network happens
        every single time the replay buffer size is reached.
        done: boolean, whether the game has end or not
        :return:
        """
        for each_ep in range(self.episodes):
            current_state = self.envs.reset()

            for step in range(self.steps):
                self.total_steps += 1

                # reshape the input state to a tensor ===> Network ====> action probabilities
                # size = (1, action dimension, number of atoms)
                # e.g. size = (1, 2, 51)
                action_prob = self.actor_network.predict(
                    np.array(current_state).reshape((1, self.input_dim[0], self.input_dim[1])))

                # calculate action value (Q-value)
                action_value = np.dot(np.array(action_prob), self.atoms)
                action = policies.epsilon_greedy(action_values=action_value[0],
                                                 episode=each_ep,
                                                 stop_explore=self.config.stop_explore)

                next_state, reward, done, _ = self.envs.step(action=action)

                # record the history to replay buffer
                self.replay_buffer.append([current_state.reshape(self.input_dim).tolist(), action,
                                           next_state.reshape(self.input_dim).tolist(), reward, done])

                # when we collect certain number of batches, perform replay and update
                # the weights in actor network and clear the replay buffer
                if len(list(self.replay_buffer)) == self.replay_buffer_size:
                    loss = self.train_by_replay()
                    self.replay_buffer = deque()

                # for certain period, we copy the actor network weights to the target network
                if self.total_steps > self.config.weights_update_frequency:
                    self.target_network.set_weights(self.actor_network.get_weights())

                # if episode is finished, break the inner loop
                # otherwise, continue
                if done:
                    break
                else:
                    current_state = next_state

    def eval_step(self, render=True):
        for each_ep in range(100):
            current_state = self.envs.reset()

            if render:
                self.envs.render(mode=['human'])
                time.sleep(0.15)

            for step in range(200):
                action_prob = self.actor_network.predict(
                    np.array(current_state).reshape((1, self.input_dim[0], self.input_dim[1])))

                action_value = np.dot(np.array(action_prob), self.atoms)
                action = np.argmax(action_value[0], axis=0)

                next_state, reward, done, _ = self.envs.step(action=action)

                if done:
                    break
                else:
                    current_state = next_state


if __name__ == '__main__':
    C = config.Config()
    cat = CategoricalDQNAgent(config=C)
    cat.envs = gym.make('CartPole-v0')
    cat.actor_network = neural_network.CategoricalNet(config=C).nn_model()
    cat.target_network = tf.keras.models.clone_model(cat.actor_network)
    cat.target_network.set_weights(cat.actor_network.get_weights())
    cat.transition()

    print("finish training")
    print("evaluating.....")
    cat.eval_step(render=True)
