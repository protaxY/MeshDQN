import torch
import os
import sys
import yaml
from ray_dqn import DQNConfig
from ray import tune, air, train
from ray.air import session, Checkpoint
import ray
from ray.air.config import ScalingConfig
from ray.train.torch import TorchTrainer
from ray.tune.registry import register_env
from ParallelMultiSnapshotEnv2DAirfoil import ParallelMultiSnapshotEnv2DAirfoil as Env2DAirfoil
from parallel_airfoilgcnn import NodeRemovalNet
from tqdm import tqdm
import time
#from dolfin import *
from itertools import count
import random
import numpy as np
from torch import optim
from matplotlib import pyplot as plt
from collections import namedtuple
from torch_geometric.data import Data
from torch_geometric.loader import DataLoader
import math


SEED = 1370
#SEED = 137*137
torch.manual_seed(SEED)
random.seed(SEED)
np.random.seed(SEED)

import os
os.environ['CUDA_LAUNCH_BLOCKING'] = "1"


if(torch.cuda.is_available()):
    print("USING GPU")
    device = torch.device('cuda:0')
else:
    print("USING CPU")
    device = torch.device('cpu')
device = torch.device('cpu')

Transition = namedtuple('Transition',
                       ('state', 'action', 'next_state', 'reward'))
@ray.remote
class ReplayMemory(object):
    def __init__(self, capacity):
        self.capacity = capacity
        self.memory = []
        self.position = 0

    def push(self, *args):
        """Saves a transition."""
        if len(self.memory) < self.capacity:
            self.memory.append(None)
        self.memory[self.position] = Transition(*args)
        self.position = (self.position + 1) % self.capacity

    def sample(self, batch_size):
        return random.sample(self.memory, batch_size)

    #def __len__(self):
    def size(self):
        return len(self.memory)


def _movingaverage(values, window):
    weights = np.repeat(1.0, window)/window
    sma = np.convolve(values, weights, 'valid')
    return sma


@ray.remote
class DataHandler(object):
    def __init__(self, save_dir):
        self.save_dir = save_dir
        self.rewards = []
        self.ep_rewards = []
        self.losses = []
        self.actions = []
        self.epss = []

    def add_step(self, loss, eps):
        #self.rewards.append(sum(ep_rew))
        #self.ep_rewards.append(ep_rew)
        self.losses.append(loss)
        self.epss.append(eps)
        #self.actions.append(action)

    def add_episode(self, ep_rew, ep_action):
        self.rewards.append(sum(ep_rew))
        self.ep_rewards.append(ep_rew)
        self.actions.append(ep_action)

    def write(self):
        np.save(self.save_dir + "reward.npy", self.rewards)
        np.save(self.save_dir + "rewards.npy", self.ep_rewards)
        np.save(self.save_dir + "losses.npy", self.losses)
        np.save(self.save_dir + "actions.npy", self.actions)
        np.save(self.save_dir + "eps.npy", self.epss)


    def plot(self):
        fig, ax = plt.subplots()
        ax.plot(self.rewards)
        if(len(self.rewards) >= 25):
            ax.plot(list(range(len(self.rewards)))[24:], _movingaverage(self.rewards, 25))

        if(len(self.rewards) >= 200):
            ax.plot(list(range(len(self.rewards)))[199:], _movingaverage(self.rewards, 200))

        ax.set(xlabel="Episode", ylabel="Reward")
        ax.set_title("DQN Training Reward")
        plt.savefig(self.save_dir + "reward.png".format(save_dir, PREFIX))
        plt.close()
        

#steps_done = 0
#def select_action(state):
#    global steps_done
#    sample = random.random()
#    eps_threshold = EPS_END + (EPS_START - EPS_END) * np.exp(-steps_done/EPS_DECAY)
#    eps_threshs.append(eps_threshold)
#    steps_done += 1
#    #if(steps_done%100 == 0):
#    #    np.save("./{}/{}eps.npy".format(save_dir, PREFIX), eps_threshs)
#    if(sample > eps_threshold): # Exploit
#        with torch.no_grad():
#            return torch.tensor([[policy_net_1(state).argmax()]]).to(device)
#    else: # Explore
#        if(flow_config['agent_params']['do_nothing']):
#            return torch.tensor([random.sample(range(n_actions+1), 1)], dtype=torch.long).to(device)
#        else:
#            return torch.tensor([random.sample(range(n_actions), 1)], dtype=torch.long).to(device)

criterion = torch.nn.HuberLoss()
losses = []
def optimize_model(optimizer):
    if ray.get(memory.size.remote()) < BATCH_SIZE:
        return
    #print("OPTIMIZING MODEL...")
    transitions = ray.get(memory.sample.remote(BATCH_SIZE))
    batch = Transition(*zip(*transitions))

    # Compute a mask of non-final states and concatenate the batch elements
    # (a final state would've been the one after which simulation ended)
    non_final_mask = torch.tensor(tuple(map(lambda s: s is not None,
                                          batch.next_state)), dtype=torch.bool)
    non_final_next_states = [s for s in batch.next_state if s is not None]

    # Get batch
    state_batch = batch.state
    action_batch = torch.cat(batch.action).to(device)
    reward_batch = torch.cat(batch.reward).to(device)

    # Compute Q(s_t, a) - the model computes Q(s_t), then we select the
    # columns of actions taken. These are the actions which would've been taken
    # for each batch state according to policy_net

    # Easiest way to batch this
    loader = DataLoader(state_batch, batch_size=BATCH_SIZE)
    for data in loader:
        try:
            output = policy_net_1(data)
        except RuntimeError:
            print("\n\n")
            #print(data)
            #print(data.x)
            #print(data.edge_index)
            #print(data.edge_attr)
            print("\n\n")
            raise
    state_action_values = output[:,action_batch[:,0]].diag()

    # Compute V(s_{t+1}) for all next states.
    # Expected values of actions for non_final_next_states are computed based
    # on the "older" target_net; selecting their best reward with max(1)[0].
    # This is merged based on the mask, such that we'll have either the expected
    # state value or 0 in case the state was final.
    next_state_values = torch.zeros(BATCH_SIZE).to(device).float()
    loader = DataLoader(non_final_next_states, batch_size=BATCH_SIZE)
    # get batched output
    for data in loader:
        try:
            output = policy_net_2(data).max(1)[0]
        except RuntimeError:
            print("\n\n")
            #print(data)
            #print(data.x)
            #print(data.edge_index)
            #print(data.edge_attr)
            print("\n\n")
            raise
    try:
        next_state_values[non_final_mask] = output
    except RuntimeError:
        return

    # Compute the expected Q values
    expected_state_action_values = (next_state_values * GAMMA) + reward_batch

    # Compute Huber loss
    loss = criterion(state_action_values.float(), expected_state_action_values.float()).float()
    #losses.append(loss.item())
    #if((len(memory)%25) == 0):
    #if((ray.get(memory.size.remote())%25) == 0):
    #    np.save("./{}/{}losses.npy".format(save_dir, PREFIX), losses)

    # Optimize the model
    optimizer.zero_grad()
    loss.backward()
    optimizer.step()
    return loss.item()

RESTART = False

# Hyperparameters to tune
BATCH_SIZE = 32
GAMMA = 1.
EPS_START = 1.
EPS_END = 0.01
EPS_DECAY = 100000
#EPS_DECAY = 10000
TARGET_UPDATE = 5
LEARNING_RATE = 0.0005

eps_threshs = []

# Prefix used for saving results
PREFIX = 'ys930_ray_'

# Save directory
save_dir = 'training_results'
if(not os.path.exists("./{}".format(save_dir))):
    os.makedirs(save_dir)
if(not os.path.exists("./{}/{}".format(save_dir, PREFIX[:-1]))):
    os.makedirs(save_dir + "/" + PREFIX[:-1])
save_dir += '/' + PREFIX[:-1]

# Set up environment
with open("../configs/ray_{}.yaml".format(PREFIX.split("_")[0]), 'r') as stream:
    flow_config = yaml.safe_load(stream)
#env = Env2DAirfoil.remote(flow_config)
#env.set_plot_dir.remote(save_dir)
#env.plot_state.remote()
env = Env2DAirfoil(flow_config)
env.set_plot_dir(save_dir)
#env.plot_state()

flow_config['agent_params']['plot_dir'] = save_dir

# Need to wait until simulation is done
sim_done = False
#time.sleep(5)
#gt_drag, gt_time = ray.get(env.return_vals.remote())
#time.sleep(1)
# Hold on to ground truth values
flow_config['agent_params']['gt_drag'] = env.gt_drag
flow_config['agent_params']['gt_time'] = env.gt_time

# Need to delete these to avoid pickling
#del env.original_u
#del env.original_p
#flow_config['agent_params']['u'] = [u.copy(deepcopy=True) for u in original_u]
#flow_config['agent_params']['p'] = [p.copy(deepcopy=True) for p in original_p]
sim_done = True

n_actions = 180
print("N CLOSEST: {}".format(n_actions))

# Set up for DQN
policy_net_1 = NodeRemovalNet(n_actions+1, conv_width=128, topk=0.1)#.to(device)#.float()
policy_net_2 = NodeRemovalNet(n_actions+1, conv_width=128, topk=0.1)#.to(device)#.float()
#policy_net_1 = NodeRemovalNet.remote(n_actions+1, conv_width=128, topk=0.1)#.to(device)#.float()
#policy_net_2 = NodeRemovalNet.remote(n_actions+1, conv_width=128, topk=0.1)#.to(device)#.float()
optimizer_fn = lambda parameters: optim.Adam(parameters, lr=LEARNING_RATE)
# Prime policy nets
try:
    NUM_INPUTS = 2 + 3 * int(flow_config['agent_params']['solver_steps']/flow_config['agent_params']['save_steps'])
except:
    NUM_INPUTS = 5
policy_net_1.set_num_nodes(NUM_INPUTS)
policy_net_2.set_num_nodes(NUM_INPUTS)
#policy_net_1.set_num_nodes.remote(NUM_INPUTS)
#policy_net_2.set_num_nodes.remote(NUM_INPUTS)


# Set up replay memory and data handler
memory = ReplayMemory.remote(10000)
save_str = "/home/fenics/drl_projects/MeshDQN/parallel_training/{}/{}".format(save_dir, PREFIX)
print("\n\nSAVE STRING: {}\n\n".format(save_str))
handler = DataHandler.remote(save_str)

# Set up training loop
num_episodes = flow_config['agent_params']['episodes']
ep_reward = []
all_actions = []
all_rewards = []
np.random.seed(137)
def train_loop_per_worker(training_config):
    #print("\n\nRANDOM SEED: {}\n\n".format(np.random.random()))
    # Sets random seed for each worker
    seed = int(10000*np.random.random())
    np.random.seed(seed)
    random.seed(seed)
    optimizer = optimizer_fn(policy_net_1.parameters())#.remote())
    first = True
    env = Env2DAirfoil(training_config['env_config'])
    steps_done = 0
    start_ep = len(ep_reward) if(RESTART) else 0
    for episode in range(start_ep, num_episodes):
        # Analysis
        episode_actions = []
        episode_rewards = []
        episode_losses = []

        print("EPISODE: {}".format(episode))
        acc_rew = 0.0
        acc_rews = []
        if(episode != 0):
            env = Env2DAirfoil(flow_config)

        state = env.get_state()
        for t in count():
            #print("\nSTEP {}\n".format(t))

            # Action selection isn't random across workers otherwise
            sample = np.random.random()
            eps_threshold = EPS_END + (EPS_START - EPS_END) * np.exp(-steps_done/EPS_DECAY)
            eps_threshs.append(eps_threshold)
            steps_done += 1
            if(sample > eps_threshold): # Exploit
                with torch.no_grad():
                    action = torch.tensor([[policy_net_1(state).argmax()]]).to(device)
            else: # Explore
                if(flow_config['agent_params']['do_nothing']):
                    action = torch.tensor([random.sample(range(n_actions+1), 1)], dtype=torch.long).to(device)
                else:
                    action =  torch.tensor([random.sample(range(n_actions), 1)], dtype=torch.long).to(device)

            #action, eps = select_action(state)
            next_state, reward, done, _ = env.step(action.item())

            # Analysis
            episode_actions.append(action.item())
            episode_rewards.append(reward)

            acc_rew += reward
            reward = torch.tensor([reward])

            # Observe new state
            if(done):
                next_state = None

            if(next_state is not None):
                memory.push.remote(state.to(device), action.to(device), next_state.to(device), reward.to(device))
            else:
                memory.push.remote(state.to(device), action.to(device), next_state, reward.to(device))

            state = next_state

            loss = optimize_model(optimizer)
            episode_losses.append(loss)

            # Add to data handler
            handler.add_step.remote(loss, eps_threshold)

            if(done):
                ep_reward.append(acc_rew)
                break

        # Analysis
        handler.add_episode.remote(episode_rewards, episode_actions)

        if((episode % TARGET_UPDATE) == 0):
            if(first):
                optimizer = optimizer_fn(policy_net_1.parameters())#.remote())
                first = False
            else:
                optimizer = optimizer_fn(policy_net_2.parameters())#.remote())
                first = True

            handler.plot.remote()

        handler.write.remote()

        #if(len(ep_reward)%1 == 0):
        torch.save(policy_net_1.state_dict(),
                    "/home/fenics/drl_projects/MeshDQN/parallel_training/{}/{}policy_net_1.pt".format(save_dir, PREFIX))
        torch.save(policy_net_2.state_dict(),
                    "/home/fenics/drl_projects/MeshDQN/parallel_training/{}/{}policy_net_2.pt".format(save_dir, PREFIX))



# If using GPUs, use the below scaling config instead.
# scaling_config = ScalingConfig(num_workers=3, use_gpu=True)
scaling_config = ScalingConfig(num_workers=12)
trainer = TorchTrainer(
    train_loop_per_worker=train_loop_per_worker,
    train_loop_config={'env_config': flow_config},
    scaling_config=scaling_config,
)
result = trainer.fit()

# Save final models
torch.save(policy_net_1.state_dict(),
           "/home/fenics/drl_projects/MeshDQN/parallel_training/{}/{}policy_net_1.pt".format(save_dir, PREFIX))
torch.save(policy_net_2.state_dict(),
           "/home/fenics/drl_projects/MeshDQN/parallel_training/{}/{}policy_net_2.pt".format(save_dir, PREFIX))

